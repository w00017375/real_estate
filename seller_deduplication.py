"""Локальная постобработка продавцов после завершения полного парсинга.

Скрипт не импортируется и не запускается из ``main.py``. Он работает только
с указанным SQLite-файлом и не выполняет сетевых запросов.
"""

from __future__ import annotations

import argparse
import re
import sqlite3
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DATABASE = Path("olx_apartments.db")
BUSY_TIMEOUT_MS = 30_000
OBSOLETE_SELLER_COLUMNS = {
    "seller_type",
    "sellers_type",
    "realtor_probability",
    "assessment_comment",
    "active_rent_listings",
    "activer_rent_listings",
    "active_sale_listings",
    "unique_listing_count",
    "distinct_housing_listings",
    "profile_scan_complete",
}


@dataclass(frozen=True)
class DeduplicationResult:
    phones_normalized: int
    full_duplicates_removed: int
    listings_relinked: int
    duplicate_phones: int
    seller_duplicate_rows: int
    realtor_phones: int
    owner_phones: int


def normalize_phone(value: Any) -> str | None:
    """Convert a non-NULL value to a string containing ASCII digits only."""

    if value is None:
        return None
    return re.sub(r"[^0-9]", "", str(value))


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _seller_columns(connection: sqlite3.Connection) -> list[str]:
    return [row[1] for row in connection.execute("PRAGMA table_info(sellers)")]


def _validate_database(connection: sqlite3.Connection) -> list[str]:
    if not _table_exists(connection, "sellers"):
        raise RuntimeError("В базе данных отсутствует таблица sellers")

    columns = _seller_columns(connection)
    required = {
        "seller_pk",
        "phone",
        "source_name",
        "profile_url",
        "seller_status",
        "phone_listing_count",
    }
    missing = required.difference(columns)
    if missing:
        raise RuntimeError(
            "В таблице sellers отсутствуют обязательные колонки: "
            + ", ".join(sorted(missing))
        )
    return columns


def _migrate_seller_columns(connection: sqlite3.Connection) -> None:
    """Remove obsolete assessment fields and add compact result fields."""

    if not _table_exists(connection, "sellers"):
        raise RuntimeError("В базе данных отсутствует таблица sellers")
    columns = set(_seller_columns(connection))
    if "seller_status" not in columns:
        connection.execute("ALTER TABLE sellers ADD COLUMN seller_status TEXT")
    if "phone_listing_count" not in columns:
        connection.execute(
            "ALTER TABLE sellers ADD COLUMN phone_listing_count INTEGER"
        )
    for column in sorted(columns & OBSOLETE_SELLER_COLUMNS):
        connection.execute(f'ALTER TABLE sellers DROP COLUMN "{column}"')


