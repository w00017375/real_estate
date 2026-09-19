from pathlib import Path
from typing import Any

from core.existing_listings import ExistingListings
from sources.base import ListingSource
from standalone_parsers.uybor_publication_json import (
    DEFAULT_CATALOG_URL,
    _parse_catalog_authenticated,
)


class UyborSource(ListingSource):
    """Uybor с уже сохранённой локальной авторизованной сессией."""

    def __init__(
        self,
        profile_dir: Path | str = Path(".uybor_auth_profile"),
        timeout: float = 30,
        max_scroll_rounds: int | None = None,
        stop_at_first_existing: bool = False,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.timeout = timeout
        self.max_scroll_rounds = max_scroll_rounds
        self.stop_at_first_existing = stop_at_first_existing

    @property
    def name(self) -> str:
        return "uybor"

    @property
    def start_url(self) -> str:
        return DEFAULT_CATALOG_URL

    def collect(
        self,
        limit: int | None,
        existing: ExistingListings | None = None,
    ) -> list[dict[str, Any]]:
        # require_login=False: сохранённый профиль используется без окна,
        # повторного ввода номера и ожидания Enter.
        return _parse_catalog_authenticated(
            self.start_url,
            self.profile_dir,
            limit,
            self.timeout,
            require_login=False,
            existing=existing,
            max_scroll_rounds=self.max_scroll_rounds,
            stop_at_first_existing=self.stop_at_first_existing,
        )
