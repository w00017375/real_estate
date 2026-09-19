import asyncio
import io
import os
import sqlite3
import tempfile
import threading
import unittest
from contextlib import closing, redirect_stdout

from core.deduplication import deduplicate_listings
from core.ordering import sort_listings_newest_first
from core.pipeline import process_listing
from main import _collect_sources_parallel
from core.seller_metadata import official_seller_details
from sources.olx import (
    _location_section_features,
    _location_from_json_ld,
    _parse_explicit_published_date,
    _publication_features,
)
from storage import sqlite_store
from storage.sqlite_store import create_schema, save_to_database


def listing(
    source: str,
    listing_id: str,
    profile_id: str,
    housing: tuple[str, str, int, float],
    description_length: int,
) -> dict:
    city, district, rooms, area = housing
    url = f"https://{source}.example/listing/{listing_id}"
    publication = {
        "id": listing_id,
        "title": listing_id,
        "type": "sale",
        "url": url,
        "city": city,
        "district": district,
        "rooms": rooms,
        "total_area_m2": area,
        "floor": 1,
        "floors_total": 5,
    }
    return {
        "source": source,
        "id": listing_id,
        "title": listing_id,
        "price": 100000,
        "currency": "USD",
        "url": url,
        "published_at": None,
        "description_length": description_length,
        "housing": {
            "city": city,
            "district": district,
            "rooms": rooms,
            "total_area_m2": area,
            "floor": 1,
            "floors_total": 5,
            "furnished": True,
            "commission": False,
        },
        "amenities": [],
        "nearby": [],
        "seller": {
            "name": "Иван",
            "phone": "+998901234567",
            "profile_url": f"https://{source}.example/user/{profile_id}",
            "active_rent_listings": 0,
            "active_sale_listings": 1,
            "unique_listing_count": 1,
            "distinct_housing_listings": 1,
            "profile_scan_complete": True,
            "active_publications": [publication],
            "closed_publications": [],
            "seller_type": "likely_owner",
            "realtor_probability": 0.1,
            "comment": "initial",
        },
    }