def _create_duplicate_table(connection: sqlite3.Connection) -> None:
    connection.execute(
        """CREATE TABLE IF NOT EXISTS seller_duplicates (
            seller_duplicate_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            phone VARCHAR(32) NOT NULL,
            source_name VARCHAR(100) NOT NULL,
            seller_url VARCHAR(2048) NOT NULL DEFAULT '',
            first_seller_pk INTEGER NOT NULL,
            occurrences INTEGER NOT NULL,
            UNIQUE (phone, source_name, seller_url)
        )"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_seller_duplicates_phone "
        "ON seller_duplicates(phone)"
    )


def _backfill_listing_seller_links(connection: sqlite3.Connection) -> None:
    if not _table_exists(connection, "listing_sellers"):
        return
    connection.execute(
        """INSERT OR IGNORE INTO listing_sellers(
               listing_pk, seller_pk, is_primary
           )
           SELECT listing_pk, seller_pk, 1 FROM listings"""
    )


def _normalize_phones(connection: sqlite3.Connection) -> int:
    rows = connection.execute(
        "SELECT seller_pk, phone FROM sellers WHERE phone IS NOT NULL"
    ).fetchall()
    updates: list[tuple[str, int]] = []
    for seller_pk, old_phone in rows:
        normalized = normalize_phone(old_phone)
        if normalized != old_phone or not isinstance(old_phone, str):
            updates.append((normalized or "", seller_pk))
    connection.executemany(
        "UPDATE sellers SET phone = ? WHERE seller_pk = ?",
        updates,
    )
    return len(updates)


def _relink_seller_references(
    connection: sqlite3.Connection,
    duplicate_pk: int,
    retained_pk: int,
) -> int:
    """Repoint every SQLite FK that refers to sellers.seller_pk."""

    relinked = 0
    table_names = [
        row[0]
        for row in connection.execute(
            """SELECT name FROM sqlite_master
               WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"""
        )
    ]
    for table in table_names:
        for foreign_key in connection.execute(f'PRAGMA foreign_key_list("{table}")'):
            referenced_table = foreign_key[2]
            local_column = foreign_key[3]
            referenced_column = foreign_key[4]
            if referenced_table != "sellers" or referenced_column != "seller_pk":
                continue
            if table == "listing_sellers":
                connection.execute(
                    """DELETE FROM listing_sellers
                       WHERE seller_pk = ? AND EXISTS (
                           SELECT 1 FROM listing_sellers AS retained
                           WHERE retained.listing_pk = listing_sellers.listing_pk
                             AND retained.seller_pk = ?
                       )""",
                    (duplicate_pk, retained_pk),
                )
            cursor = connection.execute(
                f'UPDATE "{table}" SET "{local_column}" = ? '
                f'WHERE "{local_column}" = ?',
                (retained_pk, duplicate_pk),
            )
            if table == "listings":
                relinked += max(cursor.rowcount, 0)
    return relinked


def _remove_full_duplicates(
    connection: sqlite3.Connection,
) -> tuple[int, int]:
    """Remove duplicates by (seller URL, platform, normalized phone).

    ``None`` is a real part of the signature, therefore two rows with no
    profile URL are duplicates when their platform and phone also match.
    Rows without a phone remain untouched, as required by the local
    post-processing contract.
    """

    rows = connection.execute(
        """SELECT seller_pk, profile_url, source_name, phone
            FROM sellers
            WHERE phone IS NOT NULL AND phone <> ''
            ORDER BY seller_pk"""
    ).fetchall()

    retained_by_values: dict[tuple[Any, ...], int] = {}
    duplicates: list[tuple[int, int]] = []
    for row in rows:
        seller_pk = int(row[0])
        signature = (row[1], row[2], row[3])
        retained_pk = retained_by_values.setdefault(signature, seller_pk)
        if retained_pk != seller_pk:
            duplicates.append((seller_pk, retained_pk))

    relinked = 0
    for duplicate_pk, retained_pk in duplicates:
        relinked += _relink_seller_references(
            connection, duplicate_pk, retained_pk
        )
        connection.execute(
            "DELETE FROM sellers WHERE seller_pk = ?", (duplicate_pk,)
        )
    return len(duplicates), relinked


def _classify_sellers(connection: sqlite3.Connection) -> tuple[int, int]:
    """Classify every valid phone by its number of linked listings."""

    if not _table_exists(connection, "listings"):
        raise RuntimeError("В базе данных отсутствует таблица listings")
    connection.execute(
        """UPDATE sellers
           SET seller_status = NULL, phone_listing_count = NULL
           WHERE phone IS NULL OR phone = ''"""
    )
    if _table_exists(connection, "listing_sellers"):
        phone_counts = connection.execute(
            """SELECT s.phone, COUNT(DISTINCT ls.listing_pk) AS listing_count
               FROM sellers AS s
               LEFT JOIN listing_sellers AS ls ON ls.seller_pk = s.seller_pk
               WHERE s.phone IS NOT NULL AND s.phone <> ''
               GROUP BY s.phone
               ORDER BY s.phone"""
        ).fetchall()
    else:
        phone_counts = connection.execute(
            """SELECT s.phone, COUNT(DISTINCT l.listing_pk) AS listing_count
               FROM sellers AS s
               LEFT JOIN listings AS l ON l.seller_pk = s.seller_pk
               WHERE s.phone IS NOT NULL AND s.phone <> ''
               GROUP BY s.phone
               ORDER BY s.phone"""
        ).fetchall()
    realtor_phones = 0
    owner_phones = 0
    for phone, listing_count in phone_counts:
        status = "realtor" if listing_count >= 3 else "owner"
        if status == "realtor":
            realtor_phones += 1
        else:
            owner_phones += 1
        connection.execute(
            """UPDATE sellers
               SET seller_status = ?, phone_listing_count = ?
               WHERE phone = ?""",
            (status, listing_count, phone),
        )
    return realtor_phones, owner_phones


def _refresh_duplicate_phone_snapshot(
    connection: sqlite3.Connection,
) -> tuple[int, int]:
    """Rebuild a snapshot of non-identical sellers sharing a phone."""

    connection.execute("DELETE FROM seller_duplicates")
    duplicate_phones = [
        row[0]
        for row in connection.execute(
            """SELECT phone
               FROM sellers
               WHERE phone IS NOT NULL AND phone <> ''
               GROUP BY phone
               HAVING COUNT(*) > 1
               ORDER BY phone"""
        )
    ]

    inserted = 0
    for phone in duplicate_phones:
        combinations = connection.execute(
            """SELECT
                   COALESCE(source_name, 'unknown') AS platform,
                   COALESCE(profile_url, '') AS seller_url,
                   MIN(seller_pk) AS first_seller_pk,
                   COUNT(*) AS occurrences
               FROM sellers
               WHERE phone = ?
               GROUP BY COALESCE(source_name, 'unknown'),
                        COALESCE(profile_url, '')
               ORDER BY first_seller_pk""",
            (phone,),
        ).fetchall()
        connection.executemany(
            """INSERT INTO seller_duplicates (
                   phone, source_name, seller_url,
                   first_seller_pk, occurrences
               ) VALUES (?, ?, ?, ?, ?)""",
            [(phone, *combination) for combination in combinations],
        )
        inserted += len(combinations)
    return len(duplicate_phones), inserted


def deduplicate_sellers(
    database_path: str | Path,
    *,
    dry_run: bool = False,
) -> DeduplicationResult:
    """Run all seller postprocessing steps in one atomic transaction."""

    path = Path(database_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Файл базы данных не найден: {path}")

    connection = sqlite3.connect(path, timeout=BUSY_TIMEOUT_MS / 1000)
    try:
        connection.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("BEGIN IMMEDIATE")
        _migrate_seller_columns(connection)
        _validate_database(connection)
        _create_duplicate_table(connection)
        _backfill_listing_seller_links(connection)
        normalized = _normalize_phones(connection)
        removed, relinked = _remove_full_duplicates(connection)
        realtor_phones, owner_phones = _classify_sellers(connection)
        duplicate_phones, snapshot_rows = _refresh_duplicate_phone_snapshot(
            connection
        )

        foreign_key_errors = connection.execute(
            "PRAGMA foreign_key_check"
        ).fetchall()
        if foreign_key_errors:
            raise RuntimeError(
                "После дедупликации обнаружены нарушения внешних ключей: "
                f"{len(foreign_key_errors)}"
            )

        result = DeduplicationResult(
            phones_normalized=normalized,
            full_duplicates_removed=removed,
            listings_relinked=relinked,
            duplicate_phones=duplicate_phones,
            seller_duplicate_rows=snapshot_rows,
            realtor_phones=realtor_phones,
            owner_phones=owner_phones,
        )
        if dry_run:
            connection.rollback()
        else:
            connection.commit()
        return result
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Локально нормализовать телефоны sellers, удалить полные "
            "дубли и обновить seller_duplicates"
        )
    )
    parser.add_argument(
        "--database",
        type=Path,
        default=DEFAULT_DATABASE,
        help="SQLite-файл (по умолчанию: olx_apartments.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Показать результат и откатить все изменения",
    )
    return parser.parse_args()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    args = _arguments()
    try:
        result = deduplicate_sellers(args.database, dry_run=args.dry_run)
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as error:
        raise SystemExit(f"Ошибка дедупликации sellers: {error}") from None

    mode = "ПРОВЕРКА БЕЗ СОХРАНЕНИЯ" if args.dry_run else "ГОТОВО"
    print(mode)
    print("Нормализовано телефонов:", result.phones_normalized)
    print("Удалено полных дублей sellers:", result.full_duplicates_removed)
    print("Перепривязано объявлений:", result.listings_relinked)
    print("Повторяющихся номеров:", result.duplicate_phones)
    print("Строк в seller_duplicates:", result.seller_duplicate_rows)
    print("Номеров со статусом realtor:", result.realtor_phones)
    print("Номеров со статусом owner:", result.owner_phones)
    print("SQLite:", args.database.resolve())


if __name__ == "__main__":
    main()
