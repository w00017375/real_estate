import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from core.normalization import (
    clean_text,
    normalize_phone,
    parse_float,
    parse_int,
    parse_multiselect,
    parse_yes_no,
    publication_fingerprint,
)
from core.existing_listings import ExistingListings
from core.seller_metadata import (
    extract_olx_phone_from_seller_name,
    extract_uzbek_phones,
    extract_uzbek_phones_from_description,
    extract_uzbek_phones_from_payloads,
    normalize_seller_name,
    official_seller_details,
)
from sources.base import ListingSource


@dataclass(frozen=True)
class OlxConfig:
    base_url: str = (
        "https://www.olx.uz/nedvizhimost/"
        "kvartiry/arenda-dolgosrochnaya/"
    )
    domain: str = "https://www.olx.uz"
    max_category_pages: int = 3
    max_seller_profile_pages: int = 100
    headless: bool = True
    # Optional shallow catalog scan used by the autonomous live monitor.
    # ``None`` preserves the normal main.py pagination strategy.
    catalog_page_limit: int | None = None
    stop_at_first_existing: bool = False
    newest_first: bool = False


HOUSING_KEYWORDS = (
    "квартир", "комнат", "комн", "komn", "апартамент", "жиль",
    "жилой дом", "частный дом", "дом", "коттедж", "таунхаус",
    "новострой", "дача", "kvartir", "xonali", "хона", "хонали",
    "uy ", "уй ", "hovli", "ҳовли", "house",
)
NON_HOUSING_KEYWORDS = (
    "офис", "магазин", "склад", "гараж", "коммерчес", "помещение",
    "участок", "земля", "yer maydon", "noturar",
)
RENT_KEYWORDS = (
    "аренд", "сдам", "сдаётся", "сдается", "снять", "ижар", "ijara",
    "rent", "arend", "sdacha",
)
SALE_KEYWORDS = (
    "продаж", "продам", "продаётся", "продается", "купить", "prodazh",
    "prodam", "kupit", "сотилади", "sotiladi", "sotuv", "sale",
)

TASHKENT_DISTRICTS = (
    "Алмазарский район",
    "Бектемирский район",
    "Мирабадский район",
    "Мирзо-Улугбекский район",
    "Сергелийский район",
    "Учтепинский район",
    "Чиланзарский район",
    "Шайхантахурский район",
    "Юнусабадский район",
    "Яккасарайский район",
    "Янгихаётский район",
    "Яшнабадский район",
)

TASHKENT_DISTRICT_STEMS = (
    (("алмазар", "олмазор"), "Алмазарский район"),
    (("бектемир",), "Бектемирский район"),
    (("мирабад", "mirobod"), "Мирабадский район"),
    (("мирзо-улугбек", "мирзо улугбек"), "Мирзо-Улугбекский район"),
    (("сергел",), "Сергелийский район"),
    (("учтеп",), "Учтепинский район"),
    (("чиланзар",), "Чиланзарский район"),
    (("шайхантахур",), "Шайхантахурский район"),
    (("юнусабад",), "Юнусабадский район"),
    (("яккасарай",), "Яккасарайский район"),
    (("янгихаёт", "янгихаят"), "Янгихаётский район"),
    (("яшнабад",), "Яшнабадский район"),
)


def _contains_any(text: Any, keywords: tuple[str, ...]) -> bool:
    normalized = (clean_text(text) or "").lower()
    return any(keyword in normalized for keyword in keywords)


def _parse_price(value: Any) -> int | None:
    if not value:
        return None
    digits = re.findall(r"\d+", str(value))
    return int("".join(digits)) if digits else None


def _detect_currency(value: Any) -> str | None:
    normalized = (clean_text(value) or "").lower()
    if "сум" in normalized:
        return "UZS"
    if "у.е" in normalized or "$" in normalized or "usd" in normalized:
        return "USD"
    return None


def _listing_id(url: str | None) -> str | None:
    match = re.search(r"-ID([A-Za-z0-9]+)\.html", url or "")
    return match.group(1) if match else None


def _normalize_url(url: str | None, domain: str) -> str | None:
    if not url:
        return None
    parts = urlsplit(urljoin(domain, url))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))


def _url_with_page(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query))
    query["page"] = str(page_number)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


def _first_text(container: Any, selectors: list[str]) -> str | None:
    for selector in selectors:
        try:
            locator = container.locator(selector)
            if locator.count() == 0:
                continue
            value = clean_text(locator.first.inner_text(timeout=3000))
            if value:
                return value
        except Exception:
            pass
    return None


