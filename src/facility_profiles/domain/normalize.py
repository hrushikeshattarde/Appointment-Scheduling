"""Text, contact and address normalisation helpers.

Everything here is deterministic and unit-tested. The extractor relies on
:func:`quote_in_source` to enforce FR-5 (every quote must exist in a source) and on the
phone/email/URL helpers to enforce FR-6 (no invented contacts).
"""

from __future__ import annotations

import html
import math
import re
from typing import Final

_BR_RE: Final = re.compile(r"<\s*br\s*/?\s*>|</\s*p\s*>|</\s*li\s*>|</\s*div\s*>", re.IGNORECASE)
_TAG_RE: Final = re.compile(r"<[^>]+>")
_WS_RE: Final = re.compile(r"[ \t\f\v]+")
_BLANK_LINES_RE: Final = re.compile(r"\n{3,}")
_NON_ALNUM_RE: Final = re.compile(r"[^a-z0-9 ]+")

_PHONE_RE: Final = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?\(?(\d{3})\)?[\s.-]?(\d{3})[\s.-]?(\d{4})(?!\d)"
    r"(?:\s*(?:ext\.?|x|extension)\s*(\d{1,6}))?",
    re.IGNORECASE,
)
_EMAIL_RE: Final = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_URL_RE: Final = re.compile(r"(?:https?://|www\.)[^\s<>\"')\]]+", re.IGNORECASE)

_LEGAL_SUFFIXES: Final = (
    "incorporated",
    "inc",
    "llc",
    "l l c",
    "ltd",
    "limited",
    "corporation",
    "corp",
    "company",
    "co",
    "plc",
    "gmbh",
    "sa de cv",
    "s a de c v",
)

_ADDRESS_ABBREVIATIONS: Final[dict[str, str]] = {
    "street": "st",
    "road": "rd",
    "drive": "dr",
    "avenue": "ave",
    "boulevard": "blvd",
    "highway": "hwy",
    "lane": "ln",
    "parkway": "pkwy",
    "court": "ct",
    "circle": "cir",
    "place": "pl",
    "terrace": "ter",
    "trail": "trl",
    "way": "way",
    "suite": "ste",
    "building": "bldg",
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
    "route": "rte",
    "us highway": "us hwy",
}

EARTH_RADIUS_M: Final = 6_371_000.0


def html_to_text(value: str | None) -> str:
    """Turn the API's lightly HTML-formatted notes into plain text with line breaks kept."""
    if not value:
        return ""
    text = _BR_RE.sub("\n", value)
    text = _TAG_RE.sub("", text)
    text = html.unescape(text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_WS_RE.sub(" ", line).strip() for line in text.split("\n")]
    return _BLANK_LINES_RE.sub("\n\n", "\n".join(lines)).strip()


def squash(value: str | None) -> str:
    """Lower-case and collapse all whitespace, for tolerant substring matching."""
    if not value:
        return ""
    return re.sub(r"\s+", " ", value).strip().lower()


def quote_in_source(quote: str, source: str) -> bool:
    """True when ``quote`` appears in ``source`` after whitespace and case normalisation."""
    needle = squash(quote)
    if not needle:
        return False
    return needle in squash(source)


def normalize_phone(value: str | None) -> str | None:
    """Return ``NNN-NNN-NNNN`` (with ``xEXT`` when present) for a North American number."""
    if not value:
        return None
    match = _PHONE_RE.search(value)
    if not match:
        return None
    area, prefix, line, ext = match.groups()
    number = f"{area}-{prefix}-{line}"
    return f"{number} x{ext}" if ext else number


def extract_phones(text: str | None) -> list[str]:
    """All distinct phone numbers in ``text``, normalised, in order of appearance."""
    if not text:
        return []
    seen: list[str] = []
    for match in _PHONE_RE.finditer(text):
        area, prefix, line, ext = match.groups()
        number = f"{area}-{prefix}-{line}"
        if ext:
            number = f"{number} x{ext}"
        if number not in seen:
            seen.append(number)
    return seen


def normalize_email(value: str | None) -> str | None:
    """Lower-cased email when ``value`` is a well-formed address."""
    if not value:
        return None
    match = _EMAIL_RE.search(value)
    return match.group(0).lower() if match else None


def extract_emails(text: str | None) -> list[str]:
    """All distinct email addresses in ``text``, lower-cased."""
    if not text:
        return []
    seen: list[str] = []
    for match in _EMAIL_RE.finditer(text):
        email = match.group(0).lower()
        if email not in seen:
            seen.append(email)
    return seen


