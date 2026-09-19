"""Parse one Uybor.uz listing into the project's canonical JSON shape.

The script is standalone and uses only Python's standard library.  Uybor
renders a Next.js ``__NEXT_DATA__`` object into the page, so the parser reads
that structured payload instead of scraping formatted text.  The complete
listing object is preserved unchanged under ``source_data``.

Example:
    python -m standalone_parsers.uybor_publication_json \
        "https://uybor.uz/listings/1305922" \
        --output uybor_1305922.json

To use a locally saved authenticated Uybor session (the first run opens a
browser for manual login):
    python -m standalone_parsers.uybor_publication_json \
        "https://uybor.uz/listings/1305922" --login
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from core.existing_listings import ExistingListings


DEFAULT_URL = "https://uybor.uz/listings/1305922"
DEFAULT_CATALOG_URL = "https://uybor.uz/listings?operationType__eq=rent"
DEFAULT_CATALOG_LIMIT = 2


class _NextDataCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside = False
        self._parts: list[str] = []
        self.payload: str | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        if attributes.get("id") == "__NEXT_DATA__":
            self._inside = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside:
            self.payload = "".join(self._parts)
            self._inside = False


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сохранить данные одной публикации Uybor.uz в JSON"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help=f"URL публикации (по умолчанию: {DEFAULT_URL})",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output_json/uybor/uybor_publication.json"),
        help="Выходной JSON-файл в output_json/uybor",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Сетевой таймаут в секундах",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help=(
            "Открыть постоянный браузерный профиль для ручного входа и "
            "получить номер после раскрытия на странице"
        ),
    )
    parser.add_argument(
        "--profile-dir",
        type=Path,
        default=Path(".uybor_auth_profile"),
        help="Локальная папка cookies профиля (не добавляйте её в git)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_CATALOG_LIMIT,
        help=f"Лимит карточек каталога (максимум: {DEFAULT_CATALOG_LIMIT})",
    )
    return parser.parse_args()


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL должен начинаться с http:// или https://")
    if hostname not in {"uybor.uz", "www.uybor.uz"}:
        raise ValueError("Допускаются только ссылки домена uybor.uz")


def _is_catalog_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.path.rstrip("/").lower() == "/listings"


def _download_html(url: str, timeout: float) -> tuple[str, str, int]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            return (
                response.read().decode(charset, errors="replace"),
                response.geturl(),
                response.status,
            )
    except HTTPError as error:
        raise RuntimeError(f"Uybor вернул HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Не удалось открыть Uybor: {error.reason}") from error


def _extract_next_data(html: str) -> dict[str, Any]:
    collector = _NextDataCollector()
    collector.feed(html)
    if not collector.payload:
        raise RuntimeError("На странице не найден JSON __NEXT_DATA__")
    try:
        value = json.loads(collector.payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("JSON __NEXT_DATA__ Uybor повреждён") from error
    if not isinstance(value, dict):
        raise RuntimeError("__NEXT_DATA__ имеет неожиданный формат")
    return value


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    number = _as_float(value)
    return int(number) if number is not None else None


def _name(value: Any) -> str | None:
    if isinstance(value, dict):
        if isinstance(value.get("name"), dict):
            return _name(value["name"])
        if value.get("name") is not None:
            return str(value["name"])
        return value.get("ru") or value.get("en") or value.get("uz")
    return str(value) if value is not None else None


def _currency(value: Any) -> str | None:
    normalized = str(value or "").lower()
    if normalized in {"usd", "ue", "у.е.", "у.е"} or "дол" in normalized:
        return "USD"
    if normalized in {"uzs", "sum", "сум"}:
        return "UZS"
    if normalized in {"rub", "rubles", "руб"}:
        return "RUB"
    return str(value).upper() if value else None


def _phone_from_listing(value: Any) -> str | None:
    """Return a phone only if Uybor exposes it in the listing payload."""

    if isinstance(value, dict):
        for key, nested in value.items():
            if key.lower() in {"phone", "phonenumber", "phone_number"}:
                digits = re.sub(r"\D", "", str(nested or ""))
                if len(digits) == 9:
                    digits = "998" + digits
                if len(digits) >= 10:
                    return "+" + digits
            result = _phone_from_listing(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _phone_from_listing(nested)
            if result:
                return result
    return None


def _normalize_phone_text(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 9:
        digits = "998" + digits
    return "+" + digits if len(digits) >= 10 else None


def _uybor_images(listing: dict[str, Any]) -> list[dict[str, Any]]:
    """Map Uybor media records to the common image representation."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, media in enumerate(listing.get("media") or []):
        if not isinstance(media, dict):
            continue
        url = media.get("url") or media.get("fileUrl")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(
            {
                "url": url,
                "source_image_id": media.get("id"),
                "file_name": media.get("fileName"),
                "sort_order": index,
                "is_primary": index == 0,
            }
        )
    return result


