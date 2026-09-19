"""Download one Etagi publication and save all listing data as JSON.

Parsing and JSON export work as a standalone script. When SQLite output is
enabled, saving is delegated to the same universal storage layer as main.py.

Examples:
    python -m standalone_parsers.etagi_publication_json \
        "https://tashkent.etagi.com/realty_rent/" --limit 4

    python -m standalone_parsers.etagi_publication_json \
        "https://tashkent.etagi.com/realty_rent/13885212/"

    python -m standalone_parsers.etagi_publication_json URL --output output_json/etagi/etagi_listing.json

The top level uses the same canonical shape as the main project and can be
written to its SQLite tables with the default ``--db`` option.  The complete
unchanged Etagi payload remains under ``source_data``; canonical fields map
to the database as follows:

    housing.street (zone + street + house number)
    housing.rooms / total_area_m2 / floor / floors_total
    housing.monthly_rent / rent_currency / price_per_m2 / latitude / longitude
    seller.name / seller.phone
    nearby -> nearby_places + listing_nearby

Fields that have no column in the current schema are listed in
``database_mapping_gaps`` instead of being silently discarded.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse
from urllib.request import Request, urlopen

from core.existing_listings import ExistingListings


DEFAULT_URL = "https://tashkent.etagi.com/realty_rent/"
DEFAULT_LIMIT = 7

DATABASE_MAPPING_GAPS = [
    "flat._object_id, object_id, *_id, type/class/status и служебные флаги: идентификаторы и статусы источника без соответствующих колонок",
    "flat.additional_meta: технические ID/флаги бытовой техники, парковки и инфраструктуры без справочника расшифровок",
    "flat.square_kitchen: площадь кухни отдельно не предусмотрена таблицей housing",
    "flat.deposit и flat.rental_period: залог и период аренды не предусмотрены таблицей housing",
    "flat.beds_count, animals_allowed, children_allowed, for_students, euro, studio и short_period",
    "flat.price_m2, komunal_cost, komunal_counts, wall_id и price/коммунальные дополнительные показатели (в БД считается price_per_m2 по формуле)",
    "отдельные адресные идентификаторы не сохраняются; zone, street и house number объединяются в housing.street",
    "flat.repair/class и ID жилого комплекса не сохраняются отдельными колонками",
    "flat.metro_stations: расстояния и идентификаторы станций; названия станций сохраняются в nearby_places",
    "flat.notes и flat.plate_notes: полный текст/структурированное описание; в listings хранится только description_length",
    "flat.visual и flat.has_videos: видео/визуальные служебные данные не имеют отдельной колонки; фотографии сохраняются в listing_images",
    "realtor.photo_url, echat_id, user_status, work_status, isInpreUser, isShownByInpre и ticketObjectRights",
]

# Etagi stores the human-readable labels in ``objectMnemonics`` and the
# actual feature flags/values in ``flat.additional_meta``.  Only attributes
# that have a matching canonical amenity are copied to listing_amenities.
ETAGI_AMENITY_BY_KEY = {
    "balcon": "Балкон",
    "balcon_loggia": "Балкон",
    "balcon_erker": "Балкон",
    "yes_balcon": "Балкон",
    "loggia": "Балкон",
    "erker": "Балкон",
    "more_balcon": "Балкон",
    "more_loggia": "Балкон",
    "yard_cameras": "Видеонаблюдение",
    "yard_security": "Охрана",
    "ohrana": "Охрана",
    "safe_signal": "Охрана",
    "parking": "Парковочное место",
    "subway_parking": "Парковочное место",
    "semisubway_parking": "Парковочное место",
    "garage_parking": "Парковочное место",
    "guest_parking": "Парковочное место",
    "ground_parking": "Парковочное место",
    "conditioner": "Кондиционер",
    "conditioners_count": "Кондиционер",
    "air_filters": "Кондиционер",
    "cable_tv": "Кабельное ТВ",
    "internet": "Интернет",
    "phone": "Телефон",
    "microwave": "Микроволновая печь",
    "refrigerator": "Холодильник",
    "washing_machine": "Стиральная машина",
    "tv": "Телевизор",
    "kitchen": "Кухня",
    "plate": "Кухня",
    "gas": "Газоснабжение",
    "electric": "Кухня",
    "convective": "Кухня",
    "mebel": "Мебель",
    "bed": "Мебель",
    "sofa": "Мебель",
    "wardrobe": "Мебель",
    "chairs": "Мебель",
    "kitchen_table": "Мебель",
    "children_playground": "Детская площадка",
    "elevator": "Лифт",
    "water_counters": "Водоснабжение",
    "water_filters": "Водоснабжение",
    "heat_counters": "Водоснабжение",
}


def _etagi_value_present(value: Any) -> bool:
    if value is None or value is False or value == 0:
        return False
    if isinstance(value, str):
        return value.strip().casefold() not in {
            "", "нет", "no", "false", "0", "none", "null",
        }
    return bool(value)


def _etagi_amenities(objects: dict[str, Any]) -> list[str]:
    """Map present Etagi feature flags to the project's amenity vocabulary."""

    flat = objects.get("flat") or {}
    metadata = flat.get("additional_meta") or {}
    mnemonics = objects.get("objectMnemonics") or {}
    result: list[str] = []
    for key, value in metadata.items():
        if not _etagi_value_present(value):
            continue
        label = str(mnemonics.get(key) or key).casefold()
        amenity = ETAGI_AMENITY_BY_KEY.get(str(key).casefold())
        if amenity is None:
            # Fallback by meaning for renamed/new Etagi keys.
            combined = f"{key} {label}"
            for marker, candidate in (
                ("балкон", "Балкон"),
                ("парков", "Парковочное место"),
                ("кондиционер", "Кондиционер"),
                ("интернет", "Интернет"),
                ("холодиль", "Холодильник"),
                ("стираль", "Стиральная машина"),
                ("телевиз", "Телевизор"),
                ("мебел", "Мебель"),
                ("детск.*площад", "Детская площадка"),
                ("лифт", "Лифт"),
            ):
                if re.search(marker, combined, flags=re.IGNORECASE):
                    amenity = candidate
                    break
        if amenity and amenity not in result:
            result.append(amenity)
    return result


