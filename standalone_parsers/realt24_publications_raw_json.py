"""Standalone raw JSON collector for rental apartments on Realt24.uz.

The script is intentionally independent from the main estate-parser pipeline:
it does not import project modules, write to SQLite, normalize fields, rename
keys, deduplicate listings, or classify sellers.  It reads the JSON embedded by
Realt24 in ``__NEXT_DATA__`` and writes the first three property objects exactly
with the keys and values supplied by the website.

Examples:
    python -m standalone_parsers.realt24_publications_raw_json
    python -m standalone_parsers.realt24_publications_raw_json --output output_json/realt24/realt24_rent_3_raw.json
"""

from __future__ import annotations

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_CATALOG_URL = "https://realt24.uz/ru/snyat/kvartiry/"
DEFAULT_LIMIT = 3
DEFAULT_OUTPUT = Path("output_json/realt24/realt24_rent_3_raw.json")


class _NextDataParser(HTMLParser):
    """Collect the unescaped contents of the Next.js data script."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self._inside_next_data = False
        self._parts: list[str] = []
        self.payload: str | None = None

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "script":
            return
        attributes = dict(attrs)
        if attributes.get("id") == "__NEXT_DATA__":
            self._inside_next_data = True
            self._parts = []

    def handle_data(self, data: str) -> None:
        if self._inside_next_data:
            self._parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._inside_next_data:
            self.payload = "".join(self._parts)
            self._inside_next_data = False


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Сохранить три исходных объекта объявлений Realt24 в JSON "
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
        type=float,
        default=45.0,
        help="Сетевой таймаут в секундах (по умолчанию: 45)",
    )
    return parser.parse_args()


def _validate_arguments(url: str, limit: int, timeout: float) -> None:
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("URL должен начинаться с http:// или https://")
    if hostname != "realt24.uz" and not hostname.endswith(".realt24.uz"):
        raise ValueError("Допускаются только URL домена realt24.uz")
    if not 1 <= limit <= DEFAULT_LIMIT:
        raise ValueError("Лимит должен быть от 1 до 3")
    if timeout <= 0:
        raise ValueError("Таймаут должен быть больше нуля")


def _download_html(url: str, timeout: float) -> str:
    request = Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.7",
            "Cache-Control": "no-cache",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/131.0.0.0 Safari/537.36"
            ),
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            if status != 200:
                raise RuntimeError(f"Каталог Realt24 не открыт, HTTP: {status}")
            charset = response.headers.get_content_charset() or "utf-8"
            return response.read().decode(charset, errors="strict")
    except HTTPError as error:
        raise RuntimeError(
            f"Каталог Realt24 не открыт, HTTP: {error.code}"
        ) from error
    except URLError as error:
        raise RuntimeError(f"Ошибка подключения к Realt24: {error.reason}") from error


def _next_data(html: str) -> dict[str, Any]:
    parser = _NextDataParser()
    parser.feed(html)
    if not parser.payload:
        raise RuntimeError("На странице Realt24 не найден JSON __NEXT_DATA__")
    try:
        payload = json.loads(parser.payload)
    except json.JSONDecodeError as error:
        raise RuntimeError("JSON __NEXT_DATA__ Realt24 повреждён") from error
    if not isinstance(payload, dict):
        raise RuntimeError("__NEXT_DATA__ Realt24 имеет неожиданный формат")
    return payload


def _raw_properties(next_data: dict[str, Any]) -> list[dict[str, Any]]:
    try:
        queries = next_data["props"]["pageProps"]["dehydratedState"]["queries"]
    except (KeyError, TypeError) as error:
        raise RuntimeError(
            "В __NEXT_DATA__ отсутствует dehydratedState.queries"
        ) from error

    candidates: list[tuple[str, list[dict[str, Any]]]] = []
    for query in queries:
        if not isinstance(query, dict):
            continue
        query_key = query.get("queryKey")
        if not isinstance(query_key, list) or not query_key:
            continue
        if query_key[0] != "/properties":
            continue
        response_data = query.get("state", {}).get("data")
        properties = response_data.get("data") if isinstance(response_data, dict) else None
        if not isinstance(properties, list):
            continue
        if not all(isinstance(item, dict) for item in properties):
            continue
        query_string = str(query_key[1]) if len(query_key) > 1 else ""
        candidates.append((query_string, properties))

    if not candidates:
        raise RuntimeError("В __NEXT_DATA__ не найден ответ каталога /properties")

    # The page can preload an unrelated generic property query as well.  The
    # catalog query is the one carrying its categoryType search parameter.
    for query_string, properties in candidates:
        if "categoryType=" in query_string:
            return properties
    return candidates[0][1]


def collect(url: str, limit: int, timeout: float) -> list[dict[str, Any]]:
    """Return untouched property objects embedded in the catalog page."""

    properties = _raw_properties(_next_data(_download_html(url, timeout)))
    if len(properties) < limit:
        raise RuntimeError(
            f"Realt24 вернул только {len(properties)} объявлений из запрошенных {limit}"
        )
    return properties[:limit]


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    args = _arguments()
    try:
        _validate_arguments(args.url, args.limit, args.timeout)
        publications = collect(args.url, args.limit, args.timeout)
        output = args.output.resolve()
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("w", encoding="utf-8") as file:
            json.dump(publications, file, ensure_ascii=False, indent=2)
            file.write("\n")
    except (OSError, RuntimeError, ValueError) as error:
        print(f"Ошибка парсинга Realt24: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(f"JSON сохранён: {output}")
    print(f"Объявлений: {len(publications)}")


if __name__ == "__main__":
    main()