def _first_attribute(
    container: Any,
    selectors: list[str],
    attribute: str,
) -> str | None:
    for selector in selectors:
        try:
            locator = container.locator(selector)
            if locator.count() == 0:
                continue
            value = locator.first.get_attribute(attribute, timeout=3000)
            if value:
                return value
        except Exception:
            pass
    return None


def _extract_phone(page: Any) -> str | None:
    phone_href = _first_attribute(page, ['a[href^="tel:"]'], "href")
    if phone_href:
        return normalize_phone(phone_href, default_country_code="998")

    selectors = [
        'button[data-testid="show-phone"]',
        '[data-testid="show-phone"] button',
        'button[data-cy="ad-contact-phone"]',
        '[data-testid="contact-section"] button:has-text("Показать")',
        '[data-testid="aside"] button:has-text("Показать")',
        'button:has-text("Показать номер")',
        'button:has-text("Показать телефон")',
        'button:has-text("Raqamni ko‘rsatish")',
        'button:has-text("Raqamni ko\'rsatish")',
    ]
    for selector in selectors:
        try:
            button = page.locator(selector)
            if button.count() == 0:
                continue
            button.first.click(timeout=3000)
            page.wait_for_timeout(700)
            break
        except Exception:
            continue

    phone_href = _first_attribute(page, ['a[href^="tel:"]'], "href")
    if phone_href:
        return normalize_phone(phone_href, default_country_code="998")

    return normalize_phone(
        _first_text(
            page,
            [
                '[data-testid="phone-number"]',
                '[data-testid="contact-phone"]',
                '[data-cy="ad-contact-phone"]',
                'button[data-testid="show-phone"]',
            ],
        ),
        default_country_code="998",
    )


def _extract_user_api_phones(responses: list[Any]) -> list[str]:
    """Read structured contacts returned by OLX's user JSON endpoint."""

    result: list[str] = []
    for response in responses:
        try:
            payload = response.json()
        except Exception:
            continue
        data = payload.get("data") if isinstance(payload, dict) else None
        business = data.get("business_data") if isinstance(data, dict) else None
        if not isinstance(business, dict):
            continue
        for key in ("phone1", "phone2", "phone3"):
            for phone in extract_uzbek_phones(business.get(key)):
                if phone not in result:
                    result.append(phone)
    return result


def _extract_images(page: Any) -> list[dict[str, Any]]:
    """Collect gallery image URLs without persisting the full OLX HTML."""

    candidates: list[str] = []
    selectors = [
        'meta[property="og:image"]',
        '[data-testid*="gallery"] img',
        '[data-testid*="photo"] img',
        '[data-cy*="gallery"] img',
    ]
    for selector in selectors:
        try:
            locator = page.locator(selector)
            for index in range(locator.count()):
                node = locator.nth(index)
                value = (
                    node.get_attribute("content")
                    or node.get_attribute("src")
                    or node.get_attribute("data-src")
                )
                if value and value.startswith(("http://", "https://")):
                    candidates.append(value)
        except Exception:
            continue

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, url in enumerate(candidates):
        if url in seen:
            continue
        seen.add(url)
        result.append(
            {"url": url, "sort_order": index, "is_primary": index == 0}
        )
    return result


def _extract_parameters(page: Any) -> dict[str, str]:
    selectors = [
        '[data-testid="ad-parameters-container"] li',
        '[data-testid="ad-parameters-container"] p',
        '[data-testid="ad-parameters-container"] div',
        '[data-cy="ad-parameters"] li',
    ]
    texts: list[str] = []
    for selector in selectors:
        try:
            elements = page.locator(selector)
            for index in range(elements.count()):
                text = clean_text(elements.nth(index).inner_text())
                if text and text not in texts:
                    texts.append(text)
        except Exception:
            continue

    parameters: dict[str, str] = {}
    for text in texts:
        if ":" not in text:
            continue
        key, value = (clean_text(part) for part in text.split(":", 1))
        if key and value and len(key) < 100:
            parameters[key] = value

    return parameters