def _etagi_images(objects: dict[str, Any]) -> list[dict[str, Any]]:
    """Return image URLs from Etagi's grouped media payload."""

    flat = objects.get("flat") or {}
    grouped = objects.get("groupedObjectMedia") or {}
    by_type = grouped.get("byType") if isinstance(grouped, dict) else {}
    photos = by_type.get("photos") if isinstance(by_type, dict) else []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, photo in enumerate(photos or []):
        if not isinstance(photo, dict):
            continue
        url = photo.get("fname") or photo.get("url")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            continue
        if url in seen:
            continue
        seen.add(url)
        size = photo.get("size") or {}
        result.append(
            {
                "url": url,
                "source_image_id": photo.get("id"),
                "sort_order": index,
                "is_primary": index == 0,
                "width": size.get("width") if isinstance(size, dict) else None,
                "height": size.get("height") if isinstance(size, dict) else None,
            }
        )
    if not result:
        primary = flat.get("main_photo")
        if isinstance(primary, str) and primary.startswith(("http://", "https://")):
            result.append({"url": primary, "sort_order": 0, "is_primary": True})
    return result


class _ScriptCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_script = False
        self._parts: list[str] = []
        self.scripts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        del attrs
        if tag.lower() == "script":
            self._inside_script = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_script:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_script:
            self.scripts.append("".join(self._parts))
            self._inside_script = False
            self._parts = []


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Сохранить полные данные одной публикации Etagi в JSON"
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_URL,
        help=(
            "URL публикации или раздела Etagi; для раздела будут открыты "
            f"первые {DEFAULT_LIMIT} карточки (по умолчанию: {DEFAULT_URL})"
        ),
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output_json/etagi/etagi_publication.json"),
        help="Выходной JSON-файл в output_json/etagi",
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("olx_apartments.db"),
        help="SQLite-база агрегатора (по умолчанию: olx_apartments.db)",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Не добавлять результат в SQLite, только сохранить JSON",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=30,
        help="Сетевой таймаут в секундах (по умолчанию: 30)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Лимит карточек раздела (по умолчанию: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--include-page-state",
        action="store_true",
        help="Добавить весь встроенный объект data, включая настройки сайта",
    )
    return parser.parse_args()


def _validate_url(url: str) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL должен начинаться с http:// или https://")
    if hostname != "etagi.com" and not hostname.endswith(".etagi.com"):
        raise ValueError("Допускаются только ссылки домена etagi.com")


def _download_html(url: str, timeout: float) -> tuple[str, str, int]:
    request = Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            charset = response.headers.get_content_charset() or "utf-8"
            html = response.read().decode(charset, errors="replace")
            return html, response.geturl(), response.status
    except HTTPError as error:
        raise RuntimeError(f"Etagi вернул HTTP {error.code}") from error
    except URLError as error:
        raise RuntimeError(f"Не удалось открыть Etagi: {error.reason}") from error


