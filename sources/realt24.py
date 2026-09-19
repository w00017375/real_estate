"""Адаптер каталога аренды Realt24.uz для общего конвейера."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from core.existing_listings import ExistingListings
from core.normalization import clean_text, normalize_city, normalize_district, parse_float, parse_int
from standalone_parsers.realt24_publications_raw_json import (
    DEFAULT_CATALOG_URL,
    _download_html,
    _next_data,
    _raw_properties,
)
from sources.base import ListingSource


SECONDARY_NAME_PATTERN = re.compile(
    r"(?P<rooms>\d+)\s*-\s*комнатн(?:ая|ой|ую)?\s+квартира\s*"
    r"[−–—-]\s*(?P<area>\d+(?:[.,]\d+)?)\s*м²\s*,\s*"
    r"(?P<floor>\d+)\s*/\s*(?P<floors_total>\d+)\s*этаж",
    flags=re.IGNORECASE,
)


def _secondary_name_values(value: Any) -> dict[str, int | float | None]:
    match = SECONDARY_NAME_PATTERN.search(str(value or ""))
    if not match:
        return {
            "rooms": None,
            "total_area_m2": None,
            "floor": None,
            "floors_total": None,
        }
    return {
        "rooms": parse_int(match.group("rooms")),
        "total_area_m2": parse_float(match.group("area")),
        "floor": parse_int(match.group("floor")),
        "floors_total": parse_int(match.group("floors_total")),
    }


def _address_parts(address: dict[str, Any]) -> tuple[str | None, str | None]:
    full_address = clean_text(address.get("fullAddress"))
    if not full_address:
        return None, None
    parts = [part.strip() for part in full_address.split(",")]
    city = normalize_city(parts[0]) if parts else None
    # A district is present only when the source provides a second comma;
    # for ``city, street`` records the street must not be misclassified as a
    # district.
    district = normalize_district(parts[1]) if len(parts) > 2 else None
    return city, district


def _metro_nearby(address: dict[str, Any]) -> list[str]:
    """Treat any non-empty address.metro list as the metro quick-filter flag."""

    metro = address.get("metro")
    if metro is None:
        # Current Realt24 payloads use ``metros``; retain compatibility with
        # the singular key requested by the source contract.
        metro = address.get("metros")
    return ["Метро"] if metro else []


def _first_image(raw: dict[str, Any]) -> list[dict[str, Any]]:
    for image in raw.get("imageSets") or []:
        if not isinstance(image, dict):
            continue
        url = next(
            (
                image.get(key)
                for key in ("original", "w900", "w600", "w450", "w300")
                if isinstance(image.get(key), str)
                and image.get(key).startswith(("http://", "https://"))
            ),
            None,
        )
        if url:
            return [
                {
                    "url": url,
                    "source_image_id": image.get("guid"),
                    "sort_order": 0,
                    "is_primary": True,
                }
            ]
    return []


def _canonical_listing(raw: dict[str, Any]) -> dict[str, Any]:
    address = raw.get("address") or {}
    if not isinstance(address, dict):
        address = {}
    city, district = _address_parts(address)
    values = _secondary_name_values(
        raw.get("secondaryName") or raw.get("name")
    )
    price_data = raw.get("price") or {}
    usd_price = price_data.get("usd") if isinstance(price_data, dict) else None
    user = raw.get("propertyUser") or {}
    if not isinstance(user, dict):
        user = {}
    first_name = clean_text(user.get("firstName"))
    last_name = clean_text(user.get("lastName"))
    seller_name = " ".join(part for part in (first_name, last_name) if part) or None
    phone = raw.get("phone")
    house_number = address.get("house")
    street = address.get("area")
    geo = address.get("geoLocation") or {}
    if not isinstance(geo, dict):
        geo = {}

    return {
        "source": "realt24",
        "id": str(raw.get("id")) if raw.get("id") is not None else None,
        "title": raw.get("secondaryName") or raw.get("name"),
        "price": usd_price,
        "currency": "USD" if usd_price is not None else None,
        "transaction_type": "rent",
        "url": f"https://realt24.uz/ru/listing/{raw.get('id')}/"
        if raw.get("id") is not None
        else None,
        "published_at": raw.get("publishedAt") or raw.get("createdAt"),
        "description": raw.get("description"),
        "description_length": len(str(raw.get("description") or "")),
        "housing": {
            "city": city,
            "district": district,
            "street": street,
            "house_number": house_number,
            "rooms": values["rooms"],
            "total_area_m2": values["total_area_m2"],
            "floor": values["floor"],
            "floors_total": values["floors_total"],
            "building_type": None,
            "is_new_building": None,
            "foundation_type": None,
            "residential_complex_name": None,
            "furnished": None,
            "monthly_rent": usd_price,
            "rent_currency": "USD" if usd_price is not None else None,
            "latitude": geo.get("latitude"),
            "longitude": geo.get("longitude"),
        },
        "amenities": [],
        "nearby": _metro_nearby(address),
        "images": _first_image(raw),
        "seller": {
            "name": seller_name,
            "phone": phone,
            "phone_source": "realt24" if phone else None,
            "profile_url": None,
            "seller_role": None,
        },
    }


class Realt24Source(ListingSource):
    """Каталог долгосрочной аренды Realt24.uz."""

    def __init__(
        self,
        catalog_url: str = DEFAULT_CATALOG_URL,
        *,
        timeout: float = 45.0,
        max_pages: int | None = None,
        stop_at_first_existing: bool = False,
    ) -> None:
        self.catalog_url = catalog_url
        self.timeout = timeout
        self.max_pages = max_pages
        self.stop_at_first_existing = stop_at_first_existing

    @property
    def name(self) -> str:
        return "realt24"

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
        result: list[dict[str, Any]] = []
        seen_keys: set[str] = set()
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
                raw_properties = _raw_properties(
                    _next_data(_download_html(page_url, self.timeout))
                )
            except RuntimeError as error:
                if page_number > 1 and "HTTP: 404" in str(error):
                    break
                raise
            if not raw_properties:
                break
            if catalog_page_size is None:
                catalog_page_size = len(raw_properties)
            page_has_unseen = False
            for item in raw_properties:
                platform_id = item.get("id")
                url = (
                    f"https://realt24.uz/ru/listing/{platform_id}/"
                    if platform_id is not None
                    else None
                )
                identity = str(
                    platform_id
                    or item.get("slug")
                    or item.get("guid")
                    or item.get("secondaryName")
                    or item.get("name")
                )
                if identity in seen_keys:
                    continue
                seen_keys.add(identity)
                page_has_unseen = True
                if existing.contains(platform_id, url):
                    print(
                        f"Realt24 уже есть в БД, пропуск: {url or platform_id}"
                    )
                    if self.stop_at_first_existing:
                        return result
                    continue
                result.append(_canonical_listing(item))
                if limit is not None and len(result) >= limit:
                    return result
            if not page_has_unseen:
                break
            print(f"Realt24: страница каталога {page_number} обработана")
            if len(raw_properties) < catalog_page_size:
                break
            if self.max_pages is not None and page_number >= self.max_pages:
                break
            page_number += 1
        return result


__all__ = ["Realt24Source"]
