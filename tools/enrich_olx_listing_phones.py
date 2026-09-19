"""Find missing OLX seller phones on the saved listing pages themselves."""

from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.seller_metadata import (
    extract_uzbek_phones,
    extract_uzbek_phones_from_description,
    extract_uzbek_phones_from_payloads,
)
from sources.olx import _extract_phone, _extract_user_api_phones
from storage.sqlite_store import ensure_listing_seller_links


DEFAULT_DATABASE = Path("olx_apartments.db")
DEFAULT_BACKUP = Path("backups/olx_apartments.before_olx_page_phone_scan.db")
BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class Candidate:
    listing_pk: int
    seller_pk: int
    url: str
    description: str | None


@dataclass(frozen=True)
class FoundContact:
    candidate: Candidate
    phones: tuple[str, ...]
    phone_source: str


@dataclass(frozen=True)
class ScanResult:
    contacts: tuple[FoundContact, ...]
    pages_checked: int
    pages_failed: int


def _load_candidates(
    database: Path,
    limit: int | None,
    offset: int = 0,
) -> list[Candidate]:
    connection = sqlite3.connect(database)
    try:
        query = """
            SELECT l.listing_pk, s.seller_pk, l.url, l.description
            FROM listings AS l
            JOIN sellers AS s ON s.seller_pk = l.seller_pk
            WHERE lower(l.source_name) = 'olx'
              AND (s.phone IS NULL OR trim(s.phone) = '')
              AND l.url IS NOT NULL AND trim(l.url) <> ''
            ORDER BY l.listing_pk
        """
        parameters: tuple[Any, ...] = ()
        if limit is not None:
            query += " LIMIT ? OFFSET ?"
            parameters = (limit, offset)
        elif offset:
            query += " LIMIT -1 OFFSET ?"
            parameters = (offset,)
        return [Candidate(*row) for row in connection.execute(query, parameters)]
    finally:
        connection.close()


def _scan_candidate(page: Any, candidate: Candidate, timeout_ms: int, wait_ms: int) -> FoundContact | None:
    stored = extract_uzbek_phones_from_description(candidate.description)
    if stored:
        return FoundContact(candidate, tuple(stored), "olx_description")

    responses: list[Any] = []

    def capture(response: Any) -> None:
        url = str(getattr(response, "url", ""))
        if "/api/v1/users/" in url:
            responses.append(response)

    page.on("response", capture)
    try:
        response = page.goto(
            candidate.url,
            wait_until="domcontentloaded",
            timeout=timeout_ms,
        )
        if response is None or response.status >= 400:
            return None
        page.wait_for_timeout(wait_ms)

        structured = _extract_user_api_phones(responses)
        if structured:
            return FoundContact(candidate, tuple(structured), "olx_user_api")

        revealed = extract_uzbek_phones(_extract_phone(page))
        if revealed:
            return FoundContact(candidate, tuple(revealed), "olx_detail_page")

        try:
            description = page.locator(
                '[data-cy="ad_description"], '
                '[data-testid="ad-description"], '
                '[data-testid="ad-description-container"]'
            ).first.inner_text(timeout=2000)
        except Exception:
            description = ""
        exact = extract_uzbek_phones_from_description(description)
        if exact:
            return FoundContact(candidate, tuple(exact), "olx_description")

        try:
            body_text = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body_text = ""
        try:
            page_html = page.content()
        except Exception:
            page_html = ""
        loose = extract_uzbek_phones_from_payloads(
            description,
            body_text,
            page_html,
        )
        if loose:
            return FoundContact(candidate, tuple(loose), "olx_listing_content")
        return None
    finally:
        try:
            page.remove_listener("response", capture)
        except Exception:
            pass


def scan_candidates(
    candidates: list[Candidate],
    *,
    headed: bool,
    timeout_ms: int,
    wait_ms: int,
    batch_size: int,
) -> ScanResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as error:
        raise RuntimeError(
            "Не установлен Playwright. Выполните: "
            "python -m pip install playwright; "
            "python -m playwright install chromium"
        ) from error
    found: list[FoundContact] = []
    checked = 0
    failed = 0
    with sync_playwright() as playwright:
        for batch_start in range(0, len(candidates), batch_size):
            browser = playwright.chromium.launch(headless=not headed)
            context = browser.new_context(locale="ru-RU")
            page = context.new_page()
            batch = candidates[batch_start : batch_start + batch_size]
            try:
                for offset, candidate in enumerate(batch):
                    index = batch_start + offset + 1
                    print(
                        f"[{index}/{len(candidates)}] {candidate.url}",
                        flush=True,
                    )
                    try:
                        contact = _scan_candidate(
                            page, candidate, timeout_ms, wait_ms
                        )
                    except Exception as error:
                        failed += 1
                        print(f"  Ошибка открытия: {error}", flush=True)
                        continue
                    checked += 1
                    if contact is None:
                        print("  Номер не найден", flush=True)
                        continue
                    found.append(contact)
                    print(
                        "  Найдено:",
                        ", ".join(contact.phones),
                        flush=True,
                    )
            finally:
                context.close()
                browser.close()
    return ScanResult(tuple(found), checked, failed)