def _extract_page_state(html: str) -> dict[str, Any]:
    collector = _ScriptCollector()
    collector.feed(html)

    decoder = json.JSONDecoder()
    for script in collector.scripts:
        stripped = script.lstrip()
        if not stripped.startswith("var data="):
            continue
        payload = stripped[len("var data=") :].lstrip()
        try:
            value, _ = decoder.raw_decode(payload)
        except json.JSONDecodeError as error:
            raise RuntimeError(
                "Найден var data, но его содержимое не является корректным JSON"
            ) from error
        if isinstance(value, dict):
            return value

    raise RuntimeError(
        "На странице не найден встроенный JSON `var data`. "
        "Возможно, Etagi изменил структуру страницы."
    )


def _document_title(html: str) -> str | None:
    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    return match.group(1).strip() if match else None


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    digits = re.findall(r"\d+", str(value))
    return int("".join(digits)) if digits else None


def _as_float(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(" ", "").replace(",", "."))
    except (TypeError, ValueError):
        return None


def _url_with_page(url: str, page_number: int) -> str:
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["page"] = str(page_number)
    return parsed._replace(query=urlencode(query)).geturl()


def _is_collection_url(url: str) -> bool:
    path = urlparse(url).path.rstrip("/").lower()
    return path.endswith("/realty_rent")


def _normalize_phone(value: Any) -> str | None:
    if not value:
        return None
    digits = re.sub(r"\D", "", str(value))
    if len(digits) == 9:
        digits = "998" + digits
    if len(digits) < 10:
        return None
    return "+" + digits


def _currency_from_html(html: str) -> str | None:
    if re.search(r"у\.?\s*е\.?|\$|\bUSD\b", html, re.I):
        return "USD"
    if re.search(r"сум|UZS", html, re.I):
        return "UZS"
    return None


def _etagi_floor_total(flat: dict[str, Any]) -> int | None:
    """Prefer the JSON field, then an explicit ``floor of total`` phrase."""

    structured = _as_int(
        flat.get("floors")
        or flat.get("floors_total")
        or flat.get("floor_count")
    )
    if structured is not None:
        return structured
    text = "\n".join(
        str(value or "")
        for value in (flat.get("notes"), flat.get("plate_notes"))
    )
    match = re.search(
        r"этаж\s*:?[ \t]*(?:\d+)[ \t]*(?:из|/)[ \t]*(\d+)",
        text,
        flags=re.IGNORECASE,
    )
    return int(match.group(1)) if match else None


def _etagi_building_kind(
    flat: dict[str, Any],
) -> tuple[str | None, bool | None]:
    """Classify only when Etagi explicitly says new or secondary stock."""

    text = "\n".join(
        str(value or "")
        for value in (flat.get("notes"), flat.get("plate_notes"))
    ).casefold()
    if "вторичный фонд" in text or "вторичное жиль" in text:
        return "secondary", False
    if "новострой" in text or "первичный рынок" in text:
        return "new_building", True
    return None, None


def _etagi_foundation_type(flat: dict[str, Any]) -> str | None:
    """Return wall material, not Etagi's numeric wall_id."""

    for key in ("wall_name", "wall_type", "wall_material", "material"):
        value = flat.get(key)
        if value and not str(value).isdigit():
            return str(value)
    text = "\n".join(
        str(value or "")
        for value in (flat.get("notes"), flat.get("plate_notes"))
    ).casefold()
    for marker, canonical in (
        ("кирпич", "kirpich"),
        ("панель", "panel"),
        ("монолит", "monolit"),
        ("блочн", "block"),
    ):
        if marker in text:
            return canonical
    return None


