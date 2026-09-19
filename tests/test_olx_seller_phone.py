import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from core.seller_metadata import (
    extract_olx_phone_from_seller_name,
    extract_uzbek_phones,
    extract_uzbek_phones_from_description,
    extract_uzbek_phones_loose,
)
from tools.refresh_olx_sellers import refresh_olx_sellers
from sources.olx import _extract_user_api_phones
from storage.sqlite_store import save_to_database


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class OlxSellerPhoneTest(unittest.TestCase):
    def test_extracts_international_and_local_phone(self) -> None:
        self.assertEqual(
            extract_olx_phone_from_seller_name(
                "998977051350 на OLX с январь 2023 г. Онлайн вчера"
            ),
            "998977051350",
        )
        self.assertEqual(
            extract_olx_phone_from_seller_name(
                "93 144-07-40 на OLX с сентябрь 2019 г."
            ),
            "998931440740",
        )
        self.assertIsNone(
            extract_olx_phone_from_seller_name(
                "I Home Agency на OLX с январь 2021 г."
            )
        )
        self.assertEqual(
            extract_uzbek_phones(
                "Тел: +998 97 123-45-67, второй 998901112233"
            ),
            ["998971234567", "998901112233"],
        )
        self.assertEqual(
            extract_uzbek_phones_loose(
                r'phone="\u002b998<span>97</span><b>123</b>-45-67"'
            ),
            ["998971234567"],
        )

    def test_extracts_local_numbers_from_description(self) -> None:
        self.assertEqual(
            extract_uzbek_phones_from_description(
                "Мурожаат учун: 90.1156666, тел: 7123456"
            ),
            ["998901156666", "998717123456"],
        )
        self.assertEqual(
            extract_uzbek_phones_from_description(
                "Площадь 9600000, ID 5277649"
            ),
            [],
        )

    def test_structured_api_returns_every_business_phone(self) -> None:
        responses = [
            FakeResponse(
                {
                    "data": {
                        "business_data": {
                            "phone1": "+998901112233",
                            "phone2": "998 97 444 55 66",
                            "phone3": "+998901112233",
                        }
                    }
                }
            )
        ]
        self.assertEqual(
            _extract_user_api_phones(responses),
            ["998901112233", "998974445566"],
        )

    def test_refresh_preserves_primary_and_foreign_keys(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "test.db"
            with closing(sqlite3.connect(database)) as connection:
                connection.executescript(
                    """
                    PRAGMA foreign_keys = ON;
                    CREATE TABLE sellers (
                        seller_pk INTEGER PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        name TEXT,
                        phone TEXT,
                        phone_source TEXT
                    );
                    CREATE TABLE listings (
                        listing_pk INTEGER PRIMARY KEY,
                        source_name TEXT NOT NULL,
                        seller_pk INTEGER NOT NULL REFERENCES sellers(seller_pk),
                        description TEXT
                    );
                    INSERT INTO sellers VALUES (
                        42, 'olx',
                        '998977051350 на OLX с январь 2023 г. Онлайн вчера',
                        NULL, 'olx_detail_page'
                    );
                    INSERT INTO listings VALUES (7, 'olx', 42, NULL);
                    """
                )

            result = refresh_olx_sellers(database)
            with closing(sqlite3.connect(database)) as connection:
                seller = connection.execute(
                    "SELECT seller_pk, name, phone, phone_source FROM sellers"
                ).fetchone()
                listing_seller_pk = connection.execute(
                    "SELECT seller_pk FROM listings WHERE listing_pk = 7"
                ).fetchone()[0]

            self.assertEqual(
                seller,
                (42, "998977051350", "998977051350", "olx_seller_name"),
            )
            self.assertEqual(listing_seller_pk, 42)
            self.assertEqual(result.listing_links_preserved, 1)

    def test_database_stores_each_phone_as_linked_seller(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "multi.db"
            listing = {
                "source": "olx",
                "id": "abc",
                "url": "https://www.olx.uz/d/obyavlenie/example-IDabc.html",
                "transaction_type": "rent",
                "housing": {},
                "seller": {
                    "name": "Agency",
                    "phone": "998901112233",
                    "phones": ["998901112233", "998974445566"],
                    "phone_source": "olx_user_api",
                },
            }
            save_to_database(
                [listing],
                str(database),
                source_name="olx",
                source_url="https://www.olx.uz",
                listing_limit=1,
            )
            with closing(sqlite3.connect(database)) as connection:
                phones = connection.execute(
                    "SELECT phone FROM sellers ORDER BY seller_pk"
                ).fetchall()
                links = connection.execute(
                    """SELECT s.phone, ls.is_primary
                       FROM listing_sellers AS ls
                       JOIN sellers AS s ON s.seller_pk = ls.seller_pk
                       ORDER BY ls.listing_seller_pk"""
                ).fetchall()
            self.assertEqual(phones, [("998901112233",), ("998974445566",)])
            self.assertEqual(
                links,
                [("998901112233", 1), ("998974445566", 0)],
            )


if __name__ == "__main__":
    unittest.main()
