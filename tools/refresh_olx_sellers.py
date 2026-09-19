"""Refresh stored OLX seller names and phones without changing seller keys.

The script is intentionally separate from ``main.py`` and performs no network
requests.  Existing rows are updated in place, so ``listings.seller_pk`` and
all other foreign-key relationships remain unchanged.
"""

from __future__ import annotations

import argparse
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from core.seller_metadata import (
    extract_olx_phone_from_seller_name,
    extract_uzbek_phones,
    extract_uzbek_phones_from_description,
    normalize_seller_name,
)
from storage.sqlite_store import ensure_listing_seller_links


DEFAULT_DATABASE = Path("olx_apartments.db")
DEFAULT_BACKUP = Path("backups/olx_apartments.before_olx_seller_refresh.db")
BUSY_TIMEOUT_MS = 30_000


@dataclass(frozen=True)
class RefreshResult:
    rows_seen: int
    rows_updated: int
    phones_extracted: int
    description_listings_used: int
    secondary_sellers_added: int
    listing_links_preserved: int
    contact_links: int


def refresh_olx_sellers(database: Path) -> RefreshResult:
    connection = sqlite3.connect(database, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        ensure_listing_seller_links(connection)
        before_links = connection.execute(
            """SELECT COUNT(*) FROM listings AS l
               JOIN sellers AS s ON s.seller_pk = l.seller_pk
               WHERE lower(s.source_name) = 'olx'"""
        ).fetchone()[0]
        rows = connection.execute(
            """SELECT l.listing_pk, s.seller_pk, s.name, s.phone,
                      s.phone_source, l.description
               FROM listings AS l
               JOIN sellers AS s ON s.seller_pk = l.seller_pk
               WHERE lower(l.source_name) = 'olx'
               ORDER BY l.listing_pk"""
        ).fetchall()

        rows_updated = 0
        phones_extracted = 0
        description_listings_used = 0
        secondary_sellers_added = 0
        for (
            listing_pk,
            seller_pk,
            raw_name,
            old_phone,
            old_phone_source,
            description,
        ) in rows:
            phone_from_name = extract_olx_phone_from_seller_name(raw_name)
            normalized_name = normalize_seller_name(raw_name, "olx")
            stored_phones = extract_uzbek_phones(old_phone)
            if stored_phones:
                phones = stored_phones
                phone_source = old_phone_source
            elif phone_from_name:
                phones = [phone_from_name]
                phone_source = "olx_seller_name"
            else:
                phones = extract_uzbek_phones_from_description(description)
                phone_source = "olx_description" if phones else None
                if phones:
                    description_listings_used += 1

            primary_phone = phones[0] if phones else None
            new_values = (normalized_name, primary_phone, phone_source)
            if new_values != (raw_name, old_phone, old_phone_source):
                connection.execute(
                    """UPDATE sellers
                       SET name = ?, phone = ?, phone_source = ?
                       WHERE seller_pk = ?""",
                    (*new_values, seller_pk),
                )
                rows_updated += 1
            phones_extracted += len(phones)

            linked_phones = {
                row[0]
                for row in connection.execute(
                    """SELECT s.phone
                       FROM listing_sellers AS ls
                       JOIN sellers AS s ON s.seller_pk = ls.seller_pk
                       WHERE ls.listing_pk = ? AND s.phone IS NOT NULL""",
                    (listing_pk,),
                )
            }
            for phone in phones[1:]:
                if phone in linked_phones:
                    continue
                cursor = connection.execute(
                    """INSERT INTO sellers (
                           identity_key, source_name, profile_url, name,
                           is_official_seller, official_complex_name,
                           source_seller_role, phone, phone_source,
                           seller_status, phone_listing_count, updated_at
                       )
                       SELECT identity_key, source_name, profile_url, ?,
                              is_official_seller, official_complex_name,
                              source_seller_role, ?, ?, NULL, NULL, updated_at
                       FROM sellers WHERE seller_pk = ?""",
                    (normalized_name, phone, phone_source, seller_pk),
                )
                connection.execute(
                    """INSERT INTO listing_sellers(
                           listing_pk, seller_pk, is_primary
                       ) VALUES (?, ?, 0)""",
                    (listing_pk, int(cursor.lastrowid)),
                )
                linked_phones.add(phone)
                secondary_sellers_added += 1

        after_links = connection.execute(
            """SELECT COUNT(*) FROM listings AS l
               JOIN sellers AS s ON s.seller_pk = l.seller_pk
               WHERE lower(s.source_name) = 'olx'"""
        ).fetchone()[0]
        if after_links != before_links:
            raise RuntimeError(
                "Количество связей OLX listings→sellers неожиданно изменилось"
            )
        connection.commit()
        return RefreshResult(
            rows_seen=len(rows),
            rows_updated=rows_updated,
            phones_extracted=phones_extracted,
            description_listings_used=description_listings_used,
            secondary_sellers_added=secondary_sellers_added,
            listing_links_preserved=after_links,
            contact_links=connection.execute(
                """SELECT COUNT(*) FROM listing_sellers AS ls
                   JOIN listings AS l ON l.listing_pk = ls.listing_pk
                   WHERE lower(l.source_name) = 'olx'"""
            ).fetchone()[0],
        )
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Локально обновляет имена и телефоны OLX-продавцов"
    )
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE)
    parser.add_argument("--backup", type=Path, default=DEFAULT_BACKUP)
    parser.add_argument(
        "--no-backup", action="store_true", help="Не создавать резервную копию"
    )
    args = parser.parse_args()

    database = args.database.resolve()
    if not database.is_file():
        raise SystemExit(f"База данных не найдена: {database}")
    if not args.no_backup:
        backup = args.backup.resolve()
        if backup == database:
            raise SystemExit("Путь резервной копии совпадает с путем базы")
        backup.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(database, backup)
        print(f"Резервная копия: {backup}")

    result = refresh_olx_sellers(database)
    print(f"OLX-продавцов проверено: {result.rows_seen}")
    print(f"Строк обновлено на месте: {result.rows_updated}")
    print(f"Телефонных связей найдено: {result.phones_extracted}")
    print(
        "Объявлений с резервным извлечением из description: "
        f"{result.description_listings_used}"
    )
    print(f"Дополнительных sellers добавлено: {result.secondary_sellers_added}")
    print(f"Связей listings→sellers сохранено: {result.listing_links_preserved}")
    print(f"Всего OLX-связей listing_sellers: {result.contact_links}")


if __name__ == "__main__":
    main()
