import sqlite3
import time
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from core.normalization import (
    clean_text,
    combine_street,
    normalize_phone,
    publication_fingerprint,
)
from core.seller_analysis import assess_seller
from core.seller_metadata import official_seller_details, normalize_seller_name


DATABASE_BUSY_TIMEOUT_MS = 5_000
DATABASE_LOCK_RETRIES = 3
DATABASE_TIMEZONE = ZoneInfo("Asia/Tashkent")
DATABASE_DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"


class DatabaseLockedError(RuntimeError):
    """Файл SQLite занят внешним процессом и недоступен для записи."""


def database_datetime(value: Any) -> str | None:
    """Return ``YYYY-MM-DD HH:MM:SS`` in the project's Tashkent timezone."""

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value).strip()
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            try:
                parsed = datetime.strptime(text, DATABASE_DATETIME_FORMAT)
            except ValueError:
                return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=DATABASE_TIMEZONE)
    return parsed.astimezone(DATABASE_TIMEZONE).strftime(
        DATABASE_DATETIME_FORMAT
    )


def _is_database_lock_error(error: BaseException) -> bool:
    message = str(error).lower()
    return "database is locked" in message or "database is busy" in message


def wait_for_database_write_access(database_file: str) -> None:
    """До парсинга проверяет, что SQLite сможет завершить запись схемы."""

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(DATABASE_LOCK_RETRIES):
        connection = sqlite3.connect(
            database_file,
            timeout=DATABASE_BUSY_TIMEOUT_MS / 1000,
        )
        try:
            connection.execute(
                f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}"
            )
            connection.execute("BEGIN EXCLUSIVE")
            connection.rollback()
            return
        except sqlite3.OperationalError as error:
            connection.rollback()
            if not _is_database_lock_error(error):
                raise
            last_error = error
            if attempt + 1 < DATABASE_LOCK_RETRIES:
                time.sleep(attempt + 1)
        finally:
            connection.close()

    raise DatabaseLockedError(
        "Файл SQLite занят другим процессом: "
        f"{database_file}. Закройте вкладку БД/SQLite Viewer в VS Code, "
        "DB Browser или другой запущенный парсер и повторите команду."
    ) from last_error


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS scrape_runs (
    run_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at DATETIME NOT NULL,
    source_url TEXT NOT NULL,
    listing_limit INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT 'unknown'
);

CREATE TABLE IF NOT EXISTS sellers (
    seller_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    identity_key TEXT NOT NULL,
    source_name TEXT NOT NULL DEFAULT 'unknown',
    profile_url TEXT,
    name TEXT,
    is_official_seller INTEGER NOT NULL DEFAULT 0,
    official_complex_name TEXT,
    source_seller_role TEXT,
    phone TEXT,
    phone_source TEXT,
    seller_status TEXT,
    phone_listing_count INTEGER,
    updated_at DATETIME NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    listing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    platform_listing_id TEXT,
    source_name TEXT NOT NULL DEFAULT 'unknown',
    seller_pk INTEGER NOT NULL,
    title TEXT,
    price INTEGER,
    currency TEXT,
    url TEXT NOT NULL UNIQUE,
    published_at DATETIME,
    description TEXT,
    description_length INTEGER NOT NULL DEFAULT 0,
    first_seen_at DATETIME NOT NULL,
    last_seen_at DATETIME,
    last_checked_at DATETIME,
    last_changed_at DATETIME,
    current_price REAL,
    previous_price REAL,
    previous_currency TEXT,
    publication_status TEXT NOT NULL DEFAULT 'active',
    transaction_type TEXT,
    city TEXT,
    district TEXT,
    rooms INTEGER,
    total_area_m2 REAL,
    floor INTEGER,
    floors_total INTEGER,
    FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk),
    UNIQUE (source_name, platform_listing_id)
);

