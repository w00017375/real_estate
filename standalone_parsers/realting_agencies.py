"""Импорт агентств Realting.uz в таблицу ``sellers``.

Скрипт открывает каталог агентств, сохраняет профили агентств в SQLite и
возвращает JSON со списком их публикаций. Ссылки публикаций используются как
реестр для последующего открытия объявлений, но сами карточки здесь не
записываются в ``listings``: это не полностью обработанные объявления.

Пример:
    python -m standalone_parsers.realting_agencies --database olx_apartments.db --output output_json/realting/realting_agencies.json

Для быстрой проверки можно ограничить объём:
    python -m standalone_parsers.realting_agencies --limit-agencies 2 --publication-pages 1
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from core.normalization import clean_text, normalize_phone, parse_int
from storage.sqlite_store import create_schema, database_datetime


DEFAULT_URL = "https://realting.uz/agencies"
DEFAULT_DATABASE = "olx_apartments.db"
DEFAULT_OUTPUT = "output_json/realting/realting_agencies.json"
MAX_AGENCY_PAGES = 1000
MAX_PUBLICATION_PAGES = 1000
NAVIGATION_RETRIES = 3
PUBLICATION_PATH = re.compile(
    r"^/(?:property|property-to-rent|commercial|short-term-rental|lands)/\d+/?$"
)


AGENCIES_PAGE_EVALUATION = r"""
() => {
    const decode = (value) => {
        if (!value) return null;
        try { return atob(value); } catch (_) { return null; }
    };
    return [...document.querySelectorAll('.teaser-company[data-id]')].map(card => {
        const text = (selector) => card.querySelector(selector)?.innerText?.trim() || null;
        const attr = (selector, name) => card.querySelector(selector)?.getAttribute(name) || null;
        const units = {};
        card.querySelectorAll('.units .unit-item').forEach(item => {
            const title = item.getAttribute('title');
            const value = item.querySelector('span')?.innerText?.trim();
            const href = item.getAttribute('href');
            if (title && value) units[title] = {value, href};
        });
        const profile = card.querySelector('.title a[href*="/agencies/"]');
        const contactRoot = card.querySelector('.contacts-content');
        const phone = contactRoot?.querySelector('[data-encr-ph]')?.getAttribute('data-encr-ph');
        const email = contactRoot?.querySelector('[data-encr-em]')?.getAttribute('data-encr-em');
        const telegram = [...(contactRoot?.querySelectorAll('a[href*="telegram"]') || [])]
            .map(a => a.href)[0] || null;
        const image = card.querySelector('.image img')?.src || null;
        return {
            agency_id: card.getAttribute('data-id'),
            name: profile?.innerText?.trim() || null,
            profile_url: profile?.href || null,
            location: text('.address'),
            founded_year: text('.units .unit-item[title="Год основания компании"]'),
            logo_url: image,
            units,
            phone: decode(phone),
            email: decode(email),
            telegram_url: telegram,
        };
    });
}
"""


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Импорт агентств Realting в sellers и экспорт ссылок публикаций"
    )
    parser.add_argument("--url", default=DEFAULT_URL, help="Каталог агентств")
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--output", default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--limit-agencies",
        type=int,
        default=0,
        help="Количество агентств; 0 — все",
    )
    parser.add_argument(
        "--publication-pages",
        type=int,
        default=0,
        help="Страниц публикаций на категорию; 0 — все доступные",
    )
    parser.add_argument(
        "--no-publications",
        action="store_true",
        help="Только агентства, без сбора ссылок их публикаций",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=int, default=60_000)
    return parser.parse_args()


def _decode_count(value: Any) -> int | None:
    return parse_int(value)


def _page_url(url: str, page_number: int) -> str:
    parts = urlsplit(url)
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    if page_number > 1:
        query["page"] = str(page_number)
    else:
        query.pop("page", None)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), "")
    )


def _canonical_url(url: str, base_url: str) -> str:
    absolute = urljoin(base_url, url)
    parts = urlsplit(absolute)
    return urlunsplit((parts.scheme, parts.netloc, parts.path.rstrip("/"), "", ""))


def _absolute_source_url(url: str, base_url: str) -> str:
    """Абсолютный URL категории с сохранением company_id и прочих query."""

    absolute = urljoin(base_url, url)
    parts = urlsplit(absolute)
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, parts.query, "")
    )


def _open_page(
    page: Any,
    url: str,
    timeout: int,
    *,
    ready_selector: str = "main",
) -> Any:
    """Открывает серверную страницу Realting с повторами и быстрым ожиданием."""

    last_error: Exception | None = None
    last_status: int | None = None
    for attempt in range(1, NAVIGATION_RETRIES + 1):
        response = None
        try:
            # Realting иногда не завершает domcontentloaded из-за внешних
            # ресурсов. Для серверного HTML достаточно дождаться HTTP commit,
            # а затем целевого элемента документа.
            response = page.goto(url, wait_until="commit", timeout=timeout)
            last_status = response.status if response else None
            if last_status == 404 and attempt < NAVIGATION_RETRIES:
                page.wait_for_timeout(700 * attempt)
                continue
            if not response or last_status is None or last_status >= 400:
                raise RuntimeError(
                    f"HTTP: {last_status if last_status is not None else 'нет ответа'}"
                )
            page.locator(ready_selector).first.wait_for(
                state="attached", timeout=min(timeout, 15_000)
            )
            return response
        except Exception as error:
            last_error = error
            # При timeout нужный HTML иногда уже присутствует и пригоден для
            # чтения, поэтому проверяем DOM до повторной навигации.
            try:
                if response and response.status < 400 and page.locator(
                    ready_selector
                ).count():
                    return response
            except Exception:
                pass
            if attempt < NAVIGATION_RETRIES:
                page.wait_for_timeout(700 * attempt)

    status_text = last_status if last_status is not None else "нет ответа"
    detail = str(last_error or "неизвестная ошибка").splitlines()[0]
    raise RuntimeError(
        f"Страница не открыта после {NAVIGATION_RETRIES} попыток, "
        f"HTTP: {status_text}; {detail}; URL: {url}"
    ) from last_error


def _parse_agencies_page(page: Any, url: str, timeout: int) -> list[dict[str, Any]]:
    _open_page(page, url, timeout)
    return list(page.evaluate(AGENCIES_PAGE_EVALUATION) or [])


def collect_agencies(page: Any, catalog_url: str, limit: int, timeout: int) -> list[dict[str, Any]]:
    agencies: list[dict[str, Any]] = []
    seen: set[str] = set()
    for page_number in range(1, MAX_AGENCY_PAGES + 1):
        current = _parse_agencies_page(
            page, _page_url(catalog_url, page_number), timeout
        )
        if not current:
            break
        added = 0
        for agency in current:
            profile_url = agency.get("profile_url")
            key = str(agency.get("agency_id") or profile_url or "")
            if not key or key in seen:
                continue
            seen.add(key)
            agency["profile_url"] = _canonical_url(profile_url, catalog_url)
            agencies.append(agency)
            added += 1
            if limit and len(agencies) >= limit:
                return agencies
        if added == 0:
            break
    return agencies


def _publication_category(unit_title: str) -> str | None:
    title = (unit_title or "").casefold()
    if "долгосроч" in title:
        return "rent"
    if "краткосроч" in title:
        return "short_term_rent"
    if "жил" in title:
        return "residential"
    if "коммерч" in title:
        return "commercial"
    if "земел" in title:
        return "lands"
    return None


def collect_publications(
    page: Any,
    agency: dict[str, Any],
    *,
    max_pages: int,
    timeout: int,
) -> None:
    """Собирает URL карточек, не открывая сами объявления."""

    publication_urls: list[str] = []
    seen: set[str] = set()
    categories: dict[str, list[str]] = {}
    errors: list[dict[str, str]] = []
    for title, item in (agency.get("units") or {}).items():
        category = _publication_category(title)
        href = item.get("href") if isinstance(item, dict) else None
        if category and href:
            categories[category] = [_absolute_source_url(href, DEFAULT_URL)]

    for category, urls in categories.items():
        category_urls: list[str] = []
        for base_url in urls:
            for page_number in range(1, (max_pages or MAX_PUBLICATION_PAGES) + 1):
                url = _page_url(base_url, page_number)
                try:
                    _open_page(page, url, timeout)
                except Exception as error:
                    errors.append(
                        {
                            "category": category,
                            "url": url,
                            "error": str(error),
                        }
                    )
                    print(
                        f"  Пропуск категории {category}: {error}",
                        file=sys.stderr,
                    )
                    break
                hrefs = page.locator("a[href]").evaluate_all(
                    "elements => elements.map(element => element.href)"
                )
                page_seen = set()
                new_count = 0
                for href in hrefs:
                    absolute = _canonical_url(str(href), DEFAULT_URL)
                    path = urlsplit(absolute).path
                    if not PUBLICATION_PATH.fullmatch(path):
                        continue
                    if absolute in page_seen:
                        continue
                    page_seen.add(absolute)
                    if absolute not in seen:
                        seen.add(absolute)
                        category_urls.append(absolute)
                        publication_urls.append(absolute)
                        new_count += 1
                if not page_seen:
                    break
                # При отсутствии новых ссылок дальше листать бессмысленно.
                if new_count == 0:
                    break
        if category_urls:
            agency.setdefault("publication_urls_by_category", {})[category] = category_urls
    agency["publication_urls"] = publication_urls
    agency["publication_count"] = len(publication_urls)
    if errors:
        agency["publication_errors"] = errors


def _unit_count(agency: dict[str, Any], title_part: str) -> int | None:
    for title, item in (agency.get("units") or {}).items():
        if title_part.casefold() in title.casefold():
            return _decode_count(item.get("value") if isinstance(item, dict) else item)
    return None


def save_agencies(agencies: list[dict[str, Any]], database_file: str) -> int:
    connection = sqlite3.connect(database_file, timeout=10)
    try:
        connection.execute("PRAGMA foreign_keys = ON")
        create_schema(connection)
        now = database_datetime(datetime.now(timezone.utc))
        saved = 0
        for agency in agencies:
            profile_url = agency.get("profile_url")
            name = clean_text(agency.get("name"))
            if not profile_url or not name:
                continue
            identity = profile_url
            phone = normalize_phone(agency.get("phone"), default_country_code="998")
            existing = connection.execute(
                """SELECT seller_pk FROM sellers
                   WHERE source_name = 'realting' AND profile_url = ?
                   ORDER BY seller_pk LIMIT 1""",
                (profile_url,),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE sellers SET
                           name = ?, phone = COALESCE(?, phone),
                           phone_source = COALESCE(?, phone_source),
                           source_seller_role = 'agency', updated_at = ?
                       WHERE seller_pk = ?""",
                    (
                        name,
                        phone,
                        "realting" if phone else None,
                        now,
                        existing[0],
                    ),
                )
            else:
                connection.execute(
                    """INSERT INTO sellers (
                           identity_key, source_name, profile_url, name,
                           is_official_seller, source_seller_role,
                           phone, phone_source, seller_status,
                           phone_listing_count, updated_at
                       ) VALUES (?, 'realting', ?, ?, 0, 'agency', ?, ?,
                                 NULL, NULL, ?)""",
                    (
                        identity,
                        profile_url,
                        name,
                        phone,
                        "realting" if phone else None,
                        now,
                    ),
                )
            saved += 1
        connection.commit()
        return saved
    finally:
        connection.close()


