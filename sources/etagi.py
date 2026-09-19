from typing import Any

from core.existing_listings import ExistingListings
from standalone_parsers.etagi_publication_json import (
    DEFAULT_URL,
    parse_collection,
)
from sources.base import ListingSource


class EtagiSource(ListingSource):
    """Адаптер каталога долгосрочной аренды Etagi для общего конвейера."""

    def __init__(
        self,
        *,
        max_pages: int | None = None,
        stop_at_first_existing: bool = False,
    ) -> None:
        self.max_pages = max_pages
        self.stop_at_first_existing = stop_at_first_existing

    @property
    def name(self) -> str:
        return "etagi"

    @property
    def start_url(self) -> str:
        return DEFAULT_URL

    def collect(
        self,
        limit: int | None,
        existing: ExistingListings | None = None,
    ) -> list[dict[str, Any]]:
        return parse_collection(
            self.start_url,
            limit=limit,
            existing=existing,
            max_pages=self.max_pages,
            stop_at_first_existing=self.stop_at_first_existing,
        )