CREATE TABLE IF NOT EXISTS listing_history (
    history_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_pk INTEGER NOT NULL,
    recorded_at DATETIME NOT NULL,
    price REAL,
    currency TEXT,
    description TEXT,
    publication_status TEXT NOT NULL,
    change_fields TEXT NOT NULL DEFAULT '',
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS housing (
    housing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_pk INTEGER NOT NULL UNIQUE,
    city TEXT,
    district TEXT,
    street TEXT,
    building_type TEXT,
    is_new_building INTEGER,
    foundation_type TEXT,
    residential_complex_name TEXT,
    rooms INTEGER,
    total_area_m2 REAL,
    floor INTEGER,
    floors_total INTEGER,
    furnished INTEGER,
    monthly_rent REAL,
    rent_currency TEXT,
    price_per_m2 REAL,
    latitude REAL,
    longitude REAL,
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS amenities (
    amenity_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS listing_amenities (
    listing_amenity_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_pk INTEGER NOT NULL,
    amenity_pk INTEGER NOT NULL,
    UNIQUE (listing_pk, amenity_pk),
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE,
    FOREIGN KEY (amenity_pk) REFERENCES amenities(amenity_pk)
);

CREATE TABLE IF NOT EXISTS nearby_places (
    nearby_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE
);

CREATE TABLE IF NOT EXISTS listing_nearby (
    listing_nearby_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_pk INTEGER NOT NULL,
    nearby_pk INTEGER NOT NULL,
    UNIQUE (listing_pk, nearby_pk),
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE,
    FOREIGN KEY (nearby_pk) REFERENCES nearby_places(nearby_pk)
);

CREATE TABLE IF NOT EXISTS listing_images (
    image_pk INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_pk INTEGER NOT NULL,
    source_name TEXT NOT NULL DEFAULT 'unknown',
    source_image_id TEXT,
    image_url TEXT NOT NULL,
    file_name TEXT,
    local_path TEXT,
    telegram_file_id TEXT,
    mime_type TEXT,
    width INTEGER,
    height INTEGER,
    sort_order INTEGER NOT NULL DEFAULT 0,
    is_primary INTEGER NOT NULL DEFAULT 0,
    first_seen_at DATETIME NOT NULL,
    UNIQUE (listing_pk),
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scrape_run_listings (
    run_pk INTEGER NOT NULL,
    listing_pk INTEGER NOT NULL,
    PRIMARY KEY (run_pk, listing_pk),
    FOREIGN KEY (run_pk) REFERENCES scrape_runs(run_pk),
    FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk)
);

CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_pk);
CREATE INDEX IF NOT EXISTS idx_sellers_identity ON sellers(identity_key);
CREATE INDEX IF NOT EXISTS idx_listing_images_listing ON listing_images(listing_pk, sort_order);
CREATE INDEX IF NOT EXISTS idx_listing_history_listing_recorded
ON listing_history(listing_pk, recorded_at DESC);
"""


def ensure_listing_seller_links(connection: sqlite3.Connection) -> None:
    """Create the many-to-many contact link while retaining the legacy FK."""

    connection.execute(
        """CREATE TABLE IF NOT EXISTS listing_sellers (
            listing_seller_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_pk INTEGER NOT NULL,
            seller_pk INTEGER NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 0,
            UNIQUE (listing_pk, seller_pk),
            FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk)
                ON DELETE CASCADE,
            FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk)
                ON DELETE CASCADE
        )"""
    )
    connection.execute(
        """INSERT OR IGNORE INTO listing_sellers(
               listing_pk, seller_pk, is_primary
           )
           SELECT listing_pk, seller_pk, 1 FROM listings"""
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_sellers_listing "
        "ON listing_sellers(listing_pk)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_sellers_seller "
        "ON listing_sellers(seller_pk)"
    )


def _enforce_single_listing_image(connection: sqlite3.Connection) -> None:
    """Keep only the first image and enforce one image row per listing."""

    if not connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'table' AND name = 'listing_images'"
    ).fetchone():
        return

    connection.execute(
        """
        DELETE FROM listing_images AS candidate
        WHERE EXISTS (
            SELECT 1
            FROM listing_images AS preferred
            WHERE preferred.listing_pk = candidate.listing_pk
              AND (
                  preferred.sort_order < candidate.sort_order
                  OR (
                      preferred.sort_order = candidate.sort_order
                      AND preferred.image_pk < candidate.image_pk
                  )
              )
        )
        """
    )
    connection.execute(
        "UPDATE listing_images SET sort_order = 0, is_primary = 1"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS "
        "idx_listing_images_one_per_listing ON listing_images(listing_pk)"
    )


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    definition: str,
) -> None:
    columns = {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
    }
    if column not in columns:
        connection.execute(
            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
        )


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone() is not None


def _column_names(connection: sqlite3.Connection, table: str) -> set[str]:
    if not _table_exists(connection, table):
        return set()
    return {
        row[1] for row in connection.execute(f"PRAGMA table_info({table})")
    }


def _rename_seen_columns(connection: sqlite3.Connection) -> None:
    """Migrate legacy observation timestamps without changing their values."""

    for table in ("listings", "listing_images"):
        columns = _column_names(connection, table)
        if "last_seen_at" in columns and "first_seen_at" not in columns:
            connection.execute(
                f'ALTER TABLE "{table}" '
                'RENAME COLUMN "last_seen_at" TO "first_seen_at"'
            )


def _rebuild_sellers_schema(
    connection: sqlite3.Connection,
) -> None:
    """Приводит sellers к актуальной компактной схеме.

    Старые поля оценки удаляются. ``identity_key`` остаётся неуникальным,
    чтобы парсер мог создать отдельную сырую запись для каждой публикации.
    Новые поля результата постобработки изначально сохраняются как NULL.
    """

    desired_columns = (
        "seller_pk",
        "identity_key",
        "source_name",
        "profile_url",
        "name",
        "is_official_seller",
        "official_complex_name",
        "source_seller_role",
        "phone",
        "phone_source",
        "seller_status",
        "phone_listing_count",
        "updated_at",
    )
    current_columns = tuple(
        row[1] for row in connection.execute("PRAGMA table_info(sellers)")
    )
    unique_identity = False
    for index_row in connection.execute("PRAGMA index_list(sellers)"):
        if not index_row[2]:
            continue
        index_name = index_row[1]
        index_columns = [
            row[2]
            for row in connection.execute(
                f'PRAGMA index_info("{index_name}")'
            )
        ]
        if index_columns == ["identity_key"]:
            unique_identity = True
            break
    if current_columns == desired_columns and not unique_identity:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_sellers_identity "
            "ON sellers(identity_key)"
        )
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    try:
        connection.execute("DROP TABLE IF EXISTS sellers_new")
        connection.execute(
            """CREATE TABLE sellers_new (
                seller_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                identity_key TEXT NOT NULL,
                source_name TEXT NOT NULL DEFAULT 'unknown',
                profile_url TEXT,
                name TEXT,
                is_official_seller INTEGER NOT NULL DEFAULT 0,
                official_complex_name TEXT,
                source_seller_role TEXT,
                phone TEXT,
                phone_source TEXT,
                seller_status TEXT,
                phone_listing_count INTEGER,
                updated_at DATETIME NOT NULL
            )"""
        )
        old_columns = set(current_columns)
        expressions = {
            "source_name": "'unknown'",
            "is_official_seller": "0",
            "seller_status": "NULL",
            "phone_listing_count": "NULL",
        }
        selected = [
            f'"{column}"'
            if column in old_columns
            else expressions.get(column, "NULL")
            for column in desired_columns
        ]
        connection.execute(
            f"""INSERT INTO sellers_new ({', '.join(desired_columns)})
                SELECT {', '.join(selected)} FROM sellers"""
        )
        connection.execute("DROP TABLE sellers")
        connection.execute("ALTER TABLE sellers_new RENAME TO sellers")
        connection.execute(
            "CREATE INDEX idx_sellers_identity ON sellers(identity_key)"
        )
        connection.commit()
    finally:
        connection.execute("PRAGMA foreign_keys = ON")


def _rebuild_junction_with_internal_key(
    connection: sqlite3.Connection,
    *,
    table: str,
    internal_pk: str,
    dimension_table: str,
    dimension_pk: str,
) -> None:
    if internal_pk in _column_names(connection, table):
        return

    replacement = table + "_new"
    connection.execute(f"DROP TABLE IF EXISTS {replacement}")
    connection.execute(
        f"""CREATE TABLE {replacement} (
            {internal_pk} INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_pk INTEGER NOT NULL,
            {dimension_pk} INTEGER NOT NULL,
            UNIQUE (listing_pk, {dimension_pk}),
            FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk)
                ON DELETE CASCADE,
            FOREIGN KEY ({dimension_pk})
                REFERENCES {dimension_table}({dimension_pk})
        )"""
    )
    connection.execute(
        f"""INSERT OR IGNORE INTO {replacement}(listing_pk, {dimension_pk})
            SELECT listing_pk, {dimension_pk} FROM {table}"""
    )
    connection.execute(f"DROP TABLE {table}")
    connection.execute(f"ALTER TABLE {replacement} RENAME TO {table}")


def _rebuild_housing_without_removed_columns(
    connection: sqlite3.Connection,
) -> None:
    removed = {
        "address",
        "house_number",
        "zone",
        "address_id",
        "street_id",
        "house_id",
        "zone_id",
        "repair",
        "residential_complex_id",
        "commission",
    }
    columns = _column_names(connection, "housing")
    if not (columns & removed):
        return

    cursor = connection.execute("SELECT * FROM housing ORDER BY housing_pk")
    column_order = [description[0] for description in cursor.description]
    old_rows = [dict(zip(column_order, row)) for row in cursor.fetchall()]
    connection.execute("DROP TABLE IF EXISTS housing_new")
    connection.execute(
        """CREATE TABLE housing_new (
            housing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_pk INTEGER NOT NULL UNIQUE,
            city TEXT,
            district TEXT,
            street TEXT,
            building_type TEXT,
            is_new_building INTEGER,
            foundation_type TEXT,
            residential_complex_name TEXT,
            rooms INTEGER,
            total_area_m2 REAL,
            floor INTEGER,
            floors_total INTEGER,
            furnished INTEGER,
            monthly_rent REAL,
            rent_currency TEXT,
            price_per_m2 REAL,
            latitude REAL,
            longitude REAL,
            FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk)
                ON DELETE CASCADE
        )"""
    )
    for row in old_rows:
        connection.execute(
            """INSERT INTO housing_new (
                housing_pk, listing_pk, city, district, street,
                building_type, is_new_building, foundation_type,
                residential_complex_name, rooms, total_area_m2, floor,
                floors_total, furnished, monthly_rent, rent_currency,
                price_per_m2, latitude, longitude
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row.get("housing_pk"),
                row.get("listing_pk"),
                row.get("city"),
                row.get("district"),
                combine_street(
                    row.get("street"),
                    row.get("zone"),
                    row.get("house_number"),
                    row.get("address"),
                ),
                row.get("building_type"),
                row.get("is_new_building"),
                row.get("foundation_type"),
                row.get("residential_complex_name"),
                row.get("rooms"),
                row.get("total_area_m2"),
                row.get("floor"),
                row.get("floors_total"),
                row.get("furnished"),
                row.get("monthly_rent"),
                row.get("rent_currency"),
                row.get("price_per_m2"),
                row.get("latitude"),
                row.get("longitude"),
            ),
        )
    connection.execute("DROP TABLE housing")
    connection.execute("ALTER TABLE housing_new RENAME TO housing")


def _migrate_relational_layout(connection: sqlite3.Connection) -> None:
    """Переносит старую схему в новую без таблицы seller_publications."""

    publication_fields = (
        "publication_status",
        "transaction_type",
        "city",
        "district",
        "rooms",
        "total_area_m2",
        "floor",
        "floors_total",
    )
    if _table_exists(connection, "seller_publications"):
        old_columns = _column_names(connection, "seller_publications")
        for field in publication_fields:
            if field not in old_columns:
                continue
            connection.execute(
                f"""UPDATE listings SET {field} = COALESCE((
                       SELECT p.{field} FROM seller_publications p
                       WHERE p.listing_pk = listings.listing_pk
                       ORDER BY p.publication_pk DESC LIMIT 1
                   ), {field})"""
            )

    housing_columns = _column_names(connection, "housing")
    for field in ("city", "district", "rooms", "total_area_m2", "floor", "floors_total"):
        if field in housing_columns:
            connection.execute(
                f"""UPDATE listings SET {field} = COALESCE({field}, (
                       SELECT h.{field} FROM housing h
                       WHERE h.listing_pk = listings.listing_pk
                   ))"""
            )

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TRIGGER IF EXISTS reject_incomplete_seller_publication")
    connection.execute("DROP INDEX IF EXISTS idx_seller_publications_seller")
    connection.execute("DROP INDEX IF EXISTS idx_seller_publications_location")
    connection.execute("DROP TABLE IF EXISTS seller_publications")
    _rebuild_junction_with_internal_key(
        connection,
        table="listing_amenities",
        internal_pk="listing_amenity_pk",
        dimension_table="amenities",
        dimension_pk="amenity_pk",
    )
    _rebuild_junction_with_internal_key(
        connection,
        table="listing_nearby",
        internal_pk="listing_nearby_pk",
        dimension_table="nearby_places",
        dimension_pk="nearby_pk",
    )
    _rebuild_housing_without_removed_columns(connection)
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_location ON listings(city, district)"
    )


