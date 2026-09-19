"""Адаптер Realting.uz для общего конвейера проекта.

Парсер намеренно использует структуру ``housing_source_data`` из
realting_publication_raw_json.py. Это позволяет сохранять смысл исходных
ключей Realting и при этом отдавать общий канонический формат проекта.
Контакт из JSON-LD Realting (это телефон самой организации сайта) продавцом
объявления не считается.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.existing_listings import ExistingListings
from core.normalization import clean_text, parse_float, parse_int
from standalone_parsers.realting_publication_raw_json import (
    DEFAULT_CATALOG_URL,
    _parse_publication,
    _publication_urls,
    _validate_catalog_url,
)
from sources.base import ListingSource


AGENCY_EVALUATION = r"""
() => {
    const link = document.querySelector(
        '.contacts a[href*="/agencies/"]'
    ) || [...document.querySelectorAll('a[href*="/agencies/"]')]
        .find(a => /\/agencies\/[^/]+$/.test(new URL(a.href).pathname));
    const contacts = link?.closest('.contacts') || document;
    const decode = (value) => {
        if (!value) return null;
        try { return atob(value); } catch (_) { return null; }
    };
    const phone = contacts.querySelector('[data-encr-ph]');
    const email = contacts.querySelector('[data-encr-em]');
    const telegram = [...contacts.querySelectorAll('a[href*="telegram"]')]
        .map(a => a.href)[0] || null;
    return {
        "name": link?.innerText?.trim() || null,
        "profile_url": link?.href || null,
        "phone": decode(phone?.getAttribute('data-encr-ph')),
        "email": decode(email?.getAttribute('data-encr-em')),
        "telegram_url": telegram,
        "seller_role": link ? "agency" : null,
    };
}
"""

RESIDENTIAL_KEYWORDS = (
    "квартир",
    "апартамент",
    "студи",
    "комнат",
    "жилой дом",
    "коттедж",
    "таунхаус",
    "вилл",
)
COMMERCIAL_KEYWORDS = (
    "коммерч",
    "офис",
    "склад",
    "магазин",
    "торгов",
    "помещен",
    "бизнес-центр",
    "ресторан",
)


def _first_nonempty(*values: Any) -> Any:
    for value in values:
        if value is not None and clean_text(value):
            return value
    return None


def _parse_price(price_data: Any) -> tuple[int | float | None, str | None]:
    """Берёт отображаемую цену и валюту из ключей Realting без догадок."""

    if not isinstance(price_data, dict):
        return None, None

    displayed = clean_text(price_data.get("Отображаемое значение"))
    attributes = price_data.get("Атрибуты источника") or {}
    if not isinstance(attributes, dict):
        attributes = {}

    currency: str | None = None
    value = displayed
    normalized = (displayed or "").lower()
    if "$" in normalized or "usd" in normalized:
        currency, value = "USD", displayed
    elif "€" in normalized or "eur" in normalized:
        currency, value = "EUR", displayed
    elif "₽" in normalized or "руб" in normalized:
        currency, value = "RUB", displayed
    elif "сум" in normalized or "uzs" in normalized:
        currency, value = "UZS", displayed

    # Если отображаемая цена отсутствует, используем доступные атрибуты.
    if not value:
        for key, candidate_currency in (
            ("data-price-usd", "USD"),
            ("data-price-uzs", "UZS"),
            ("data-price-eur", "EUR"),
            ("data-price-rub", "RUB"),
        ):
            candidate = clean_text(attributes.get(key))
            if candidate:
                value, currency = candidate, candidate_currency
                break

    amount = parse_float(value)
    if amount is None:
        return None, currency
    scale_text = (str(value).lower() if value is not None else "")
    if "млн" in scale_text or "million" in scale_text:
        amount *= 1_000_000
    elif "тыс" in scale_text or "k сум" in scale_text:
        amount *= 1_000
    # Для денежных значений без дробной части сохраняем целое число.
    return (int(amount) if amount.is_integer() else amount), currency


def _coordinates(scripts: Any) -> tuple[float | None, float | None]:
    """Извлекает longitude/latitude из singleMarker Realting."""

    if not isinstance(scripts, list):
        scripts = [scripts]
    text = "\n".join(str(item) for item in scripts if item)
    match = re.search(
        r"singleMarker\s*\(\s*\[\s*['\"]?"
        r"(-?\d+(?:\.\d+)?)['\"]?\s*,\s*['\"]?"
        r"(-?\d+(?:\.\d+)?)",
        text,
    )
    if not match:
        match = re.search(
            r"LOCATION_YANDEX_MAP\s*=.*?\[\s*(-?\d+(?:\.\d+)?)\s*,\s*"
            r"(-?\d+(?:\.\d+)?)\s*\]",
            text,
            flags=re.DOTALL,
        )
    if not match:
        return None, None
    return float(match.group(1)), float(match.group(2))


def _realting_images(source_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract image URLs captured from Realting's gallery markup."""

    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, photo in enumerate(source_data.get("Фотографии") or []):
        if not isinstance(photo, dict):
            continue
        attributes = photo.get("attributes") or {}
        if not isinstance(attributes, dict):
            attributes = {}
        style = str(photo.get("style") or "")
        url = (
            attributes.get("src")
            or attributes.get("data-src")
            or attributes.get("data-original")
            or attributes.get("data-lazy-src")
        )
        if not url:
            match = re.search(r"url\(['\"]?([^'\")]+)", style)
            url = match.group(1) if match else None
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        result.append(
            {
                "url": url,
                "sort_order": index,
                "is_primary": index == 0,
            }
        )
    return result


