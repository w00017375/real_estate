"""Standalone raw JSON collector for rental housing on Uysot.uz.

The file is independent from the main estate-parser pipeline.  It does not
import project modules, write to SQLite, rename fields, normalize values,
deduplicate listings, or classify sellers.  Uysot embeds the catalog response
inside Next.js ``__NEXT_DATA__``; this script saves the first three listing
objects with the original keys, values, and nested structures.

Examples:
    python -m standalone_parsers.uysot_publications_raw_json
    python -m standalone_parsers.uysot_publications_raw_json --output output_json/uysot/uysot_rent_3_raw.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


DEFAULT_CATALOG_URL = "https://uysot.uz/uzbekistan/snyat"
DEFAULT_LIMIT = 3
DEFAULT_OUTPUT = Path("output_json/uysot/uysot_rent_3_raw.json")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сохранить три исходных объекта объявлений Uysot в JSON "
            "без преобразований и подключения к основному проекту"
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_CATALOG_URL,
        help=f"URL каталога (по умолчанию: {DEFAULT_CATALOG_URL})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Количество объявлений, от 1 до 3 (по умолчанию: 3)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Выходной JSON-файл (по умолчанию: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60_000,
        help="Таймаут загрузки страницы в миллисекундах (по умолчанию: 60000)",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Показать окно Chromium; по умолчанию браузер работает скрыто",
    )
    return parser.parse_args()


def _validate_arguments(url: str, limit: int, timeout: int) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL должен начинаться с http:// или https://")
    if hostname != "uysot.uz" and not hostname.endswith(".uysot.uz"):
        raise ValueError("Допускаются только URL домена uysot.uz")
    if not 1 <= limit <= DEFAULT_LIMIT:
        raise ValueError("Лимит должен быть от 1 до 3")
    if timeout <= 0:
        raise ValueError("Таймаут должен быть больше нуля")


def _raw_listings(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "В __NEXT_DATA__ отсутствует dehydratedState.queries"
        ) from error

    for query in queries:
        if not isinstance(query, dict):
            continue
        query_key = query.get("queryKey")
        if not isinstance(query_key, list) or not query_key:
            continue
        if query_key[0] != "view_all_announcement":
            continue

        query_data = query.get("state", {}).get("data")
        pages = query_data.get("pages") if isinstance(query_data, dict) else None
        if not isinstance(pages, list):
            continue

        listings: list[dict[str, Any]] = []
        for page in pages:
            page_data = page.get("data") if isinstance(page, dict) else None
            if not isinstance(page_data, list):
                continue
            if not all(isinstance(item, dict) for item in page_data):
                raise RuntimeError("Ответ каталога Uysot содержит неожиданные элементы")
            listings.extend(page_data)
        return listings

    raise RuntimeError(
        "В __NEXT_DATA__ не найден исходный ответ view_all_announcement"
    )


def collect(
    url: str,
    limit: int,
    timeout: int,
    headed: bool = False,
) -> list[dict[str, Any]]:
    """Return untouched listing objects embedded in the Uysot catalog."""

    try:
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Не найден Playwright. Установите: pip install playwright; "
            "playwright install chromium"
        ) from error

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=not headed,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = browser.new_context(
            locale="ru-RU",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()
        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=timeout,
            )
            if response and response.status >= 400:
                raise RuntimeError(
                    f"Каталог Uysot не открыт, HTTP: {response.status}"
                )
            next_data_node = page.locator("#__NEXT_DATA__")
            next_data_node.wait_for(state="attached", timeout=timeout)
            payload = next_data_node.text_content()
        except PlaywrightTimeoutError as error:
            raise RuntimeError(
                "Uysot не отдал __NEXT_DATA__ до истечения таймаута"
            ) from error
        finally:
            context.close()
            browser.close()

    if not payload:
        raise RuntimeError("На странице Uysot найден пустой JSON __NEXT_DATA__")
    try:
        next_data = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("JSON __NEXT_DATA__ Uysot повреждён") from error
    if not isinstance(next_data, dict):
        raise RuntimeError("__NEXT_DATA__ Uysot имеет неожиданный формат")

    listings = _raw_listings(next_data)
    if len(listings) < limit:
        raise RuntimeError(
            f"Uysot вернул только {len(listings)} объявлений из запрошенных {limit}"
        )
    return listings[:limit]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _arguments()
    try:
        _validate_arguments(args.url, args.limit, args.timeout)
        listings = collect(args.url, args.limit, args.timeout, args.headed)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file:
            json.dump(listings, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка парсинга Uysot: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(f"JSON сохранён: {output}")
    print(f"Объявлений: {len(listings)}")


if __name__ == "__main__":
    main()