def _rebuild_listings_without_identity(
    connection: sqlite3.Connection,
) -> None:
    """Remove listing_identity and declare actual date columns as DATETIME."""

    connection.execute("DROP VIEW IF EXISTS listings_newest_first")
    connection.execute("DROP VIEW IF EXISTS listing_newest_first")
    table_info = connection.execute("PRAGMA table_info(listings)").fetchall()
    columns = {row[1] for row in table_info}
    types = {row[1]: str(row[2] or "").upper() for row in table_info}
    rebuild_required = (
        "listing_identity" in columns
        or types.get("published_at") != "DATETIME"
        or types.get("first_seen_at") != "DATETIME"
    )
    if not rebuild_required:
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_platform "
            "ON listings(source_name, platform_listing_id)"
        )
        return

    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE IF EXISTS listings_new")
    connection.execute(
        """CREATE TABLE listings_new (
            listing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            platform_listing_id TEXT,
            source_name TEXT NOT NULL DEFAULT 'unknown',
            seller_pk INTEGER NOT NULL,
            title TEXT,
            price INTEGER,
            currency TEXT,
            url TEXT NOT NULL UNIQUE,
            published_at DATETIME,
            description TEXT,
            description_length INTEGER NOT NULL DEFAULT 0,
            first_seen_at DATETIME NOT NULL,
            last_seen_at DATETIME,
            last_checked_at DATETIME,
            last_changed_at DATETIME,
            current_price REAL,
            previous_price REAL,
            previous_currency TEXT,
            publication_status TEXT NOT NULL DEFAULT 'active',
            transaction_type TEXT,
            city TEXT,
            district TEXT,
            rooms INTEGER,
            total_area_m2 REAL,
            floor INTEGER,
            floors_total INTEGER,
            FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk),
            UNIQUE (source_name, platform_listing_id)
        )"""
    )
    connection.execute(
        """INSERT INTO listings_new (
            listing_pk, platform_listing_id, source_name, seller_pk,
            title, price, currency, url, published_at, description,
            description_length, first_seen_at, last_seen_at,
            last_checked_at, last_changed_at, current_price,
            previous_price, previous_currency, publication_status,
            transaction_type, city, district, rooms, total_area_m2,
            floor, floors_total
        )
        SELECT listing_pk, platform_listing_id, source_name, seller_pk,
            title, price, currency, url, published_at, description,
            description_length, first_seen_at, last_seen_at,
            last_checked_at, last_changed_at, current_price,
            previous_price, previous_currency, publication_status,
            transaction_type, city, district, rooms, total_area_m2,
            floor, floors_total
        FROM listings"""
    )
    connection.execute("DROP TABLE listings")
    connection.execute("ALTER TABLE listings_new RENAME TO listings")
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_pk)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_location "
        "ON listings(city, district)"
    )
    connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_listings_source_platform "
        "ON listings(source_name, platform_listing_id)"
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")


def _normalize_database_dates(connection: sqlite3.Connection) -> None:
    for table, primary_key, column in (
        ("scrape_runs", "run_pk", "started_at"),
        ("sellers", "seller_pk", "updated_at"),
        ("listings", "listing_pk", "published_at"),
        ("listings", "listing_pk", "first_seen_at"),
        ("listings", "listing_pk", "last_seen_at"),
        ("listings", "listing_pk", "last_checked_at"),
        ("listings", "listing_pk", "last_changed_at"),
        ("listing_history", "history_pk", "recorded_at"),
    ):
        if column not in _column_names(connection, table):
            continue
        rows = connection.execute(
            f"SELECT {primary_key}, {column} FROM {table} "
            f"WHERE {column} IS NOT NULL"
        ).fetchall()
        for row_pk, value in rows:
            formatted = database_datetime(value)
            if formatted and formatted != value:
                connection.execute(
                    f"UPDATE {table} SET {column} = ? "
                    f"WHERE {primary_key} = ?",
                    (formatted, row_pk),
                )


def _normalize_seller_identity_urls(
    connection: sqlite3.Connection,
) -> None:
    """Store only a profile URL, or the listing URL when no profile exists."""

    rows = connection.execute(
        "SELECT seller_pk, identity_key, profile_url FROM sellers"
    ).fetchall()
    for seller_pk, current_identity, profile_url in rows:
        # Uybor has no usable personal profile URLs.  New rows are keyed by
        # (phone, name), so do not rewrite that identity to the first listing
        # URL during the legacy URL-normalization migration.
        if str(current_identity or "").startswith("uybor:contact:"):
            continue
        uybor_row = connection.execute(
            """SELECT 1 FROM listings
               WHERE seller_pk = ? AND lower(source_name) = 'uybor' LIMIT 1""",
            (seller_pk,),
        ).fetchone()
        if uybor_row:
            continue
        listing_row = connection.execute(
            "SELECT url FROM listings WHERE seller_pk = ? ORDER BY listing_pk LIMIT 1",
            (seller_pk,),
        ).fetchone()
        desired = clean_text(profile_url) or (
            clean_text(listing_row[0]) if listing_row else None
        )
        if not desired or desired == current_identity:
            continue
        conflict = connection.execute(
            "SELECT seller_pk FROM sellers WHERE identity_key = ? AND seller_pk <> ?",
            (desired, seller_pk),
        ).fetchone()
        if conflict:
            target_pk = conflict[0]
            connection.execute(
                """UPDATE sellers SET
                       profile_url = COALESCE(profile_url, ?),
                       name = COALESCE(name, (SELECT name FROM sellers WHERE seller_pk = ?)),
                       phone = COALESCE(phone, (SELECT phone FROM sellers WHERE seller_pk = ?)),
                       phone_source = COALESCE(phone_source, (SELECT phone_source FROM sellers WHERE seller_pk = ?))
                   WHERE seller_pk = ?""",
                (profile_url, seller_pk, seller_pk, seller_pk, target_pk),
            )
            connection.execute(
                "UPDATE listings SET seller_pk = ? WHERE seller_pk = ?",
                (target_pk, seller_pk),
            )
            connection.execute(
                "DELETE FROM sellers WHERE seller_pk = ?", (seller_pk,)
            )
        else:
            connection.execute(
                "UPDATE sellers SET identity_key = ? WHERE seller_pk = ?",
                (desired, seller_pk),
            )


def _merge_uybor_sellers_by_contact(connection: sqlite3.Connection) -> None:
    """Merge legacy Uybor seller rows by the stable ``(phone, name)`` pair.

    Older runs used the listing URL as ``identity_key``.  Once a phone is
    revealed by an authenticated page, all Uybor listings for the same author
    must point to one seller row.  Rows with a missing part of the pair are
    deliberately left untouched to avoid merging unrelated people.
    """

    rows = connection.execute(
        """
        SELECT s.seller_pk, s.name, s.phone
        FROM sellers AS s
        WHERE EXISTS (
            SELECT 1 FROM listings AS l
            WHERE l.seller_pk = s.seller_pk AND lower(l.source_name) = 'uybor'
        )
        ORDER BY s.seller_pk
        """
    ).fetchall()
    grouped: dict[tuple[str, str], list[tuple[int, str, str]]] = {}
    for seller_pk, name, phone in rows:
        normalized_phone = normalize_phone(phone, default_country_code="998")
        normalized_name = clean_text(name)
        if not normalized_phone or not normalized_name:
            continue
        grouped.setdefault(
            (normalized_phone, normalized_name.casefold()), []
        ).append((seller_pk, normalized_name, normalized_phone))

    for (phone, name_key), candidates in grouped.items():
        target_pk, target_name, _ = candidates[0]
        for duplicate_pk, _, _ in candidates[1:]:
            connection.execute(
                "UPDATE listings SET seller_pk = ? WHERE seller_pk = ?",
                (target_pk, duplicate_pk),
            )
            connection.execute(
                """
                UPDATE sellers SET
                    profile_url = COALESCE(profile_url,
                        (SELECT profile_url FROM sellers WHERE seller_pk = ?)),
                    official_complex_name = COALESCE(official_complex_name,
                        (SELECT official_complex_name FROM sellers WHERE seller_pk = ?)),
                    phone = COALESCE(phone, ?),
                    phone_source = COALESCE(phone_source,
                        (SELECT phone_source FROM sellers WHERE seller_pk = ?)),
                    name = COALESCE(name, ?)
                WHERE seller_pk = ?
                """,
                (
                    duplicate_pk,
                    duplicate_pk,
                    phone,
                    duplicate_pk,
                    target_name,
                    target_pk,
                ),
            )
            connection.execute(
                "DELETE FROM sellers WHERE seller_pk = ?", (duplicate_pk,)
            )
        connection.execute(
            """
            UPDATE sellers
            SET identity_key = ?, phone = ?, name = ?
            WHERE seller_pk = ?
            """,
            (f"uybor:contact:{phone}:{name_key}", phone, target_name, target_pk),
        )


