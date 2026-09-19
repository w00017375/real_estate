from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import threading
import webbrowser
from collections import Counter
from datetime import datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dashboard.air_quality import (
    AirQualityConfigurationError,
    AirQualityError,
    OpenWeatherAirQualityService,
    load_openweather_api_key,
)


PROJECT_ROOT = Path(__file__).resolve().parent.parent
STATIC_ROOT = Path(__file__).resolve().parent
DEFAULT_DATABASE = PROJECT_ROOT / "olx_apartments.db"

CITY_DISTRICTS = (
    "Бектемирский район",
    "Чиланзарский район",
    "Мирабадский район",
    "Мирзо-Улугбекский район",
    "Алмазарский район",
    "Сергелийский район",
    "Шайхантахурский район",
    "Учтепинский район",
    "Яккасарайский район",
    "Янгихаётский район",
    "Яшнабадский район",
    "Юнусабадский район",
)

REGION_DISTRICTS = (
    "Бекабадский район",
    "Бостанлыкский район",
    "Букинский район",
    "Чиназский район",
    "Кибрайский район",
    "Ахангаранский район",
    "Аккурганский район",
    "Паркентский район",
    "Пскентский район",
    "Куйичирчикский район",
    "Уртачирчикский район",
    "Янгиюльский район",
    "Юкоричирчикский район",
    "Зангиатинский район",
    "Ташкентский район",
    "город Алмалык",
    "город Ангрен",
    "город Бекабад",
    "город Ахангаран",
    "город Нурафшан",
    "город Чирчик",
    "город Янгиюль",
)

REGION_CITY_PARENT_DISTRICTS = {
    "город Алмалык": "Ахангаранский район",
    "город Ангрен": "Ахангаранский район",
    "город Бекабад": "Бекабадский район",
    "город Ахангаран": "Ахангаранский район",
    "город Нурафшан": "Уртачирчикский район",
    "город Чирчик": "Кибрайский район",
    "город Янгиюль": "Янгиюльский район",
}

REGION_MAP_DISTRICTS = tuple(
    name for name in REGION_DISTRICTS if name not in REGION_CITY_PARENT_DISTRICTS
)

CITY_ALIASES = {
    "бектемирский": "Бектемирский район",
    "бектемир": "Бектемирский район",
    "чиланзарский": "Чиланзарский район",
    "чиланзар": "Чиланзарский район",
    "чиланзор": "Чиланзарский район",
    "мирабадский": "Мирабадский район",
    "мирабад": "Мирабадский район",
    "миробод": "Мирабадский район",
    "мирзо улугбекский": "Мирзо-Улугбекский район",
    "мирзо улугбек": "Мирзо-Улугбекский район",
    "алмазарский": "Алмазарский район",
    "алмазар": "Алмазарский район",
    "олмазор": "Алмазарский район",
    "сергелийский": "Сергелийский район",
    "сергели": "Сергелийский район",
    "шайхантахурский": "Шайхантахурский район",
    "шайхантахур": "Шайхантахурский район",
    "учтепинский": "Учтепинский район",
    "учтепа": "Учтепинский район",
    "яккасарайский": "Яккасарайский район",
    "яккасарай": "Яккасарайский район",
    "яккасарой": "Яккасарайский район",
    "янгихаётский": "Янгихаётский район",
    "янгихаёт": "Янгихаётский район",
    "янгихаятский": "Янгихаётский район",
    "янгихаят": "Янгихаётский район",
    "яшнабадский": "Яшнабадский район",
    "яшнабад": "Яшнабадский район",
    "яшнобод": "Яшнабадский район",
    "юнусабадский": "Юнусабадский район",
    "юнусабад": "Юнусабадский район",
    "юнусобод": "Юнусабадский район",
}

