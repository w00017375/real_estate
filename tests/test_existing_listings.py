import os
import sqlite3
import tempfile
import unittest
from contextlib import closing

from core.existing_listings import load_existing_listings, normalize_listing_url


class ExistingListingsTest(unittest.TestCase):
    def test_url_normalization_removes_query_fragment_and_trailing_slash(self) -> None:
        self.assertEqual(
            normalize_listing_url("HTTPS://Example.COM/listings/42/?from=map#top"),
            "https://example.com/listings/42",
        )

    def test_database_index_matches_platform_id_or_url(self) -> None:
        handle, database_path = tempfile.mkstemp(suffix=".db")
        os.close(handle)
        try:
            with closing(sqlite3.connect(database_path)) as connection:
                connection.execute(
                    """CREATE TABLE listings(
                           source_name TEXT,
                           platform_listing_id TEXT,
                           url TEXT
                       )"""
                )
                connection.execute(
                    "INSERT INTO listings VALUES (?, ?, ?)",
                    ("OLX", "abc", "https://www.olx.uz/d/obyavlenie/abc/"),
                )
                connection.commit()

            existing = load_existing_listings(database_path)["olx"]
            self.assertTrue(existing.contains("abc", None))
            self.assertTrue(
                existing.contains(
                    None,
                    "https://www.olx.uz/d/obyavlenie/abc?reason=observed_ad",
                )
            )
            self.assertFalse(existing.contains("new", "https://example.test/new"))
        finally:
            os.remove(database_path)


if __name__ == "__main__":
    unittest.main()
