import os
import sqlite3
import tempfile
import unittest

from sources.realt24 import _canonical_listing, _secondary_name_values
from storage.sqlite_store import save_to_database


class Realt24SourceTest(unittest.TestCase):
    def test_secondary_name_and_source_fields_are_mapped(self) -> None:
        raw = {
            "id": "123",
            "name": "2-комнатная квартира − 45 м², 3/9 этаж",
            "secondaryName": "2-комнатная квартира − 45 м², 3/9 этаж",
            "price": {"usd": 600, "uzs": 7_000_000},
            "publishedAt": "2026-09-01T10:00:00+00:00",
            "description": "Описание",
            "phone": "+998901234567",
            "address": {
                "area": "улица Амира Темура",
                "house": "15",
                "fullAddress": "Ташкент, Юнусабадский район, улица Амира Темура, 15",
                "metro": [{"name": "Юнусабад"}],
                "geoLocation": {"latitude": 41.33, "longitude": 69.29},
            },
            "propertyUser": {
                "firstName": "Иван",
                "lastName": "Петров",
            },
            "imageSets": [
                {"guid": "one", "original": "https://example.test/one.webp"},
                {"guid": "two", "original": "https://example.test/two.webp"},
            ],
        }

        listing = _canonical_listing(raw)

        self.assertEqual(listing["source"], "realt24")
        self.assertEqual(listing["price"], 600)
        self.assertEqual(listing["currency"], "USD")
        self.assertEqual(listing["housing"]["city"], "Ташкент")
        self.assertEqual(listing["housing"]["district"], "Юнусабадский район")
        self.assertEqual(listing["housing"]["rooms"], 2)
        self.assertEqual(listing["housing"]["total_area_m2"], 45.0)
        self.assertEqual(listing["housing"]["floor"], 3)
        self.assertEqual(listing["housing"]["floors_total"], 9)
        self.assertEqual(listing["nearby"], ["Метро"])
        self.assertEqual(len(listing["images"]), 1)
        self.assertEqual(listing["images"][0]["source_image_id"], "one")

    def test_realt24_seller_is_stored_once_per_publication(self) -> None:
        base = {
            "source": "realt24",
            "id": "r1",
            "title": "Объект",
            "price": 500,
            "currency": "USD",
            "transaction_type": "rent",
            "url": "https://realt24.uz/ru/listing/r1/",
            "published_at": "2026-09-01T10:00:00+00:00",
            "description_length": 10,
            "amenities": [],
            "nearby": [],
            "images": [],
            "seller": {
                "name": "Продавец",
                "phone": "+998901234567",
                "seller_role": None,
                "seller_type": None,
            },
        }
        listings = []
        for index in range(3):
            item = dict(base)
            item["id"] = f"r{index}"
            item["url"] = f"https://realt24.uz/ru/listing/r{index}/"
            item["housing"] = {
                "city": "Ташкент",
                "district": f"Район {index}",
                "street": f"Улица {index}",
                "rooms": index + 1,
                "total_area_m2": 40 + index,
                "floor": 2,
                "floors_total": 9,
                "monthly_rent": 500,
                "rent_currency": "USD",
            }
            listings.append(item)

        database_handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(database_handle)
        try:
            output = save_to_database(
                listings,
                database_path,
                source_name="all_sources",
                source_url="https://realt24.uz/ru/snyat/kvartiry/",
                listing_limit=3,
            )
            self.assertEqual(output["listings_saved"], 3)
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0],
                    3,
                )
                identities = {
                    row[0]
                    for row in connection.execute(
                        "SELECT identity_key FROM sellers"
                    )
                }
                self.assertEqual(
                    identities,
                    {
                        f"https://realt24.uz/ru/listing/r{index}/"
                        for index in range(3)
                    },
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sellers WHERE seller_status IS NULL"
                    ).fetchone()[0],
                    3,
                )
            finally:
                connection.close()
        finally:
            os.remove(database_path)

    def test_secondary_name_without_expected_formula_returns_nulls(self) -> None:
        self.assertEqual(
            _secondary_name_values("Аренда квартиры"),
            {
                "rooms": None,
                "total_area_m2": None,
                "floor": None,
                "floors_total": None,
            },
        )


if __name__ == "__main__":
    unittest.main()