class CrossSourceSellerAssessmentTest(unittest.TestCase):
    def test_olx_explicit_published_date_is_parsed(self) -> None:
        parsed = _parse_explicit_published_date(
            "Ташкент · Опубликовано\u00a026 августа 2026 г."
        )
        self.assertIsNotNone(parsed)
        self.assertTrue(parsed.startswith("2026-08-26T00:00:00+05:00"))

    def test_database_lock_is_reported_before_parsing(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        holder = sqlite3.connect(database_path)
        old_timeout = sqlite_store.DATABASE_BUSY_TIMEOUT_MS
        old_retries = sqlite_store.DATABASE_LOCK_RETRIES
        try:
            holder.execute("CREATE TABLE lock_test(id INTEGER)")
            holder.commit()
            holder.execute("BEGIN EXCLUSIVE")
            sqlite_store.DATABASE_BUSY_TIMEOUT_MS = 20
            sqlite_store.DATABASE_LOCK_RETRIES = 2
            with self.assertRaises(sqlite_store.DatabaseLockedError):
                sqlite_store.wait_for_database_write_access(database_path)
        finally:
            sqlite_store.DATABASE_BUSY_TIMEOUT_MS = old_timeout
            sqlite_store.DATABASE_LOCK_RETRIES = old_retries
            holder.rollback()
            holder.close()
            os.remove(database_path)

    def test_housing_location_is_backfilled_from_current_profile_card(self) -> None:
        opened = listing("olx", "a1", "pa", (None, None, 2, 60), 20)
        opened["seller"]["active_publications"] = [
            {
                "url": opened["url"],
                "city": None,
                "district": None,
            },
            {
                "url": opened["url"],
                "city": "Ташкент",
                "district": "Юнусабадский район",
            },
        ]

        processed = process_listing(opened)

        self.assertEqual(processed["housing"]["city"], "Ташкент")
        self.assertEqual(
            processed["housing"]["district"], "Юнусабадский район"
        )

    def test_housing_location_is_backfilled_from_raw_profile_stats(self) -> None:
        opened = listing("olx", "a1", "pa", (None, None, 2, 60), 20)
        opened["seller"]["active_publications"] = []
        opened["seller_profile_stats"] = {
            "active_publications": [
                {
                    "url": opened["url"],
                    "city": "Ташкент",
                    "district": "Яккасарайский район",
                }
            ]
        }

        processed = process_listing(opened)

        self.assertEqual(processed["housing"]["city"], "Ташкент")
        self.assertEqual(
            processed["housing"]["district"], "Яккасарайский район"
        )

    def test_olx_json_ld_location_has_priority_ready_values(self) -> None:
        location = _location_from_json_ld(
            {
                "@type": "Product",
                "offers": {
                    "areaServed": {
                        "@type": "AdministrativeArea",
                        "name": "Мирабадский район",
                    }
                },
            }
        )

        self.assertEqual(location["city"], "Ташкент")
        self.assertEqual(location["district"], "Мирабадский район")

    def test_olx_profile_card_accepts_district_on_separate_line(self) -> None:
        location = _publication_features(
            "Assolom sohil 25/25, 10 этаж, 2 комнатная\n"
            "7 095 000 сум\nМирабад\n24 августа 2026 г."
        )

        self.assertEqual(location["city"], "Ташкент")
        self.assertEqual(location["district"], "Мирабадский район")

    def test_olx_visible_location_section_fills_city_and_district(self) -> None:
        location = _location_section_features(
            "Описание\nтекст\nМестоположение\nТашкент\n"
            "Яккасарайский район\nПосмотреть расположение на карте"
        )

        self.assertEqual(location["city"], "Ташкент")
        self.assertEqual(location["district"], "Яккасарайский район")

    def test_olx_oblast_and_settlement_are_kept_in_separate_columns(self) -> None:
        for body, expected in (
            (
                "Местоположение\nЧирчик\nТашкентская область\n"
                "Посмотреть расположение на карте",
                ("Ташкентская область", "Чирчик"),
            ),
            (
                "Местоположение\nСамаркандская область\nСамарканд\n"
                "Посмотреть расположение на карте",
                ("Самаркандская область", "Самарканд"),
            ),
        ):
            location = _location_section_features(body)
            self.assertEqual(
                (location["city"], location["district"]), expected
            )

    def test_newest_order_places_unknown_dates_last(self) -> None:
        records = [
            {"id": "unknown", "published_at": None},
            {"id": "old", "published_at": "2026-08-01T00:00:00Z"},
            {"id": "new", "published_at": "2026-08-30T00:00:00+05:00"},
        ]

        ordered = sort_listings_newest_first(records)

        self.assertEqual([item["id"] for item in ordered], ["new", "old", "unknown"])

    def test_sources_are_collected_in_parallel(self) -> None:
        barrier = threading.Barrier(2)

        class Source:
            def __init__(self, name: str) -> None:
                self.name = name
                self.start_url = f"https://{name}.example"

            def collect(self, limit: int, existing=None) -> list[dict]:
                barrier.wait(timeout=2)
                return [{"source": self.name, "id": str(limit)}]

        with redirect_stdout(io.StringIO()):
            collected, errors = asyncio.run(
                _collect_sources_parallel([Source("a"), Source("b")], 3)
            )

        self.assertFalse(errors)
        self.assertEqual(set(collected), {"a", "b"})

    def test_repeat_save_does_not_erase_known_location(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            opened = listing(
                "olx", "a1", "pa", ("Ташкент", "Мирабадский район", 2, 45), 20
            )
            save_to_database(
                [opened],
                database_path,
                source_name="olx",
                source_url="https://www.olx.uz",
                listing_limit=1,
            )
            opened["housing"]["city"] = None
            opened["housing"]["district"] = None
            save_to_database(
                [opened],
                database_path,
                source_name="olx",
                source_url="https://www.olx.uz",
                listing_limit=1,
            )

            with closing(sqlite3.connect(database_path)) as connection:
                listing_location = connection.execute(
                    "SELECT city, district FROM listings"
                ).fetchone()
                housing_location = connection.execute(
                    "SELECT city, district FROM housing"
                ).fetchone()
                seller_count = connection.execute(
                    "SELECT COUNT(*) FROM sellers"
                ).fetchone()[0]

            expected = ("Ташкент", "Мирабадский район")
            self.assertEqual(listing_location, expected)
            self.assertEqual(housing_location, expected)
            self.assertEqual(seller_count, 1)
        finally:
            os.remove(database_path)

    def test_repeat_save_updates_known_listing_and_keeps_history(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            opened = listing(
                "site-a", "a1", "pa", ("T", "D", 2, 45), 20
            )
            opened.update(
                {
                    "price": 500,
                    "currency": "USD",
                    "published_at": "2026-08-30T00:00:00Z",
                }
            )
            opened["housing"].update(
                {"floor": 3, "floors_total": 9, "street": "Street 1"}
            )
            save_to_database(
                [opened], database_path, source_name="site-a",
                source_url="https://site-a.example", listing_limit=1,
            )
            opened.update(
                {
                    "price": 999,
                    "currency": "UZS",
                    "published_at": "2026-09-05T00:00:00Z",
                }
            )
            opened["housing"].update(
                {"rooms": 4, "total_area_m2": 120, "floor": 8,
                 "floors_total": 12, "street": "Changed street"}
            )
            repeat_result = save_to_database(
                [opened], database_path, source_name="site-a",
                source_url="https://site-a.example", listing_limit=1,
            )
            unchanged_result = save_to_database(
                [opened], database_path, source_name="site-a",
                source_url="https://site-a.example", listing_limit=1,
            )

            with closing(sqlite3.connect(database_path)) as connection:
                saved = connection.execute(
                    """SELECT price, current_price, previous_price, currency,
                              published_at, rooms, total_area_m2, floor,
                              floors_total
                       FROM listings"""
                ).fetchone()
                street = connection.execute("SELECT street FROM housing").fetchone()[0]
                history_count = connection.execute(
                    "SELECT COUNT(*) FROM listing_history"
                ).fetchone()[0]

            self.assertEqual(
                saved,
                (999, 999.0, 500.0, "UZS", "2026-09-05 05:00:00", 4, 120.0, 8, 12),
            )
            self.assertEqual(street, "Changed street")
            self.assertEqual(repeat_result["listings_saved"], 0)
            self.assertEqual(repeat_result["listings_updated"], 1)
            self.assertEqual(repeat_result["price_changes"], 1)
            self.assertEqual(unchanged_result["listings_unchanged"], 1)
            self.assertEqual(history_count, 2)
        finally:
            os.remove(database_path)

    def test_official_olx_profile_is_saved_with_complex_name(self) -> None:
        profile_url = "https://urbanestate.olx.uz/home/"
        seller_name = "Urban Estate на OLX с январь 2021 г. Онлайн сегодня"
        is_official, complex_name = official_seller_details(
            profile_url, seller_name
        )
        self.assertTrue(is_official)
        self.assertEqual(complex_name, "Urban Estate")
        self.assertEqual(
            official_seller_details(
                "https://www.olx.uz/list/user/abc/", seller_name
            ),
            (False, None),
        )

        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            opened = listing(
                "olx", "official-1", "ignored", ("T", "D1", 2, 60), 20
            )
            opened["seller"]["profile_url"] = profile_url
            opened["seller"]["name"] = seller_name
            opened["seller"]["is_official_seller"] = is_official
            opened["seller"]["official_complex_name"] = complex_name
            output = [opened]

            save_to_database(
                output,
                database_path,
                source_name="olx",
                source_url="https://www.olx.uz",
                listing_limit=1,
            )

            with closing(sqlite3.connect(database_path)) as connection:
                row = connection.execute(
                    """SELECT is_official_seller, official_complex_name
                       FROM sellers"""
                ).fetchone()
                self.assertEqual(row, (1, "Urban Estate"))
                connection.execute(
                    """UPDATE sellers SET
                       is_official_seller = 0, official_complex_name = NULL"""
                )
                create_schema(connection)
                row = connection.execute(
                    """SELECT is_official_seller, official_complex_name
                       FROM sellers"""
                ).fetchone()
            # Повторное открытие схемы больше не классифицирует и не
            # восстанавливает метаданные продавца автоматически.
            self.assertEqual(row, (0, None))
            self.assertTrue(output[0]["seller"]["is_official_seller"])
            self.assertEqual(
                output[0]["seller"]["official_complex_name"],
                "Urban Estate",
            )
        finally:
            os.remove(database_path)

    def test_duplicate_key_ignores_name_furniture_and_commission(self) -> None:
        first = listing("site-a", "a1", "pa", ("T", "D1", 1, 40), 20)
        second = listing("site-a", "a2", "pa", ("T", "D1", 1, 40), 10)
        second["seller"]["name"] = "Другое имя"
        second["housing"]["furnished"] = False
        second["housing"]["commission"] = True

        selected, discarded = deduplicate_listings([first, second])

        self.assertEqual(len(selected), 1)
        self.assertEqual(len(discarded), 1)

    def test_sellers_are_stored_per_publication_without_assessment(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            source_a = [
                listing("site-a", "a1", "pa", ("T", "D1", 1, 40), 20),
                listing("site-a", "a2", "pa", ("T", "D2", 2, 60), 20),
            ]
            source_a[0]["seller"]["seller_role"] = "private"
            save_to_database(
                source_a,
                database_path,
                source_name="site-a",
                source_url="https://site-a.example",
                listing_limit=2,
            )

            source_b = [
                # Совпадающий объект пока тоже сохраняется как отдельная
                # сырая публикация: дедупликация будет постпроцессом.
                listing("site-b", "b1", "pb", ("T", "D1", 1, 40), 30),
                listing("site-b", "b2", "pb", ("T", "D3", 3, 80), 20),
            ]
            save_to_database(
                source_b,
                database_path,
                source_name="site-b",
                source_url="https://site-b.example",
                listing_limit=2,
            )

            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0],
                    4,
                )
                seller_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sellers)")
                }
                self.assertFalse(
                    {
                        "seller_type", "sellers_type", "realtor_probability",
                        "assessment_comment", "active_rent_listings",
                        "active_sale_listings", "unique_listing_count",
                        "distinct_housing_listings", "profile_scan_complete",
                    }
                    & seller_columns
                )
                self.assertTrue(
                    {"seller_status", "phone_listing_count"}
                    <= seller_columns
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT source_seller_role FROM sellers
                           WHERE identity_key = ?""",
                        ("https://site-a.example/listing/a1",),
                    ).fetchone()[0],
                    "private",
                )
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table'
                             AND name = 'seller_publications'"""
                    ).fetchone()
                )
        finally:
            os.remove(database_path)

    def test_profile_cards_do_not_trigger_assessment(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            opened = listing(
                "site-a", "a1", "pa", ("T", "D1", 1, 40), 20
            )
            opened["seller"]["active_publications"].extend(
                [
                    {
                        "id": "profile-only-2",
                        "title": "card 2",
                        "type": "sale",
                        "url": "https://site-a.example/listing/profile-only-2",
                        "city": "T",
                        "district": "D2",
                        "rooms": 2,
                        "total_area_m2": 60,
                    },
                    {
                        "id": "profile-only-3",
                        "title": "card 3",
                        "type": "rent",
                        "url": "https://site-a.example/listing/profile-only-3",
                        "city": "T",
                        "district": "D3",
                        "rooms": 3,
                        "total_area_m2": 80,
                    },
                ]
            )

            output = [process_listing(opened)]
            save_to_database(
                output,
                database_path,
                source_name="site-a",
                source_url="https://site-a.example",
                listing_limit=1,
            )

            self.assertNotIn("seller_type", output[0]["seller"])
            self.assertNotIn("unique_listing_count", output[0]["seller"])
            with closing(sqlite3.connect(database_path)) as connection:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM listings").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM housing").fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0],
                    1,
                )
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table'
                             AND name = 'seller_publications'"""
                    ).fetchone()
                )
        finally:
            os.remove(database_path)

    def test_new_database_layout(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            opened = listing(
                "site-a", "a1", "pa", ("T", "D1", 1, 40), 20
            )
            opened["transaction_type"] = "rent"
            opened["housing"].update(
                {
                    "street": "ул. Амира Темура",
                    "zone": "Юнусабад",
                    "house_number": "15",
                    "address": "старое полное значение",
                }
            )
            opened["amenities"] = ["Мебель"]
            opened["nearby"] = ["Метро"]
            processed = process_listing(opened)
            save_to_database(
                [processed],
                database_path,
                source_name="site-a",
                source_url="https://site-a.example",
                listing_limit=1,
            )

            with closing(sqlite3.connect(database_path)) as connection:
                tables = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                self.assertNotIn("seller_publications", tables)
                listing_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(listings)")
                }
                self.assertNotIn("listing_identity", listing_columns)
                self.assertNotIn("last_modified_at", listing_columns)
                self.assertIn("description", listing_columns)
                listing_types = {
                    row[1]: row[2]
                    for row in connection.execute("PRAGMA table_info(listings)")
                }
                self.assertEqual(listing_types["published_at"], "DATETIME")
                self.assertEqual(listing_types["first_seen_at"], "DATETIME")
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'view'
                             AND name IN (
                                 'listings_newest_first',
                                 'listing_newest_first'
                             )"""
                    ).fetchone()
                )
                identity_key = connection.execute(
                    "SELECT identity_key FROM sellers"
                ).fetchone()[0]
                self.assertEqual(
                    identity_key, "https://site-a.example/listing/a1"
                )
                seller_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(sellers)")
                }
                self.assertTrue(
                    {"seller_status", "phone_listing_count"}
                    <= seller_columns
                )
                self.assertTrue(
                    {
                        "publication_status",
                        "transaction_type",
                        "city",
                        "district",
                        "rooms",
                        "total_area_m2",
                        "floor",
                        "floors_total",
                    }.issubset(listing_columns)
                )
                housing_columns = {
                    row[1]
                    for row in connection.execute("PRAGMA table_info(housing)")
                }
                self.assertFalse(
                    {
                        "address",
                        "house_number",
                        "zone",
                        "address_id",
                        "street_id",
                        "house_id",
                        "zone_id",
                        "residential_complex_id",
                        "commission",
                        "repair",
                    }
                    & housing_columns
                )
                street = connection.execute(
                    "SELECT street FROM housing"
                ).fetchone()[0]
                self.assertEqual(
                    street, "Юнусабад, ул. Амира Темура, 15"
                )
                self.assertIn(
                    "listing_amenity_pk",
                    {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(listing_amenities)"
                        )
                    },
                )
                self.assertIn(
                    "listing_nearby_pk",
                    {
                        row[1]
                        for row in connection.execute(
                            "PRAGMA table_info(listing_nearby)"
                        )
                    },
                )
        finally:
            os.remove(database_path)


if __name__ == "__main__":
    unittest.main()