def _canonical_listing(
    page_state: dict[str, Any],
    requested_url: str,
    final_url: str,
    status: int,
    html: str,
) -> dict[str, Any]:
    objects = page_state["objects"]
    flat = objects["flat"]
    realtor = objects.get("realtor") or {}
    page_data = objects.get("pageData") or {}
    meta = flat.get("meta") or {}
    price = _as_int(flat.get("price"))
    currency = _currency_from_html(html)
    area = _as_float(flat.get("square"))
    is_rent = page_data.get("realtyType") == "rent" or flat.get("action_sl") == "lease"
    monthly_rent = price if is_rent else None
    price_per_m2 = (
        round(monthly_rent / area, 2)
        if monthly_rent is not None and area and area > 0
        else None
    )
    building_type, is_new_building = _etagi_building_kind(flat)
    housing = {
        "city": meta.get("city"),
        # Район хранится отдельно от полного адреса.
        "district": meta.get("district") or meta.get("city_district"),
        "address": flat.get("address") or meta.get("address"),
        "street": meta.get("street") or flat.get("street"),
        "house_number": flat.get("house_num") or flat.get("house_address_number"),
        "zone": meta.get("zone") or flat.get("districttr"),
        "address_id": flat.get("address_id"),
        "street_id": flat.get("street_id"),
        "house_id": flat.get("house_id"),
        "zone_id": flat.get("zone_id"),
        "building_type": building_type,
        "is_new_building": is_new_building,
        "repair": flat.get("repair") or flat.get("class"),
        "foundation_type": _etagi_foundation_type(flat),
        "residential_complex_name": flat.get("newhouses_name"),
        "residential_complex_id": flat.get("newhouses_id"),
        "rooms": flat.get("rooms"),
        # Приоритет у структурированного значения Etagi: square = 40.
        "total_area_m2": area,
        "floor": flat.get("floor"),
        "floors_total": _etagi_floor_total(flat),
        "furnished": (
            not flat.get("without_furniture")
            if flat.get("without_furniture") is not None
            else None
        ),
        "commission": None,
        "monthly_rent": monthly_rent,
        "rent_currency": currency if is_rent else None,
        "price_per_m2": price_per_m2,
        "latitude": _as_float(flat.get("la")),
        "longitude": _as_float(flat.get("lo")),
    }
    metro_names = [
        station.get("name")
        for station in flat.get("metro_stations") or []
        if isinstance(station, dict) and station.get("name")
    ]
    realtor_id = realtor.get("id") or flat.get("user_id")
    profile_url = None
    if realtor_id is not None:
        parsed = urlparse(final_url)
        profile_url = f"{parsed.scheme}://{parsed.netloc}/realtors/{realtor_id}/"
    seller = {
        "name": realtor.get("fio"),
        "phone": realtor.get("phone"),
        "profile_url": profile_url,
        "phone_source": "etagi_realtor_card" if realtor.get("phone") else None,
        "is_official_seller": False,
        "official_complex_name": None,
    }
    return {
        "source": "etagi",
        "id": str(flat.get("_ticket_id") or flat.get("object_id")),
        "title": _document_title(html),
        "price": price,
        "currency": currency,
        "transaction_type": page_data.get("realtyType"),
        "url": final_url,
        "published_at": flat.get("date_update"),
        "description": flat.get("notes") or flat.get("plate_notes"),
        "description_length": len(flat.get("notes") or ""),
        "housing": housing,
        "amenities": _etagi_amenities(objects),
        "nearby": metro_names,
        "images": _etagi_images(objects),
        "seller": seller,
        # Keep the complete object graph untouched.  The canonical fields
        # above are only a DB-compatible projection of this payload.
        "source_data": objects,
        "source_url": requested_url,
        "http_status": status,
        "database_mapping_gaps": DATABASE_MAPPING_GAPS,
    }