def _find_structured_date(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("datePosted", "datePublished", "uploadDate"):
            if value.get(key):
                return str(value[key])
        for nested in value.values():
            result = _find_structured_date(nested)
            if result:
                return result
    elif isinstance(value, list):
        for nested in value:
            result = _find_structured_date(nested)
            if result:
                return result
    return None


def _parse_explicit_published_date(text: str | None) -> str | None:
    """Разбирает видимую строку OLX ``Опубликовано 26 августа 2026 г.``."""

    normalized = clean_text(text) or ""
    months = {
        "января": 1,
        "февраля": 2,
        "марта": 3,
        "апреля": 4,
        "мая": 5,
        "июня": 6,
        "июля": 7,
        "августа": 8,
        "сентября": 9,
        "октября": 10,
        "ноября": 11,
        "декабря": 12,
    }
    month_names = "|".join(months)
    match = re.search(
        r"опубликовано\s+(\d{1,2})\s+("
        + month_names
        + r")(?:\s+(\d{4}))?\s*г?\.?",
        normalized,
        flags=re.IGNORECASE,
    )
    if not match:
        return None

    tashkent_tz = timezone(timedelta(hours=5))
    now = datetime.now(tashkent_tz)
    return datetime(
        int(match.group(3) or now.year),
        months[match.group(2).lower()],
        int(match.group(1)),
        tzinfo=tashkent_tz,
    ).isoformat()


def _extract_published_at(page: Any, visible_text: str | None) -> str | None:
    """Возвращает ISO-дату OLX из явной строки, JSON-LD или fallback-текста."""

    explicit_date = _parse_explicit_published_date(visible_text)
    if explicit_date:
        return explicit_date

    try:
        scripts = page.locator('script[type="application/ld+json"]')
        for index in range(scripts.count()):
            try:
                date_value = _find_structured_date(
                    json.loads(scripts.nth(index).inner_text())
                )
                if date_value:
                    return date_value
            except Exception:
                continue
    except Exception:
        pass

    text = (clean_text(visible_text) or "").lower()
    tashkent_tz = timezone(timedelta(hours=5))
    now = datetime.now(tashkent_tz)
    time_match = re.search(r"(?:в|soat)?\s*(\d{1,2}):(\d{2})", text)
    hour = int(time_match.group(1)) if time_match else 0
    minute = int(time_match.group(2)) if time_match else 0

    if "сегодня" in text or "bugun" in text:
        return now.replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ).isoformat()
    if "вчера" in text or "kecha" in text:
        return (now - timedelta(days=1)).replace(
            hour=hour, minute=minute, second=0, microsecond=0
        ).isoformat()

    months = {
        "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
        "мая": 5, "июня": 6, "июля": 7, "августа": 8,
        "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
    }
    date_match = re.search(
        r"(\d{1,2})\s+(" + "|".join(months) + r")(?:\s+(\d{4}))?",
        text,
    )
    if date_match:
        return datetime(
            int(date_match.group(3) or now.year),
            months[date_match.group(2)],
            int(date_match.group(1)),
            hour,
            minute,
            tzinfo=tashkent_tz,
        ).isoformat()
    return None


def _known_housing_from_body(body_text: str) -> dict[str, Any]:
    patterns = {
        "rooms": (r"Количество комнат\s*:?\s*(\d+)", r"Комнат\s*:?\s*(\d+)"),
        "total_area_m2": (
            r"Общая площадь\s*:?\s*([\d.,]+)\s*м",
            r"Площадь\s*:?\s*([\d.,]+)\s*м",
        ),
        "floor": (r"Этаж\s*:?\s*(\d+)",),
        "floors_total": (
            r"Этажность дома\s*:?\s*(\d+)",
            r"Количество этажей\s*:?\s*(\d+)",
        ),
    }
    result: dict[str, Any] = {}
    for field, field_patterns in patterns.items():
        for pattern in field_patterns:
            match = re.search(pattern, body_text, flags=re.IGNORECASE)
            if not match:
                continue
            result[field] = (
                parse_int(match.group(1))
                if field in {"rooms", "floor", "floors_total"}
                else parse_float(match.group(1))
            )
            break
    return result


def _housing_from_parameters(
    parameters: dict[str, str],
    fallback: dict[str, Any],
) -> dict[str, Any]:
    market = (
        parameters.get("Рынок")
        or parameters.get("Тип жилья")
        or parameters.get("Состояние жилья")
    )
    market_text = (clean_text(market) or "").casefold()
    is_new_building = None
    building_type = None
    if "новост" in market_text or "первич" in market_text:
        is_new_building, building_type = True, "new_building"
    elif "вторич" in market_text:
        is_new_building, building_type = False, "secondary"

    return {
        "rooms": parse_int(parameters.get("Количество комнат"))
        or fallback.get("rooms"),
        "total_area_m2": parse_float(parameters.get("Общая площадь"))
        or fallback.get("total_area_m2"),
        "floor": parse_int(parameters.get("Этаж")) or fallback.get("floor"),
        "floors_total": parse_int(parameters.get("Этажность дома"))
        or fallback.get("floors_total"),
        "furnished": parse_yes_no(parameters.get("Меблирована")),
        "commission": parse_yes_no(parameters.get("Комиссионные")),
        "address": parameters.get("Адрес"),
        "street": parameters.get("Улица"),
        "house_number": parameters.get("Номер дома"),
        "zone": parameters.get("Махалля") or parameters.get("Зона"),
        "building_type": building_type,
        "is_new_building": is_new_building,
        "foundation_type": parameters.get("Тип строения")
        or parameters.get("Материал стен")
        or parameters.get("Тип дома"),
        "repair": parameters.get("Ремонт"),
        "residential_complex_name": parameters.get("Жилой комплекс")
        or parameters.get("Название ЖК"),
    }