REGION_ALIASES = {
    "бекабадский": "Бекабадский район",
    "бекабадский район": "Бекабадский район",
    "bekobod": "Бекабадский район",
    "бостанлыкский": "Бостанлыкский район",
    "бостанлык": "Бостанлыкский район",
    "бостанлик": "Бостанлыкский район",
    "bostonliq": "Бостанлыкский район",
    "bo'stonliq": "Бостанлыкский район",
    "букинский": "Букинский район",
    "бука": "Букинский район",
    "boka": "Букинский район",
    "bo'ka": "Букинский район",
    "чиназский": "Чиназский район",
    "чиназ": "Чиназский район",
    "chinoz": "Чиназский район",
    "кибрайский": "Кибрайский район",
    "кибрай": "Кибрайский район",
    "qibray": "Кибрайский район",
    "ахангаранский": "Ахангаранский район",
    "ohangaron": "Ахангаранский район",
    "аккурганский": "Аккурганский район",
    "аккурган": "Аккурганский район",
    "oqqorgon": "Аккурганский район",
    "oqqo'rg'on": "Аккурганский район",
    "паркентский": "Паркентский район",
    "паркент": "Паркентский район",
    "parkent": "Паркентский район",
    "пскентский": "Пскентский район",
    "пскент": "Пскентский район",
    "piskent": "Пскентский район",
    "куйичирчикский": "Куйичирчикский район",
    "куйичирчик": "Куйичирчикский район",
    "quyi chirchiq": "Куйичирчикский район",
    "уртачирчикский": "Уртачирчикский район",
    "уртачирчик": "Уртачирчикский район",
    "orta chirchiq": "Уртачирчикский район",
    "o'rta chirchiq": "Уртачирчикский район",
    "янгиюльский": "Янгиюльский район",
    "yangiyol": "Янгиюльский район",
    "yangiyo'l": "Янгиюльский район",
    "юкоричирчикский": "Юкоричирчикский район",
    "юкоричирчик": "Юкоричирчикский район",
    "yuqori chirchiq": "Юкоричирчикский район",
    "зангиатинский": "Зангиатинский район",
    "зангиата": "Зангиатинский район",
    "zangiota": "Зангиатинский район",
    "назарбек": "Зангиатинский район",
    "nazarbek": "Зангиатинский район",
    "ташкентский": "Ташкентский район",
    "toshkent": "Ташкентский район",
    "келес": "Ташкентский район",
    "keles": "Ташкентский район",
    "алмалык": "город Алмалык",
    "ангрен": "город Ангрен",
    "бекабад": "город Бекабад",
    "ахангаран": "город Ахангаран",
    "нурафшан": "город Нурафшан",
    "чирчик": "город Чирчик",
    "янгиюль": "город Янгиюль",
}


def _normalized_words(value: Any) -> str:
    text = " ".join(str(value or "").strip().lower().replace("ё", "е").split())
    for character in ("-", "_", ",", ".", "/", "(", ")"):
        text = text.replace(character, " ")
    return " ".join(text.split())


def _canonical_district(value: Any, scope: str) -> str | None:
    text = _normalized_words(value)
    if not text:
        return None
    text = text.replace(" район", "").replace(" tumani", "").strip()
    aliases = CITY_ALIASES if scope == "city" else REGION_ALIASES
    if text in aliases:
        return aliases[text]
    for alias, canonical in aliases.items():
        if alias in text:
            return canonical
    return str(value).strip()


OWNER_SELLER_TYPES = {
    "private", "owner", "likely_owner", "probable_owner", "probably_owner",
    "частник", "собственник", "скорее_всего_собственник", "вероятный_собственник",
}
PROFESSIONAL_SELLER_TYPES = {
    "agent", "agency", "realtor", "likely_realtor", "probable_realtor",
    "probably_realtor", "official", "риэлтор", "риелтор", "агентство", "агент",
    "скорее_всего_риэлтор", "скорее_всего_риелтор", "вероятный_риэлтор",
    "вероятный_риелтор",
}


def _normalized_seller_type(value: Any) -> str:
    return "_".join(str(value or "").strip().casefold().replace("-", " ").split())


def _seller_group(seller_status: Any, is_official: Any) -> str:
    model_type = _normalized_seller_type(seller_status)
    if model_type in {"owner", "likely_owner"}:
        return "owner"
    if model_type in {"realtor", "likely_realtor"}:
        return "professional"
    if model_type in OWNER_SELLER_TYPES:
        return "owner"
    if bool(is_official) or model_type in PROFESSIONAL_SELLER_TYPES:
        return "professional"
    return "unknown"


def _scope_for(city: Any, district: Any) -> str | None:
    city_text = _normalized_words(city)
    district_text = _normalized_words(district)
    if any(token in city_text for token in ("область", "viloyat", "region")):
        if any(token in city_text for token in ("ташкент", "tashkent", "toshkent")):
            return "region"
    if any(token in city_text for token in ("ташкент", "tashkent", "toshkent")):
        return "city"
    if _canonical_district(district_text, "city") in CITY_DISTRICTS:
        return "city"
    if _canonical_district(district_text, "region") in REGION_DISTRICTS:
        return "region"
    if _canonical_district(city_text, "region") in REGION_DISTRICTS:
        return "region"
    return None


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
    ).fetchone() is not None


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}


def _select_column(
    available: set[str], table_alias: str, column: str, output_name: str | None = None
) -> str:
    alias = output_name or column
    if column in available:
        return f"{table_alias}.{column} AS {alias}"
    return f"NULL AS {alias}"