def _title(listing: dict[str, Any]) -> str | None:
    rooms = listing.get("room")
    category = _name(listing.get("category")) or "Недвижимость"
    operation = listing.get("operationType")
    operation_label = "аренду" if operation == "rent" else "продажу"
    if rooms not in {None, "", "0"}:
        return f"{rooms}-комнатная {category.lower()} в {operation_label}"
    return f"{category} в {operation_label}"


def _canonical_listing(
    listing: dict[str, Any],
    requested_url: str,
    final_url: str,
    status: int,
    phone_override: str | None = None,
) -> dict[str, Any]:
    operation = listing.get("operationType")
    price = _as_float(listing.get("price"))
    area = _as_float(listing.get("square"))
    is_rent = operation == "rent"
    monthly_rent = price if is_rent else None
    price_per_m2 = (
        round(monthly_rent / area, 2)
        if monthly_rent is not None and area and area > 0
        else None
    )
    region = listing.get("region") or {}
    district = listing.get("district") or {}
    street = listing.get("street") or {}
    zone = listing.get("zone") or {}
    user = listing.get("user") or {}
    facilities = listing.get("facilities") or []
    amenities = [
        _name(item.get("name"))
        for item in facilities
        if isinstance(item, dict) and _name(item.get("name"))
    ]
    address = listing.get("address")
    nearby = (
        ["Метро"]
        if isinstance(address, str)
        and address.strip().casefold().startswith("метро")
        else []
    )
    city = _name(region) or _name(listing.get("city"))
    if city and city.lower().startswith("город "):
        city = city[6:]
    housing = {
        "city": city,
        "district": _name(district),
        "address": address,
        "street": _name(street),
        "house_number": listing.get("houseNumber") or listing.get("house_num"),
        "zone": _name(zone),
        "address_id": listing.get("addressId"),
        "street_id": listing.get("streetId"),
        "house_id": listing.get("houseId"),
        "zone_id": listing.get("zoneId"),
        "building_type": (
            "new_building" if listing.get("isNewBuilding") is True
            else "secondary" if listing.get("isNewBuilding") is False
            else None
        ),
        "is_new_building": listing.get("isNewBuilding"),
        "repair": listing.get("repair"),
        "foundation_type": listing.get("foundation"),
        "residential_complex_name": _name(listing.get("residentialComplex")),
        "residential_complex_id": listing.get("residentialComplexId"),
        "rooms": _as_int(listing.get("room")),
        "total_area_m2": area,
        "floor": _as_int(listing.get("floor")),
        "floors_total": _as_int(listing.get("floorTotal")),
        "furnished": "Мебель" in amenities,
        "commission": None,
        "monthly_rent": monthly_rent,
        "rent_currency": _currency(listing.get("priceCurrency")) if is_rent else None,
        "price_per_m2": price_per_m2,
        "latitude": _as_float(listing.get("lat")),
        "longitude": _as_float(listing.get("lng")),
    }
    seller_name = user.get("displayName") or " ".join(
        filter(None, [user.get("firstName"), user.get("lastName")])
    ) or None
    return {
        "source": "uybor",
        "id": str(listing.get("id")),
        "title": _title(listing),
        "price": price,
        "currency": _currency(listing.get("priceCurrency")),
        "transaction_type": operation,
        "url": final_url,
        "published_at": listing.get("createdAt"),
        "description": listing.get("description"),
        "description_length": len(listing.get("description") or ""),
        "housing": housing,
        "amenities": amenities,
        "nearby": nearby,
        "images": _uybor_images(listing),
        "seller": {
            "name": seller_name,
            "phone": phone_override or _phone_from_listing(user),
            "phone_source": "authenticated_page" if phone_override else None,
            # Uybor не предоставляет действующие персональные страницы
            # продавцов: ссылки /users/<id> не сохраняем как profile_url.
            "profile_url": None,
            "seller_role": user.get("role"),
        },
        "source_data": listing,
        "source_url": requested_url,
        "http_status": status,
        "source_fields_not_in_canonical": {
            "media": listing.get("media"),
            "views": listing.get("views"),
            "favorites": listing.get("favorites"),
            "is_premium": listing.get("isPremium"),
            "is_vip": listing.get("isVip"),
        },
    }