def _classify_profile_listing(text: str) -> str | None:
    if _contains_any(text, NON_HOUSING_KEYWORDS):
        return None
    if not _contains_any(text, HOUSING_KEYWORDS):
        return None

    rent = _contains_any(text, RENT_KEYWORDS)
    sale = _contains_any(text, SALE_KEYWORDS)
    if rent and not sale:
        return "rent"
    if sale and not rent:
        return "sale"

    normalized = text.lower()
    if "аренда долгосрочная" in normalized:
        return "rent"
    if "продажа квартир" in normalized or "продажа домов" in normalized:
        return "sale"
    return None


def _publication_features(text: str) -> dict[str, Any]:
    raw_text = text or ""
    normalized = (clean_text(raw_text) or "").lower()
    rooms_match = re.search(
        r"(?<!\d)(\d+)(?:\+\d+)?\s*[-–—]?\s*"
        r"(?:комн(?:ат[аы]?)?|komn(?:at)?|хонали?|хона|xonali|xona)\b",
        normalized,
    )
    area_match = re.search(
        r"(?<!\d)(\d+(?:[.,]\d+)?)\s*(?:м²|м2|кв\.?\s*м|m²|m2)\b",
        normalized,
    )
    city = None
    district = None
    lines = [clean_text(line) for line in raw_text.splitlines()]
    for line in filter(None, lines):
        if "," not in line or _detect_currency(line):
            continue
        if re.search(r"\d+\s*(?:сум|у\.е|\$)", line, re.I):
            continue
        if _contains_any(line, HOUSING_KEYWORDS + RENT_KEYWORDS + SALE_KEYWORDS):
            continue
        parts = [clean_text(part) for part in line.split(",")]
        if len(parts) >= 2 and parts[0]:
            first = parts[0]
            second = re.split(r"\s*[·|].*$", parts[1] or "")[0] or None
            if second and re.search(r"\bобласть\b", second, re.IGNORECASE):
                city, district = second, first
            else:
                city, district = first, second
            break

    # За пределами города Ташкента OLX часто отдаёт пару
    # ``населённый пункт + область``. По схеме проекта область хранится в
    # city, а город/посёлок — в district, независимо от порядка этих строк.
    meaningful_lines = [
        line
        for line in lines
        if line
        and not re.search(
            r"опубликовано|размещено|посмотреть.*карт|^\d{1,2}:\d{2}$",
            line,
            flags=re.IGNORECASE,
        )
    ]
    if city is None:
        for index, line in enumerate(meaningful_lines):
            if re.search(r"\bобласть\b", line, flags=re.IGNORECASE) and index:
                candidate = meaningful_lines[index - 1]
                if not _detect_currency(candidate) and not re.search(
                    r"\d", candidate
                ):
                    city = line
                    district = district or candidate
                    break
            if re.search(r"\bобласть\b", line, flags=re.IGNORECASE):
                following = (
                    meaningful_lines[index + 1]
                    if index + 1 < len(meaningful_lines)
                    else None
                )
                if following and not re.search(r"\d", following):
                    city = line
                    district = district or following
                    break
    if city is None:
        for line in meaningful_lines:
            if line.casefold() in {"ташкент", "tashkent", "toshkent"}:
                city = "Ташкент"
                break

    if district is None:
        for stems, canonical_name in TASHKENT_DISTRICT_STEMS:
            if any(stem in normalized for stem in stems):
                district = canonical_name
                break
    if city is None and district in TASHKENT_DISTRICTS:
        city = "Ташкент"

    return {
        "city": city,
        "district": district,
        "rooms": int(rooms_match.group(1)) if rooms_match else None,
        "total_area_m2": (
            float(area_match.group(1).replace(",", "."))
            if area_match
            else None
        ),
    }


