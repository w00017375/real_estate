from typing import Any


def _probability(distinct_count: int) -> float:
    """Вероятность, основанная только на числе различных объектов."""

    if distinct_count >= 10:
        return 0.95
    if distinct_count >= 5:
        return 0.90
    if distinct_count >= 3:
        return 0.75
    if distinct_count == 2:
        return 0.40
    if distinct_count == 1:
        return 0.10
    return 0.0


def _seller_type(distinct_count: int) -> str:
    if distinct_count >= 3:
        return "likely_realtor"
    if distinct_count == 1:
        return "likely_owner"
    return "unknown"


def build_comment(
    seller_type: str,
    unique_count: int,
    distinct_count: int,
    scan_complete: bool,
) -> str:
    labels = {
        "likely_realtor": "Продавец, вероятно, риэлтор.",
        "likely_owner": "Продавец, вероятно, собственник.",
        "unknown": "Данных недостаточно для уверенного определения типа продавца.",
    }
    comment = (
        f"{labels[seller_type]} После межплощадочной дедупликации найдено "
        f"уникальных объявлений о жилье: {unique_count}; различных объектов "
        f"по городу, району, числу комнат, площади, этажу и этажности: "
        f"{distinct_count}."
    )
    if distinct_count >= 3:
        comment += " Порог риэлтора (не менее 3 различных объектов) достигнут."
    elif not scan_complete:
        comment += " Доступный список публикаций может быть неполным."
    return comment


def assess_seller(
    seller: dict[str, Any],
    housing: dict[str, Any] | None,
    profile_stats: dict[str, Any] | None,
) -> dict[str, Any]:
    """
    Оценивает продавца только по уже дедуплицированному списку жилья.

    ``housing`` оставлен в сигнатуре для совместимости общего конвейера, но
    параметры текущего объявления, его описание, комиссия, имя продавца и
    ключевые слова на результат не влияют.
    """

    del housing
    stats = profile_stats or {}
    active_publications = list(stats.get("active_publications") or [])
    closed_publications = list(stats.get("closed_publications") or [])
    rent_count = int(stats.get("active_rent_listings") or 0)
    sale_count = int(stats.get("active_sale_listings") or 0)
    unique_count = int(
        stats.get("unique_listing_count")
        or len(active_publications) + len(closed_publications)
    )
    distinct_count = int(stats.get("distinct_housing_listings") or 0)
    scan_complete = bool(stats.get("profile_scan_complete"))
    seller_type = _seller_type(distinct_count)

    return {
        "name": seller.get("name"),
        "phone": seller.get("phone"),
        "phone_source": seller.get("phone_source"),
        "profile_url": seller.get("profile_url"),
        "is_official_seller": bool(seller.get("is_official_seller")),
        "official_complex_name": seller.get("official_complex_name"),
        "active_rent_listings": rent_count,
        "active_sale_listings": sale_count,
        "total_real_estate_listings": rent_count + sale_count,
        "unique_listing_count": unique_count,
        "distinct_housing_listings": distinct_count,
        "profile_pages_scanned": int(stats.get("profile_pages_scanned") or 0),
        "profile_scan_complete": scan_complete,
        "active_publications": active_publications,
        "closed_publications": closed_publications,
        "closed_publications_available": bool(
            stats.get("closed_publications_available")
        ),
        "seller_type": seller_type,
        "seller_role": seller.get("seller_role") or seller_type,
        "realtor_probability": _probability(distinct_count),
        "comment": build_comment(
            seller_type,
            unique_count,
            distinct_count,
            scan_complete,
        ),
        "assessment_basis": (
            "unique_publications_after_cross_source_deduplication"
        ),
    }
