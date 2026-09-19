"""Autonomous JSON-only launcher for selected integrated platforms.

Examples:
    python parse_platform.py olx --limit 3
    python parse_platform.py etagi uybor realting --limit 10
    python parse_platform.py all --limit 3

Unlike ``main.py``, this command does not read or modify SQLite. Every source
is written into its own directory under ``output_json``.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path
from typing import Any, Callable

from core.existing_listings import EMPTY_EXISTING_LISTINGS
from core.ordering import sort_listings_newest_first
from core.pipeline import process_listings
from sources.base import ListingSource
from storage.json_store import save_json


PLATFORM_NAMES = ("olx", "etagi", "uybor", "realting", "realt24")


def _source_factories() -> dict[str, Callable[[], ListingSource]]:
    from sources.etagi import EtagiSource
    from sources.olx import OlxSource
    from sources.realt24 import Realt24Source
    from sources.realting import RealtingSource
    from sources.uybor import UyborSource

    return {
        "olx": OlxSource,
        "etagi": EtagiSource,
        "uybor": UyborSource,
        "realting": RealtingSource,
        "realt24": Realt24Source,
    }


async def _collect(
    source: ListingSource,
    limit: int | None,
) -> tuple[str, list[dict[str, Any]], str | None]:
    try:
        rows = await asyncio.to_thread(
            source.collect, limit, EMPTY_EXISTING_LISTINGS
        )
        return source.name, rows, None
    except Exception as error:
        return source.name, [], str(error)


async def _collect_all(
    sources: list[ListingSource],
    limit: int | None,
) -> list[tuple[str, list[dict[str, Any]], str | None]]:
    return await asyncio.gather(*(_collect(source, limit) for source in sources))


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Автономно собрать JSON только с выбранных платформ, без SQLite"
        )
    )
    parser.add_argument(
        "platforms",
        nargs="+",
        choices=(*PLATFORM_NAMES, "all"),
        help="Одна или несколько платформ либо all",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Лимит на платформу; без параметра обходится весь каталог",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output_json"),
        help="Корневая папка JSON (по умолчанию: output_json)",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("Лимит должен быть положительным числом")

    selected = (
        list(PLATFORM_NAMES)
        if "all" in args.platforms
        else list(dict.fromkeys(args.platforms))
    )
    factories = _source_factories()
    sources = [factories[name]() for name in selected]
    started = time.perf_counter()
    results = asyncio.run(_collect_all(sources, args.limit))
    failures = 0
    for name, raw_rows, error in results:
        if error:
            failures += 1
            print(f"{name}: ошибка — {error}")
            continue
        rows = sort_listings_newest_first(process_listings(raw_rows))
        output = args.output_dir / name / f"{name}_listings.json"
        save_json(rows, str(output))
        print(f"{name}: {len(rows)} объектов → {output.resolve()}")
    print(f"Время выполнения: {time.perf_counter() - started:.2f} сек.")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