def create_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)
    _rename_seen_columns(connection)
    _enforce_single_listing_image(connection)
    # Миграция со старой нормализацией телефонов: телефон теперь хранится
    # непосредственно у продавца, поэтому одинаковые номера не являются
    # уникальными сущностями и могут встречаться в нескольких строках.
    _ensure_column(connection, "sellers", "phone", "TEXT")
    _ensure_column(connection, "sellers", "phone_source", "TEXT")
    # Старый listings.phone_pk больше не используется. SQLite поддерживает
    # DROP COLUMN в актуальных версиях, а для очень старых баз оставляем
    # совместимый хвост до следующей пересборки.
    listing_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(listings)")
    }
    if "phone_pk" in listing_columns and connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='phones'"
    ).fetchone():
        connection.execute(
            """UPDATE sellers SET phone = COALESCE(phone, (
                   SELECT p.normalized_phone FROM phones p
                   JOIN listings l ON l.phone_pk = p.phone_pk
                   WHERE l.seller_pk = sellers.seller_pk LIMIT 1))
               WHERE phone IS NULL"""
        )
        connection.execute(
            """UPDATE sellers SET phone_source = COALESCE(phone_source, (
                   SELECT l.source_name FROM listings l
                   JOIN phones p ON p.phone_pk = l.phone_pk
                   WHERE l.seller_pk = sellers.seller_pk LIMIT 1))
               WHERE phone IS NOT NULL AND phone_source IS NULL"""
        )
    if "phone_pk" in listing_columns:
        connection.execute("DROP INDEX IF EXISTS idx_listings_phone")
        connection.commit()
        connection.execute("PRAGMA foreign_keys = OFF")
        try:
            connection.execute("ALTER TABLE listings DROP COLUMN phone_pk")
        except sqlite3.OperationalError:
            # Старые SQLite не могут удалить колонку, участвующую во внешнем
            # ключе. Пересоздаём только эту таблицу без phone_pk.
            connection.execute("DROP TABLE IF EXISTS listings_new")
            connection.execute(
                """CREATE TABLE listings_new (
                    listing_pk INTEGER PRIMARY KEY AUTOINCREMENT,
                    platform_listing_id TEXT,
                    source_name TEXT NOT NULL DEFAULT 'unknown',
                    seller_pk INTEGER NOT NULL,
                    title TEXT, price INTEGER, currency TEXT,
                    url TEXT NOT NULL UNIQUE, published_at DATETIME,
                    description_length INTEGER NOT NULL DEFAULT 0,
                    first_seen_at DATETIME NOT NULL,
                    FOREIGN KEY (seller_pk) REFERENCES sellers(seller_pk),
                    UNIQUE (source_name, platform_listing_id)
                )"""
            )
            connection.execute(
                """INSERT INTO listings_new
                   (listing_pk, platform_listing_id, source_name,
                    seller_pk, title, price, currency, url,
                    published_at, description_length, first_seen_at)
                   SELECT listing_pk, platform_listing_id, source_name,
                    seller_pk, title, price, currency, url,
                    published_at, description_length, first_seen_at
                   FROM listings"""
            )
            connection.execute("DROP TABLE listings")
            connection.execute("ALTER TABLE listings_new RENAME TO listings")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_listings_seller ON listings(seller_pk)"
            )
    # У старых баз возможна зависимость listings -> phones; отключаем
    # проверку только на время удаления устаревших таблиц.
    # Переносим уже известные номера в sellers до удаления справочника.
    old_tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "phones" in old_tables and "phone_pk" in {
        row[1] for row in connection.execute("PRAGMA table_info(listings)")
    }:
        connection.execute(
            """UPDATE sellers SET phone = COALESCE(phone, (
                   SELECT p.normalized_phone FROM phones p
                   JOIN listings l ON l.phone_pk = p.phone_pk
                   WHERE l.seller_pk = sellers.seller_pk LIMIT 1))
               WHERE phone IS NULL"""
        )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TABLE IF EXISTS seller_phones")
    connection.execute("DROP TABLE IF EXISTS phones")
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    # Мягкая миграция базы, созданной монолитной версией скрипта.
    _ensure_column(
        connection,
        "scrape_runs",
        "source_name",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(
        connection,
        "listings",
        "source_name",
        "TEXT NOT NULL DEFAULT 'unknown'",
    )
    _ensure_column(connection, "listings", "published_at", "DATETIME")
    _ensure_column(connection, "listings", "description", "TEXT")
    _ensure_column(
        connection,
        "listings",
        "description_length",
        "INTEGER NOT NULL DEFAULT 0",
    )
    for name, definition in (
        ("publication_status", "TEXT NOT NULL DEFAULT 'active'"),
        ("transaction_type", "TEXT"),
        ("city", "TEXT"),
        ("district", "TEXT"),
        ("rooms", "INTEGER"),
        ("total_area_m2", "REAL"),
        ("floor", "INTEGER"),
        ("floors_total", "INTEGER"),
    ):
        _ensure_column(connection, "listings", name, definition)
    for name, definition in (
        ("last_seen_at", "DATETIME"),
        ("last_checked_at", "DATETIME"),
        ("last_changed_at", "DATETIME"),
        ("current_price", "REAL"),
        ("previous_price", "REAL"),
        ("previous_currency", "TEXT"),
    ):
        _ensure_column(connection, "listings", name, definition)
    _ensure_column(connection, "housing", "city", "TEXT")
    _ensure_column(connection, "housing", "district", "TEXT")
    for name, definition in (
        ("street", "TEXT"),
        ("building_type", "TEXT"),
        ("is_new_building", "INTEGER"),
        ("foundation_type", "TEXT"),
        ("residential_complex_name", "TEXT"),
    ):
        _ensure_column(connection, "housing", name, definition)
    _ensure_column(connection, "housing", "monthly_rent", "REAL")
    _ensure_column(connection, "housing", "rent_currency", "TEXT")
    _ensure_column(connection, "housing", "price_per_m2", "REAL")
    _ensure_column(connection, "housing", "latitude", "REAL")
    _ensure_column(connection, "housing", "longitude", "REAL")
    _ensure_column(
        connection,
        "sellers",
        "is_official_seller",
        "INTEGER NOT NULL DEFAULT 0",
    )
    _ensure_column(connection, "sellers", "official_complex_name", "TEXT")
    _rebuild_sellers_schema(connection)
    _migrate_relational_layout(connection)
    _rebuild_listings_without_identity(connection)
    ensure_listing_seller_links(connection)
    # Columns may have been removed by a legacy table rebuild above, so the
    # observation model is asserted once more against the final table.
    for name, definition in (
        ("last_seen_at", "DATETIME"),
        ("last_checked_at", "DATETIME"),
        ("last_changed_at", "DATETIME"),
        ("current_price", "REAL"),
        ("previous_price", "REAL"),
        ("previous_currency", "TEXT"),
    ):
        _ensure_column(connection, "listings", name, definition)
    connection.execute(
        """CREATE TABLE IF NOT EXISTS listing_history (
            history_pk INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_pk INTEGER NOT NULL,
            recorded_at DATETIME NOT NULL,
            price REAL,
            currency TEXT,
            description TEXT,
            publication_status TEXT NOT NULL,
            change_fields TEXT NOT NULL DEFAULT '',
            FOREIGN KEY (listing_pk) REFERENCES listings(listing_pk)
                ON DELETE CASCADE
        )"""
    )
    connection.execute(
        """UPDATE listings
           SET current_price = COALESCE(current_price, price),
               last_seen_at = COALESCE(last_seen_at, first_seen_at)"""
    )
    connection.execute(
        """INSERT INTO listing_history(
               listing_pk, recorded_at, price, currency, description,
               publication_status, change_fields
           )
           SELECT l.listing_pk, l.first_seen_at,
                  COALESCE(l.current_price, l.price), l.currency,
                  l.description, l.publication_status, 'initial'
           FROM listings l
           WHERE NOT EXISTS (
               SELECT 1 FROM listing_history h
               WHERE h.listing_pk = l.listing_pk
           )"""
    )
    _normalize_database_dates(connection)
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listings_published_julianday "
        "ON listings(julianday(published_at) DESC)"
    )
    connection.execute(
        "CREATE INDEX IF NOT EXISTS idx_listing_history_listing_recorded "
        "ON listing_history(listing_pk, recorded_at DESC)"
    )
    connection.execute("DROP VIEW IF EXISTS listings_newest_first")
    connection.execute("DROP VIEW IF EXISTS listing_newest_first")
    connection.commit()