def _read_rows(database: Path) -> list[dict[str, Any]]:
    database_uri = database.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True, timeout=5)
    connection.row_factory = sqlite3.Row
    try:
        listing_columns = _columns(connection, "listings")
        if not listing_columns:
            raise RuntimeError("В базе отсутствует таблица listings")
        housing_columns = _columns(connection, "housing")
        seller_columns = _columns(connection, "sellers")
        amenity_columns = _columns(connection, "listing_amenities")
        amenities_lookup_columns = _columns(connection, "amenities")
        nearby_columns = _columns(connection, "listing_nearby")
        nearby_lookup_columns = _columns(connection, "nearby_places")
        image_columns = _columns(connection, "listing_images")
        listing_seller_columns = _columns(connection, "listing_sellers")

        fields = [
            _select_column(listing_columns, "l", "listing_pk"),
            _select_column(listing_columns, "l", "platform_listing_id"),
            _select_column(listing_columns, "l", "source_name"),
            _select_column(listing_columns, "l", "title"),
            _select_column(listing_columns, "l", "price"),
            _select_column(listing_columns, "l", "current_price"),
            _select_column(listing_columns, "l", "previous_price"),
            _select_column(listing_columns, "l", "previous_currency"),
            _select_column(listing_columns, "l", "currency"),
            _select_column(listing_columns, "l", "url"),
            _select_column(listing_columns, "l", "published_at"),
            _select_column(listing_columns, "l", "first_seen_at"),
            _select_column(listing_columns, "l", "last_seen_at"),
            _select_column(listing_columns, "l", "last_checked_at"),
            _select_column(listing_columns, "l", "last_changed_at"),
            _select_column(listing_columns, "l", "description"),
            _select_column(listing_columns, "l", "publication_status"),
            _select_column(listing_columns, "l", "transaction_type"),
            _select_column(listing_columns, "l", "description_length"),
            _select_column(listing_columns, "l", "city", "listing_city"),
            _select_column(listing_columns, "l", "district", "listing_district"),
            _select_column(listing_columns, "l", "rooms", "listing_rooms"),
            _select_column(listing_columns, "l", "total_area_m2", "listing_area"),
            _select_column(listing_columns, "l", "floor", "listing_floor"),
            _select_column(listing_columns, "l", "floors_total", "listing_floors_total"),
            _select_column(housing_columns, "h", "city", "housing_city"),
            _select_column(housing_columns, "h", "district", "housing_district"),
            _select_column(housing_columns, "h", "street"),
            _select_column(housing_columns, "h", "building_type"),
            _select_column(housing_columns, "h", "is_new_building"),
            _select_column(housing_columns, "h", "foundation_type"),
            _select_column(housing_columns, "h", "residential_complex_name"),
            _select_column(housing_columns, "h", "rooms", "housing_rooms"),
            _select_column(housing_columns, "h", "total_area_m2", "housing_area"),
            _select_column(housing_columns, "h", "floor", "housing_floor"),
            _select_column(housing_columns, "h", "floors_total", "housing_floors_total"),
            _select_column(housing_columns, "h", "furnished"),
            _select_column(housing_columns, "h", "monthly_rent"),
            _select_column(housing_columns, "h", "rent_currency"),
            _select_column(housing_columns, "h", "price_per_m2"),
            _select_column(housing_columns, "h", "latitude"),
            _select_column(housing_columns, "h", "longitude"),
            _select_column(seller_columns, "s", "name", "seller_name"),
            _select_column(seller_columns, "s", "identity_key", "seller_identity_key"),
            _select_column(seller_columns, "s", "profile_url", "seller_profile_url"),
            _select_column(seller_columns, "s", "seller_status"),
            _select_column(seller_columns, "s", "phone_listing_count"),
            _select_column(seller_columns, "s", "is_official_seller"),
            _select_column(seller_columns, "s", "official_complex_name"),
            _select_column(seller_columns, "s", "phone", "seller_phone"),
            _select_column(seller_columns, "s", "phone_source", "seller_phone_source"),
            _select_column(seller_columns, "s", "source_seller_role"),
            _select_column(seller_columns, "s", "updated_at", "seller_updated_at"),
        ]
        if {"listing_pk", "image_url"} <= image_columns:
            fields.append(
                "(SELECT li.image_url FROM listing_images li "
                "WHERE li.listing_pk = l.listing_pk "
                "ORDER BY li.is_primary DESC, li.sort_order, li.image_pk LIMIT 1) "
                "AS image_url"
            )
        else:
            fields.append("NULL AS image_url")
        history_columns = _columns(connection, "listing_history")
        if {"listing_pk", "recorded_at", "change_fields"} <= history_columns:
            fields.append(
                "(SELECT lh.recorded_at FROM listing_history lh "
                "WHERE lh.listing_pk = l.listing_pk "
                "AND (',' || lh.change_fields || ',') LIKE '%,price,%' "
                "ORDER BY lh.history_pk DESC LIMIT 1) AS price_changed_at"
            )
        else:
            fields.append("NULL AS price_changed_at")
        if {"listing_pk", "amenity_pk"} <= amenity_columns and {"amenity_pk", "name"} <= amenities_lookup_columns:
            fields.append(
                "(SELECT group_concat(a.name, '||') FROM listing_amenities la "
                "JOIN amenities a ON a.amenity_pk = la.amenity_pk "
                "WHERE la.listing_pk = l.listing_pk) AS amenities_raw"
            )
        else:
            fields.append("NULL AS amenities_raw")
        if {"listing_pk", "nearby_pk"} <= nearby_columns and {"nearby_pk", "name"} <= nearby_lookup_columns:
            fields.append(
                "(SELECT group_concat(n.name, '||') FROM listing_nearby ln "
                "JOIN nearby_places n ON n.nearby_pk = ln.nearby_pk "
                "WHERE ln.listing_pk = l.listing_pk) AS nearby_raw"
            )
        else:
            fields.append("NULL AS nearby_raw")
        if {"listing_pk", "seller_pk"} <= listing_seller_columns and "seller_pk" in seller_columns:
            seller_json_fields = {
                "seller_pk": "seller_pk",
                "name": "name",
                "phone": "phone",
                "phone_source": "phone_source",
                "profile_url": "profile_url",
                "identity_key": "identity_key",
                "seller_status": "seller_status",
                "source_seller_role": "source_seller_role",
                "phone_listing_count": "phone_listing_count",
                "is_official_seller": "is_official_seller",
                "official_complex_name": "official_complex_name",
                "updated_at": "updated_at",
            }
            json_arguments = ", ".join(
                f"'{output_name}', "
                + (f"sx.{column_name}" if column_name in seller_columns else "NULL")
                for output_name, column_name in seller_json_fields.items()
            )
            fields.append(
                "(SELECT json_group_array(json_object("
                + json_arguments
                + ")) FROM listing_sellers ls "
                "JOIN sellers sx ON sx.seller_pk = ls.seller_pk "
                "WHERE ls.listing_pk = l.listing_pk) AS seller_contacts_raw"
            )
        else:
            fields.append("NULL AS seller_contacts_raw")

        joins: list[str] = []
        if housing_columns and "listing_pk" in housing_columns:
            joins.append("LEFT JOIN housing h ON h.listing_pk = l.listing_pk")
        else:
            joins.append("LEFT JOIN (SELECT NULL AS listing_pk) h ON 0")
        if seller_columns and "seller_pk" in listing_columns:
            joins.append("LEFT JOIN sellers s ON s.seller_pk = l.seller_pk")
        else:
            joins.append("LEFT JOIN (SELECT NULL AS seller_pk) s ON 0")

        query = f"SELECT {', '.join(fields)} FROM listings l {' '.join(joins)}"
        return [dict(row) for row in connection.execute(query)]
    finally:
        connection.close()


