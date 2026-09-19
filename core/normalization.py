import re
from typing import Any


def clean_text(value: Any) -> str | None:
    if value is None:
        return None

    cleaned = re.sub(r"\s+", " ", str(value)).strip()
    return cleaned or None


def parse_float(value: Any) -> float | None:
    if value is None:
        return None

    match = re.search(r"\d+(?:[.,]\d+)?", str(value))
    if not match:
        return None

    return float(match.group(0).replace(",", "."))


def parse_int(value: Any) -> int | None:
    number = parse_float(value)
    return int(number) if number is not None else None


def parse_yes_no(value: Any) -> bool | None:
    normalized = (clean_text(value) or "").lower()

    if normalized in {"да", "yes", "есть", "ha"}:
        return True
    if normalized in {"нет", "no", "yo'q", "yo‘q"}:
        return False

    return None


def parse_multiselect(value: Any) -> list[str]:
    if not value:
        return []

    result = []
    for item in str(value).split(","):
        cleaned = clean_text(item)
        if cleaned:
            result.append(cleaned)

    return result


def normalize_phone(
    value: Any,
    default_country_code: str | None = None,
) -> str | None:
    if not value:
        return None

    digits = re.sub(r"\D", "", str(value))
    if digits.startswith("00"):
        digits = digits[2:]
    if len(digits) == 9 and default_country_code:
        digits = default_country_code + digits
    if len(digits) < 10 or len(digits) > 15:
        return None

    return "+" + digits


def combine_street(
    street: Any = None,
    zone: Any = None,
    house_number: Any = None,
    address: Any = None,
) -> str | None:
    """Собирает одну строку улицы из адресных частей разных источников."""

    parts: list[str] = []
    for value in (zone, street, house_number):
        cleaned = clean_text(value)
        if cleaned and cleaned.casefold() not in {
            part.casefold() for part in parts
        }:
            parts.append(cleaned)
    if parts:
        return ", ".join(parts)
    return clean_text(address)


TASHKENT_DISTRICT_ALIASES = (
    (("алмазар", "олмазор"), "Алмазарский район"),
    (("бектемир",), "Бектемирский район"),
    (("мирабад", "mirobod"), "Мирабадский район"),
    (("мирзо-улугбек", "мирзо улугбек", "mirzo ulugbek"), "Мирзо-Улугбекский район"),
    (("сергел", "sirgal"), "Сергелийский район"),
    (("учтеп", "uchtepa"), "Учтепинский район"),
    (("чиланзар", "chilonzor"), "Чиланзарский район"),
    (("шайхантахур", "shayxontohur"), "Шайхантахурский район"),
    (("юнусабад", "yunusobod"), "Юнусабадский район"),
    (("яккасарай", "yakkasaroy"), "Яккасарайский район"),
    (("янгихаёт", "янгихаят", "yangihayot"), "Янгихаётский район"),
    (("яшнабад", "yashnobod"), "Яшнабадский район"),
)


def normalize_city(value: Any) -> str | None:
    """Remove only administrative prefixes, preserving the source city."""

    result = clean_text(value)
    if not result:
        return None
    result = re.sub(r"^(?:город|г\.)\s+", "", result, flags=re.IGNORECASE)
    if result.casefold() in {"tashkent", "toshkent"}:
        return "Ташкент"
    return result


def normalize_district(value: Any) -> str | None:
    """Keep only the district name and unify known Tashkent spellings."""

    result = clean_text(value)
    if not result:
        return None
    result = result.split(",", 1)[0].strip()
    normalized = result.casefold().replace("ё", "е")
    for aliases, canonical in TASHKENT_DISTRICT_ALIASES:
        if any(alias.casefold().replace("ё", "е") in normalized for alias in aliases):
            return canonical
    if normalized.startswith("район "):
        return clean_text(result[6:] + " район")
    return result


HOUSING_ID_FIELDS = (
    "city",
    "district",
    "rooms",
    "total_area_m2",
    "floor",
    "floors_total",
)


def publication_fingerprint(features: dict[str, Any]) -> tuple | None:
    """Единый отпечаток объекта для дедупликации и оценки продавца."""

    values = tuple(features.get(field) for field in HOUSING_ID_FIELDS)
    if sum(value is not None for value in values) < 2:
        return None

    return values
