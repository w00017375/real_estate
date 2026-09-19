"""Daily availability check followed by new-listing and change detection."""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import html as html_module
import re
import sqlite3
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from core.existing_listings import (
    EMPTY_EXISTING_LISTINGS,
    ExistingListings,
    load_existing_listings,
)
from core.ordering import sort_listings_newest_first
from core.pipeline import process_listings
from sources.base import ListingSource
from storage.sqlite_store import create_schema, database_datetime, save_to_database


DEFAULT_DATABASE = Path("olx_apartments.db")
DEFAULT_AVAILABILITY_WORKERS = 8
DEFAULT_REQUEST_TIMEOUT = 15.0
SUPPORTED_SOURCES = ("olx", "etagi", "uybor", "realting", "realt24")
_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
_CLOSED_MARKERS: dict[str, tuple[str, ...]] = {
    "olx": (
        "объявление не активно",
        "объявление удалено",
        "это объявление больше не доступно",
        "this ad is no longer available",
        "e'lon faol emas",
    ),
    "etagi": ("объект снят", "объявление снято", "объект не найден"),
    "uybor": ("объявление не найдено", "listing not found"),
    "realting": (
        "property not found",
        "объект не найден",
        "объявление недоступно",
    ),
    "realt24": ("объявление не найдено", "объект не найден"),
}


class ListingNotifier(Protocol):
    """Extension point for terminal now and Telegram later."""

    def notify(self, listing: dict[str, Any]) -> None: ...


class TerminalNotifier:
    def notify(self, listing: dict[str, Any]) -> None:
        housing = listing.get("housing") or {}
        details = []
        if housing.get("rooms") is not None:
            details.append(f"{housing['rooms']} комн.")
        if housing.get("total_area_m2") is not None:
            details.append(f"{housing['total_area_m2']:g} м²")
        location = ", ".join(
            str(value)
            for value in (housing.get("city"), housing.get("district"))
            if value
        )
        price = " ".join(
            str(value)
            for value in (listing.get("price"), listing.get("currency"))
            if value not in (None, "")
        )
        print("\n" + "=" * 80)
        print(f"НОВОЕ ОБЪЯВЛЕНИЕ [{listing.get('source', 'unknown').upper()}]")
        print("Заголовок:", listing.get("title") or "Без заголовка")
        if price:
            print("Цена:", price)
        if details:
            print("Жильё:", " · ".join(details))
        if location:
            print("Локация:", location)
        print("URL:", listing.get("url") or "не указан")
        print("Обнаружено:", datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"))
        print("=" * 80, flush=True)


@dataclass(frozen=True)
class StoredListing:
    listing_pk: int
    source_name: str
    url: str


AvailabilityState = Literal["active", "closed", "unknown"]


@dataclass(frozen=True)
class AvailabilityCheck:
    listing: StoredListing
    state: AvailabilityState
    reason: str
    http_status: int | None = None


@dataclass(frozen=True)
class AvailabilitySummary:
    checked: int
    active: int
    closed: int
    unknown: int
    elapsed_seconds: float


def _load_active_listings(database: Path) -> list[StoredListing]:
    if not database.exists():
        return []
    connection = sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listings'"
        ).fetchone()
        if not table:
            return []
        columns = {row[1] for row in connection.execute("PRAGMA table_info(listings)")}
        required = {"listing_pk", "source_name", "url", "publication_status"}
        if not required <= columns:
            return []
        rows = connection.execute(
            """SELECT listing_pk, source_name, url
               FROM listings
               WHERE publication_status = 'active'
                 AND url IS NOT NULL AND TRIM(url) <> ''
               ORDER BY source_name, listing_pk"""
        ).fetchall()
    finally:
        connection.close()
    return [StoredListing(int(pk), str(source).casefold(), str(url)) for pk, source, url in rows]