def collect_and_save(
    catalog_url: str,
    database_file: str,
    *,
    limit_agencies: int = 0,
    publication_pages: int = 0,
    collect_publication_links: bool = True,
    headed: bool = False,
    timeout: int = 60_000,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Не найден Playwright. Установите: pip install playwright; "
            "playwright install chromium"
        ) from error

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=not headed)
        page = browser.new_page(
            locale="ru-RU", viewport={"width": 1440, "height": 1000}
        )
        page.route(
            "**/*",
            lambda route: (
                route.abort()
                if route.request.resource_type in {"image", "media", "font"}
                else route.continue_()
            ),
        )
        try:
            agencies = collect_agencies(
                page, catalog_url, limit_agencies, timeout
            )
            if collect_publication_links:
                for index, agency in enumerate(agencies, start=1):
                    print(
                        f"[{index}/{len(agencies)}] "
                        f"{agency.get('name') or agency.get('profile_url')}",
                        file=sys.stderr,
                    )
                    collect_publications(
                        page,
                        agency,
                        max_pages=publication_pages,
                        timeout=timeout,
                    )
            saved = save_agencies(agencies, database_file)
            return {
                "catalog_url": catalog_url,
                "agency_count": len(agencies),
                "saved_to_sellers": saved,
                "publication_links_collected": collect_publication_links,
                "agencies": agencies,
            }
        finally:
            browser.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    if args.limit_agencies < 0 or args.publication_pages < 0:
        raise SystemExit("Лимиты не могут быть отрицательными")
    try:
        result = collect_and_save(
            args.url,
            args.database,
            limit_agencies=args.limit_agencies,
            publication_pages=args.publication_pages,
            collect_publication_links=not args.no_publications,
            headed=args.headed,
            timeout=args.timeout,
        )
    except Exception as error:
        raise SystemExit(f"Ошибка импорта агентств Realting: {error}") from None
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"Агентств обработано: {result['agency_count']}")
    print(f"Строк sellers добавлено/обновлено: {result['saved_to_sellers']}")
    print(f"JSON сохранён: {output}")


if __name__ == "__main__":
    main()