def _database_bool(value: Any) -> int | None:
    return None if value is None else int(bool(value))


def _seller_identity(listing: dict[str, Any]) -> str:
    """Временный ключ сырой строки seller, привязанный к публикации."""

    listing_url = str(listing.get("url") or "").strip()
    if listing_url:
        return listing_url
    return (
        f"{listing.get('source') or 'unknown'}:"
        f"{listing.get('id') or 'unknown'}"
    )


def _insert_seller(
    cursor: sqlite3.Cursor,
    listing: dict[str, Any],
    saved_at: str,
    *,
    phone: Any = None,
    use_phone_override: bool = False,
) -> int:
    seller = listing.get("seller") or {}
    stored_phone = phone if use_phone_override else seller.get("phone")
    cursor.execute(
        """
        INSERT INTO sellers (
            identity_key, source_name, profile_url, name, is_official_seller,
            official_complex_name, source_seller_role, phone, phone_source,
            seller_status, phone_listing_count, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, ?)
        """,
        (
            _seller_identity(listing),
            listing.get("source") or "unknown",
            seller.get("profile_url"),
            seller.get("name"),
            _database_bool(seller.get("is_official_seller")) or 0,
            seller.get("official_complex_name"),
            seller.get("seller_role"),
            stored_phone,
            seller.get("phone_source") or listing.get("source"),
            saved_at,
        ),
    )
    return int(cursor.lastrowid)


def _insert_listing_sellers(
    cursor: sqlite3.Cursor,
    listing: dict[str, Any],
    saved_at: str,
) -> list[int]:
    """Insert one seller fact per contact phone for this publication."""

    seller = listing.get("seller") or {}
    raw_phones = seller.get("phones")
    candidates = raw_phones if isinstance(raw_phones, list) else []
    if seller.get("phone") is not None:
        candidates = [seller.get("phone"), *candidates]
    phones: list[str] = []
    for value in candidates:
        text = str(value or "").strip()
        if text and text not in phones:
            phones.append(text)
    if not phones:
        return [_insert_seller(cursor, listing, saved_at)]
    return [
        _insert_seller(
            cursor,
            listing,
            saved_at,
            phone=phone,
            use_phone_override=True,
        )
        for phone in phones
    ]


def _save_dimensions(
    cursor: sqlite3.Cursor,
    listing_pk: int,
    values: list[str] | None,
    dimension_table: str,
    dimension_pk: str,
    junction_table: str,
) -> None:
    cursor.execute(
        f"DELETE FROM {junction_table} WHERE listing_pk = ?",
        (listing_pk,),
    )
    for value in values or []:
        cursor.execute(
            f"INSERT OR IGNORE INTO {dimension_table}(name) VALUES (?)",
            (value,),
        )
        cursor.execute(
            f"SELECT {dimension_pk} FROM {dimension_table} WHERE name = ?",
            (value,),
        )
        value_pk = cursor.fetchone()[0]
        cursor.execute(
            f"INSERT OR IGNORE INTO {junction_table} "
            f"(listing_pk, {dimension_pk}) VALUES (?, ?)",
            (listing_pk, value_pk),
        )


def _save_images(
    cursor: sqlite3.Cursor,
    listing_pk: int,
    source_name: str,
    images: list[Any] | None,
    saved_at: str,
) -> None:
    """Save only the first valid image reference for a listing.

    Images are intentionally stored as references rather than blobs.  The
    optional ``local_path`` and ``telegram_file_id`` columns let a Telegram
    worker cache/download an image once and reuse it on later sends.
    """

    selected: tuple[dict[str, Any], str] | None = None
    for image in images or []:
        if isinstance(image, str):
            image = {"url": image}
        if not isinstance(image, dict):
            continue
        image_url = (
            image.get("url")
            or image.get("image_url")
            or image.get("src")
            or image.get("fname")
        )
        if not isinstance(image_url, str):
            continue
        image_url = image_url.strip()
        if not image_url.startswith(("http://", "https://")):
            continue
        selected = (image, image_url)
        break

    if selected is None:
        return

    image, image_url = selected
    cursor.execute(
        """
        INSERT INTO listing_images(
            listing_pk, source_name, source_image_id, image_url, file_name,
            local_path, telegram_file_id, mime_type, width, height,
            sort_order, is_primary, first_seen_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?)
        ON CONFLICT(listing_pk) DO UPDATE SET
            source_name = excluded.source_name,
            source_image_id = COALESCE(
                excluded.source_image_id, listing_images.source_image_id
            ),
            image_url = excluded.image_url,
            file_name = COALESCE(excluded.file_name, listing_images.file_name),
            local_path = CASE
                WHEN listing_images.image_url = excluded.image_url
                THEN COALESCE(excluded.local_path, listing_images.local_path)
                ELSE excluded.local_path
            END,
            telegram_file_id = CASE
                WHEN listing_images.image_url = excluded.image_url
                THEN COALESCE(
                    excluded.telegram_file_id,
                    listing_images.telegram_file_id
                )
                ELSE excluded.telegram_file_id
            END,
            mime_type = CASE
                WHEN listing_images.image_url = excluded.image_url
                THEN COALESCE(excluded.mime_type, listing_images.mime_type)
                ELSE excluded.mime_type
            END,
            width = CASE
                WHEN listing_images.image_url = excluded.image_url
                THEN COALESCE(excluded.width, listing_images.width)
                ELSE excluded.width
            END,
            height = CASE
                WHEN listing_images.image_url = excluded.image_url
                THEN COALESCE(excluded.height, listing_images.height)
                ELSE excluded.height
            END,
            sort_order = 0,
            is_primary = 1,
            first_seen_at = excluded.first_seen_at
        """,
        (
            listing_pk,
            source_name,
            image.get("source_image_id") or image.get("id"),
            image_url,
            image.get("file_name") or image.get("fileName"),
            image.get("local_path"),
            image.get("telegram_file_id"),
            image.get("mime_type"),
            image.get("width"),
            image.get("height"),
            saved_at,
        ),
    )


def _listing_publication_status(listing: dict[str, Any]) -> str:
    explicit = listing.get("publication_status")
    if explicit in {"active", "closed"}:
        return explicit

    listing_url = str(listing.get("url") or "").split("?", 1)[0].rstrip("/")
    seller = listing.get("seller") or {}
    for status, publications in (
        ("active", seller.get("active_publications") or []),
        ("closed", seller.get("closed_publications") or []),
    ):
        if any(
            str(item.get("url") or "").split("?", 1)[0].rstrip("/")
            == listing_url
            for item in publications
        ):
            return status
    return "active"


def _save_listing(
    cursor: sqlite3.Cursor,
    listing: dict[str, Any],
    seller_pk: int,
    saved_at: str,
) -> int:
    source_name = listing.get("source") or "unknown"
    platform_listing_id = listing.get("id")
    listing_url = listing.get("url")
    housing = listing.get("housing") or {}
    values = (
        platform_listing_id,
        source_name,
        seller_pk,
        listing.get("title"),
        listing.get("price"),
        listing.get("currency"),
        listing_url,
        database_datetime(listing.get("published_at")),
        clean_text(listing.get("description")),
        listing.get("description_length", 0),
        saved_at,
        saved_at,
        saved_at,
        listing.get("price"),
        _listing_publication_status(listing),
        listing.get("transaction_type") or listing.get("type"),
        housing.get("city"),
        housing.get("district"),
        housing.get("rooms"),
        housing.get("total_area_m2"),
        housing.get("floor"),
        housing.get("floors_total"),
    )
    cursor.execute(
        """INSERT INTO listings (
            platform_listing_id, source_name, seller_pk, title, price,
            currency, url, published_at, description, description_length,
            first_seen_at, last_seen_at, last_checked_at, current_price,
            publication_status, transaction_type, city, district, rooms,
            total_area_m2, floor, floors_total
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        values,
    )
    return int(cursor.lastrowid)


def _find_listing_pk(
    cursor: sqlite3.Cursor,
    listing: dict[str, Any],
) -> int | None:
    source_name = listing.get("source") or "unknown"
    platform_listing_id = listing.get("id")
    if platform_listing_id is not None:
        row = cursor.execute(
            """SELECT listing_pk FROM listings
               WHERE source_name = ? AND platform_listing_id = ?""",
            (source_name, platform_listing_id),
        ).fetchone()
        if row:
            return row[0]
    row = cursor.execute(
        "SELECT listing_pk FROM listings WHERE url = ?",
        (listing.get("url"),),
    ).fetchone()
    return row[0] if row else None


def _values_equal(left: Any, right: Any) -> bool:
    """Compare persisted values without treating integer/float forms as changes."""

    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    if isinstance(left, (int, float)) or isinstance(right, (int, float)):
        try:
            return abs(float(left) - float(right)) < 1e-9
        except (TypeError, ValueError):
            pass
    return str(left).strip() == str(right).strip()


def _insert_listing_history(
    cursor: sqlite3.Cursor,
    listing_pk: int,
    recorded_at: str,
    *,
    price: Any,
    currency: Any,
    description: Any,
    publication_status: str,
    change_fields: list[str] | tuple[str, ...],
) -> None:
    cursor.execute(
        """INSERT INTO listing_history(
               listing_pk, recorded_at, price, currency, description,
               publication_status, change_fields
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            listing_pk,
            recorded_at,
            price,
            currency,
            description,
            publication_status,
            ",".join(change_fields),
        ),
    )