def _location_section_features(body_text: str) -> dict[str, Any]:
    """Читает только секцию местоположения, не используя описание."""

    lines = [clean_text(line) for line in (body_text or "").splitlines()]
    lines = [line for line in lines if line]
    marker_index = next(
        (
            index
            for index, line in enumerate(lines)
            if line.casefold() in {
                "местоположение",
                "расположение",
                "joylashuv",
                "location",
            }
        ),
        None,
    )
    if marker_index is None:
        return {"city": None, "district": None}

    block: list[str] = []
    for line in lines[marker_index + 1 : marker_index + 7]:
        if re.search(
            r"посмотреть.*карт|похожие объявления|мобильные приложения",
            line,
            flags=re.IGNORECASE,
        ):
            break
        block.append(line)

    result = _publication_features("\n".join(block))
    if result["city"] is None:
        for line in block:
            if (
                not re.search(r"\b(?:область|район)\b", line, re.IGNORECASE)
                and not re.search(r"\d", line)
                and not _detect_currency(line)
            ):
                result["city"] = line
                break
    return result


def _location_from_json_ld(payload: Any) -> dict[str, str | None]:
    """Достаёт населённый пункт и район из структурированных данных OLX."""

    city = None
    district = None

    def visit(value: Any, parent_key: str | None = None) -> None:
        nonlocal city, district
        if isinstance(value, list):
            for item in value:
                visit(item, parent_key)
            return
        if not isinstance(value, dict):
            return

        locality = clean_text(value.get("addressLocality"))
        if locality and city is None:
            city = locality

        name = clean_text(value.get("name"))
        if (
            name
            and district is None
            and (
                parent_key in {"areaServed", "addressRegion"}
                or value.get("@type") == "AdministrativeArea"
            )
            and "район" in name.lower()
        ):
            district = name

        for key, child in value.items():
            visit(child, key)

    visit(payload)
    if city is None and district in TASHKENT_DISTRICTS:
        city = "Ташкент"
    return {"city": city, "district": district}


def _extract_json_ld_location(page: Any) -> dict[str, str | None]:
    result: dict[str, str | None] = {"city": None, "district": None}
    try:
        scripts = page.locator('script[type="application/ld+json"]')
        for index in range(scripts.count()):
            raw = scripts.nth(index).text_content()
            if not raw:
                continue
            try:
                location = _location_from_json_ld(json.loads(raw))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            result["city"] = result["city"] or location["city"]
            result["district"] = result["district"] or location["district"]
            if result["city"] and result["district"]:
                break
    except Exception:
        pass
    return result


def _features_from_listing(listing: dict[str, Any]) -> dict[str, Any]:
    features = _publication_features(
        "\n".join(
            filter(None, [listing.get("_location"), listing.get("title")])
        )
    )
    housing = listing.get("housing") or {}
    features["rooms"] = housing.get("rooms") or features["rooms"]
    features["total_area_m2"] = (
        housing.get("total_area_m2") or features["total_area_m2"]
    )
    features["floor"] = housing.get("floor")
    features["floors_total"] = housing.get("floors_total")
    return features


