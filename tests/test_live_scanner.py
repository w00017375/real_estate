import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

from live_scanner import (
    AvailabilityCheck,
    StoredListing,
    _classify_response,
    _load_active_listings,
    _mark_closed,
    _record_availability_results,
    build_sources,
    scan_new_publications,
)
from storage.sqlite_store import save_to_database


class FakeNotifier:
    def __init__(self) -> None:
        self.listings = []

    def notify(self, listing):
        self.listings.append(listing)


class FakeSource:
    name = "fake"
    start_url = "https://example.test/catalog"

    def collect(self, limit, existing=None):
        url = "https://example.test/listing/1"
        if existing and existing.contains("1", url):
            return []
        return [
            {
                "source": self.name,
                "id": "1",
                "url": url,
                "title": "Test listing",
                "price": 500,
                "currency": "USD",
                "transaction_type": "rent",
                "housing": {"rooms": 2, "total_area_m2": 50},
                "seller": {"name": "Seller"},
            }
        ]


class DailyScannerTest(unittest.TestCase):
    def test_every_supported_source_can_be_constructed(self) -> None:
        sources = build_sources(["olx", "etagi", "uybor", "realting", "realt24"])
        self.assertEqual(
            [source.name for source in sources],
            ["olx", "etagi", "uybor", "realting", "realt24"],
        )

    def test_response_classifier_closes_only_unambiguous_response(self) -> None:
        self.assertEqual(_classify_response("olx", 404, "")[0], "closed")
        self.assertEqual(
            _classify_response("olx", 200, "Объявление не активно")[0],
            "closed",
        )
        self.assertEqual(_classify_response("olx", 403, "")[0], "unknown")
        self.assertEqual(_classify_response("olx", 200, "normal listing")[0], "active")

    def test_only_active_rows_are_loaded_and_selected_row_is_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "status.db"
            connection = sqlite3.connect(database)
            connection.execute(
                """CREATE TABLE listings (
                    listing_pk INTEGER PRIMARY KEY,
                    source_name TEXT,
                    url TEXT,
                    publication_status TEXT
                )"""
            )
            connection.executemany(
                "INSERT INTO listings VALUES (?, ?, ?, ?)",
                [
                    (1, "olx", "https://example.test/1", "active"),
                    (2, "olx", "https://example.test/2", "closed"),
                ],
            )
            connection.commit()
            connection.close()

            self.assertEqual(
                _load_active_listings(database),
                [StoredListing(1, "olx", "https://example.test/1")],
            )
            _mark_closed(database, [1])
            connection = sqlite3.connect(database)
            try:
                statuses = connection.execute(
                    "SELECT publication_status FROM listings ORDER BY listing_pk"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(statuses, [("closed",), ("closed",)])

    def test_availability_transition_is_recorded_in_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "status-history.db"
            listing = FakeSource().collect(None)[0]
            save_to_database(
                [listing],
                str(database),
                source_name="fake",
                source_url="https://example.test/catalog",
                listing_limit=1,
            )
            stored = StoredListing(1, "fake", listing["url"])

            _record_availability_results(
                database,
                [AvailabilityCheck(stored, "closed", "HTTP 404", 404)],
            )

            connection = sqlite3.connect(database)
            try:
                row = connection.execute(
                    """SELECT publication_status, last_checked_at,
                              last_changed_at FROM listings"""
                ).fetchone()
                changes = connection.execute(
                    "SELECT change_fields FROM listing_history ORDER BY history_pk"
                ).fetchall()
            finally:
                connection.close()
            self.assertEqual(row[0], "closed")
            self.assertTrue(row[1])
            self.assertTrue(row[2])
            self.assertEqual(changes, [("initial",), ("status",)])

    def test_new_listing_is_notified_once_and_unchanged_refresh_is_quiet(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "daily.db"
            source = FakeSource()
            notifier = FakeNotifier()

            with redirect_stdout(StringIO()):
                saved, errors, _ = scan_new_publications(
                    [source], database, notifier, limit=None
                )
            self.assertEqual(saved, 1)
            self.assertEqual(errors, {})
            with redirect_stdout(StringIO()):
                saved, errors, _ = scan_new_publications(
                    [source], database, notifier, limit=None
                )
            self.assertEqual(saved, 0)
            self.assertEqual(errors, {})
            self.assertEqual(len(notifier.listings), 1)


if __name__ == "__main__":
    unittest.main()