def _interleave_sources(listings: list[StoredListing]) -> list[StoredListing]:
    """Avoid sending one large uninterrupted request burst to the same host."""

    groups: dict[str, list[StoredListing]] = {}
    for listing in listings:
        groups.setdefault(listing.source_name, []).append(listing)
    result: list[StoredListing] = []
    while groups:
        for source in tuple(groups):
            bucket = groups[source]
            result.append(bucket.pop())
            if not bucket:
                del groups[source]
    return result


def _classify_response(
    source_name: str, status: int, html: str
) -> tuple[AvailabilityState, str]:
    if status in (404, 410):
        return "closed", f"HTTP {status}"
    if status in (401, 403, 429) or status >= 500:
        return "unknown", f"временный/защищённый ответ HTTP {status}"
    # Do not inspect JavaScript bundles: they often contain every translated
    # UI message, including the phrase used for a closed listing.
    without_code = re.sub(
        r"<(?:script|style)\b[^>]*>.*?</(?:script|style)>",
        " ",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    lowered = " ".join(
        html_module.unescape(re.sub(r"<[^>]+>", " ", without_code))
        .casefold()
        .split()
    )
    for marker in _CLOSED_MARKERS.get(source_name.casefold(), ()):
        if marker in lowered:
            return "closed", f"маркер закрытой публикации: {marker}"
    if 200 <= status < 400:
        return "active", f"HTTP {status}"
    return "unknown", f"неоднозначный HTTP {status}"


def _check_listing(listing: StoredListing, timeout: float) -> AvailabilityCheck:
    request = Request(
        listing.url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(response.getcode() or 200)
            html = response.read(1_048_576).decode("utf-8", errors="ignore")
    except HTTPError as error:
        status = int(error.code)
        try:
            html = error.read(262_144).decode("utf-8", errors="ignore")
        except Exception:
            html = ""
    except (URLError, TimeoutError, OSError) as error:
        return AvailabilityCheck(listing, "unknown", str(error), None)
    state, reason = _classify_response(listing.source_name, status, html)
    return AvailabilityCheck(listing, state, reason, status)


def _mark_closed(database: Path, listing_pks: list[int]) -> None:
    """Backward-compatible direct close helper used by scripts and tests."""

    if not listing_pks:
        return
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(listings)")
        }
        assignments = ["publication_status = 'closed'"]
        checked_at = database_datetime(datetime.now().astimezone())
        if "last_checked_at" in columns:
            assignments.append("last_checked_at = ?")
        if "last_changed_at" in columns:
            assignments.append("last_changed_at = ?")
        parameters = [checked_at] * (len(assignments) - 1)
        connection.executemany(
            f"UPDATE listings SET {', '.join(assignments)} WHERE listing_pk = ?",
            ((*parameters, listing_pk) for listing_pk in listing_pks),
        )
        connection.commit()
    finally:
        connection.close()


def _record_availability_results(
    database: Path,
    results: list[AvailabilityCheck],
) -> None:
    """Persist checks, including status transitions, without losing history."""

    if not results:
        return
    checked_at = database_datetime(datetime.now().astimezone())
    if checked_at is None:
        return
    connection = sqlite3.connect(database, timeout=30)
    try:
        connection.execute("PRAGMA busy_timeout = 30000")
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(listings)")
        }
        has_history = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='listing_history'"
        ).fetchone() is not None
        for result in results:
            listing_pk = result.listing.listing_pk
            row = connection.execute(
                """SELECT publication_status,
                          COALESCE(current_price, price), currency, description
                   FROM listings WHERE listing_pk = ?""",
                (listing_pk,),
            ).fetchone()
            if row is None:
                continue
            old_status, price, currency, description = row
            assignments: list[str] = []
            parameters: list[Any] = []
            if "last_checked_at" in columns:
                assignments.append("last_checked_at = ?")
                parameters.append(checked_at)
            if result.state == "active" and "last_seen_at" in columns:
                assignments.append("last_seen_at = ?")
                parameters.append(checked_at)
            status_changed = result.state in {"active", "closed"} and old_status != result.state
            if result.state in {"active", "closed"}:
                assignments.append("publication_status = ?")
                parameters.append(result.state)
            if status_changed and "last_changed_at" in columns:
                assignments.append("last_changed_at = ?")
                parameters.append(checked_at)
            if assignments:
                connection.execute(
                    f"UPDATE listings SET {', '.join(assignments)} WHERE listing_pk = ?",
                    (*parameters, listing_pk),
                )
            if status_changed and has_history:
                connection.execute(
                    """INSERT INTO listing_history(
                           listing_pk, recorded_at, price, currency, description,
                           publication_status, change_fields
                       ) VALUES (?, ?, ?, ?, ?, ?, 'status')""",
                    (
                        listing_pk,
                        checked_at,
                        price,
                        currency,
                        description,
                        result.state,
                    ),
                )
        connection.commit()
    finally:
        connection.close()