def _first(row: dict[str, Any], *names: str) -> Any:
    for name in names:
        value = row.get(name)
        if value not in (None, ""):
            return value
    return None


def _split_related(value: Any) -> list[str]:
    if not value:
        return []
    return list(dict.fromkeys(part.strip() for part in str(value).split("||") if part.strip()))


def _split_seller_contacts(value: Any) -> list[dict[str, Any]]:
    if not value:
        return []
    try:
        contacts = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not isinstance(contacts, list):
        return []
    result: list[dict[str, Any]] = []
    seen: set[tuple[Any, Any, Any]] = set()
    for contact in contacts:
        if not isinstance(contact, dict):
            continue
        identity = (contact.get("seller_pk"), contact.get("phone"), contact.get("profile_url"))
        if identity in seen:
            continue
        seen.add(identity)
        contact["is_official_seller"] = bool(contact.get("is_official_seller"))
        result.append(contact)
    return result


def _short_description(item: dict[str, Any]) -> str:
    details: list[str] = []
    if item["rooms"]:
        details.append(f"{item['rooms']}-комнатное жильё")
    if item["area_m2"]:
        details.append(f"площадью {item['area_m2']:g} м²")
    if item["floor"]:
        floor = f"на {item['floor']}-м этаже"
        if item["floors_total"]:
            floor += f" {item['floors_total']}-этажного дома"
        details.append(floor)
    if item["residential_complex_name"]:
        details.append(f"ЖК {item['residential_complex_name']}")
    if item["street"]:
        details.append(f"адрес: {item['street']}")
    if not details:
        return "Подробности доступны на странице объявления"
    return ", ".join(details).capitalize() + "."