DB_SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS scrape_runs (run_pk INTEGER PRIMARY KEY AUTOINCREMENT, started_at DATETIME NOT NULL, source_url TEXT NOT NULL, listing_limit INTEGER NOT NULL, source_name TEXT NOT NULL DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS sellers (seller_pk INTEGER PRIMARY KEY AUTOINCREMENT, identity_key TEXT NOT NULL UNIQUE, profile_url TEXT, name TEXT, phone TEXT, phone_source TEXT, is_official_seller INTEGER NOT NULL DEFAULT 0, official_complex_name TEXT, seller_type TEXT, sellers_type TEXT, realtor_probability REAL, assessment_comment TEXT, active_rent_listings INTEGER, active_sale_listings INTEGER, unique_listing_count INTEGER, distinct_housing_listings INTEGER, profile_scan_complete INTEGER NOT NULL DEFAULT 0, updated_at DATETIME NOT NULL);
CREATE TABLE IF NOT EXISTS listings (listing_pk INTEGER PRIMARY KEY AUTOINCREMENT, platform_listing_id TEXT, source_name TEXT NOT NULL DEFAULT 'unknown', seller_pk INTEGER NOT NULL, title TEXT, price INTEGER, currency TEXT, url TEXT NOT NULL UNIQUE, published_at DATETIME, description_length INTEGER NOT NULL DEFAULT 0, first_seen_at DATETIME NOT NULL, publication_status TEXT NOT NULL DEFAULT 'active', transaction_type TEXT, city TEXT, district TEXT, rooms INTEGER, total_area_m2 REAL, floor INTEGER, floors_total INTEGER, FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk), UNIQUE (source_name, platform_listing_id));
CREATE TABLE IF NOT EXISTS housing (housing_pk INTEGER PRIMARY KEY AUTOINCREMENT, listing_pk INTEGER NOT NULL UNIQUE, city TEXT, district TEXT, street TEXT, building_type TEXT, is_new_building INTEGER, foundation_type TEXT, residential_complex_name TEXT, rooms INTEGER, total_area_m2 REAL, floor INTEGER, floors_total INTEGER, furnished INTEGER, monthly_rent REAL, rent_currency TEXT, price_per_m2 REAL, latitude REAL, longitude REAL, FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS amenities (amenity_pk INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS listing_amenities (listing_amenity_pk INTEGER PRIMARY KEY AUTOINCREMENT, listing_pk INTEGER NOT NULL, amenity_pk INTEGER NOT NULL, UNIQUE (listing_pk, amenity_pk), FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE, FOREIGN KEY (amenity_pk) REFERENCES amenities(amenity_pk));
CREATE TABLE IF NOT EXISTS nearby_places (nearby_pk INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE);
CREATE TABLE IF NOT EXISTS listing_nearby (listing_nearby_pk INTEGER PRIMARY KEY AUTOINCREMENT, listing_pk INTEGER NOT NULL, nearby_pk INTEGER NOT NULL, UNIQUE (listing_pk, nearby_pk), FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE, FOREIGN KEY (nearby_pk) REFERENCES nearby_places(nearby_pk));
CREATE TABLE IF NOT EXISTS scrape_run_listings (run_pk INTEGER NOT NULL, listing_pk INTEGER NOT NULL, PRIMARY KEY (run_pk, listing_pk), FOREIGN KEY (run_pk) REFERENCES scrape_runs(run_pk), FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk));
CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_pk);
"""


def _ensure_housing_columns(connection: sqlite3.Connection) -> None:
    columns = {
        row[1] for row in connection.execute("PRAGMA table_info(housing)")
    }
    for name, definition in (
        ("address", "TEXT"), ("street", "TEXT"), ("house_number", "TEXT"),
        ("zone", "TEXT"), ("address_id", "TEXT"), ("street_id", "TEXT"),
        ("house_id", "TEXT"), ("zone_id", "TEXT"), ("building_type", "TEXT"),
        ("is_new_building", "INTEGER"), ("repair", "TEXT"),
        ("foundation_type", "TEXT"), ("residential_complex_name", "TEXT"),
        ("residential_complex_id", "TEXT"),
        ("monthly_rent", "REAL"),
        ("rent_currency", "TEXT"),
        ("price_per_m2", "REAL"),
        ("latitude", "REAL"),
        ("longitude", "REAL"),
    ):
        if name not in columns:
            connection.execute(f"ALTER TABLE housing ADD COLUMN {name} {definition}")


def _ensure_database_migration(connection: sqlite3.Connection) -> None:
    def ensure(table: str, column: str, definition: str) -> None:
        names = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
        if column not in names:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    for column, definition in (("phone", "TEXT"), ("phone_source", "TEXT"),
                               ("sellers_type", "TEXT")):
        ensure("sellers", column, definition)
    listing_columns = {row[1] for row in connection.execute("PRAGMA table_info(listings)")}
    if "phone_pk" in listing_columns:
        connection.execute("DROP INDEX IF EXISTS idx_listings_phone")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("ALTER TABLE listings DROP COLUMN phone_pk")
        except sqlite3.OperationalError:
            connection.execute("DROP TABLE IF EXISTS listings_new")
            connection.execute("""CREATE TABLE listings_new (
                listing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                listing_identity TEXT NOT NULL UNIQUE,
                platform_listing_id TEXT,
                source_name TEXT NOT NULL DEFAULT 'unknown',
                seller_pk INTEGER NOT NULL, title TEXT, price INTEGER,
                currency TEXT, url TEXT NOT NULL UNIQUE, published_at TEXT,
                description_length INTEGER NOT NULL DEFAULT 0,
                first_seen_at TEXT NOT NULL,
                FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk))""")
            connection.execute("""INSERT INTO listings_new
                (listing_pk, listing_identity, platform_listing_id, source_name,
                 seller_pk, title, price, currency, url, published_at,
                 description_length, first_seen_at)
                SELECT listing_pk, listing_identity, platform_listing_id, source_name,
                 seller_pk, title, price, currency, url, published_at,
                 description_length, first_seen_at FROM listings""")
            connection.execute("DROP TABLE listings")
            connection.execute("ALTER TABLE listings_new RENAME TO listings")
            connection.execute("CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_pk)")
        connection.commit()
    connection.execute("DROP TABLE IF EXISTS seller_phones")
    connection.execute("DROP TABLE IF EXISTS phones")
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


def save_to_database(listing: dict[str, Any], database_path: Path) -> dict[str, Any]:
    # Сохранение всех источников выполняет единый слой. Это гарантирует,
    # что отдельный скрипт Etagi использует ту же актуальную схему, что main.py.
    from core.pipeline import process_listing
    from storage.sqlite_store import save_to_database as save_listings

    canonical = process_listing(listing)
    result = save_listings(
        [canonical],
        str(database_path),
        source_name="etagi",
        source_url=listing.get("url") or "",
        listing_limit=1,
    )
    listing["seller"] = canonical["seller"]
    return {
        "saved": True,
        "database": str(database_path),
        "run_pk": result["run_pk"],
    }

    # Legacy implementation is intentionally unreachable and retained below
    # only until old standalone databases are no longer supported.
    import datetime

    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    seller = listing["seller"]
    seller_id = seller.get("profile_url") or f"listing:{listing.get('id') or 'unknown'}"
    seller_identity = f"etagi:{seller_id}"
    connection = sqlite3.connect(database_path)
    try:
        connection.executescript(DB_SCHEMA)
        _ensure_housing_columns(connection)
        _ensure_database_migration(connection)
        cursor = connection.cursor()
        cursor.execute(
            """INSERT INTO sellers (identity_key, profile_url, name, phone,
               phone_source, seller_type, sellers_type, realtor_probability,
               assessment_comment, active_rent_listings, active_sale_listings,
               unique_listing_count, distinct_housing_listings,
               profile_scan_complete, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
               ON CONFLICT(identity_key) DO UPDATE SET
                 profile_url = excluded.profile_url, name = excluded.name,
                 phone = COALESCE(excluded.phone, sellers.phone),
                 phone_source = COALESCE(excluded.phone_source, sellers.phone_source),
                 seller_type = excluded.seller_type,
                 sellers_type = excluded.sellers_type,
                 realtor_probability = excluded.realtor_probability,
                 active_rent_listings = excluded.active_rent_listings,
                 active_sale_listings = excluded.active_sale_listings,
                 unique_listing_count = excluded.unique_listing_count,
                 distinct_housing_listings = excluded.distinct_housing_listings,
                 updated_at = excluded.updated_at""",
            (seller_identity, seller.get("profile_url"), seller.get("name"),
             seller.get("phone"), seller.get("phone_source") or "etagi_realtor_card",
             "likely_realtor",
             "realtor",
             1.0, seller.get("comment"),
             seller.get("active_rent_listings") or (1 if listing.get("transaction_type") == "rent" else 0),
             seller.get("active_sale_listings") or (1 if listing.get("transaction_type") == "sale" else 0),
             seller.get("unique_listing_count") or 1,
             seller.get("distinct_housing_listings") or 1, now),
        )
        seller_pk = cursor.execute(
            "SELECT seller_pk FROM sellers WHERE identity_key = ?",
            (seller_identity,),
        ).fetchone()[0]
        listing_identity = f"etagi:{listing['id']}"
        cursor.execute(
            """INSERT INTO listings (listing_identity, platform_listing_id,
               source_name, seller_pk, title, price, currency, url,
               published_at, description_length, first_seen_at)
               VALUES (?, ?, 'etagi', ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(listing_identity) DO UPDATE SET
                 seller_pk = excluded.seller_pk,
                 title = excluded.title, price = excluded.price,
                 currency = excluded.currency, url = excluded.url,
                 published_at = excluded.published_at,
                 description_length = excluded.description_length,
                 first_seen_at = excluded.first_seen_at""",
            (listing_identity, listing["id"], seller_pk,
             listing.get("title"), listing.get("price"), listing.get("currency"),
             listing.get("url"), listing.get("published_at"),
             listing.get("description_length", 0), now),
        )
        listing_pk = cursor.execute(
            "SELECT listing_pk FROM listings WHERE listing_identity = ?",
            (listing_identity,),
        ).fetchone()[0]
        housing = listing.get("housing") or {}
        housing_placeholders = ", ".join("?" for _ in range(28))
        cursor.execute(
            f"""INSERT INTO housing (listing_pk, city, district, address, street,
               house_number, zone, address_id, street_id, house_id, zone_id,
               building_type, is_new_building, repair, foundation_type,
               residential_complex_name, residential_complex_id, rooms,
               total_area_m2, floor, floors_total, furnished, commission,
               monthly_rent, rent_currency, price_per_m2, latitude, longitude)
               VALUES ({housing_placeholders})
               ON CONFLICT(listing_pk) DO UPDATE SET city = excluded.city,
                 district = excluded.district, address = excluded.address,
                 street = excluded.street, house_number = excluded.house_number,
                 zone = excluded.zone, address_id = excluded.address_id,
                 street_id = excluded.street_id, house_id = excluded.house_id,
                 zone_id = excluded.zone_id, building_type = excluded.building_type,
                 is_new_building = excluded.is_new_building, repair = excluded.repair,
                 foundation_type = excluded.foundation_type,
                 residential_complex_name = excluded.residential_complex_name,
                 residential_complex_id = excluded.residential_complex_id,
                 rooms = excluded.rooms,
                 total_area_m2 = excluded.total_area_m2,
                 floor = excluded.floor, floors_total = excluded.floors_total,
                 furnished = excluded.furnished,
                 commission = excluded.commission,
                 monthly_rent = excluded.monthly_rent,
                 rent_currency = excluded.rent_currency,
                 price_per_m2 = excluded.price_per_m2,
                 latitude = excluded.latitude,
                 longitude = excluded.longitude""",
            (listing_pk, housing.get("city"), housing.get("district"),
             housing.get("address"), housing.get("street"), housing.get("house_number"),
             housing.get("zone"), housing.get("address_id"), housing.get("street_id"),
             housing.get("house_id"), housing.get("zone_id"), housing.get("building_type"),
             None if housing.get("is_new_building") is None else int(housing["is_new_building"]),
             housing.get("repair"), housing.get("foundation_type"),
             housing.get("residential_complex_name"), housing.get("residential_complex_id"),
             housing.get("rooms"), housing.get("total_area_m2"),
             housing.get("floor"), housing.get("floors_total"),
            None if housing.get("furnished") is None else int(housing["furnished"]),
            None,
            housing.get("monthly_rent"), housing.get("rent_currency"),
            housing.get("price_per_m2"), housing.get("latitude"),
            housing.get("longitude")),
        )
        for junction_table in ("listing_amenities", "listing_nearby"):
            cursor.execute(
                f"DELETE FROM {junction_table} WHERE listing_pk = ?",
                (listing_pk,),
            )
        for value, dimension_table, dimension_pk, junction_table in (
            (listing.get("amenities"), "amenities", "amenity_pk", "listing_amenities"),
            (listing.get("nearby"), "nearby_places", "nearby_pk", "listing_nearby"),
        ):
            for item in value or []:
                cursor.execute(
                    f"INSERT OR IGNORE INTO {dimension_table}(name) VALUES (?)",
                    (item,),
                )
                item_pk = cursor.execute(
                    f"SELECT {dimension_pk} FROM {dimension_table} WHERE name = ?",
                    (item,),
                ).fetchone()[0]
                cursor.execute(
                    f"INSERT OR IGNORE INTO {junction_table}(listing_pk, {dimension_pk}) VALUES (?, ?)",
                    (listing_pk, item_pk),
                )
        counts = cursor.execute(
            """SELECT COUNT(*),
                      SUM(transaction_type = 'rent'),
                      SUM(transaction_type = 'sale')
               FROM listings WHERE seller_pk = ?""",
            (seller_pk,),
        ).fetchone()
        cursor.execute(
            """UPDATE sellers SET seller_type = 'likely_realtor',
               sellers_type = 'realtor', realtor_probability = 1.0,
               active_rent_listings = COALESCE(?, 0),
               active_sale_listings = COALESCE(?, 0),
               unique_listing_count = COALESCE(?, 0),
               distinct_housing_listings = COALESCE(?, 0)
               WHERE seller_pk = ?""",
            (counts[1], counts[2], counts[0], counts[0], seller_pk),
        )
        cursor.execute(
            """INSERT INTO scrape_runs(started_at, source_url, listing_limit,
               source_name) VALUES (?, ?, 1, 'etagi')""",
            (now, listing.get("url") or ""),
        )
        run_pk = cursor.lastrowid
        cursor.execute(
            """INSERT OR IGNORE INTO scrape_run_listings(run_pk, listing_pk)
               VALUES (?, ?)""",
            (run_pk, listing_pk),
        )
        connection.commit()
        return {"saved": True, "database": str(database_path),
                "run_pk": run_pk, "listing_pk": listing_pk}
    finally:
        connection.close()


def parse_publication(url: str, timeout: float = 30) -> dict[str, Any]:
    """Return one canonical listing plus untouched Etagi source fields."""

    _validate_url(url)
    html, final_url, status = _download_html(url, timeout)
    page_state = _extract_page_state(html)
    objects = page_state.get("objects")
    if not isinstance(objects, dict):
        raise RuntimeError("Во встроенном JSON отсутствует объект `objects`")

    flat = objects.get("flat")
    if not isinstance(flat, dict):
        raise RuntimeError("Во встроенном JSON отсутствуют данные `objects.flat`")

    return _canonical_listing(page_state, url, final_url, status, html)


def _collection_listing_urls(
    page_state: dict[str, Any],
    page_url: str,
) -> list[str]:
    """Extract detail URLs from Etagi's structured list page."""

    parsed = urlparse(page_url)
    base = f"{parsed.scheme}://{parsed.netloc}/realty_rent/"
    urls: list[str] = []
    seen: set[str] = set()
    rents = ((page_state.get("lists") or {}).get("rents") or [])
    for card in rents:
        if not isinstance(card, dict):
            continue
        ticket_id = card.get("_ticket_id") or card.get("ticket_id")
        if ticket_id is None:
            continue
        detail_url = f"{base}{ticket_id}/"
        if detail_url not in seen:
            seen.add(detail_url)
            urls.append(detail_url)
    return urls


def parse_collection(
    url: str,
    limit: int | None = DEFAULT_LIMIT,
    timeout: float = 30,
    existing: ExistingListings | None = None,
    max_pages: int | None = None,
    stop_at_first_existing: bool = False,
) -> list[dict[str, Any]]:
    """Paginate Etagi; ``None`` opens every card through the last page."""

    _validate_url(url)
    if not _is_collection_url(url):
        raise ValueError("Для пролистывания нужен URL раздела /realty_rent/")
    if limit is not None and limit < 1:
        raise ValueError("Лимит раздела должен быть положительным числом")
    existing = existing or ExistingListings()

    listings: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    known_count = max(len(existing.platform_ids), len(existing.urls))
    page_limit = (
        max(20, min(50, known_count // 20 + 2))
        if limit is not None
        else None
    )
    page_number = 1
    while (
        (page_limit is None or page_number <= page_limit)
        and (max_pages is None or page_number <= max_pages)
    ):
        page_url = _url_with_page(url, page_number)
        try:
            html, _, _ = _download_html(page_url, timeout)
        except RuntimeError as error:
            if page_number > 1 and "HTTP 404" in str(error):
                break
            raise
        state = _extract_page_state(html)
        page_urls = _collection_listing_urls(state, page_url)
        new_urls = [item for item in page_urls if item not in seen_urls]
        if not new_urls:
            break
        for detail_url in new_urls:
            seen_urls.add(detail_url)
            ticket_id = urlparse(detail_url).path.rstrip("/").rsplit("/", 1)[-1]
            if existing.contains(ticket_id, detail_url):
                print(f"Пропуск существующей публикации {detail_url}")
                if stop_at_first_existing:
                    return listings
                continue
            try:
                listings.append(parse_publication(detail_url, timeout))
            except (RuntimeError, ValueError) as error:
                print(f"Пропуск публикации {detail_url}: {error}", file=sys.stderr)
                continue
            if limit is not None and len(listings) >= limit:
                return listings
        page_number += 1
    return listings


def _save_collection_to_database(
    listings: list[dict[str, Any]],
    database_path: Path,
) -> list[dict[str, Any]]:
    return [save_to_database(listing, database_path) for listing in listings]


def _apply_etagi_batch_seller_counts(listings: list[dict[str, Any]]) -> None:
    """Уточняет число открытых карточек Etagi в текущей выборке."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for listing in listings:
        seller = listing.get("seller") or {}
        key = seller.get("profile_url") or f"listing:{listing.get('id')}"
        groups.setdefault(key, []).append(listing)
    for group in groups.values():
        rent = sum(item.get("transaction_type") == "rent" for item in group)
        sale = sum(item.get("transaction_type") == "sale" for item in group)
        for listing in group:
            seller = listing.setdefault("seller", {})
            seller.update({
                "active_rent_listings": rent,
                "active_sale_listings": sale,
                "total_real_estate_listings": len(group),
                "unique_listing_count": len(group),
                "distinct_housing_listings": len(group),
                "seller_type": "likely_realtor",
                "seller_role": "realtor",
                "sellers_type": "realtor",
                "realtor_probability": 1.0,
            })


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.limit < 1:
        raise SystemExit("--limit должен быть положительным числом")
    if _is_collection_url(args.url):
        result = parse_collection(args.url, args.limit, args.timeout)
        if not args.no_db:
            database_results = _save_collection_to_database(result, args.db)
        else:
            database_results = []
        if args.include_page_state:
            html, _, _ = _download_html(_url_with_page(args.url, 1), args.timeout)
            for listing in result:
                listing["page_state"] = _extract_page_state(html)
        output_value: Any = result
        print(f"Собрано публикаций: {len(result)} из лимита {args.limit}")
        if not args.no_db:
            print(f"Записано в SQLite: {len(database_results)}")
    else:
        result = parse_publication(args.url, args.timeout)
        if not args.no_db:
            result["database"] = save_to_database(result, args.db)
        else:
            result["database"] = {"saved": False, "reason": "disabled_by_flag"}
        if args.include_page_state:
            html, _, _ = _download_html(args.url, args.timeout)
            result["page_state"] = _extract_page_state(html)
        output_value = result

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(output_value, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON сохранён: {args.output.resolve()}")


if __name__ == "__main__":
    main()