def _update_existing_listing(
    cursor: sqlite3.Cursor,
    listing_pk: int,
    listing: dict[str, Any],
    saved_at: str,
) -> tuple[bool, bool]:
    """Refresh a known publication and append history only for real changes."""

    cursor.execute(
        """SELECT l.*, h.street, h.building_type, h.is_new_building,
                  h.foundation_type, h.residential_complex_name, h.furnished,
                  h.monthly_rent, h.rent_currency, h.price_per_m2,
                  h.latitude, h.longitude
           FROM listings l
           LEFT JOIN housing h ON h.listing_pk = l.listing_pk
           WHERE l.listing_pk = ?""",
        (listing_pk,),
    )
    row = cursor.fetchone()
    if row is None:
        return False, False
    columns = [column[0] for column in cursor.description]
    previous = dict(zip(columns, row))
    housing = listing.get("housing") or {}

    old_price = (
        previous.get("current_price")
        if previous.get("current_price") is not None
        else previous.get("price")
    )
    incoming_price = listing.get("price")
    current_price = old_price if incoming_price is None else incoming_price
    incoming_currency = listing.get("currency")
    current_currency = incoming_currency or previous.get("currency")
    incoming_description = clean_text(listing.get("description"))
    current_description = (
        previous.get("description")
        if incoming_description is None
        else incoming_description
    )
    current_status = _listing_publication_status(listing)
    price_changed = incoming_price is not None and (
        not _values_equal(old_price, incoming_price)
        or (
            incoming_currency not in (None, "")
            and not _values_equal(previous.get("currency"), incoming_currency)
        )
    )

    changed_fields: list[str] = []
    if price_changed:
        changed_fields.append("price")
    if (
        incoming_description is not None
        and not _values_equal(previous.get("description"), incoming_description)
    ):
        changed_fields.append("description")
    if not _values_equal(previous.get("publication_status"), current_status):
        changed_fields.append("status")

    published_at = database_datetime(listing.get("published_at"))
    transaction_type = listing.get("transaction_type") or listing.get("type")
    listing_updates = {
        "title": listing.get("title"),
        "published_at": published_at,
        "transaction_type": transaction_type,
        "city": housing.get("city"),
        "district": housing.get("district"),
        "rooms": housing.get("rooms"),
        "total_area_m2": housing.get("total_area_m2"),
        "floor": housing.get("floor"),
        "floors_total": housing.get("floors_total"),
    }
    for field, value in listing_updates.items():
        if value is not None and not _values_equal(previous.get(field), value):
            changed_fields.append(field)

    housing_updates = {
        "street": housing.get("street"),
        "building_type": housing.get("building_type"),
        "is_new_building": _database_bool(housing.get("is_new_building")),
        "foundation_type": housing.get("foundation_type"),
        "residential_complex_name": housing.get("residential_complex_name"),
        "furnished": _database_bool(housing.get("furnished")),
        "monthly_rent": housing.get("monthly_rent"),
        "rent_currency": housing.get("rent_currency"),
        "price_per_m2": housing.get("price_per_m2"),
        "latitude": housing.get("latitude"),
        "longitude": housing.get("longitude"),
    }
    for field, value in housing_updates.items():
        if value is not None and not _values_equal(previous.get(field), value):
            changed_fields.append(field)

    changed_fields = list(dict.fromkeys(changed_fields))
    description_length = previous.get("description_length") or 0
    if incoming_description is not None:
        description_length = len(incoming_description)
    cursor.execute(
        """UPDATE listings SET
               title = COALESCE(?, title),
               price = ?,
               current_price = ?,
               previous_price = CASE WHEN ? THEN ? ELSE previous_price END,
               previous_currency = CASE WHEN ? THEN currency ELSE previous_currency END,
               currency = ?,
               published_at = COALESCE(?, published_at),
               description = ?,
               description_length = ?,
               last_seen_at = ?,
               last_checked_at = ?,
               last_changed_at = CASE WHEN ? THEN ? ELSE last_changed_at END,
               publication_status = ?,
               transaction_type = COALESCE(?, transaction_type),
               city = COALESCE(?, city),
               district = COALESCE(?, district),
               rooms = COALESCE(?, rooms),
               total_area_m2 = COALESCE(?, total_area_m2),
               floor = COALESCE(?, floor),
               floors_total = COALESCE(?, floors_total)
           WHERE listing_pk = ?""",
        (
            listing.get("title"),
            current_price,
            current_price,
            int(price_changed),
            old_price,
            int(price_changed),
            current_currency,
            published_at,
            current_description,
            description_length,
            saved_at,
            saved_at,
            int(bool(changed_fields)),
            saved_at,
            current_status,
            transaction_type,
            housing.get("city"),
            housing.get("district"),
            housing.get("rooms"),
            housing.get("total_area_m2"),
            housing.get("floor"),
            housing.get("floors_total"),
            listing_pk,
        ),
    )
    _save_housing(cursor, listing_pk, housing)
    if listing.get("amenities"):
        _save_dimensions(
            cursor, listing_pk, listing.get("amenities"),
            "amenities", "amenity_pk", "listing_amenities",
        )
    if listing.get("nearby"):
        _save_dimensions(
            cursor, listing_pk, listing.get("nearby"),
            "nearby_places", "nearby_pk", "listing_nearby",
        )
    _save_images(
        cursor,
        listing_pk,
        listing.get("source") or previous.get("source_name") or "unknown",
        listing.get("images"),
        saved_at,
    )
    if changed_fields:
        _insert_listing_history(
            cursor,
            listing_pk,
            saved_at,
            price=current_price,
            currency=current_currency,
            description=current_description,
            publication_status=current_status,
            change_fields=changed_fields,
        )
    return bool(changed_fields), price_changed


def _save_housing(
    cursor: sqlite3.Cursor,
    listing_pk: int,
    housing: dict[str, Any],
) -> None:
    cursor.execute(
        """
        INSERT INTO housing (
            listing_pk, city, district, street, building_type,
            is_new_building, foundation_type, residential_complex_name,
            rooms, total_area_m2, floor, floors_total, furnished,
            monthly_rent, rent_currency, price_per_m2, latitude, longitude
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(listing_pk) DO UPDATE SET
            city = COALESCE(excluded.city, housing.city),
            district = COALESCE(excluded.district, housing.district),
            street = COALESCE(excluded.street, housing.street),
            building_type = COALESCE(
                excluded.building_type, housing.building_type
            ),
            is_new_building = COALESCE(
                excluded.is_new_building, housing.is_new_building
            ),
            foundation_type = COALESCE(
                excluded.foundation_type, housing.foundation_type
            ),
            residential_complex_name = COALESCE(
                excluded.residential_complex_name,
                housing.residential_complex_name
            ),
            rooms = COALESCE(excluded.rooms, housing.rooms),
            total_area_m2 = COALESCE(
                excluded.total_area_m2, housing.total_area_m2
            ),
            floor = COALESCE(excluded.floor, housing.floor),
            floors_total = COALESCE(
                excluded.floors_total, housing.floors_total
            ),
            furnished = COALESCE(excluded.furnished, housing.furnished),
            monthly_rent = COALESCE(
                excluded.monthly_rent, housing.monthly_rent
            ),
            rent_currency = COALESCE(
                excluded.rent_currency, housing.rent_currency
            ),
            price_per_m2 = COALESCE(
                excluded.price_per_m2, housing.price_per_m2
            ),
            latitude = COALESCE(excluded.latitude, housing.latitude),
            longitude = COALESCE(excluded.longitude, housing.longitude)
        """,
        (
            listing_pk,
            housing.get("city"),
            housing.get("district"),
            housing.get("street"),
            housing.get("building_type"),
            _database_bool(housing.get("is_new_building")),
            housing.get("foundation_type"),
            housing.get("residential_complex_name"),
            housing.get("rooms"),
            housing.get("total_area_m2"),
            housing.get("floor"),
            housing.get("floors_total"),
            _database_bool(housing.get("furnished")),
            housing.get("monthly_rent"),
            housing.get("rent_currency"),
            housing.get("price_per_m2"),
            housing.get("latitude"),
            housing.get("longitude"),
        ),
    )