def _identity_text(value: Any) -> str:
    text = _normalized_words(value)
    return " ".join(
        word
        for word in text.split()
        if word not in {"улица", "ул", "дом", "район", "город", "квартира"}
    )


def _number(value: Any) -> float | None:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _valid_coordinates(latitude: Any, longitude: Any) -> bool:
    lat = _number(latitude)
    lon = _number(longitude)
    return bool(
        lat is not None
        and lon is not None
        and -90 <= lat <= 90
        and -180 <= lon <= 180
        and not (lat == 0 and lon == 0)
    )


def _has_street_and_house(value: Any) -> bool:
    """Recognise a usable street + house address without treating a district as one."""

    text = " ".join(str(value or "").strip().split())
    if not text:
        return False
    return bool(
        re.search(r"(?:,|\b(?:дом|д\.)\s*)\s*\d+[\wА-Яа-яЁё/-]*\s*$", text, re.IGNORECASE)
        or re.search(r"(?:^|\s)(?:улица|ул\.|проспект|пр-т|переулок|проезд|шоссе)\s+.+\s\d+[\wА-Яа-яЁё/-]*\s*$", text, re.IGNORECASE)
    )


def _location_precision(item: dict[str, Any]) -> str:
    if _valid_coordinates(item.get("latitude"), item.get("longitude")):
        return "coordinates"
    if str(item.get("street") or "").strip() or str(
        item.get("residential_complex_name") or ""
    ).strip():
        return "address"
    return "district"


def _same_text(left: Any, right: Any) -> bool:
    left_text = _identity_text(left)
    right_text = _identity_text(right)
    if not left_text or not right_text:
        return False
    if left_text == right_text:
        return True
    left_words = set(left_text.split())
    right_words = set(right_text.split())
    overlap = len(left_words & right_words) / max(len(left_words | right_words), 1)
    return overlap >= 0.72


