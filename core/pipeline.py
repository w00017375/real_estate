from copy import deepcopy
from typing import Any

from core.normalization import (
    combine_street,
    normalize_city,
    normalize_district,
)


PUBLICATION_HOUSING_FIELDS = (
    "city",
    "district",
    "rooms",
    "total_area_m2",
    "floor",
    "floors_total",
)


def backfill_housing_from_profile_publications(
    listing: dict[str, Any],
    housing: dict[str, Any],
) -> dict[str, Any]:
    """Дополняет жильё данными карточки текущего объявления в профиле."""

    current_url = str(listing.get("url") or "").split("?", 1)[0].rstrip("/")
    if not current_url:
        return housing
    seller = listing.get("seller") or {}
    profile_stats = listing.get("seller_profile_stats") or {}
    publications = list(seller.get("active_publications") or [])
    publications.extend(seller.get("closed_publications") or [])
    # OLX сохраняет карточки профиля отдельно до этапа assess_seller().
    # Читаем структурированные city/district прямо из этого блока, иначе
    # данные текущей карточки становятся доступны слишком поздно.
    publications.extend(profile_stats.get("active_publications") or [])
    publications.extend(profile_stats.get("closed_publications") or [])
    matches = [
        publication
        for publication in publications
        if str(publication.get("url") or "").split("?", 1)[0].rstrip("/")
        == current_url
    ]
    for field in PUBLICATION_HOUSING_FIELDS:
        if housing.get(field) is not None:
            continue
        for publication in matches:
            if publication.get(field) is not None:
                housing[field] = publication[field]
                break
    return housing


def process_listing(raw_listing: dict[str, Any]) -> dict[str, Any]:
    """Преобразует каноническую запись источника в публичный результат."""

    raw_housing = backfill_housing_from_profile_publications(
        raw_listing,
        deepcopy(raw_listing.get("housing") or {}),
    )
    housing = {
        "city": normalize_city(
            raw_housing.get("city") or raw_listing.get("city")
        ),
        "district": normalize_district(
            raw_housing.get("district") or raw_listing.get("district")
        ),
        "street": combine_street(
            raw_housing.get("street"),
            raw_housing.get("zone"),
            raw_housing.get("house_number"),
            raw_housing.get("address"),
        ),
        "building_type": raw_housing.get("building_type"),
        "is_new_building": raw_housing.get("is_new_building"),
        "foundation_type": raw_housing.get("foundation_type"),
        "residential_complex_name": raw_housing.get("residential_complex_name"),
        "rooms": raw_housing.get("rooms"),
        "total_area_m2": raw_housing.get("total_area_m2"),
        "floor": raw_housing.get("floor"),
        "floors_total": raw_housing.get("floors_total"),
        "furnished": raw_housing.get("furnished"),
        "latitude": raw_housing.get("latitude"),
        "longitude": raw_housing.get("longitude"),
    }
    listing_type = raw_listing.get("transaction_type") or raw_listing.get("type")
    is_rent = listing_type == "rent"
    monthly_rent = raw_housing.get("monthly_rent")
    if monthly_rent is None and is_rent:
        monthly_rent = raw_listing.get("price")
    rent_currency = raw_housing.get("rent_currency")
    if rent_currency is None and is_rent:
        rent_currency = raw_listing.get("currency")
    area = housing.get("total_area_m2")
    price_per_m2 = raw_housing.get("price_per_m2")
    if price_per_m2 is None and monthly_rent and area:
        try:
            price_per_m2 = round(float(monthly_rent) / float(area), 2)
        except (TypeError, ValueError, ZeroDivisionError):
            price_per_m2 = None
    housing.update(
        {
            "monthly_rent": monthly_rent,
            "rent_currency": rent_currency,
            "price_per_m2": price_per_m2,
        }
    )
    # На этапе сбора продавец сохраняется как факт конкретной публикации.
    # Классификация, статистика и объединение продавцов будут отдельным
    # постпроцессом после завершения полного парсинга.
    seller = deepcopy(raw_listing.get("seller") or {})
    for deferred_field in (
        "seller_type",
        "sellers_type",
        "realtor_probability",
        "comment",
        "assessment_basis",
        "active_rent_listings",
        "active_sale_listings",
        "total_real_estate_listings",
        "unique_listing_count",
        "distinct_housing_listings",
        "profile_pages_scanned",
        "profile_scan_complete",
        "active_publications",
        "closed_publications",
        "closed_publications_available",
    ):
        seller.pop(deferred_field, None)

    return {
        "source": raw_listing.get("source"),
        "id": raw_listing.get("id"),
        "title": raw_listing.get("title"),
        "price": raw_listing.get("price"),
        "currency": raw_listing.get("currency"),
        "transaction_type": raw_listing.get("transaction_type")
        or raw_listing.get("type"),
        "url": raw_listing.get("url"),
        "published_at": raw_listing.get("published_at"),
        "description": raw_listing.get("description")
        or raw_listing.get("_description"),
        "description_length": raw_listing.get("description_length", 0),
        "housing": housing,
        "amenities": list(raw_listing.get("amenities") or []),
        "nearby": list(raw_listing.get("nearby") or []),
        "images": list(raw_listing.get("images") or []),
        "seller": seller,
    }


def process_listings(raw_listings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [process_listing(listing) for listing in raw_listings]