def _listing_from_next_data(page_data: dict[str, Any]) -> dict[str, Any]:
    listing = ((page_data.get("props") or {}).get("pageProps") or {}).get("listing")
    if not isinstance(listing, dict):
        raise RuntimeError("В __NEXT_DATA__ отсутствует pageProps.listing")
    return listing


def _listing_from_browser_page(page: Any, timeout: float) -> dict[str, Any]:
    """Read Next data after hydration/client-side navigation has completed."""

    attempts = max(4, min(12, int(timeout / 2)))
    for _ in range(attempts):
        try:
            page_data = page.evaluate("window.__NEXT_DATA__ || null")
            if isinstance(page_data, dict):
                return _listing_from_next_data(page_data)
        except Exception:
            pass
        try:
            html = page.evaluate("document.documentElement.outerHTML")
            return _listing_from_next_data(_extract_next_data(html))
        except (RuntimeError, TypeError, ValueError):
            page.wait_for_timeout(500)
    raise RuntimeError(
        f"На странице {page.url} не найдено pageProps.listing после ожидания"
    )


def parse_publication(url: str, timeout: float = 30) -> dict[str, Any]:
    _validate_url(url)
    html, final_url, status = _download_html(url, timeout)
    listing = _listing_from_next_data(_extract_next_data(html))
    return _canonical_listing(listing, url, final_url, status)


def _phone_from_authenticated_page(page: Any) -> str | None:
    phone_link = page.locator('a[href^="tel:"]')
    if phone_link.count():
        phone = _normalize_phone_text(phone_link.first.get_attribute("href"))
        if phone:
            return phone

    button = page.get_by_role("button", name="show-phone-button")
    if button.count() == 0:
        button = page.locator("button").filter(has_text="+")
    if button.count():
        try:
            button.first.click(timeout=5000)
            page.wait_for_timeout(700)
        except Exception:
            pass

    phone_link = page.locator('a[href^="tel:"]')
    if phone_link.count():
        phone = _normalize_phone_text(phone_link.first.get_attribute("href"))
        if phone:
            return phone
    try:
        body_text = page.locator("body").inner_text(timeout=5000)
    except Exception:
        body_text = ""
    phone_match = re.search(
        r"(?<!\d)(?:\+?998[\s()-]*\d{2}[\s()-]*\d{3}[\s()-]*\d{2}[\s()-]*\d{2})(?!\d)",
        body_text,
    )
    return _normalize_phone_text(phone_match.group(0)) if phone_match else None