def _published_at(source_data: dict[str, Any]) -> str | None:
    """Use only an explicit structured publication timestamp from Realting."""

    candidates: list[Any] = [source_data.get("Дата публикации")]
    for item in source_data.get("Мета-данные страницы") or []:
        attributes = item.get("attributes") if isinstance(item, dict) else None
        if not isinstance(attributes, dict):
            continue
        marker = str(
            attributes.get("property") or attributes.get("name") or ""
        ).casefold()
        if marker in {"article:published_time", "date", "datepublished"}:
            candidates.append(attributes.get("content"))

    for value in candidates:
        cleaned = clean_text(value)
        if not cleaned:
            continue
        try:
            parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.isoformat()
    return None


def _is_residential(source_data: dict[str, Any]) -> bool:
    """Exclude commercial records from Realting's mixed rent catalog."""

    title = (clean_text(source_data.get("Заголовок")) or "").casefold()
    sections = " ".join(
        str(name).casefold()
        for name in (source_data.get("Характеристики объекта") or {})
    )
    if "параметры квартиры" in sections or "параметры дома" in sections:
        return True
    if any(keyword in title for keyword in COMMERCIAL_KEYWORDS):
        return False
    return any(keyword in title for keyword in RESIDENTIAL_KEYWORDS)


def _canonical_listing(publication: dict[str, Any]) -> dict[str, Any]:
    source_data = publication.get("housing_source_data") or {}
    location = source_data.get("Местонахождение") or {}
    characteristics = source_data.get("Характеристики объекта") or {}
    apartment = characteristics.get("Параметры квартиры") or {}
    building = characteristics.get("Параметры здания") or {}
    price, currency = _parse_price(source_data.get("Цена"))
    longitude, latitude = _coordinates(
        source_data.get("Исходные скрипты координат")
    )
    agency = publication.get("seller_source_data") or {}

    title = _first_nonempty(source_data.get("Заголовок"))
    description = source_data.get("Описание")
    url = publication.get("final_url") or publication.get("requested_url")
    listing_id = _first_nonempty(source_data.get("ID"))
    if listing_id is None:
        match = re.search(r"/property-to-rent/(\d+)", str(url))
        listing_id = match.group(1) if match else None

    metro = _first_nonempty(location.get("Метро"))
    return {
        "source": "realting",
        "id": str(listing_id) if listing_id is not None else None,
        "title": title,
        "price": price,
        "currency": currency,
        "transaction_type": "rent",
        "url": url,
        "published_at": _published_at(source_data),
        "description": description,
        "description_length": len(str(description or "")),
        "housing": {
            "city": _first_nonempty(location.get("Город")),
            "district": _first_nonempty(location.get("Район")),
            "street": None,
            "building_type": None,
            "is_new_building": None,
            "foundation_type": None,
            "residential_complex_name": None,
            "rooms": parse_int(apartment.get("Количество комнат")),
            "total_area_m2": parse_float(apartment.get("Общая площадь")),
            "floor": parse_int(apartment.get("Этаж")),
            "floors_total": parse_int(building.get("Количество этажей")),
            "furnished": None,
            "monthly_rent": price,
            "rent_currency": currency,
            "latitude": latitude,
            "longitude": longitude,
        },
        "amenities": [],
        "nearby": [metro] if metro else [],
        "images": _realting_images(source_data),
        # Контакт берётся из блока агентства объявления. Телефон из JSON-LD
        # организации Realting сюда намеренно не попадает.
        "seller": {
            "name": _first_nonempty(agency.get("name")),
            "phone": _first_nonempty(agency.get("phone")),
            "phone_source": "realting" if agency.get("phone") else None,
            "profile_url": _first_nonempty(agency.get("profile_url")),
            "seller_role": "agency" if agency.get("profile_url") else None,
        },
    }


