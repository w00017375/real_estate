from dataclasses import dataclass


@dataclass(frozen=True)
class AppConfig:
    """Настройки общего конвейера, не зависящие от сайта-источника."""

    # None означает полный обход каталога до его фактического окончания.
    max_listings: int | None = None
    output_json: str = "output_json/combined/estate_listings_full.json"
    database_file: str = "olx_apartments.db"