def _parse_authenticated(url: str, profile_dir: Path, timeout: float) -> dict[str, Any]:
    """Use a persistent Playwright profile; credentials are entered manually."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Для --login нужен Playwright. Установите его в окружении проекта."
        ) from error

    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir.resolve()),
            headless=False,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            print(
                "В открывшемся окне войдите в Uybor вручную, затем "
                "вернитесь в консоль и нажмите Enter. Пароль скрипт не получает."
            )
            input()
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            page.wait_for_timeout(700)

            phone = _phone_from_authenticated_page(page)

            final_url = page.url
            listing = _listing_from_browser_page(page, timeout)
            result = _canonical_listing(listing, url, final_url, 200, phone)
            result["authentication"] = {
                "used": True,
                "profile_dir": str(profile_dir),
                "phone_obtained": bool(phone),
            }
            return result
        finally:
            context.close()


def _parse_catalog_authenticated(
    url: str,
    profile_dir: Path,
    limit: int | None,
    timeout: float,
    require_login: bool,
    existing: ExistingListings | None = None,
    max_scroll_rounds: int | None = None,
    stop_at_first_existing: bool = False,
) -> list[dict[str, Any]]:
    """Collect the first catalog listings in one persistent browser session."""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Для каталога Uybor нужен Playwright. Установите его в окружении проекта."
        ) from error

    existing = existing or ExistingListings()
    profile_dir.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir.resolve()),
            headless=not require_login,
            viewport={"width": 1440, "height": 1000},
            locale="ru-RU",
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
            try:
                page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            page.wait_for_timeout(3000)
            if require_login:
                print(
                    "В окне браузера войдите в Uybor вручную, затем нажмите Enter. "
                    "Пароль и SMS-код скрипт не получает и не сохраняет."
                )
                input()
                page.goto(url, wait_until="domcontentloaded", timeout=int(timeout * 1000))
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(3000)

            urls: list[str] = []
            seen: set[str] = set()
            candidate_limit = (
                max(limit * 8, limit + 20)
                if limit is not None
                else None
            )
            stable_rounds = 0
            scroll_rounds = 0
            previous_seen_count = -1
            previous_height = -1
            reached_existing_boundary = False
            while True:
                links = page.locator('a[href*="/listings/"]')
                for index in range(links.count()):
                    href = links.nth(index).get_attribute("href")
                    if not href:
                        continue
                    absolute_url = urljoin("https://uybor.uz", href)
                    parsed_href = urlparse(absolute_url)
                    clean_path = parsed_href.path.rstrip("/") + "/"
                    # Исключаем служебные страницы вроде /listings/map.
                    if not re.fullmatch(r"/listings/\d+/", clean_path):
                        continue
                    detail_url = "https://uybor.uz" + clean_path
                    if detail_url in seen:
                        continue
                    seen.add(detail_url)
                    listing_id = clean_path.strip("/").rsplit("/", 1)[-1]
                    if existing.contains(listing_id, detail_url):
                        print(f"Уже есть в БД, пропуск: {detail_url}")
                        if stop_at_first_existing:
                            reached_existing_boundary = True
                            break
                        continue
                    urls.append(detail_url)
                    if (
                        candidate_limit is not None
                        and len(urls) >= candidate_limit
                    ):
                        break

                if candidate_limit is not None and len(urls) >= candidate_limit:
                    break
                if reached_existing_boundary:
                    break
                scroll_rounds += 1
                if (
                    max_scroll_rounds is not None
                    and scroll_rounds >= max_scroll_rounds
                ):
                    break

                document_height = page.evaluate(
                    "Math.max(document.body.scrollHeight, "
                    "document.documentElement.scrollHeight)"
                )
                if (
                    len(seen) == previous_seen_count
                    and document_height == previous_height
                ):
                    stable_rounds += 1
                else:
                    stable_rounds = 0
                if stable_rounds >= 3:
                    break
                previous_seen_count = len(seen)
                previous_height = document_height
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(1_500)

            print("Объявления к обработке:")
            for detail_url in urls:
                print(f"  - {detail_url}")

            results: list[dict[str, Any]] = []
            for detail_url in urls:
                try:
                    page.goto(
                        detail_url,
                        wait_until="networkidle",
                        timeout=int(timeout * 1000),
                    )
                    page.wait_for_timeout(700)
                    listing = _listing_from_browser_page(page, timeout)
                    phone = _phone_from_authenticated_page(page)
                    item = _canonical_listing(
                        listing,
                        url,
                        page.url,
                        200,
                        phone,
                    )
                    item["authentication"] = {
                        "used": require_login,
                        "profile_dir": str(profile_dir),
                        "phone_obtained": bool(phone),
                    }
                    results.append(item)
                    if limit is not None and len(results) >= limit:
                        break
                except Exception as error:
                    print(
                        f"Пропуск публикации {detail_url}: {error}",
                        file=sys.stderr,
                    )
            return results
        finally:
            context.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.limit < 1 or args.limit > DEFAULT_CATALOG_LIMIT:
        raise SystemExit(f"--limit должен быть от 1 до {DEFAULT_CATALOG_LIMIT}")
    if _is_catalog_url(args.url):
        result: Any = _parse_catalog_authenticated(
            args.url,
            args.profile_dir,
            args.limit,
            args.timeout,
            args.login,
        )
    else:
        result = (
            _parse_authenticated(args.url, args.profile_dir, args.timeout)
            if args.login
            else parse_publication(args.url, args.timeout)
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON сохранён: {args.output.resolve()}")


if __name__ == "__main__":
    main()