class RealtingSource(ListingSource):
    """Каталог объявлений аренды Realting.uz."""

    def __init__(
        self,
        catalog_url: str = DEFAULT_CATALOG_URL,
        *,
        headed: bool = False,
        timeout: int = 60_000,
        max_pages: int | None = None,
        stop_at_first_existing: bool = False,
    ) -> None:
        _validate_catalog_url(catalog_url)
        self.catalog_url = catalog_url
        self.headed = headed
        self.timeout = timeout
        self.max_pages = max_pages
        self.stop_at_first_existing = stop_at_first_existing

    @property
    def name(self) -> str:
        return "realting"

    @property
    def start_url(self) -> str:
        return self.catalog_url

    def collect(
        self,
        limit: int | None,
        existing: ExistingListings | None = None,
    ) -> list[dict[str, Any]]:
        if limit is not None and limit <= 0:
            return []
        existing = existing or ExistingListings()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as error:
            raise RuntimeError(
                "Не найден Playwright. Установите: pip install playwright; "
                "playwright install chromium"
            ) from error

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=not self.headed)
            catalog_page = browser.new_page(
                locale="ru-RU", viewport={"width": 1440, "height": 1000}
            )
            detail_page = browser.new_page(
                locale="ru-RU", viewport={"width": 1440, "height": 1000}
            )
            try:
                result: list[dict[str, Any]] = []
                seen_urls: set[str] = set()
                page_number = 1
                catalog_page_size: int | None = None
                while True:
                    parsed = urlsplit(self.catalog_url)
                    query = [
                        pair for pair in parse_qsl(parsed.query, keep_blank_values=True)
                        if pair[0] != "page"
                    ]
                    if page_number > 1:
                        query.append(("page", str(page_number)))
                    page_url = urlunsplit(
                        (parsed.scheme, parsed.netloc, parsed.path, urlencode(query), "")
                    )
                    try:
                        urls = _publication_urls(
                            catalog_page, page_url, None, self.timeout
                        )
                    except RuntimeError as error:
                        if page_number > 1 and "HTTP: 404" in str(error):
                            break
                        raise
                    if not urls:
                        break
                    if catalog_page_size is None:
                        catalog_page_size = len(urls)
                    new_urls = [url for url in urls if url not in seen_urls]
                    if not new_urls:
                        break
                    seen_urls.update(new_urls)
                    print(f"Realting: страница каталога {page_number}")
                    for index, url in enumerate(new_urls, start=1):
                        id_match = re.search(r"/property-to-rent/(\d+)", url)
                        platform_id = id_match.group(1) if id_match else None
                        if existing.contains(platform_id, url):
                            print(
                                f"[Realting {index}/{len(new_urls)}] "
                                f"уже есть в БД, пропуск: {url}"
                            )
                            if self.stop_at_first_existing:
                                return result
                            continue
                        print(f"[Realting {index}/{len(new_urls)}] {url}")
                        try:
                            publication = _parse_publication(
                                detail_page, url, self.timeout
                            )
                        except Exception as error:
                            print(f"  Пропуск: {error}")
                            continue
                        if publication.get("error"):
                            print(f"  Пропуск: {publication['error']}")
                            continue
                        source_data = publication.get("housing_source_data") or {}
                        if not _is_residential(source_data):
                            print("  Пропуск: объект не относится к жилью")
                            continue
                        publication["seller_source_data"] = detail_page.evaluate(
                            AGENCY_EVALUATION
                        )
                        result.append(_canonical_listing(publication))
                        if limit is not None and len(result) >= limit:
                            return result
                    if len(urls) < catalog_page_size:
                        break
                    if self.max_pages is not None and page_number >= self.max_pages:
                        break
                    page_number += 1
                return result
            finally:
                browser.close()


__all__ = ["RealtingSource"]
