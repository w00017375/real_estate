import html
import re
from urllib.parse import urlsplit

from core.normalization import normalize_phone


_OLX_SELLER_SUFFIX_RE = re.compile(
    r"\s+на\s+OLX\s+с\b", flags=re.IGNORECASE
)
_UZBEK_PHONE_RE = re.compile(
    r"(?<!\d)\+?998(?:[\s().\-]*\d){9}(?!\d)"
)
_UZBEK_PHONE_LOOSE_RE = re.compile(
    r"(?<!\d)(?:\+|\\u002b|\\x2b)?998"
    r"(?:(?:[\s().,;:/_\-]|&nbsp;|&#160;|\\u00a0|<[^>]{1,100}>){0,30}\d){9}"
    r"(?!\d)",
    flags=re.IGNORECASE,
)
_UZBEK_LOCAL_NINE_DIGIT_RE = re.compile(
    r"(?<!\d)(?:\d[\s().,/_\-]*){8}\d(?!\d)"
)
_PHONE_LABELLED_LOCAL_RE = re.compile(
    r"(?:тел(?:ефон)?|тел\.?|phone|мурожаат(?:\s+учун)?|aloqa|связаться)"
    r"\s*(?:учун\s*)?[:=\-]?\s*"
    r"(?P<number>(?:\d[\s().,/_\-]*){6,8}\d)(?!\d)",
    flags=re.IGNORECASE,
)


def normalize_seller_name(
    seller_name: str | None,
    source_name: str | None = None,
) -> str | None:
    """Возвращает имя продавца в каноническом виде для указанного источника.

    OLX добавляет к имени служебный суффикс вида ``на OLX с ...``.
    В базе и итоговом каноническом JSON сохраняется только настоящее имя;
    имена продавцов других источников не изменяются.
    """

    if seller_name is None:
        return None
    value = str(seller_name).strip()
    if (source_name or "").strip().lower() == "olx":
        value = _OLX_SELLER_SUFFIX_RE.split(value, maxsplit=1)[0].strip()
    return value or None


def extract_olx_phone_from_seller_name(
    seller_name: str | None,
) -> str | None:
    """Extract a phone used by OLX as the visible seller-name prefix.

    Some OLX cards expose a contact as ``998901234567 на OLX с ...`` (or
    as a local nine-digit number).  The database post-processing convention
    is digits-only, so the returned value contains no leading ``+``.
    Ordinary textual seller names are ignored.
    """

    prefix = normalize_seller_name(seller_name, "olx")
    if not prefix:
        return None
    # Do not interpret arbitrary digits inside an agency/person name as a
    # phone: the entire visible prefix must consist of phone punctuation.
    if re.fullmatch(r"[+()\-\s0-9]+", prefix) is None:
        return None
    phone = normalize_phone(prefix, default_country_code="998")
    return phone.lstrip("+") if phone else None


def extract_uzbek_phones(value: object) -> list[str]:
    """Return unique Uzbekistan phone numbers in source order, digits only."""

    if value is None:
        return []
    result: list[str] = []
    for match in _UZBEK_PHONE_RE.finditer(str(value)):
        phone = normalize_phone(match.group(0), default_country_code="998")
        digits = phone.lstrip("+") if phone else None
        if digits and digits not in result:
            result.append(digits)
    return result


def extract_uzbek_phones_from_description(value: object) -> list[str]:
    """Extract full and local phone forms from an OLX description.

    Priority is a complete ``998`` number, then a local nine-digit mobile
    number.  A seven-digit value is accepted only next to an explicit phone
    label and is treated as a Tashkent landline (country code 998, city code
    71).  Requiring the label prevents prices, listing IDs and areas from
    being interpreted as phone contacts.
    """

    if value is None:
        return []
    text = html.unescape(str(value))
    result = extract_uzbek_phones(text)

    for match in _UZBEK_LOCAL_NINE_DIGIT_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group(0))
        if len(digits) != 9:
            continue
        phone = f"998{digits}"
        if phone not in result:
            result.append(phone)

    for match in _PHONE_LABELLED_LOCAL_RE.finditer(text):
        digits = re.sub(r"\D", "", match.group("number"))
        if len(digits) == 9:
            phone = f"998{digits}"
        elif len(digits) == 7:
            phone = f"99871{digits}"
        else:
            continue
        if phone not in result:
            result.append(phone)
    return result


def extract_uzbek_phones_loose(value: object) -> list[str]:
    """Extract Uzbekistan phones from HTML and escaped page payloads.

    The strict parser remains the first choice.  The fallback accepts digits
    separated by HTML tags, entities and JSON escapes, but still requires a
    complete ``998`` prefix followed by exactly nine digits.
    """

    if value is None:
        return []
    text = html.unescape(str(value))
    result = extract_uzbek_phones(text)
    for match in _UZBEK_PHONE_LOOSE_RE.finditer(text):
        digits = re.sub(r"[^0-9]", "", match.group(0))
        start = digits.find("998")
        if start < 0:
            continue
        phone = digits[start : start + 12]
        if len(phone) == 12 and phone not in result:
            result.append(phone)
    return result


def extract_uzbek_phones_from_payloads(*values: object) -> list[str]:
    """Return unique phones from several listing payloads in priority order."""

    result: list[str] = []
    for value in values:
        for phone in extract_uzbek_phones_loose(value):
            if phone not in result:
                result.append(phone)
    return result


def official_seller_details(
    profile_url: str | None,
    seller_name: str | None = None,
) -> tuple[bool, str | None]:
    """Проверяет официальный домен и извлекает название из имени продавца."""

    if not profile_url:
        return False, None
    parts = urlsplit(profile_url)
    hostname = (parts.hostname or "").lower()
    suffix = ".olx.uz"
    if not hostname.endswith(suffix):
        return False, None
    subdomain = hostname[: -len(suffix)]
    if not subdomain or "." in subdomain:
        return False, None
    if parts.path.rstrip("/") != "/home":
        return False, None
    name = normalize_seller_name(seller_name, "olx")
    return True, name or None