def _seller_group_key(
    identity_key: str,
    name: Any,
    phones: list[str],
) -> tuple[str, ...]:
    """Связывает записи продавца между площадками по номеру телефона."""

    normalized_phones = sorted(filter(None, (normalize_phone(p) for p in phones)))
    if normalized_phones:
        return "phone", normalized_phones[0]
    normalized_name = (clean_text(name) or "").casefold()
    if normalized_name:
        return "name", normalized_name
    return "identity", identity_key


def _publication_key(publication: dict[str, Any]) -> tuple:
    fingerprint = publication_fingerprint(publication)
    if fingerprint is not None:
        normalized_fingerprint = tuple(
            (clean_text(value) or "").casefold()
            if isinstance(value, str)
            else value
            for value in fingerprint
        )
        return "housing", *normalized_fingerprint
    return (
        "publication",
        publication.get("source") or "unknown",
        publication.get("id") or publication.get("url"),
    )


def _deduplicate_publications(
    publications: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Объединяет один объект, опубликованный на нескольких площадках."""

    unique: dict[tuple, dict[str, Any]] = {}
    for original in publications:
        publication = dict(original)
        occurrence = {
            "source": publication.get("source") or "unknown",
            "id": publication.get("id"),
            "url": publication.get("url"),
        }
        publication["sources"] = [occurrence["source"]]
        publication["source_urls"] = [occurrence]
        key = _publication_key(publication)
        current = unique.get(key)
        if current is None:
            unique[key] = publication
            continue

        if publication.get("status") == "active":
            current["status"] = "active"
        if not current.get("type") and publication.get("type"):
            current["type"] = publication["type"]
        for field in (
            "title",
            "city",
            "district",
            "rooms",
            "total_area_m2",
            "floor",
            "floors_total",
        ):
            if current.get(field) is None and publication.get(field) is not None:
                current[field] = publication[field]
        if occurrence["source"] not in current["sources"]:
            current["sources"].append(occurrence["source"])
        if occurrence not in current["source_urls"]:
            current["source_urls"].append(occurrence)

    return sorted(
        unique.values(),
        key=lambda item: (
            item.get("status") != "active",
            item.get("source") or "",
            item.get("url") or "",
        ),
    )


def _group_publications(
    cursor: sqlite3.Cursor,
    seller_pks: list[int],
    source_name: str | None = None,
) -> list[dict[str, Any]]:
    placeholders = ",".join("?" for _ in seller_pks)
    source_clause = ""
    parameters: list[Any] = list(seller_pks)
    if source_name:
        source_clause = " AND lower(source_name) = ?"
        parameters.append(source_name.casefold())
    return [
        {
            "source": row[0],
            "id": row[1],
            "status": row[2],
            "type": row[3],
            "title": row[4],
            "url": row[5],
            "city": row[6],
            "district": row[7],
            "rooms": row[8],
            "total_area_m2": row[9],
            "floor": row[10],
            "floors_total": row[11],
        }
        for row in cursor.execute(
            f"""
            SELECT source_name, platform_listing_id, publication_status,
                   transaction_type, title, url, city, district, rooms,
                   total_area_m2, floor, floors_total
            FROM listings
            WHERE seller_pk IN ({placeholders}){source_clause}
            """,
            parameters,
        ).fetchall()
    ]


def _observed_profile_publications(
    cursor: sqlite3.Cursor,
    listings: list[dict[str, Any]],
) -> dict[int, list[dict[str, Any]]]:
    """
    Собирает неполные карточки только в памяти для текущего расчёта.

    Эти данные намеренно не записываются ни в одну таблицу объявлений.
    """

    observed: dict[int, list[dict[str, Any]]] = {}
    for listing in listings:
        row = cursor.execute(
            "SELECT seller_pk FROM sellers WHERE identity_key = ?",
            (_seller_identity(listing),),
        ).fetchone()
        if not row:
            continue
        seller_pk = row[0]
        seller = listing.get("seller") or {}
        for status, publications in (
            ("active", seller.get("active_publications") or []),
            ("closed", seller.get("closed_publications") or []),
        ):
            for publication in publications:
                item = dict(publication)
                item["source"] = listing.get("source") or "unknown"
                item["status"] = status
                observed.setdefault(seller_pk, []).append(item)
    return observed


def _refresh_seller_assessments(
    cursor: sqlite3.Cursor,
    observed_publications: dict[int, list[dict[str, Any]]] | None = None,
) -> dict[int, dict[str, Any]]:
    """Пересчитывает оценки после дедупликации всей межплощадочной базы."""

    rows = cursor.execute(
        """
        SELECT s.seller_pk, s.identity_key, s.name, s.profile_url,
               s.is_official_seller, s.official_complex_name,
               s.profile_scan_complete, s.phone, s.sellers_type,
               EXISTS(SELECT 1 FROM listings l
                      WHERE l.seller_pk = s.seller_pk
                        AND lower(l.source_name) = 'etagi') AS has_etagi,
               EXISTS(SELECT 1 FROM listings l
                      WHERE l.seller_pk = s.seller_pk
                        AND lower(l.source_name) = 'uybor') AS has_uybor
               , EXISTS(SELECT 1 FROM listings l
                      WHERE l.seller_pk = s.seller_pk
                        AND lower(l.source_name) = 'realt24') AS has_realt24
        FROM sellers AS s
        ORDER BY s.seller_pk
        """
    ).fetchall()
    sellers: dict[int, dict[str, Any]] = {}
    for (
        seller_pk,
        identity,
        name,
        profile_url,
        is_official_seller,
        official_complex_name,
        scan_complete,
        phone,
        source_role,
        has_etagi,
        has_uybor,
        has_realt24,
    ) in rows:
        entry = sellers.setdefault(
            seller_pk,
            {
                "identity": identity,
                "name": name,
                "profile_url": profile_url,
                "is_official_seller": bool(is_official_seller),
                "official_complex_name": official_complex_name,
                "scan_complete": bool(scan_complete),
                "phones": [],
                "source_role": source_role,
                "has_etagi": bool(has_etagi),
                "has_uybor": bool(has_uybor),
                "has_realt24": bool(has_realt24),
            },
        )
        if phone:
            entry["phones"].append(phone)

    groups: dict[tuple[str, ...], list[int]] = {}
    for seller_pk, seller in sellers.items():
        key = _seller_group_key(
            seller["identity"], seller["name"], seller["phones"]
        )
        groups.setdefault(key, []).append(seller_pk)

    assessments: dict[int, dict[str, Any]] = {}
    for seller_pks in groups.values():
        has_uybor = any(sellers[pk]["has_uybor"] for pk in seller_pks)
        has_realt24 = any(sellers[pk]["has_realt24"] for pk in seller_pks)
        if has_uybor and not has_realt24:
            # Uybor не публикует рабочие страницы продавца, поэтому считаем
            # только полностью просмотренные Uybor-карточки из listings.
            publications = _group_publications(
                cursor, seller_pks, source_name="uybor"
            )
        else:
            publications = _group_publications(cursor, seller_pks)
            for seller_pk in seller_pks:
                publications.extend(
                    (observed_publications or {}).get(seller_pk, [])
                )
            publications = _deduplicate_publications(publications)
        active = [p for p in publications if p.get("status") == "active"]
        closed = [p for p in publications if p.get("status") == "closed"]
        distinct_fingerprints = {
            fingerprint
            for publication in publications
            if (fingerprint := publication_fingerprint(publication)) is not None
        }
        distinct_count = len(distinct_fingerprints)
        representative = sellers[seller_pks[0]]
        stats = {
            "profile_available": bool(publications),
            "active_rent_listings": sum(
                p.get("type") == "rent" for p in active
            ),
            "active_sale_listings": sum(
                p.get("type") == "sale" for p in active
            ),
            "unique_listing_count": len(publications),
            "distinct_housing_listings": distinct_count,
            "active_publications": active,
            "closed_publications": closed,
            "profile_scan_complete": all(
                sellers[pk]["scan_complete"] for pk in seller_pks
            ),
            "closed_publications_available": bool(closed),
        }
        assessment = assess_seller(
            {
                "name": representative["name"],
                "phone": (
                    representative["phones"][0]
                    if representative["phones"]
                    else None
                ),
                "profile_url": representative["profile_url"],
                "is_official_seller": representative[
                    "is_official_seller"
                ],
                "official_complex_name": representative[
                    "official_complex_name"
                ],
                "seller_role": representative.get("source_role"),
            },
            None,
            stats,
        )
        # Агентства, импортированные из Realting, имеют подтверждённый тип
        # профиля. Не заменяем его эвристикой по числу уже открытых объявлений.
        if source_role == "agency" or identity.startswith(
            "https://realting.uz/agencies/"
        ):
            assessment.update(
                {
                    "seller_type": "agency",
                    "seller_role": "agency",
                    "realtor_probability": 1.0,
                    "comment": (
                        "Продавец отмечен как агентство по профилю Realting."
                    ),
                    "assessment_basis": "realting_agency_profile",
                }
            )
        elif any(sellers[pk]["has_etagi"] for pk in seller_pks):
            assessment.update(
                {
                    "seller_type": "likely_realtor",
                    "seller_role": "realtor",
                    "realtor_probability": 1.0,
                    "comment": (
                        "Продавец отмечен как риелтор, поскольку публикация "
                        "получена с Etagi. Количество учитывает полностью "
                        "открытые объявления в текущей базе."
                    ),
                    "assessment_basis": "etagi_realtor_profile",
                }
            )
        for seller_pk in seller_pks:
            assessments[seller_pk] = assessment
            cursor.execute(
                """
                UPDATE sellers SET
                    seller_type = ?, sellers_type = ?, realtor_probability = ?,
                    assessment_comment = ?, active_rent_listings = ?,
                    active_sale_listings = ?, unique_listing_count = ?,
                    distinct_housing_listings = ?
                WHERE seller_pk = ?
                """,
                (
                    assessment["seller_type"],
                    assessment.get("seller_role") or assessment["seller_type"],
                    assessment["realtor_probability"],
                    assessment["comment"],
                    assessment["active_rent_listings"],
                    assessment["active_sale_listings"],
                    assessment["unique_listing_count"],
                    assessment["distinct_housing_listings"],
                    seller_pk,
                ),
            )
    return assessments


def _apply_assessments_to_output(
    cursor: sqlite3.Cursor,
    listings: list[dict[str, Any]],
    assessments: dict[int, dict[str, Any]],
) -> None:
    retained: list[dict[str, Any]] = []
    for listing in listings:
        source_name = listing.get("source") or "unknown"
        listing_pk = _find_listing_pk(cursor, listing)
        row = (
            cursor.execute(
                "SELECT seller_pk FROM listings WHERE listing_pk = ?",
                (listing_pk,),
            ).fetchone()
            if listing_pk is not None
            else None
        )
        # Если текущая запись проиграла накопленной записи при дедупликации,
        # она не должна снова появиться в JSON.
        if not row:
            continue
        if row[0] not in assessments:
            retained.append(listing)
            continue
        contact = listing.get("seller") or {}
        assessment = dict(assessments[row[0]])
        assessment.update(
            {
                "name": normalize_seller_name(contact.get("name"), source_name),
                "phone": contact.get("phone"),
                "phone_source": contact.get("phone_source") or source_name,
                "profile_url": contact.get("profile_url"),
                "is_official_seller": bool(
                    contact.get("is_official_seller")
                ),
                "official_complex_name": contact.get(
                    "official_complex_name"
                ),
                "profile_pages_scanned": contact.get(
                    "profile_pages_scanned", 0
                ),
                "seller_role": contact.get("seller_role")
                or contact.get("sellers_type")
                or assessment.get("seller_type"),
            }
        )
        listing["seller"] = assessment
        retained.append(listing)
    listings[:] = retained


def _save_to_database_once(
    listings: list[dict[str, Any]],
    database_file: str,
    *,
    source_name: str,
    source_url: str,
    listing_limit: int,
    discarded_listings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    saved_at = database_datetime(datetime.now(timezone.utc))
    if saved_at is None:
        raise RuntimeError("Не удалось сформировать время сохранения SQLite")
    connection = sqlite3.connect(
        database_file,
        timeout=DATABASE_BUSY_TIMEOUT_MS / 1000,
    )

    try:
        connection.execute(
            f"PRAGMA busy_timeout = {DATABASE_BUSY_TIMEOUT_MS}"
        )
        connection.execute("PRAGMA foreign_keys = ON")
        create_schema(connection)
        cursor = connection.cursor()
        cursor.execute(
            """
            INSERT INTO scrape_runs(
                started_at, source_url, listing_limit, source_name
            ) VALUES (?, ?, ?, ?)
            """,
            (saved_at, source_url, listing_limit, source_name),
        )
        run_pk = cursor.lastrowid
        listings_saved = 0
        listings_updated = 0
        listings_unchanged = 0
        price_changes = 0
        history_rows_added = 0
        for listing in listings:
            existing_listing_pk = _find_listing_pk(cursor, listing)
            if existing_listing_pk is not None:
                changed, price_changed = _update_existing_listing(
                    cursor, existing_listing_pk, listing, saved_at
                )
                cursor.execute(
                    """INSERT OR IGNORE INTO scrape_run_listings(
                           run_pk, listing_pk
                       ) VALUES (?, ?)""",
                    (run_pk, existing_listing_pk),
                )
                if changed:
                    listings_updated += 1
                    history_rows_added += 1
                else:
                    listings_unchanged += 1
                if price_changed:
                    price_changes += 1
                continue
            seller_pks = _insert_listing_sellers(cursor, listing, saved_at)
            seller_pk = seller_pks[0]
            listing_pk = _save_listing(cursor, listing, seller_pk, saved_at)
            cursor.executemany(
                """INSERT OR IGNORE INTO listing_sellers(
                       listing_pk, seller_pk, is_primary
                   ) VALUES (?, ?, ?)""",
                [
                    (listing_pk, linked_seller_pk, int(index == 0))
                    for index, linked_seller_pk in enumerate(seller_pks)
                ],
            )
            listings_saved += 1
            cursor.execute(
                """
                INSERT OR IGNORE INTO scrape_run_listings(run_pk, listing_pk)
                VALUES (?, ?)
                """,
                (run_pk, listing_pk),
            )
            _save_housing(cursor, listing_pk, listing.get("housing") or {})
            _save_dimensions(
                cursor,
                listing_pk,
                listing.get("amenities"),
                "amenities",
                "amenity_pk",
                "listing_amenities",
            )
            _save_dimensions(
                cursor,
                listing_pk,
                listing.get("nearby"),
                "nearby_places",
                "nearby_pk",
                "listing_nearby",
            )
            _save_images(
                cursor,
                listing_pk,
                listing.get("source") or source_name,
                listing.get("images"),
                saved_at,
            )
            _insert_listing_history(
                cursor,
                listing_pk,
                saved_at,
                price=listing.get("price"),
                currency=listing.get("currency"),
                description=clean_text(listing.get("description")),
                publication_status=_listing_publication_status(listing),
                change_fields=("initial",),
            )
            history_rows_added += 1

        connection.commit()
        return {
            "run_pk": run_pk,
            "listings_saved": listings_saved,
            "listings_updated": listings_updated,
            "listings_unchanged": listings_unchanged,
            "price_changes": price_changes,
            "history_rows_added": history_rows_added,
            "duplicates_removed": 0,
            "historical_duplicates_removed": 0,
        }
    finally:
        connection.close()


def save_to_database(
    listings: list[dict[str, Any]],
    database_file: str,
    *,
    source_name: str,
    source_url: str,
    listing_limit: int,
    discarded_listings: list[dict[str, Any]] | None = None,
) -> dict[str, int]:
    """Сохраняет пакет, повторяя транзакцию при кратком внешнем lock."""

    last_error: sqlite3.OperationalError | None = None
    for attempt in range(DATABASE_LOCK_RETRIES):
        try:
            return _save_to_database_once(
                listings,
                database_file,
                source_name=source_name,
                source_url=source_url,
                listing_limit=listing_limit,
                discarded_listings=discarded_listings,
            )
        except sqlite3.OperationalError as error:
            if not _is_database_lock_error(error):
                raise
            last_error = error
            if attempt + 1 < DATABASE_LOCK_RETRIES:
                time.sleep(attempt + 1)

    raise DatabaseLockedError(
        "Не удалось записать SQLite: файл всё ещё занят другим процессом. "
        "Закройте вкладку БД/SQLite Viewer в VS Code, DB Browser или другой "
        f"парсер, использующий {database_file}, затем повторите запуск."
    ) from last_error
