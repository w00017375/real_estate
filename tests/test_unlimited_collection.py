import io
import sys
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

import main as main_module
from core.config import AppConfig
from core.existing_listings import ExistingListings
from standalone_parsers.etagi_publication_json import DEFAULT_URL, parse_collection
from sources.realt24 import Realt24Source


def _realt24_item(listing_id: int) -> dict:
    return {
        "id": listing_id,
        "secondaryName": "1-комнатная квартира − 40 м², 2/5 этаж",
        "price": {"usd": 300},
        "address": {},
    }


class UnlimitedCollectionTest(unittest.TestCase):
    def test_cli_has_no_implicit_limit(self) -> None:
        with patch.object(sys, "argv", ["main.py"]):
            self.assertIsNone(main_module._arguments().limit)
        with patch.object(sys, "argv", ["main.py", "--limit", "7"]):
            self.assertEqual(main_module._arguments().limit, 7)
        self.assertIsNone(AppConfig().max_listings)

    def test_realt24_unlimited_walks_pages_and_skips_existing(self) -> None:
        visited: list[str] = []

        def download(url: str, timeout: float) -> str:
            visited.append(url)
            return url

        def properties(page_url: str) -> list[dict]:
            if "page=2" in page_url:
                return [_realt24_item(3)]
            if "page=3" in page_url:
                return []
            return [_realt24_item(1), _realt24_item(2)]

        existing = ExistingListings(platform_ids=frozenset({"1"}))
        with (
            patch("sources.realt24._download_html", side_effect=download),
            patch("sources.realt24._next_data", side_effect=lambda value: value),
            patch("sources.realt24._raw_properties", side_effect=properties),
        ):
            with redirect_stdout(io.StringIO()):
                listings = Realt24Source().collect(None, existing)

        self.assertEqual([item["id"] for item in listings], ["2", "3"])
        self.assertEqual(len(visited), 2)
        self.assertIn("page=2", visited[1])

    def test_realt24_explicit_limit_stops_early(self) -> None:
        visited: list[str] = []

        def download(url: str, timeout: float) -> str:
            visited.append(url)
            return url

        with (
            patch("sources.realt24._download_html", side_effect=download),
            patch("sources.realt24._next_data", side_effect=lambda value: value),
            patch(
                "sources.realt24._raw_properties",
                return_value=[_realt24_item(1), _realt24_item(2)],
            ),
        ):
            listings = Realt24Source().collect(1)

        self.assertEqual([item["id"] for item in listings], ["1"])
        self.assertEqual(len(visited), 1)

    def test_etagi_unlimited_stops_after_empty_page(self) -> None:
        def page_urls(state: str, page_url: str) -> list[str]:
            if "page=1" in page_url:
                return [
                    "https://tashkent.etagi.com/realty_rent/1/",
                    "https://tashkent.etagi.com/realty_rent/2/",
                ]
            if "page=2" in page_url:
                return ["https://tashkent.etagi.com/realty_rent/3/"]
            return []

        existing = ExistingListings(platform_ids=frozenset({"1"}))
        with (
            patch(
                "standalone_parsers.etagi_publication_json._download_html",
                side_effect=lambda url, timeout: (url, url, 200),
            ),
            patch(
                "standalone_parsers.etagi_publication_json._extract_page_state",
                side_effect=lambda html: html,
            ),
            patch(
                "standalone_parsers.etagi_publication_json._collection_listing_urls",
                side_effect=page_urls,
            ),
            patch(
                "standalone_parsers.etagi_publication_json.parse_publication",
                side_effect=lambda url, timeout: {"url": url},
            ),
        ):
            with redirect_stdout(io.StringIO()):
                listings = parse_collection(DEFAULT_URL, None, existing=existing)

        self.assertEqual(
            [item["url"] for item in listings],
            [
                "https://tashkent.etagi.com/realty_rent/2/",
                "https://tashkent.etagi.com/realty_rent/3/",
            ],
        )


if __name__ == "__main__":
    unittest.main()