def normalize_url(value: str | None) -> str | None:
    """Lower-cased scheme and host, trailing punctuation and slash removed."""
    if not value:
        return None
    match = _URL_RE.search(value)
    if not match:
        return None
    url = match.group(0).rstrip(".,;:").rstrip("/")
    if url.lower().startswith("www."):
        url = f"https://{url}"
    scheme, _, rest = url.partition("://")
    host, slash, path = rest.partition("/")
    return f"{scheme.lower()}://{host.lower()}{slash}{path}"


# Portal hosts and the scheduling system behind them. A URL names its vendor with certainty, so
# this beats a model's reading of the notes. Hosts seen in the pod stores, plus vendor domains.
PORTAL_HOSTS: Final[tuple[tuple[str, str], ...]] = (
    ("opendock.com", "opendock"),
    ("datadocks.com", "datadocks"),
    ("c3reservations.com", "c3"),
    ("onenetwork.com", "one_network"),
    ("e2open.com", "e2open"),
    ("blueyonder.com", "blue_yonder"),
    ("ncrpowertraffic.com", "retalix"),  # NCR Power Traffic, formerly Retalix
    ("costcotraffic.com", "costco"),
    ("cwtraffic.com", "costco"),  # appointments.cwtraffic.com books Costco depots
    ("myunfi.com", "unfi"),
    ("ahold-tlm.logistics.com", "ahold"),
    ("publix.io", "publix"),
    ("bozzutos.net", "bozzutos"),
)


def url_host(value: str | None) -> str | None:
    """The lower-cased host of a URL (``www.`` dropped), or None."""
    url = normalize_url(value)
    if not url:
        return None
    host = url.partition("://")[2].partition("/")[0].partition(":")[0]
    return host.removeprefix("www.") or None


def portal_vendor_from_url(value: str | None) -> str | None:
    """The PortalVendor value a portal URL belongs to, or None when the host is not known."""
    host = url_host(value)
    if not host:
        return None
    for suffix, vendor in PORTAL_HOSTS:
        if host == suffix or host.endswith(f".{suffix}"):
            return vendor
    return None


def extract_urls(text: str | None) -> list[str]:
    """All distinct URLs in ``text``, normalised."""
    if not text:
        return []
    seen: list[str] = []
    for match in _URL_RE.finditer(text):
        url = normalize_url(match.group(0))
        if url and url not in seen:
            seen.append(url)
    return seen


def normalize_company_name(value: str | None) -> str:
    """Lower-case, strip punctuation and legal suffixes, collapse whitespace."""
    if not value:
        return ""
    text = _NON_ALNUM_RE.sub(" ", value.lower().replace("&", " and "))
    text = re.sub(r"\s+", " ", text).strip()
    changed = True
    while changed and text:
        changed = False
        for suffix in _LEGAL_SUFFIXES:
            if text.endswith(f" {suffix}"):
                text = text[: -len(suffix) - 1].strip()
                changed = True
    return text


def normalize_address(value: str | None) -> str:
    """Lower-case street address with common abbreviations applied."""
    if not value:
        return ""
    text = _NON_ALNUM_RE.sub(" ", value.lower())
    text = re.sub(r"\s+", " ", text).strip()
    for long_form, short in sorted(_ADDRESS_ABBREVIATIONS.items(), key=lambda kv: -len(kv[0])):
        text = re.sub(rf"\b{re.escape(long_form)}\b", short, text)
    return text


def normalize_postal(value: str | None) -> str:
    """First five digits of a US ZIP; other countries pass through upper-cased without spaces."""
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if len(digits) >= 5 and value.strip()[0].isdigit():
        return digits[:5]
    return re.sub(r"\s+", "", value).upper()


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = phi2 - phi1
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return 2 * EARTH_RADIUS_M * math.asin(math.sqrt(a))


_SCHEDULING_KEYWORDS: Final = re.compile(
    r"\b(appt|appointment|appointments|fcfs|first come|first-come|schedule|scheduled|scheduling|"
    r"reschedule|slot|dock|door|portal|opendock|c3|datadocks|notice|hours|hrs|open|close|closes|"
    r"receiving|shipping hours|check[- ]?in|preset|pre-set|cita|call ahead|call before|"
    r"work[- ]?in|walk[- ]?in|by appt|window)\b",
    re.IGNORECASE,
)


def looks_scheduling_related(text: str | None) -> bool:
    """Cheap keyword gate used to keep only appointment-related load and tracking notes."""
    return bool(text) and _SCHEDULING_KEYWORDS.search(text or "") is not None