def _same_property(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Weighted cross-platform match; seller identity is supporting evidence.

    A strong address/complex/coordinate match can unite offers from unrelated
    agents.  A shared seller alone is deliberately insufficient because one
    agent can publish many different apartments.
    """

    if left.get("scope") != right.get("scope"):
        return False
    for field in ("city", "district", "transaction_type"):
        if left.get(field) and right.get(field) and not _same_text(
            left.get(field), right.get(field)
        ):
            return False

    score = 0
    strong_location = 0
    left_lat, left_lon = _number(left.get("latitude")), _number(left.get("longitude"))
    right_lat, right_lon = _number(right.get("latitude")), _number(right.get("longitude"))
    if None not in (left_lat, left_lon, right_lat, right_lon):
        distance = math.hypot(left_lat - right_lat, left_lon - right_lon)
        if distance <= 0.0022:
            score += 7
            strong_location += 1
        elif distance > 0.012:
            return False

    for field in ("street", "residential_complex_name"):
        left_value, right_value = left.get(field), right.get(field)
        if left_value and right_value and _same_text(left_value, right_value):
            score += 6
            strong_location += 1

    rooms_left, rooms_right = _number(left.get("rooms")), _number(right.get("rooms"))
    if rooms_left is not None and rooms_right is not None:
        if rooms_left != rooms_right:
            return False
        score += 2

    area_left, area_right = _number(left.get("area_m2")), _number(right.get("area_m2"))
    if area_left is not None and area_right is not None:
        area_difference = abs(area_left - area_right)
        if area_difference > max(1.5, max(area_left, area_right) * 0.02):
            return False
        score += 4

    for field in ("floor", "floors_total"):
        left_value, right_value = _number(left.get(field)), _number(right.get(field))
        if left_value is None or right_value is None:
            continue
        if left_value != right_value:
            return False
        score += 1

    left_phone = re.sub(r"\D", "", str(left.get("seller_phone") or ""))
    right_phone = re.sub(r"\D", "", str(right.get("seller_phone") or ""))
    same_phone = bool(left_phone and left_phone == right_phone)
    if same_phone:
        score += 4
    if _same_text(left.get("seller_name"), right.get("seller_name")):
        score += 1

    # A textual likeness only reinforces structured housing evidence.
    if _same_text(left.get("title"), right.get("title")):
        score += 1
    same_description = _same_text(left.get("description"), right.get("description"))
    if same_description:
        score += 3
    same_image = bool(
        left.get("image_url")
        and left.get("image_url") == right.get("image_url")
    )
    if same_image:
        score += 5
    structured_pair = (
        rooms_left is not None
        and rooms_right is not None
        and area_left is not None
        and area_right is not None
    )
    supporting_identity = (
        strong_location >= 2 or same_phone or same_description or same_image
    )
    return (
        strong_location > 0
        and structured_pair
        and supporting_identity
        and score >= 12
    )


def _offer(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "listing_pk": item.get("listing_pk"),
        "platform_listing_id": item.get("platform_listing_id"),
        "source": item.get("source"),
        "title": item.get("title"),
        "description": item.get("description"),
        "image_url": item.get("image_url"),
        "url": item.get("url"),
        "price": item.get("price"),
        "current_price": item.get("current_price"),
        "previous_price": item.get("previous_price"),
        "previous_currency": item.get("previous_currency"),
        "currency": item.get("currency"),
        "price_changed_at": item.get("price_changed_at"),
        "published_at": item.get("published_at"),
        "first_seen_at": item.get("first_seen_at"),
        "last_seen_at": item.get("last_seen_at"),
        "last_checked_at": item.get("last_checked_at"),
        "last_changed_at": item.get("last_changed_at"),
        "status": item.get("status"),
        "seller_name": item.get("seller_name"),
        "seller_phone": item.get("seller_phone"),
        "seller_phone_source": item.get("seller_phone_source"),
        "seller_identity_key": item.get("seller_identity_key"),
        "seller_profile_url": item.get("seller_profile_url"),
        "seller_status": item.get("seller_status"),
        "source_seller_role": item.get("source_seller_role"),
        "phone_listing_count": item.get("phone_listing_count"),
        "is_official_seller": item.get("is_official_seller"),
        "official_complex_name": item.get("official_complex_name"),
        "seller_updated_at": item.get("seller_updated_at"),
        "seller_contacts": item.get("seller_contacts", []),
        "seller_group": item.get("seller_group"),
    }


def _property_bucket_keys(item: dict[str, Any]) -> list[tuple[Any, ...]]:
    rooms = _number(item.get("rooms"))
    area = _number(item.get("area_m2"))
    if rooms is None or area is None:
        return []
    area_bucket = int(round(area / 2.0))
    prefix = (
        item.get("scope"),
        _identity_text(item.get("city")),
        _identity_text(item.get("district")),
        rooms,
    )
    return [(*prefix, area_bucket + offset) for offset in (-1, 0, 1)]


def _group_property_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: list[list[dict[str, Any]]] = []
    buckets: dict[tuple[Any, ...], list[int]] = {}
    for item in items:
        bucket_keys = _property_bucket_keys(item)
        candidate_indices = list(
            dict.fromkeys(
                group_index
                for key in bucket_keys
                for group_index in buckets.get(key, [])
            )
        )
        matching_index = next(
            (
                group_index
                for group_index in candidate_indices
                if all(
                    _same_property(item, member)
                    for member in groups[group_index]
                )
            ),
            None,
        )
        if matching_index is None:
            matching_index = len(groups)
            groups.append([item])
        else:
            groups[matching_index].append(item)
        for key in bucket_keys:
            group_indices = buckets.setdefault(key, [])
            if matching_index not in group_indices:
                group_indices.append(matching_index)

    merged_items: list[dict[str, Any]] = []
    for members in groups:
        representative = max(
            members,
            key=lambda item: (
                item.get("status") == "active",
                bool(item.get("image_url")),
                int(bool(item.get("street"))) + int(bool(item.get("residential_complex_name"))),
                item.get("description_length") or 0,
                str(item.get("published_at") or item.get("first_seen_at") or ""),
            ),
        )
        merged = dict(representative)
        # A grouped property may have coordinates or a fuller address on a
        # non-representative publication. Preserve the strongest location for
        # the map instead of losing it during cross-platform aggregation.
        coordinate_member = next(
            (
                item
                for item in members
                if _valid_coordinates(item.get("latitude"), item.get("longitude"))
            ),
            None,
        )
        address_member = next(
            (item for item in members if _has_street_and_house(item.get("street"))),
            None,
        )
        if coordinate_member is not None:
            merged["latitude"] = coordinate_member.get("latitude")
            merged["longitude"] = coordinate_member.get("longitude")
        if address_member is not None and not _has_street_and_house(merged.get("street")):
            merged["street"] = address_member.get("street")
        offers = sorted(
            (_offer(item) for item in members),
            key=lambda offer: (
                offer.get("status") != "active",
                str(offer.get("source") or ""),
            ),
        )
        merged["offers"] = offers
        merged["sources"] = list(
            dict.fromkeys(str(offer["source"]) for offer in offers if offer.get("source"))
        )
        merged["listing_pks"] = [offer.get("listing_pk") for offer in offers]
        stable_members = ",".join(
            str(value) for value in sorted(pk for pk in merged["listing_pks"] if pk is not None)
        )
        merged["property_group_id"] = "property-" + hashlib.sha1(
            stable_members.encode("utf-8")
        ).hexdigest()[:12]
        merged["publication_count"] = len(offers)
        merged["is_duplicate"] = len(offers) > 1
        merged["duplicate_scope"] = (
            "cross_platform"
            if len(merged["sources"]) > 1
            else "same_platform"
            if len(offers) > 1
            else "unique"
        )
        merged["status"] = "active" if any(
            offer.get("status") == "active" for offer in offers
        ) else representative.get("status")
        merged["amenities"] = list(
            dict.fromkeys(value for item in members for value in item.get("amenities", []))
        )
        merged["nearby_places"] = list(
            dict.fromkeys(value for item in members for value in item.get("nearby_places", []))
        )
        merged["location_precision"] = _location_precision(merged)
        merged_items.append(merged)
    return merged_items


def _serialize(database: Path) -> dict[str, Any]:
    raw_rows = _read_rows(database)
    items: list[dict[str, Any]] = []
    for row in raw_rows:
        city = _first(row, "listing_city", "housing_city")
        district = _first(row, "listing_district", "housing_district")
        scope = _scope_for(city, district)
        if scope not in {"city", "region"}:
            continue
        canonical_district = _canonical_district(district, scope)
        if scope == "region":
            canonical_district = REGION_CITY_PARENT_DISTRICTS.get(
                canonical_district, canonical_district
            )
            if canonical_district not in REGION_MAP_DISTRICTS:
                city_district = _canonical_district(city, "region")
                city_district = REGION_CITY_PARENT_DISTRICTS.get(
                    city_district, city_district
                )
                if city_district in REGION_MAP_DISTRICTS:
                    canonical_district = city_district
        seller_status = row.get("seller_status")
        is_official_seller = bool(row.get("is_official_seller"))
        item = {
            "listing_pk": row.get("listing_pk"),
            "platform_listing_id": row.get("platform_listing_id"),
            "source": row.get("source_name") or "unknown",
            "title": row.get("title") or "Объявление без заголовка",
            "price": _first(row, "current_price", "price"),
            "current_price": _first(row, "current_price", "price"),
            "previous_price": row.get("previous_price"),
            "previous_currency": row.get("previous_currency"),
            "currency": row.get("currency") or row.get("rent_currency"),
            "url": row.get("url"),
            "published_at": row.get("published_at"),
            "first_seen_at": row.get("first_seen_at"),
            "last_seen_at": row.get("last_seen_at"),
            "last_checked_at": row.get("last_checked_at"),
            "last_changed_at": row.get("last_changed_at"),
            "price_changed_at": row.get("price_changed_at"),
            "description": row.get("description"),
            "image_url": row.get("image_url"),
            "status": row.get("publication_status") or "unknown",
            "transaction_type": row.get("transaction_type"),
            "description_length": row.get("description_length") or 0,
            "scope": scope,
            "city": city,
            "district": canonical_district,
            "street": row.get("street"),
            "rooms": _first(row, "listing_rooms", "housing_rooms"),
            "area_m2": _first(row, "listing_area", "housing_area"),
            "floor": _first(row, "listing_floor", "housing_floor"),
            "floors_total": _first(row, "listing_floors_total", "housing_floors_total"),
            "building_type": row.get("building_type"),
            "is_new_building": row.get("is_new_building"),
            "foundation_type": row.get("foundation_type"),
            "residential_complex_name": row.get("residential_complex_name"),
            "furnished": row.get("furnished"),
            "monthly_rent": row.get("monthly_rent"),
            "rent_currency": row.get("rent_currency"),
            "price_per_m2": row.get("price_per_m2"),
            "latitude": row.get("latitude"),
            "longitude": row.get("longitude"),
            "seller_name": row.get("seller_name"),
            "seller_identity_key": row.get("seller_identity_key"),
            "seller_profile_url": row.get("seller_profile_url"),
            "seller_status": seller_status,
            "source_seller_role": row.get("source_seller_role"),
            "phone_listing_count": row.get("phone_listing_count"),
            "seller_group": _seller_group(seller_status, is_official_seller),
            "is_official_seller": is_official_seller,
            "official_complex_name": row.get("official_complex_name"),
            "seller_phone": row.get("seller_phone"),
            "seller_phone_source": row.get("seller_phone_source"),
            "seller_updated_at": row.get("seller_updated_at"),
            "seller_contacts": _split_seller_contacts(row.get("seller_contacts_raw")),
            "amenities": _split_related(row.get("amenities_raw")),
            "nearby_places": _split_related(row.get("nearby_raw")),
        }
        item["description"] = (
            str(item.get("description") or "").strip()
            or _short_description(item)
        )
        items.append(item)

    def date_key(item: dict[str, Any]) -> str:
        return str(item.get("published_at") or item.get("first_seen_at") or "")

    items = _group_property_items(items)
    items.sort(key=date_key, reverse=True)
    city_counts = Counter(item["district"] for item in items if item["scope"] == "city")
    region_counts = Counter(item["district"] for item in items if item["scope"] == "region")
    sources = sorted(
        {
            str(source)
            for item in items
            for source in item.get("sources", [item.get("source")])
            if source
        }
    )
    currencies = sorted({str(item["currency"]) for item in items if item.get("currency")})
    room_values = sorted({int(item["rooms"]) for item in items if item.get("rooms")})
    building_types = sorted({str(item["building_type"]) for item in items if item.get("building_type")})
    amenity_counts = Counter(
        amenity for item in items for amenity in item.get("amenities", [])
    )
    nearby_counts = Counter(
        place for item in items for place in item.get("nearby_places", [])
    )

    def popular_options(counts: Counter[str], limit: int = 8) -> list[dict[str, Any]]:
        return [
            {"name": name, "count": count}
            for name, count in sorted(counts.items(), key=lambda pair: (-pair[1], pair[0]))[:limit]
        ]

    nearby_without_metro = Counter(
        {
            name: count
            for name, count in nearby_counts.items()
            if "метро" not in name.casefold()
        }
    )
    nearby_options = popular_options(nearby_without_metro, limit=8)

    return {
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "database": database.name,
        "stats": {
            "total": len(items),
            "city": sum(city_counts.values()),
            "region": sum(region_counts.values()),
            "publications": sum(item.get("publication_count", 1) for item in items),
        },
        "districts": {
            "city": [
                {"name": name, "count": city_counts.get(name, 0)} for name in CITY_DISTRICTS
            ],
            "region": [
                {"name": name, "count": region_counts.get(name, 0)}
                for name in REGION_MAP_DISTRICTS
            ],
        },
        "options": {
            "sources": sources,
            "currencies": currencies,
            "rooms": room_values,
            "building_types": building_types,
            "amenities": popular_options(amenity_counts),
            "nearby_places": nearby_options,
        },
        "items": items,
    }


class DashboardHandler(SimpleHTTPRequestHandler):
    database: Path = DEFAULT_DATABASE
    air_quality: OpenWeatherAirQualityService | None = None

    def end_headers(self) -> None:
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self._write_json({"status": "ok", "database": self.database.name})
            return
        if path == "/api/bootstrap":
            try:
                self._write_json(_serialize(self.database))
            except (OSError, sqlite3.Error, RuntimeError) as error:
                self._write_json({"error": str(error)}, status=500)
            return
        if path == "/api/air-quality":
            if self.air_quality is None:
                self._write_json({"error": "Сервис качества воздуха не настроен."}, status=503)
                return
            try:
                self._write_json(self.air_quality.get_tashkent())
            except AirQualityConfigurationError as error:
                self._write_json({"error": str(error)}, status=503)
            except AirQualityError as error:
                self._write_json({"error": str(error)}, status=502)
            return
        super().do_GET()

    def _write_json(self, payload: dict[str, Any], status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        if urlparse(self.path).path.startswith("/api/"):
            return
        super().log_message(format, *args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Локальная интерактивная карта объявлений Ташкента"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--open",
        action="store_true",
        help="Открыть интерфейс в браузере после запуска",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    database = args.database.resolve()
    if not database.exists():
        raise SystemExit(f"База данных не найдена: {database}")
    if not (STATIC_ROOT / "index.html").exists():
        raise SystemExit(f"Файлы интерфейса не найдены: {STATIC_ROOT}")

    handler = partial(DashboardHandler, directory=str(STATIC_ROOT))
    DashboardHandler.database = database
    DashboardHandler.air_quality = OpenWeatherAirQualityService(
        load_openweather_api_key(PROJECT_ROOT),
        STATIC_ROOT / ".cache" / "openweather_tashkent.json",
        districts_path=STATIC_ROOT / "data" / "tashkent_city_districts.geojson",
    )
    server = ThreadingHTTPServer((args.host, args.port), handler)
    url = f"http://{args.host}:{args.port}"
    print(f"Карта объявлений: {url}")
    print(f"База данных (только чтение): {database}")
    print("Остановка: Ctrl+C")
    if args.open:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nСервер остановлен")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
