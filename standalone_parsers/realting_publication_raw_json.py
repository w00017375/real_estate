"""Standalone raw housing-data collector for Realting.uz.

This file does not import or write to the estate-parser project.  It opens the
Realting long-term-rent catalog, selects the first two publication URLs and
writes the housing information exactly as displayed by the source.  Prices,
areas, coordinates, labels and other values stay strings; no normalization,
database mapping, deduplication or seller classification is performed.

Examples:
    python -m standalone_parsers.realting_publication_raw_json
    python -m standalone_parsers.realting_publication_raw_json \
        "https://realting.uz/property-to-rent" --limit 2
    python -m standalone_parsers.realting_publication_raw_json --output output_json/realting/realting_rent_2_raw.json
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse


DEFAULT_CATALOG_URL = "https://realting.uz/property-to-rent"
DEFAULT_LIMIT = 2
DEFAULT_OUTPUT = Path("output_json/realting/realting_rent_2_raw.json")
PUBLICATION_PATH = re.compile(r"^/property-to-rent/\d+/?$")


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сохранить исходные жилищные данные двух публикаций Realting "
            "без нормализации и подключения к основному проекту"
        )
    )
    parser.add_argument(
        "url",
        nargs="?",
        default=DEFAULT_CATALOG_URL,
        help="URL каталога долгосрочной аренды Realting",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help="Количество публикаций (по умолчанию: 2)",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"Выходной JSON (по умолчанию: {DEFAULT_OUTPUT})",
    )
    parser.add_argument(
        "--headed",
        action="store_true",
        help="Показать окно Chromium во время работы",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=60_000,
        help="Таймаут открытия страницы в миллисекундах",
    )
    return parser.parse_args()


def _validate_catalog_url(url: str) -> None:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL должен начинаться с http:// или https://")
    if host != "realting.uz" and not host.endswith(".realting.uz"):
        raise ValueError("Допускаются только URL домена realting.uz")


def _publication_urls(
    page: Any,
    catalog_url: str,
    limit: int | None,
    timeout: int,
) -> list[str]:
    response = page.goto(
        catalog_url,
        wait_until="domcontentloaded",
        timeout=timeout,
    )
    if not response or response.status != 200:
        status = response.status if response else "нет ответа"
        raise RuntimeError(f"Каталог Realting не открыт, HTTP: {status}")
    page.wait_for_timeout(1_500)

    hrefs = page.locator('a[href*="/property-to-rent/"]').evaluate_all(
        "elements => elements.map(element => element.getAttribute('href'))"
    )
    result: list[str] = []
    seen: set[str] = set()
    for href in hrefs:
        absolute = urljoin(catalog_url, href or "")
        parsed = urlparse(absolute)
        if not PUBLICATION_PATH.fullmatch(parsed.path):
            continue
        canonical = f"{parsed.scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        if canonical in seen:
            continue
        seen.add(canonical)
        result.append(canonical)
        if limit is not None and len(result) >= limit:
            break
    if limit is not None and len(result) < limit:
        raise RuntimeError(
            f"В каталоге найдено только {len(result)} публикаций из {limit}"
        )
    return result


RAW_HOUSING_EVALUATION = r"""
() => {
    const objectRoot = document.querySelector('.newb-object[data-id]');
    const top = objectRoot?.querySelector('.newb-top') || document;
    const priceNode = top.querySelector('.price-item');
    const priceParent = priceNode?.parentElement;

    const exactText = (node) => node ? node.innerText : null;
    const attributes = (node) => node
        ? Object.fromEntries(Array.from(node.attributes).map(
            attribute => [attribute.name, attribute.value]
        ))
        : {};

    const pairs = (root) => {
        const result = {};
        if (!root) return result;
        for (const box of root.querySelectorAll('li .lh-small')) {
            const children = Array.from(box.children).filter(
                child => child.tagName !== 'SCRIPT'
            );
            const name = children[0]?.innerText;
            const value = children[1]?.innerText;
            if (name !== undefined && value !== undefined) {
                result[name] = value;
            }
        }
        return result;
    };

    const characteristics = {};
    const paramsRoot = document.querySelector('#blockParams');
    if (paramsRoot) {
        for (const section of paramsRoot.querySelectorAll('section.tags-block')) {
            const heading = section.querySelector('h3')?.innerText;
            if (heading) characteristics[heading] = pairs(section);
        }
    }

    const mapScripts = Array.from(document.scripts)
        .map(script => script.textContent || '')
        .filter(text => (
            text.includes('LOCATION_YANDEX_MAP') ||
            text.includes('singleMarker(')
        ));

    return {
        'ID': objectRoot?.getAttribute('data-id') || null,
        'Заголовок': exactText(top.querySelector('h1')),
        'Адрес под заголовком': exactText(top.querySelector('.address')),
        'Цена': {
            'Отображаемое значение': exactText(priceNode),
            'Период': exactText(priceParent?.querySelector('span')),
            'Атрибуты источника': attributes(priceNode),
        },
        'Местонахождение': pairs(document.querySelector('#blockAddress')),
        'Описание': exactText(
            document.querySelector('#blockDescription .description .readmore')
        ),
        'Дата публикации': (() => {
            const node = document.querySelector(
                'time[datetime], [itemprop="datePosted"], '
                + 'meta[property="article:published_time"], '
                + 'meta[name="date"]'
            );
            return node?.getAttribute('datetime')
                || node?.getAttribute('content')
                || exactText(node);
        })(),
        'Характеристики объекта': characteristics,
        'Местонахождение на карте': exactText(
            document.querySelector('#blockMap .address')
        ),
        'Исходные скрипты координат': mapScripts,
        'Фотографии': Array.from(
            document.querySelectorAll('.slider-container .image-item')
        ).map(node => ({
            'attributes': attributes(node),
            'style': node.getAttribute('style'),
        })),
        'Встроенный JSON': Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        ).map((script, index) => ({
            'index': index,
            'type': script.type,
            'raw': script.textContent,
        })),
        'Мета-данные страницы': Array.from(document.querySelectorAll('meta')).map(
            meta => ({'attributes': attributes(meta)})
        ),
        'Исходный HTML жилищных блоков': {
            'Заголовок и цена': objectRoot?.querySelector('.newb-top')?.outerHTML || null,
            'Местонахождение': document.querySelector('#blockAddress')?.outerHTML || null,
            'Описание': document.querySelector('#blockDescription')?.outerHTML || null,
            'Характеристики объекта': paramsRoot?.outerHTML || null,
            'Местонахождение на карте': document.querySelector('#blockMap')?.outerHTML || null,
        },
    };
}
"""


def _parse_publication(page: Any, url: str, timeout: int) -> dict[str, Any]:
    json_responses: list[dict[str, Any]] = []

    def capture_json(response: Any) -> None:
        try:
            content_type = response.headers.get("content-type", "")
            if "json" not in content_type.lower():
                return
            json_responses.append(
                {
                    "url": response.url,
                    "status": response.status,
                    "content-type": content_type,
                    "raw": response.text(),
                }
            )
        except Exception:
            return

    page.on("response", capture_json)
    try:
        response = page.goto(
            url,
            wait_until="domcontentloaded",
            timeout=timeout,
        )
        if not response or response.status != 200:
            status = response.status if response else "нет ответа"
            raise RuntimeError(f"Публикация не открыта, HTTP: {status}")
        page.wait_for_timeout(1_500)
        housing = page.evaluate(RAW_HOUSING_EVALUATION)
        return {
            "requested_url": url,
            "final_url": page.url,
            "document_status": response.status,
            "housing_source_data": housing,
            "raw_json_responses": json_responses,
        }
    finally:
        page.remove_listener("response", capture_json)


def collect(
    catalog_url: str,
    *,
    limit: int,
    headed: bool,
    timeout: int,
) -> dict[str, Any]:
    _validate_catalog_url(catalog_url)
    if limit < 1:
        raise ValueError("Лимит должен быть положительным числом")

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Не найден Playwright. Установите: pip install playwright; "
            "playwright install chromium"
        ) from error

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        catalog_page = browser.new_page(
            locale="ru-RU",
            viewport={"width": 1440, "height": 1000},
        )
        detail_page = browser.new_page(
            locale="ru-RU",
            viewport={"width": 1440, "height": 1000},
        )
        try:
            urls = _publication_urls(
                catalog_page, catalog_url, limit, timeout
            )
            publications: list[dict[str, Any]] = []
            for index, url in enumerate(urls, start=1):
                print(f"[{index}/{limit}] {url}", file=sys.stderr)
                try:
                    publications.append(
                        _parse_publication(detail_page, url, timeout)
                    )
                except Exception as error:
                    publications.append(
                        {"requested_url": url, "error": str(error)}
                    )
            return {
                "catalog_url": catalog_url,
                "limit": limit,
                "publication_urls": urls,
                "publications": publications,
            }
        finally:
            browser.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    try:
        result = collect(
            args.url,
            limit=args.limit,
            headed=args.headed,
            timeout=args.timeout,
        )
    except Exception as error:
        raise SystemExit(f"Ошибка Realting: {error}") from None

    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"JSON сохранён: {output}")


if __name__ == "__main__":
    main()
