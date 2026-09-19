import os
import sqlite3
import tempfile
import unittest

from seller_deduplication import deduplicate_sellers, normalize_phone
from storage.sqlite_store import create_schema


class SellerDeduplicationTest(unittest.TestCase):
    def test_phone_normalization_keeps_ascii_digits_only(self) -> None:
        self.assertEqual(normalize_phone("+998 (90) 123-45-67"), "998901234567")
        self.assertEqual(normalize_phone(998901234567), "998901234567")
        self.assertEqual(normalize_phone("телефон отсутствует"), "")
        self.assertIsNone(normalize_phone(None))

    def test_full_duplicates_are_relinked_and_phone_snapshot_is_unique(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(database_path)
            create_schema(connection)
            seller_values = [
                (
                    "listing:1", "olx", "https://olx.test/user/a", "Seller A",
                    "+998 (90) 123-45-67", "2026-09-01 10:00:00",
                ),
                # Полный бизнес-дубль первой строки. Технические поля
                # identity_key и updated_at намеренно отличаются.
                (
                    "listing:2", "olx", "https://olx.test/user/a", "Seller A",
                    "998901234567", "2026-09-02 10:00:00",
                ),
                # Тот же номер, но другая платформа — строка сохраняется.
                (
                    "listing:3", "uybor", None, "Seller A",
                    "998 90 123 45 67", "2026-09-03 10:00:00",
                ),
                # Та же комбинация номера, платформы и URL. Имя не входит
                # в бизнес-ключ, поэтому эта строка тоже удаляется.
                (
                    "listing:4", "olx", "https://olx.test/user/a", "Seller B",
                    "+998901234567", "2026-09-04 10:00:00",
                ),
                (
                    "listing:5", "olx", None, "No phone", None,
                    "2026-09-05 10:00:00",
                ),
                # Даже полностью совпадающие строки без телефона неприкосновенны.
                (
                    "listing:6", "olx", None, "No phone", None,
                    "2026-09-06 10:00:00",
                ),
            ]
            connection.executemany(
                """INSERT INTO sellers (
                       identity_key, source_name, profile_url, name,
                       phone, updated_at
                   ) VALUES (?, ?, ?, ?, ?, ?)""",
                seller_values,
            )
            connection.executemany(
                """INSERT INTO listings (
                       platform_listing_id, source_name, seller_pk,
                       url, first_seen_at
                   ) VALUES (?, 'olx', ?, ?, '2026-09-05 10:00:00')""",
                [
                    ("1", 1, "https://olx.test/listing/1"),
                    ("2", 2, "https://olx.test/listing/2"),
                    ("3", 3, "https://uybor.test/listing/3"),
                ],
            )
            connection.commit()
            connection.close()

            result = deduplicate_sellers(database_path)

            self.assertEqual(result.phones_normalized, 3)
            self.assertEqual(result.full_duplicates_removed, 2)
            self.assertEqual(result.listings_relinked, 1)
            self.assertEqual(result.duplicate_phones, 1)
            self.assertEqual(result.seller_duplicate_rows, 2)
            self.assertEqual(result.realtor_phones, 1)
            self.assertEqual(result.owner_phones, 0)

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0],
                    4,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT COUNT(*) FROM sellers WHERE phone IS NULL"
                    ).fetchone()[0],
                    2,
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT seller_pk FROM listings WHERE platform_listing_id = '2'"
                    ).fetchone()[0],
                    1,
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT DISTINCT seller_status, phone_listing_count
                           FROM sellers WHERE phone = '998901234567'"""
                    ).fetchall(),
                    [("realtor", 3)],
                )
                self.assertIsNone(
                    connection.execute(
                        "SELECT phone FROM sellers WHERE identity_key = 'listing:5'"
                    ).fetchone()[0]
                )
                self.assertEqual(
                    connection.execute(
                        """SELECT source_name, seller_url, occurrences
                           FROM seller_duplicates ORDER BY source_name"""
                    ).fetchall(),
                    [
                        ("olx", "https://olx.test/user/a", 1),
                        ("uybor", "", 1),
                    ],
                )
                self.assertFalse(connection.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                connection.close()
        finally:
            os.remove(database_path)

    def test_null_profile_url_is_part_of_duplicate_signature(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(database_path)
            create_schema(connection)
            connection.executemany(
                """INSERT INTO sellers (
                       identity_key, source_name, profile_url, name,
                       phone, updated_at
                   ) VALUES (?, 'uybor', NULL, ?, ?, ?)""",
                [
                    ("listing:1", "First name", "+998 90 111 22 33", "2026-09-01 10:00:00"),
                    ("listing:2", "Other name", "998901112233", "2026-09-02 10:00:00"),
                ],
            )
            connection.commit()
            connection.close()

            result = deduplicate_sellers(database_path)

            self.assertEqual(result.full_duplicates_removed, 1)
            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT COUNT(*) FROM sellers").fetchone()[0],
                    1,
                )
            finally:
                connection.close()
        finally:
            os.remove(database_path)

    def test_dry_run_rolls_back_schema_and_data_changes(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            connection = sqlite3.connect(database_path)
            create_schema(connection)
            connection.execute(
                """INSERT INTO sellers (
                       identity_key, source_name, phone, updated_at
                   ) VALUES ('listing:1', 'olx', '+998 90', '2026-09-01 10:00:00')"""
            )
            connection.commit()
            connection.close()

            deduplicate_sellers(database_path, dry_run=True)

            connection = sqlite3.connect(database_path)
            try:
                self.assertEqual(
                    connection.execute("SELECT phone FROM sellers").fetchone()[0],
                    "+998 90",
                )
                self.assertIsNone(
                    connection.execute(
                        """SELECT 1 FROM sqlite_master
                           WHERE type = 'table' AND name = 'seller_duplicates'"""
                    ).fetchone()
                )
            finally:
                connection.close()
        finally:
            os.remove(database_path)


if __name__ == "__main__":
    unittest.main()
