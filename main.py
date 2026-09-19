import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.existing_listings import (
    EMPTY_EXISTING_LISTINGS,
    ExistingListings,
)
from core.ordering import sort_listings_newest_first
from core.pipeline import process_listings
from sources.base import ListingSource
from storage.json_store import save_json
from storage.sqlite_store import (
    DatabaseLockedError,
    save_to_database,
    wait_for_database_write_access,
)


def run_source(source: ListingSource, config: AppConfig) -> dict[str, int]:
    """Универсальный конвейер для OLX и будущих источников."""

    # Known cards are intentionally parsed again: storage records changes and
    # keeps the original first_seen_at value.
    raw_listings = source.collect(config.max_listings, EMPTY_EXISTING_LISTINGS)
    if not raw_listings:
        save_json([], config.output_json)
        print(f"Объявлений {source.name} не найдено.")
        return {
            "run_pk": 0,
            "listings_saved": 0,
            "listings_updated": 0,
            "listings_unchanged": 0,
            "price_changes": 0,
            "history_rows_added": 0,
            "duplicates_removed": 0,
            "historical_duplicates_removed": 0,
        }
    listings = sort_listings_newest_first(
        process_listings(raw_listings)
    )

    database_result = save_to_database(
        listings,
        config.database_file,
        source_name=source.name,
        source_url=source.start_url,
        listing_limit=config.max_listings or 0,
        discarded_listings=None,
    )
    save_json(listings, config.output_json)

    print("\n" + "=" * 80)
    print("ГОТОВО.")
    print("Источник:", source.name)
    print("Всего объектов:", len(listings))
    print("Классификация и дедупликация продавцов: отложены")
    print("JSON:", config.output_json)
    print("SQLite:", config.database_file)
    print("DB run_pk:", database_result["run_pk"])

    return database_result


def run_all_sources(
    sources: list[ListingSource],
    config: AppConfig,
) -> dict[str, object]:
    """Собирает одинаковый лимит с каждого источника одним запуском."""

    collected_by_source, errors = asyncio.run(
        _collect_sources_parallel(
            sources,
            config.max_listings,
            {},
        )
    )
    source_counts = {
        source.name: len(collected_by_source.get(source.name, []))
        for source in sources
    }
    # gather() возвращает результаты в порядке sources, поэтому при равных
    # датах результат остаётся воспроизводимым, несмотря на параллельный I/O.
    raw_listings = [
        listing
        for source in sources
        for listing in collected_by_source.get(source.name, [])
    ]

    if not raw_listings:
        print("\nОбъявлений для обработки не найдено.")
        if errors:
            print("Ошибки:", errors)
        save_json([], config.output_json)
        return {
            "database": None,
            "source_counts": source_counts,
            "errors": errors,
            "listing_count": 0,
        }

    listings = sort_listings_newest_first(
        process_listings(raw_listings)
    )
    database_result = save_to_database(
        listings,
        config.database_file,
        source_name="all_sources",
        source_url=" | ".join(source.start_url for source in sources),
        listing_limit=(
            config.max_listings * len(sources)
            if config.max_listings is not None
            else 0
        ),
        discarded_listings=None,
    )
    save_json(listings, config.output_json)

    print("\n" + "=" * 80)
    print("ОБЩИЙ ЗАПУСК ЗАВЕРШЁН")
    for name, count in source_counts.items():
        if config.max_listings is None:
            print(f"{name}: {count} карточек, каталог пройден полностью")
        else:
            print(f"{name}: {count}/{config.max_listings}")
    if errors:
        print("Ошибки:", errors)
    print("Новых объявлений:", database_result["listings_saved"])
    print("Обновлено известных:", database_result.get("listings_updated", 0))
    print("Без изменений:", database_result.get("listings_unchanged", 0))
    print("Изменений цены:", database_result.get("price_changes", 0))
    print("JSON:", config.output_json)
    print("SQLite:", config.database_file)
    return {
        "database": database_result,
        "source_counts": source_counts,
        "errors": errors,
        "listing_count": len(listings),
    }


async def _collect_one_source(
    source: ListingSource,
    limit: int | None,
    existing: ExistingListings,
) -> tuple[str, list[dict[str, Any]], str | None]:
    """Run one blocking parser in its own worker thread."""

    print("\n" + "=" * 80)
    limit_label = limit if limit is not None else "без ограничений"
    print(f"ИСТОЧНИК: {source.name}; лимит: {limit_label}")
    try:
        collected = await asyncio.to_thread(source.collect, limit, existing)
        return source.name, collected, None
    except Exception as error:
        print(f"Ошибка источника {source.name}: {error}")
        return source.name, [], str(error)


async def _collect_sources_parallel(
    sources: list[ListingSource],
    limit: int | None,
    existing_by_source: dict[str, ExistingListings] | None = None,
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Collect independent websites concurrently, one task per source."""

    results = await asyncio.gather(
        *(
            _collect_one_source(
                source,
                limit,
                (existing_by_source or {}).get(
                    source.name.casefold(), EMPTY_EXISTING_LISTINGS
                ),
            )
            for source in sources
        )
    )
    listings_by_source: dict[str, list[dict[str, Any]]] = {}
    errors: dict[str, str] = {}
    for name, listings, error in results:
        listings_by_source[name] = listings
        if error:
            errors[name] = error
    return listings_by_source, errors


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Собрать объявления OLX, Etagi, Uybor, Realting и Realt24 "
            "в общую SQLite-базу"
        )
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help=(
            "Количество новых объявлений с каждого источника. "
            "Если параметр не указан, каталог обходится полностью"
        ),
    )
    parser.add_argument(
        "--reset-db",
        action="store_true",
        help="Перед запуском полностью удалить текущий файл SQLite",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("Лимит объявлений должен быть положительным числом")

    started = time.perf_counter()
    try:
        from sources.olx import OlxSource
        from sources.etagi import EtagiSource
        from sources.uybor import UyborSource
        from sources.realting import RealtingSource
        from sources.realt24 import Realt24Source

        config = AppConfig(max_listings=args.limit)
        database_path = Path(config.database_file).resolve()
        if database_path.exists():
            try:
                wait_for_database_write_access(str(database_path))
            except DatabaseLockedError as error:
                raise SystemExit(str(error)) from None
        if args.reset_db and database_path.exists():
            database_path.unlink()
            print(f"SQLite полностью очищена: {database_path}")

        run_all_sources(
            [
                OlxSource(),
                EtagiSource(),
                UyborSource(),
                RealtingSource(),
                Realt24Source(),
            ],
            config,
        )
    finally:
        elapsed = time.perf_counter() - started
        print(f"\nВремя выполнения: {elapsed:.2f} сек.")


if __name__ == "__main__":
    main()