class OlxSource(ListingSource):
    def __init__(self, config: OlxConfig | None = None):
        self.config = config or OlxConfig()

    @property
    def name(self) -> str:
        return "olx"

    @property
    def start_url(self) -> str:
        return self.config.base_url

    def _parse_detail_page(
        self,
        page: Any,
        listing: dict[str, Any],
    ) -> dict[str, Any]:
        print(f"    -> {listing['url']}")
        listing["_detail_loaded"] = False
        user_api_responses: list[Any] = []

        def capture_user_api_response(response: Any) -> None:
            if re.search(r"/api/v1/users/\d+/?(?:\?|$)", response.url):
                user_api_responses.append(response)

        page.on("response", capture_user_api_response)
        try:
            response = page.goto(
                listing["url"],
                wait_until="domcontentloaded",
                timeout=60000,
            )
            if not response or response.status != 200:
                return listing
            page.wait_for_timeout(1500)

            listing["title"] = _first_text(
                page,
                ["h1", '[data-cy="ad_title"]', '[data-testid="ad-title"]'],
            ) or listing.get("title")
            price_text = _first_text(
                page,
                [
                    '[data-testid="ad-price-container"]',
                    '[data-testid="ad-price"]',
                    '[data-cy="ad_price"]',
                ],
            )
            if price_text:
                listing["price"] = _parse_price(price_text)
                listing["currency"] = _detect_currency(price_text)

            location = _first_text(
                page,
                [
                    '[data-testid="location-date"]',
                    '[data-testid="map-link"]',
                    '[data-cy="ad_location"]',
                ],
            )
            description = _first_text(
                page,
                [
                    '[data-cy="ad_description"]',
                    '[data-testid="ad-description"]',
                    '[data-testid="ad-description-container"]',
                ],
            )
            seller_name = _first_text(
                page,
                [
                    '[data-testid="user-profile-link"]',
                    '[data-testid="user-profile-name"]',
                ],
            )
            profile_href = _first_attribute(
                page,
                [
                    'a[data-testid="user-profile-link"]',
                    '[data-testid="user-profile-link"] a',
                    'a[href*="/oferta/user/"]',
                    'a[href*="/profile/"]',
                ],
                "href",
            )
            parameters = _extract_parameters(page)
            try:
                body_text = page.locator("body").inner_text()
            except Exception:
                body_text = ""

            listing["_location"] = location
            listing["_description"] = description
            listing["published_at"] = _extract_published_at(
                page, body_text or location
            )
            listing["housing"] = _housing_from_parameters(
                parameters,
                _known_housing_from_body(body_text),
            )
            structured_location = _extract_json_ld_location(page)
            location_features = _publication_features(location or "")
            body_location = _location_section_features(body_text)
            listing["housing"]["city"] = (
                structured_location["city"]
                or location_features["city"]
                or body_location["city"]
            )
            listing["housing"]["district"] = (
                structured_location["district"]
                or location_features["district"]
                or body_location["district"]
            )
            listing["amenities"] = parse_multiselect(
                parameters.get("В квартире есть")
            )
            listing["nearby"] = parse_multiselect(parameters.get("Рядом есть"))
            listing["images"] = _extract_images(page)
            profile_url = _normalize_url(profile_href, self.config.domain)
            is_official_seller, official_complex_name = (
                official_seller_details(profile_url, seller_name)
            )
            # Priority: structured OLX JSON, revealed contact control, exact
            # phone-shaped seller name, and finally free-form description.
            phones = _extract_user_api_phones(user_api_responses)
            phone_source = "olx_user_api" if phones else None
            if not phones:
                revealed_phone = _extract_phone(page)
                phones = extract_uzbek_phones(revealed_phone)
                phone_source = "olx_detail_page" if phones else None
                if not phones:
                    phones = _extract_user_api_phones(user_api_responses)
                    phone_source = "olx_user_api" if phones else None
            if not phones:
                name_phone = extract_olx_phone_from_seller_name(seller_name)
                phones = [name_phone] if name_phone else []
                phone_source = "olx_seller_name" if phones else None
            if not phones:
                phones = extract_uzbek_phones_from_description(description)
                phone_source = "olx_description" if phones else None
            if not phones:
                try:
                    page_html = page.content()
                except Exception:
                    page_html = ""
                phones = extract_uzbek_phones_from_payloads(
                    body_text,
                    page_html,
                )
                phone_source = "olx_listing_content" if phones else None
            listing["seller"] = {
                "name": normalize_seller_name(seller_name, "olx"),
                "profile_url": profile_url,
                "phone": phones[0] if phones else None,
                "phones": phones,
                "phone_source": phone_source,
                "seller_role": None,
                "is_official_seller": is_official_seller,
                "official_complex_name": official_complex_name,
            }
            listing["_detail_loaded"] = True
        except Exception as error:
            print("    Ошибка detail page:", error)
        finally:
            try:
                page.remove_listener("response", capture_user_api_response)
            except Exception:
                pass
        return listing

    def _publication_record(
        self,
        card: Any,
        listing_url: str,
        listing_type: str,
        raw_text: str,
    ) -> dict[str, Any]:
        return {
            "id": _listing_id(listing_url),
            "title": _first_text(
                card,
                ["h4", "h6", '[data-cy="ad-card-title"]'],
            ),
            "type": listing_type,
            "url": listing_url,
            **_publication_features(raw_text),
        }

    def _collect_seller_profile(
        self,
        page: Any,
        profile_url: str,
        current_url: str,
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {
            "profile_available": False,
            "active_rent_listings": 0,
            "active_sale_listings": 0,
            "active_publications": [],
            "closed_publications": [],
            "closed_publications_available": False,
            "profile_pages_scanned": 0,
            "profile_scan_complete": False,
            "distinct_housing_listings": 0,
            "_classified_urls": set(),
            "_fingerprints": set(),
        }
        seen_urls: set[str] = set()
        current_url = _normalize_url(current_url, self.config.domain) or current_url

        for page_number in range(1, self.config.max_seller_profile_pages + 1):
            try:
                response = page.goto(
                    _url_with_page(profile_url, page_number),
                    wait_until="domcontentloaded",
                    timeout=60000,
                )
                if not response:
                    break
                if response.status == 404:
                    stats["profile_scan_complete"] = stats["profile_available"]
                    break
                if response.status != 200:
                    break
                page.wait_for_timeout(1200)
                try:
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(500)
                except Exception:
                    pass

                cards = page.locator('[data-cy="l-card"]')
                new_count = 0
                for index in range(cards.count()):
                    card = cards.nth(index)
                    try:
                        link = card.locator('a[href*="/d/"]')
                        if link.count() == 0:
                            continue
                        url = _normalize_url(
                            link.first.get_attribute("href"),
                            self.config.domain,
                        )
                        if not url or url in seen_urls:
                            continue
                        seen_urls.add(url)
                        new_count += 1
                        raw_text = card.inner_text() or ""
                        listing_type = (
                            "rent"
                            if url == current_url
                            else _classify_profile_listing(
                                f"{url} {clean_text(raw_text) or ''}"
                            )
                        )
                        if listing_type not in {"rent", "sale"}:
                            continue

                        stats[f"active_{listing_type}_listings"] += 1
                        stats["_classified_urls"].add(url)
                        publication = self._publication_record(
                            card, url, listing_type, raw_text
                        )
                        stats["active_publications"].append(publication)
                        fingerprint = publication_fingerprint(publication)
                        if fingerprint:
                            stats["_fingerprints"].add(fingerprint)
                    except Exception:
                        continue

                stats["profile_available"] = True
                stats["profile_pages_scanned"] += 1
                if cards.count() == 0 or new_count == 0:
                    stats["profile_scan_complete"] = True
                    break
            except Exception:
                break
        else:
            stats["profile_scan_complete"] = False

        stats["distinct_housing_listings"] = len(stats["_fingerprints"])
        self._collect_closed_publications(page, stats, seen_urls)
        return stats

    def _collect_closed_publications(
        self,
        page: Any,
        stats: dict[str, Any],
        seen_urls: set[str],
    ) -> None:
        selectors = [
            'a:has-text("Завершенные")', 'a:has-text("Завершённые")',
            'button:has-text("Завершенные")', 'button:has-text("Завершённые")',
            'a:has-text("Закрытые")', 'button:has-text("Закрытые")',
            'a:has-text("Неактивные")', 'button:has-text("Неактивные")',
            'a:has-text("Проданные")', 'button:has-text("Проданные")',
            'a:has-text("Архив")', 'button:has-text("Архив")',
            'a:has-text("Tugatilgan")', 'button:has-text("Tugatilgan")',
        ]
        if not stats["profile_available"]:
            return
        for selector in selectors:
            try:
                tab = page.locator(selector)
                if tab.count() == 0:
                    continue
                tab.first.click(timeout=3000)
                page.wait_for_timeout(1000)
                stats["closed_publications_available"] = True
                break
            except Exception:
                continue
        if not stats["closed_publications_available"]:
            return

        try:
            cards = page.locator('[data-cy="l-card"]')
            for index in range(cards.count()):
                card = cards.nth(index)
                try:
                    link = card.locator('a[href*="/d/"]')
                    if link.count() == 0:
                        continue
                    url = _normalize_url(
                        link.first.get_attribute("href"),
                        self.config.domain,
                    )
                    if not url or url in seen_urls:
                        continue
                    raw_text = card.inner_text() or ""
                    listing_type = _classify_profile_listing(
                        f"{url} {clean_text(raw_text) or ''}"
                    )
                    if listing_type not in {"rent", "sale"}:
                        continue
                    stats["closed_publications"].append(
                        self._publication_record(
                            card, url, listing_type, raw_text
                        )
                    )
                    seen_urls.add(url)
                except Exception:
                    continue
        except Exception:
            pass

    def _enrich_profile_with_current(
        self,
        stats: dict[str, Any],
        listing: dict[str, Any],
    ) -> None:
        if not stats.get("profile_available"):
            return
        current_url = _normalize_url(
            listing.get("url"), self.config.domain
        )
        features = _features_from_listing(listing)
        fingerprint = publication_fingerprint(features)
        if fingerprint:
            stats["_fingerprints"].add(fingerprint)
        stats["distinct_housing_listings"] = len(stats["_fingerprints"])

        for publication in stats.get("active_publications", []):
            if publication.get("url") == current_url:
                publication.update(
                    {key: value for key, value in features.items() if value is not None}
                )
                break

        if current_url and current_url not in stats["_classified_urls"]:
            stats["active_rent_listings"] += 1
            stats["_classified_urls"].add(current_url)
            stats["active_publications"].append(
                {
                    "id": _listing_id(current_url),
                    "title": listing.get("title"),
                    "type": "rent",
                    "url": current_url,
                    **features,
                }
            )

    def _category_card_listing(
        self,
        card: Any,
        page_number: int,
    ) -> dict[str, Any] | None:
        links = card.locator('a[href*="/d/"]')
        if links.count() == 0:
            return None
        url = _normalize_url(
            links.first.get_attribute("href"), self.config.domain
        )
        if not url:
            return None
        title = _first_text(
            card,
            ["h4", "h6", '[data-cy="ad-card-title"]'],
        )
        price_text = None
        for line in card.inner_text().splitlines():
            line = clean_text(line)
            if line and _detect_currency(line) and re.search(r"\d", line):
                price_text = line
                break

        return {
            "source": self.name,
            "id": _listing_id(url),
            "title": title,
            "transaction_type": "rent",
            "price": _parse_price(price_text),
            "currency": _detect_currency(price_text),
            "url": url,
            "housing": {},
            "amenities": [],
            "nearby": [],
            "images": [],
            "seller": {},
            "_category_page": page_number,
        }

    def collect(
        self,
        limit: int | None,
        existing: ExistingListings | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        existing = existing or ExistingListings()

        # Playwright нужен только конкретному источнику OLX. Импорт здесь не
        # навязывает эту зависимость универсальному конвейеру и другим сайтам.
        from playwright.sync_api import sync_playwright

        listings: list[dict[str, Any]] = []
        seen_urls: set[str] = set()
        known_count = max(len(existing.platform_ids), len(existing.urls))
        category_page_limit = self.config.catalog_page_limit
        if category_page_limit is None:
            category_page_limit = (
                max(
                    self.config.max_category_pages,
                    min(50, known_count // 20 + 2),
                )
                if limit is not None
                else None
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=self.config.headless)
            category_page = browser.new_page(
                locale="ru-RU", viewport={"width": 1440, "height": 1000}
            )
            detail_page = browser.new_page(
                locale="ru-RU", viewport={"width": 1440, "height": 1000}
            )

            try:
                page_number = 1
                reached_existing_boundary = False
                while (
                    category_page_limit is None
                    or page_number <= category_page_limit
                ):
                    if limit is not None and len(listings) >= limit:
                        break
                    category_parts = urlsplit(self.config.base_url)
                    category_query = dict(
                        parse_qsl(category_parts.query, keep_blank_values=True)
                    )
                    category_query["currency"] = "UZS"
                    category_query["page"] = str(page_number)
                    if self.config.newest_first:
                        category_query["search[order]"] = "created_at:desc"
                    category_url = urlunsplit(
                        (
                            category_parts.scheme,
                            category_parts.netloc,
                            category_parts.path,
                            urlencode(category_query),
                            "",
                        )
                    )
                    print("\n" + "=" * 80)
                    page_label = (
                        f"{page_number}/{category_page_limit}"
                        if category_page_limit is not None
                        else str(page_number)
                    )
                    print(f"OLX CATEGORY PAGE {page_label}")
                    response = category_page.goto(
                        category_url,
                        wait_until="domcontentloaded",
                        timeout=60000,
                    )
                    if not response or response.status != 200:
                        if category_page_limit is None:
                            break
                        page_number += 1
                        continue
                    category_page.wait_for_timeout(2000)
                    cards = category_page.locator('[data-cy="l-card"]')
                    if cards.count() == 0:
                        break
                    current: list[dict[str, Any]] = []
                    remaining = (
                        limit - len(listings) if limit is not None else None
                    )
                    candidate_limit = (
                        max(remaining * 3, remaining + 5)
                        if remaining is not None
                        else None
                    )
                    page_had_unseen_url = False
                    for index in range(cards.count()):
                        try:
                            listing = self._category_card_listing(
                                cards.nth(index), page_number
                            )
                            if not listing or listing["url"] in seen_urls:
                                continue
                            seen_urls.add(listing["url"])
                            page_had_unseen_url = True
                            if existing.contains(listing.get("id"), listing["url"]):
                                print("    Уже есть в БД, пропуск:", listing["url"])
                                if self.config.stop_at_first_existing:
                                    reached_existing_boundary = True
                                    break
                                continue
                            current.append(listing)
                            if (
                                candidate_limit is not None
                                and len(current) >= candidate_limit
                            ):
                                break
                        except Exception as error:
                            print(f"Ошибка карточки {index}:", error)

                    if not page_had_unseen_url:
                        break

                    for index, listing in enumerate(current, start=1):
                        print(f"[PAGE {page_number}] {index}/{len(current)}")
                        listing = self._parse_detail_page(detail_page, listing)
                        if not listing.pop("_detail_loaded", False):
                            print("    Пропуск: detail-page загружена не полностью")
                            continue
                        listings.append(listing)
                        if limit is not None and len(listings) >= limit:
                            break
                        time.sleep(0.7)

                    print("Всего собрано:", len(listings))
                    if limit is not None and len(listings) >= limit:
                        print(f"Достигнут тестовый лимит: {limit} объявлений.")
                        break
                    if reached_existing_boundary:
                        print("Достигнута первая известная публикация OLX.")
                        break
                    page_number += 1
                    time.sleep(1)
            finally:
                browser.close()

        return listings