def check_stored_publications(
    database: Path,
    *,
    workers: int = DEFAULT_AVAILABILITY_WORKERS,
    timeout: float = DEFAULT_REQUEST_TIMEOUT,
) -> AvailabilitySummary:
    started = time.perf_counter()
    if database.exists():
        connection = sqlite3.connect(database, timeout=30)
        try:
            create_schema(connection)
        finally:
            connection.close()
    listings = _interleave_sources(_load_active_listings(database))
    if not listings:
        return AvailabilitySummary(0, 0, 0, 0, time.perf_counter() - started)

    print(f"Активных URL для проверки: {len(listings)}; потоков: {workers}")
    results: list[AvailabilityCheck] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
        future_to_listing = {
            executor.submit(_check_listing, listing, timeout): listing
            for listing in listings
        }
        for completed, future in enumerate(
            concurrent.futures.as_completed(future_to_listing), start=1
        ):
            listing = future_to_listing[future]
            try:
                result = future.result()
            except Exception as error:
                result = AvailabilityCheck(listing, "unknown", str(error))
            results.append(result)
            if result.state == "closed":
                print(f"CLOSED [{listing.source_name}] {listing.url} ({result.reason})")
            if completed % 100 == 0 or completed == len(listings):
                print(f"Проверено актуальности: {completed}/{len(listings)}", flush=True)

    closed_ids = [item.listing.listing_pk for item in results if item.state == "closed"]
    _record_availability_results(database, results)
    elapsed = time.perf_counter() - started
    return AvailabilitySummary(
        checked=len(results),
        active=sum(item.state == "active" for item in results),
        closed=len(closed_ids),
        unknown=sum(item.state == "unknown" for item in results),
        elapsed_seconds=elapsed,
    )


def build_sources(names: list[str]) -> list[ListingSource]:
    """Create newest-first adapters that also reopen known publication cards."""

    from sources.etagi import EtagiSource
    from sources.olx import OlxConfig, OlxSource
    from sources.realt24 import Realt24Source
    from sources.realting import RealtingSource
    from sources.uybor import UyborSource

    available: dict[str, ListingSource] = {
        "olx": OlxSource(
            OlxConfig(stop_at_first_existing=False, newest_first=True)
        ),
        "etagi": EtagiSource(stop_at_first_existing=False),
        "uybor": UyborSource(stop_at_first_existing=False),
        "realting": RealtingSource(stop_at_first_existing=False),
        "realt24": Realt24Source(stop_at_first_existing=False),
    }
    return [available[name] for name in names]


async def _collect_one(
    source: ListingSource,
    limit: int | None,
    existing: ExistingListings,
) -> tuple[ListingSource, list[dict[str, Any]], str | None]:
    try:
        listings = await asyncio.to_thread(source.collect, limit, existing)
        return source, listings, None
    except Exception as error:
        return source, [], str(error)


