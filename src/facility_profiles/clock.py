"""Eastern time: the one clock people read.

Circle runs on US Eastern time. Every time a person reads (the board, the CLI, to-dos, the daily
summary, notes in Transport Pro, the logs) and every time in an email to a facility is Eastern:
EST, or EDT while daylight saving is on, written "ET".

The store keeps two kinds of time, and keeps them as they are:

- instants in UTC (when a message was sent, the delivery slot, a booked start and end).
  Transport Pro takes and gives UTC, so what is written there stays UTC;
- pickup slots on the facility's own clock, "YYYY-MM-DD HH:MM", because that is the clock its
  hours, cut-offs and weekends are written in.

This module shows either on the Eastern clock, and turns an Eastern time a person or a facility
wrote back onto the facility's clock. For a facility on Eastern time (every Lidl vendor but
Seneca in Ripon, WI) the two clocks are the same, and nothing changes but the label.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

EASTERN_ZONE = "America/New_York"
EASTERN = ZoneInfo(EASTERN_ZONE)
LABEL = "ET"
LOCAL_FORMAT = "%Y-%m-%d %H:%M"
# A zone keeps Eastern time when it has New York's offset in winter and in summer.
_SEASONS = (datetime(2026, 1, 15, 12, tzinfo=UTC), datetime(2026, 7, 15, 12, tzinfo=UTC))
# The words a facility writes after a time, and the zone each one means.
ZONE_WORDS: dict[str, str] = {
    "et": EASTERN_ZONE,
    "est": EASTERN_ZONE,
    "edt": EASTERN_ZONE,
    "eastern": EASTERN_ZONE,
    "ct": "America/Chicago",
    "cst": "America/Chicago",
    "cdt": "America/Chicago",
    "central": "America/Chicago",
    "mt": "America/Denver",
    "mst": "America/Denver",
    "mdt": "America/Denver",
    "mountain": "America/Denver",
    "pt": "America/Los_Angeles",
    "pst": "America/Los_Angeles",
    "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles",
}
# What a reply says when it means the facility's own clock.
LOCAL_WORDS = frozenset({"local", "local time", "our time", "facility time", "plant time"})
_ZONE_NAMES = {
    EASTERN_ZONE: ("Eastern", "ET"),
    "America/Chicago": ("Central", "CT"),
    "America/Denver": ("Mountain", "MT"),
    "America/Phoenix": ("Mountain (Arizona)", "MST"),
    "America/Los_Angeles": ("Pacific", "PT"),
}


def zone(name: str | None) -> ZoneInfo:
    """The zone a case or facility names; Eastern when it names none, or one that does not exist."""
    if not name:
        return EASTERN
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return EASTERN


def _offsets(tz: ZoneInfo) -> tuple[object, ...]:
    return tuple(moment.astimezone(tz).utcoffset() for moment in _SEASONS)


def is_eastern(name: str | None) -> bool:
    """True when the zone keeps Eastern time all year (New York, Detroit, Indianapolis ...)."""
    return _offsets(zone(name)) == _offsets(EASTERN)


def zone_name(name: str | None) -> tuple[str, str]:
    """The zone in words and its short label: ("Central", "CT")."""
    tz = zone(name)
    for known, words in _ZONE_NAMES.items():
        if _offsets(ZoneInfo(known)) == _offsets(tz):
            return words
    return (str(tz.key), str(tz.key))


def to_eastern(moment: datetime) -> datetime:
    """An instant on the Eastern clock. A naive value is UTC, as the store keeps it."""
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(EASTERN)


def now_eastern() -> datetime:
    """The time now, on the Eastern clock."""
    return datetime.now(tz=EASTERN)


def stamp(moment: datetime | None, pattern: str = "%m/%d %H:%M", *, label: bool = True) -> str:
    """An instant as Eastern text, "10/01 09:00 ET"; empty for none."""
    if moment is None:
        return ""
    text = to_eastern(moment).strftime(pattern)
    return f"{text} {LABEL}" if label else text


def _move(local: str | None, source: str | None, target: str | None) -> str | None:
    """A "YYYY-MM-DD HH:MM" wall time on one clock, as the same moment on another.

    A date with no time stays the date: a desk given dates only books a day, not a moment.
    """
    if not local:
        return local
    day, _, clock = local.partition(" ")
    if not clock or _offsets(zone(source)) == _offsets(zone(target)):
        return local
    try:
        naive = datetime.strptime(f"{day} {clock}", LOCAL_FORMAT)
    except ValueError:
        return local
    moved = naive.replace(tzinfo=zone(source)).astimezone(zone(target))
    return moved.strftime(LOCAL_FORMAT)


def local_to_eastern(local: str | None, timezone: str | None) -> str | None:
    """A slot on the facility's clock ("YYYY-MM-DD HH:MM") as that moment on the Eastern clock."""
    return _move(local, timezone, EASTERN_ZONE)


def eastern_to_local(eastern: str | None, timezone: str | None) -> str | None:
    """An Eastern slot ("YYYY-MM-DD HH:MM") on the facility's own clock."""
    return _move(eastern, EASTERN_ZONE, timezone)


def between(local: str | None, source: str | None, target: str | None) -> str | None:
    """A wall time on ``source``'s clock as the same moment on ``target``'s."""
    return _move(local, source, target)


def slot_text(local: str | None, timezone: str | None, *, label: bool = True) -> str:
    """A slot on the facility's clock, for people, on the Eastern clock: "Thu 10/01 09:00 ET"."""
    eastern = local_to_eastern(local, timezone)
    if not eastern:
        return "no date"
    day, _, clock = eastern.partition(" ")
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return eastern
    text = f"{parsed:%a %m/%d}" + (f" {clock}" if clock else "")
    return f"{text} {LABEL}" if clock and label else text


_ZONE_RE = re.compile(r"\b(" + "|".join(sorted(ZONE_WORDS, key=len, reverse=True)) + r")\b", re.I)


def zone_named(word: str | None, facility_zone: str | None) -> str | None:
    """The zone a word in a reply names ("CST", "eastern", "local"); None when it names none.

    "Local" or "our time" is the facility's own clock. A zone word with the facility's own offsets
    is the facility's zone, so Arizona's "MST" stays Arizona's.
    """
    text = (word or "").strip().lower().rstrip(".")
    if not text:
        return None
    if text in LOCAL_WORDS:
        return facility_zone or EASTERN_ZONE
    found = _ZONE_RE.search(text)
    if found is None:
        return None
    named = ZONE_WORDS[found.group(1).lower()]
    winter = _SEASONS[0]
    if facility_zone and (
        winter.astimezone(ZoneInfo(named)).utcoffset()
        == winter.astimezone(zone(facility_zone)).utcoffset()
    ):
        return facility_zone
    return named


def tpro_time(moment: datetime | None, now: datetime) -> str | None:
    """A time Transport Pro gives (a load's last change, a dispatch made), ISO UTC; None past now.

    It stands in for when the scan saw a change: a change made overnight is not this morning's.
    """
    if moment is None:
        return None
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat() if aware <= now else None
