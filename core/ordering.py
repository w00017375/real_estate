"""Deterministic ordering helpers for canonical listings."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def published_datetime(listing: dict[str, Any]) -> datetime | None:
    """Parse an ISO publication date without replacing an unknown date."""

    value = listing.get("published_at")
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=timezone.utc)
    return result.astimezone(timezone.utc)


def sort_listings_newest_first(
    listings: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return newest publications first and records without a date last."""

    def key(listing: dict[str, Any]) -> tuple[Any, ...]:
        published = published_datetime(listing)
        return (
            published is None,
            -published.timestamp() if published is not None else 0,
        )

    return sorted(listings, key=key)


__all__ = ["published_datetime", "sort_listings_newest_first"]
