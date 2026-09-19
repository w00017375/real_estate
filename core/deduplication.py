from datetime import datetime, timezone
from typing import Any

from core.normalization import HOUSING_ID_FIELDS, clean_text, normalize_phone


HOUSING_FIELDS = HOUSING_ID_FIELDS


def description_length(listing: dict[str, Any]) -> int:
    """Количество значимых символов без повторяющихся пробелов."""

    if "_description" not in listing:
        try:
            return int(listing.get("description_length") or 0)
        except (TypeError, ValueError):
            return 0
    return len(clean_text(listing.get("_description")) or "")


def duplicate_key(listing: dict[str, Any]) -> tuple | None:
    """
    Полное совпадение продавца и канонических характеристик жилья.

    Без телефона или хотя бы одного известного параметра жилья
    автоматическое удаление не выполняется, чтобы не склеивать неизвестные
    объекты.
    """

    seller = listing.get("seller") or {}
    phone = normalize_phone(seller.get("phone"))
    housing = listing.get("housing") or {}
    housing_values = tuple(housing.get(field) for field in HOUSING_FIELDS)

    if not phone:
        return None
    if all(value is None for value in housing_values):
        return None

    return phone, housing_values


def _published_datetime(listing: dict[str, Any]) -> datetime | None:
    value = listing.get("published_at")
    if not value:
        return None
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result if result.tzinfo else result.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def candidate_is_better(
    current: dict[str, Any],
    candidate: dict[str, Any],
) -> bool:
    current_length = description_length(current)
    candidate_length = description_length(candidate)

    if candidate_length != current_length:
        return candidate_length > current_length

    current_date = _published_datetime(current)
    candidate_date = _published_datetime(candidate)
    if current_date is not None and candidate_date is not None:
        if candidate_date != current_date:
            # При равной длине удаляется более ранняя публикация.
            return candidate_date > current_date

    # Если даты равны или одна из них неизвестна, удаляем первую встреченную.
    return True


def deduplicate_listings(
    listings: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Возвращает сохранённые и удалённые как дубликаты объявления."""

    selected: list[dict[str, Any] | None] = []
    positions: dict[tuple, int] = {}
    discarded: list[dict[str, Any]] = []

    for listing in listings:
        listing["description_length"] = description_length(listing)
        key = duplicate_key(listing)
        if key is None:
            selected.append(listing)
            continue
        if key not in positions:
            positions[key] = len(selected)
            selected.append(listing)
            continue

        position = positions[key]
        current = selected[position]
        if current is None:
            selected[position] = listing
            continue

        if candidate_is_better(current, listing):
            discarded.append(current)
            selected[position] = listing
        else:
            discarded.append(listing)

    return [listing for listing in selected if listing is not None], discarded
