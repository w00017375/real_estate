from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit


def normalize_listing_url(value: Any) -> str | None:
    """Return a stable URL key without query, fragment or trailing slash."""

    text = str(value or "").strip()
    if not text:
        return None
    parsed = urlsplit(text)
    if not parsed.scheme or not parsed.netloc:
        return text.split("?", 1)[0].split("#", 1)[0].rstrip("/")
    return urlunsplit(
        (
            parsed.scheme.casefold(),
            parsed.netloc.casefold(),
            parsed.path.rstrip("/"),
            "",
            "",
        )
    )


@dataclass(frozen=True)
class ExistingListings:
    """Identifiers already persisted for one marketplace."""

    platform_ids: frozenset[str] = frozenset()
    urls: frozenset[str] = frozenset()

    def contains(self, platform_id: Any = None, url: Any = None) -> bool:
        normalized_id = str(platform_id).strip() if platform_id is not None else None
        normalized_url = normalize_listing_url(url)
        return bool(
            (normalized_id and normalized_id in self.platform_ids)
            or (normalized_url and normalized_url in self.urls)
        )


EMPTY_EXISTING_LISTINGS = ExistingListings()


def load_existing_listings(database_file: str | Path) -> dict[str, ExistingListings]:
    """Read existing listing keys without changing the SQLite database."""

    database = Path(database_file)
    if not database.exists():
        return {}

    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listings'"
        ).fetchone()
        if not table_exists:
            return {}
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(listings)")
        }
        if "source_name" not in columns:
            return {}
        id_expression = (
            "platform_listing_id" if "platform_listing_id" in columns else "NULL"
        )
        url_expression = "url" if "url" in columns else "NULL"
        rows = connection.execute(
            f"SELECT source_name, {id_expression}, {url_expression} FROM listings"
        ).fetchall()
    finally:
        connection.close()

    grouped: dict[str, dict[str, set[str]]] = {}
    for source_name, platform_id, url in rows:
        source = str(source_name or "unknown").strip().casefold()
        bucket = grouped.setdefault(source, {"ids": set(), "urls": set()})
        if platform_id is not None and str(platform_id).strip():
            bucket["ids"].add(str(platform_id).strip())
        if normalized_url := normalize_listing_url(url):
            bucket["urls"].add(normalized_url)

    return {
        source: ExistingListings(
            platform_ids=frozenset(values["ids"]),
            urls=frozenset(values["urls"]),
        )
        for source, values in grouped.items()
    }