def save_contacts(database: Path, contacts: list[FoundContact]) -> tuple[int, int]:
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        ensure_listing_seller_links(connection)
        updated = 0
        added = 0
        for contact in contacts:
            candidate = contact.candidate
            primary = contact.phones[0]
            current = connection.execute(
                "SELECT phone FROM sellers WHERE seller_pk = ?",
                (candidate.seller_pk,),
            ).fetchone()
            if current is None or (current[0] is not None and str(current[0]).strip()):
                continue
            connection.execute(
                """UPDATE sellers
                   SET phone = ?, phone_source = ?
                   WHERE seller_pk = ?""",
                (primary, contact.phone_source, candidate.seller_pk),
            )
            updated += 1
            linked_phones = {
                row[0]
                for row in connection.execute(
                    """SELECT s.phone
                       FROM listing_sellers AS ls
                       JOIN sellers AS s ON s.seller_pk = ls.seller_pk
                       WHERE ls.listing_pk = ? AND s.phone IS NOT NULL""",
                    (candidate.listing_pk,),
                )
            }
            for phone in contact.phones[1:]:
                if phone in linked_phones:
                    continue
                cursor = connection.execute(
                    """INSERT INTO sellers (
                           identity_key, source_name, profile_url, name,
                           is_official_seller, official_complex_name,
                           source_seller_role, phone, phone_source,
                           seller_status, phone_listing_count, updated_at
                       )
                       SELECT identity_key, source_name, profile_url, name,
                              is_official_seller, official_complex_name,
                              source_seller_role, ?, ?, NULL, NULL, updated_at
                       FROM sellers WHERE seller_pk = ?""",
                    (phone, contact.phone_source, candidate.seller_pk),
                )
                connection.execute(
                    """INSERT OR IGNORE INTO listing_sellers(
                           listing_pk, seller_pk, is_primary
                       ) VALUES (?, ?, 0)""",
                    (candidate.listing_pk, int(cursor.lastrowid)),
                )
                linked_phones.add(phone)
                added += 1
        errors = connection.execute("PRAGMA foreign_key_check").fetchall()
        if errors:
            raise RuntimeError(
                f"После обновления нарушены внешние ключи: {len(errors)}"
            )
        connection.commit()
        return updated, added
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Открывает сохранённые OLX-объявления без телефона и ищет "
            "узбекские номера в содержимом самих публикаций"
        )
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--offset",
        type=int,
        default=0,
        help="Пропустить указанное число кандидатов (для пакетного запуска)",
    )
    parser.add_argument("--headed", action="store_true")
    parser.add_argument("--timeout", type=int, default=60_000)
    parser.add_argument("--wait-ms", type=int, default=1200)
    parser.add_argument(
        "--browser-batch-size",
        type=int,
        default=20,
        help="Перезапускать Chromium после этого количества страниц",
    )
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.limit is not None and args.limit <= 0:
        raise SystemExit("--limit должен быть положительным")
    if args.offset < 0:
        raise SystemExit("--offset не может быть отрицательным")
    if args.browser_batch_size <= 0:
        raise SystemExit("--browser-batch-size должен быть положительным")
    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"База данных не найдена: {database}")

    candidates = _load_candidates(database, args.limit, args.offset)
    print(f"OLX-объявлений без телефона: {len(candidates)}")
    if not candidates:
        return
    scan = scan_candidates(
        candidates,
        headed=args.headed,
        timeout_ms=args.timeout,
        wait_ms=args.wait_ms,
        batch_size=args.browser_batch_size,
    )
    print(f"Страниц успешно проверено: {scan.pages_checked}")
    print(f"Страниц не удалось открыть: {scan.pages_failed}")
    if not scan.contacts:
        print("На успешно открытых страницах новых телефонов не найдено")
        return

    if not args.no_backup:
        backup = args.backup.resolve()
        if backup == database:
            raise SystemExit("Путь резервной копии совпадает с путём базы")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, backup)
        print(f"Резервная копия: {backup}")

    updated, added = save_contacts(database, list(scan.contacts))
    print(f"Основных продавцов обновлено: {updated}")
    print(f"Дополнительных телефонных контактов добавлено: {added}")


if __name__ == "__main__":
    main()