async def _collect_new_parallel(
    sources: list[ListingSource],
    limit: int | None,
    existing_by_source: dict[str, ExistingListings],
) -> list[tuple[ListingSource, list[dict[str, Any]], str | None]]:
    return await asyncio.gather(
        *(
            _collect_one(
                source,
                limit,
                existing_by_source.get(source.name.casefold(), EMPTY_EXISTING_LISTINGS),
            )
            for source in sources
        )
    )


def scan_new_publications(
    sources: list[ListingSource],
    database: Path,
    notifier: ListingNotifier,
    *,
    limit: int | None = None,
) -> tuple[int, dict[str, str], float]:
    started = time.perf_counter()
    existing = load_existing_listings(database)
    # Passing an empty index is intentional: known cards must be reopened so
    # changes in price, description, housing data and status can be recorded.
    results = asyncio.run(_collect_new_parallel(sources, limit, {}))
    raw: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    for source, listings, error in results:
        print(f"{source.name}: карточек получено — {len(listings)}")
        raw.extend(listings)
        if error:
            errors[source.name] = error
            print(f"{source.name}: ошибка — {error}", file=sys.stderr)
    if not raw:
        return 0, errors, time.perf_counter() - started

    normalized = sort_listings_newest_first(process_listings(raw))
    new_listings = [
        listing
        for listing in normalized
        if not existing.get(
            str(listing.get("source") or "unknown").casefold(),
            EMPTY_EXISTING_LISTINGS,
        ).contains(listing.get("id"), listing.get("url"))
    ]
    db_result = save_to_database(
        normalized,
        str(database),
        source_name="daily_scanner",
        source_url=" | ".join(source.start_url for source in sources),
        listing_limit=limit or 0,
        discarded_listings=None,
    )
    saved = int(db_result["listings_saved"])
    for listing in new_listings[:saved]:
        notifier.notify(listing)
    print(
        "Обновление известных: "
        f"изменено {db_result.get('listings_updated', 0)}, "
        f"без изменений {db_result.get('listings_unchanged', 0)}, "
        f"изменений цены {db_result.get('price_changes', 0)}"
    )
    return saved, errors, time.perf_counter() - started


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ежедневно проверить актуальность БД и найти новые объявления"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument(
        "--platforms", nargs="+", choices=SUPPORTED_SOURCES, default=list(SUPPORTED_SOURCES)
    )
    parser.add_argument(
        "--new-limit",
        type=int,
        default=None,
        help="Тестовый максимум новых объявлений с источника; по умолчанию без лимита",
    )
    parser.add_argument(
        "--availability-workers", type=int, default=DEFAULT_AVAILABILITY_WORKERS
    )
    parser.add_argument("--request-timeout", type=float, default=DEFAULT_REQUEST_TIMEOUT)
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.new_limit is not None and args.new_limit <= 0:
        raise SystemExit("--new-limit должен быть положительным")
    if args.availability_workers <= 0:
        raise SystemExit("--availability-workers должен быть положительным")
    if args.request_timeout <= 0:
        raise SystemExit("--request-timeout должен быть положительным")

    total_started = time.perf_counter()
    print("ЭТАП 1/2 — проверка актуальности сохранённых объявлений")
    availability = check_stored_publications(
        args.database,
        workers=args.availability_workers,
        timeout=args.request_timeout,
    )
    print(
        "Итог актуальности: "
        f"проверено {availability.checked}, active {availability.active}, "
        f"closed {availability.closed}, без изменения {availability.unknown}; "
        f"{availability.elapsed_seconds:.1f} сек."
    )

    print("\nЭТАП 2/2 — поиск новых и обновление известных объявлений")
    saved, errors, new_elapsed = scan_new_publications(
        build_sources(args.platforms),
        args.database,
        TerminalNotifier(),
        limit=args.new_limit,
    )
    print(f"Новых объявлений сохранено: {saved}; этап занял {new_elapsed:.1f} сек.")
    if errors:
        print("Ошибки источников (остальные источники завершены):", errors)
    print(f"Общее время выполнения: {time.perf_counter() - total_started:.1f} сек.")


if __name__ == "__main__":
    main()
