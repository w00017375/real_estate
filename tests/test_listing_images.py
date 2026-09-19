import sqlite3
import unittest

from storage.sqlite_store import (
    _enforce_single_listing_image,
    _save_images,
    create_schema,
)


class ListingImagesTest(unittest.TestCase):
    def test_only_first_valid_image_is_saved(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            create_schema(connection)
            connection.execute(
                "INSERT INTO sellers(identity_key, updated_at) VALUES (?, ?)",
                ("seller:test", "2026-09-01 12:00:00"),
            )
            connection.execute(
                """INSERT INTO listings(
                       source_name, seller_pk, url, first_seen_at
                   ) VALUES (?, ?, ?, ?)""",
                ("test", 1, "https://example.test/listing/1", "2026-09-01 12:00:00"),
            )

            _save_images(
                connection.cursor(),
                1,
                "test",
                [
                    {"url": "not-a-url"},
                    {
                        "url": "https://example.test/first.jpg",
                        "source_image_id": "first",
                        "sort_order": 4,
                    },
                    {"url": "https://example.test/second.jpg"},
                ],
                "2026-09-01 12:00:00",
            )

            rows = connection.execute(
                """SELECT image_url, source_image_id, sort_order, is_primary
                   FROM listing_images WHERE listing_pk = 1"""
            ).fetchall()
            self.assertEqual(
                rows,
                [("https://example.test/first.jpg", "first", 0, 1)],
            )
        finally:
            connection.close()

    def test_legacy_rows_are_reduced_to_lowest_sort_order(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute(
                """CREATE TABLE listing_images(
                       image_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                       listing_pk INTEGER NOT NULL,
                       sort_order INTEGER NOT NULL,
                       is_primary INTEGER NOT NULL
                   )"""
            )
            connection.executemany(
                """INSERT INTO listing_images(
                       listing_pk, sort_order, is_primary
                   ) VALUES (?, ?, ?)""",
                [(10, 2, 0), (10, 0, 1), (10, 1, 0), (11, 3, 0)],
            )

            _enforce_single_listing_image(connection)

            rows = connection.execute(
                """SELECT listing_pk, sort_order, is_primary
                   FROM listing_images ORDER BY listing_pk"""
            ).fetchall()
            self.assertEqual(rows, [(10, 0, 1), (11, 0, 1)])
            with self.assertRaises(sqlite3.IntegrityError):
                connection.execute(
                    """INSERT INTO listing_images(
                           listing_pk, sort_order, is_primary
                       ) VALUES (?, ?, ?)""",
                    (10, 1, 0),
                )
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main()
