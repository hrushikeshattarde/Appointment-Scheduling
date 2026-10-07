"""What the agent may tell a facility about a pickup: the case, its load and the load's dispatch.

The fact sheet is the only source an answer may draw on. The load's part is read fresh from
Transport Pro when the reply is written, because the trucking company, the driver and the
trailer are often assigned after the request went out. It holds what facilities ask about: the
freight, the equipment, the load's notes, the trucking company and its driver.

Money never enters it. No rate, max buy, charge or insurance is read, so no answer can carry one.
A fact Transport Pro does not have is None: the writer says it will follow up rather than guess.
Every date and time is as our emails give them, on the Eastern clock ("10/01 @ 0900", with "ET"
for a facility outside Eastern time).

For a facility asking when the driver will arrive, the newest check call on the live dispatch
(what the driver or the carrier last told us) and the newest tracking position are given, with
their times, when they are less than a day old. The agent never estimates an arrival itself.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from facility_profiles.booking.models import BookingCase
from facility_profiles.booking.templates import vendor_when
from facility_profiles.clock import to_eastern
from facility_profiles.config import Settings
from facility_profiles.customers import customer_of
from facility_profiles.logging import get_logger
from facility_profiles.storage.repository import as_utc
from facility_profiles.tpro.models import Dispatch, Load, TrackingNote, Waypoint

log = get_logger(__name__)

# Money and claims: never in an answer, and a reply that raises them goes to a person.
FORBIDDEN_TOPICS = re.compile(
    r"\b(rate|rates|detention|accessorial|tonu|invoice|charge|charges|fee|fees|payment|pay|"
    r"claim|damage|lumper)\b|\$\s?\d",
    re.I,
)
# The keys an answer may cite, case and load together.
FACT_KEYS = (
    "po_numbers",
    "load_id",
    "customer",
    "broker",
    "pickup_facility",
    "pickup_address",
    "pickup_notes",
    "requested_pickup",
    "confirmed_pickup",
    "pickup_number",
    "delivery_site",
    "delivery_address",
    "delivery_notes",
    "delivery_date",
    "delivery_ref",
    "equipment",
    "commodity",
    "weight",
    "piece_count",
    "temperature",
    "hazmat",
    "bol_number",
    "seal_number",
    "carrier_assigned",
    "carrier",
    "carrier_mc",
    "carrier_dot",
    "carrier_phone",
    "driver_name",
    "driver_phone",
    "truck_number",
    "trailer_number",
    "last_check_call",
    "last_location",
)
# How old a check call or a position may be and still say where the driver is.
TRACKING_FRESH = timedelta(hours=24)
# A check call a facility may hear is one about when or where the driver is ("30 minutes out",
# "ETA 1400", "on the way"); the rest are Circle's own tracking notes ("POD indexed", "Called
# DISP again for times at REC") and stay inside.
_ETA_WORDS = re.compile(
    r"\beta\b|\b(?:\d+|an?|one|half an?)\s*(?:min(?:ute)?s?|hrs?|hours?)\s+(?:out|away)\b|"
    r"\bon (?:the|his|her|their) way\b|\ben ?route\b|\barriv\w*|\brolling\b|\bclose by\b",
    re.I,
)
# Circle's tracking shorthand: a note written in it is for the team, not the facility.
_INTERNAL_WORDS = re.compile(r"\b(?:POD|DISP|REC|TM|SHP|CNEE)\b|escalat|index", re.I)
_CANCELED = frozenset({"canceled", "cancelled", "void", "voided"})
_BREAK_RE = re.compile(r"<br\s*/?>|\r?\n", re.I)
_TAG_RE = re.compile(r"<[^>]+>")


class FactsSource(Protocol):
    """Where the load's facts come from: the Transport Pro client, read only."""

    def get_load(self, load_id: int) -> Load:
        """The load record."""
        ...

    def search_dispatches(self, load_id: int) -> list[Dispatch]:
        """The load's dispatch records."""
        ...


def _when(local: str | None, timezone: str | None) -> str | None:
    if not local:
        return None
    mmdd, clock = vendor_when(local, timezone)
    return f"{mmdd} @ {clock}" if clock else mmdd


def case_facts(case: BookingCase, settings: Settings) -> dict[str, Any]:
    """What the case itself knows. The load's facts (``load_facts``) fill in the rest."""
    delivery = as_utc(case.delivery_at_utc)
    facts: dict[str, Any] = dict.fromkeys(FACT_KEYS)
    facts.update(
        {
            "po_numbers": [str(p) for p in case.po_numbers],
            "load_id": case.load_id,
            "customer": customer_of(case, settings).label(case.customer_name),
            "broker": settings.booking_carrier_name,
            "pickup_facility": case.vendor_name,
            "requested_pickup": _when(case.requested_local, case.vendor_timezone),
            "confirmed_pickup": _when(case.confirmed_local, case.vendor_timezone),
            "pickup_number": case.pickup_number,
            "delivery_site": case.delivery_site,
            "delivery_date": to_eastern(delivery).strftime("%m/%d") if delivery else None,
            "delivery_ref": case.delivery_ref,
        }
    )
    return facts


def _notes(text: str | None) -> str | None:
    if not text:
        return None
    parts = [" ".join(_TAG_RE.sub("", p).split()).rstrip(".;") for p in _BREAK_RE.split(text)]
    joined = "; ".join(p for p in parts if p)
    return joined or None


def _address(stop: Waypoint | None) -> str | None:
    loc = stop.location if stop else None
    if loc is None:
        return None
    street = ", ".join(x for x in (loc.address, loc.address2) if x)
    place = " ".join(x for x in (loc.state, loc.postal_code) if x)
    parts = [x for x in (street, loc.city, place) if x]
    return ", ".join(parts) or None


def _number(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _plain(value: float) -> str:
    return f"{value:,.0f}" if value == int(value) else f"{value:,}"


def _phone(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10:
        return str(value or "").strip() or None
    return f"{digits[:3]}-{digits[3:6]}-{digits[6:]}"


def _temperature(ref: dict[str, Any]) -> str | None:
    point = _number(ref.get("reeferTemperatureSetPoint") or ref.get("reeferTemperature"))
    low = _number(ref.get("reeferTemperatureMin"))
    high = _number(ref.get("reeferTemperatureMax"))
    if point is not None:
        return f"set to {_plain(point)} F"
    if low is not None and high is not None:
        return f"{_plain(low)} to {_plain(high)} F"
    return None


def live_dispatch(dispatches: list[Dispatch]) -> Dispatch | None:
    """The load's live dispatch: the newest one that was not canceled."""
    live = [d for d in dispatches if (d.status or "").lower() not in _CANCELED]
    live.sort(key=lambda d: (d.date_created or "", d.id))
    return live[-1] if live else None


def _tracking(notes: list[TrackingNote], live: Dispatch, now: datetime) -> dict[str, str | None]:
    """The live dispatch's newest check call and position, each under a day old, with its time."""
    fresh = [
        n
        for n in notes
        if n.dispatch_id in (None, live.id)
        and (at := n.event_at) is not None
        and now - at <= TRACKING_FRESH
    ]
    fresh.sort(key=lambda n: n.event_at or now)
    calls = [
        n
        for n in fresh
        if n.is_user_entered
        and n.comments
        and _ETA_WORDS.search(n.comments)
        and not _INTERNAL_WORDS.search(n.comments)
        and not FORBIDDEN_TOPICS.search(n.comments)
    ]
    pings = [n for n in fresh if not n.is_user_entered and n.location and n.location.city]
    out: dict[str, str | None] = {"last_check_call": None, "last_location": None}
    if calls:
        call = calls[-1]
        said = " ".join(_TAG_RE.sub(" ", call.comments or "").split())[:160]
        out["last_check_call"] = f"{_stamp(call.event_at)}: {said}"
    if pings:
        ping = pings[-1]
        loc = ping.location
        place = ", ".join(x for x in (loc.city, loc.state) if x) if loc else ""
        out["last_location"] = f"{place} at {_stamp(ping.event_at)}"
    return out


def _stamp(at: datetime | None) -> str:
    return f"{to_eastern(at):%m/%d @ %H%M} ET" if at else ""


def load_facts(
    load: Load,
    dispatches: list[Dispatch],
    *,
    waypoint_index: int = 0,
    notes: list[TrackingNote] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """The freight, its notes, and who hauls it, as a facility would ask about them.

    ``notes`` are the load's tracking notes: they give the driver's latest check call and
    position (:func:`_tracking`).
    """
    ref = load.reference or {}
    stops = load.waypoints
    pickup = stops[waypoint_index] if 0 <= waypoint_index < len(stops) else None
    if pickup is None or (pickup.type or "").upper() != "SH":
        pickup = next((s for s in stops if (s.type or "").upper() == "SH"), None)
    delivery = next((s for s in reversed(stops) if (s.type or "").upper() == "CN"), None)
    length = _number((ref.get("dimensions") or {}).get("length"))
    kind = ref.get("equipmentType")
    weight = _number(ref.get("weight"))
    pieces = _number(ref.get("numberOfPieces"))
    commodity = ": ".join(x for x in (ref.get("commodity"), ref.get("commodityDesc")) if x)
    hazmat = ref.get("hazmat")
    facts: dict[str, Any] = {
        "pickup_address": _address(pickup),
        "pickup_notes": _notes(pickup.notes if pickup else None),
        "delivery_address": _address(delivery),
        "delivery_notes": _notes(delivery.notes if delivery else None),
        "equipment": (f"{_plain(length)} ft {kind}" if length and kind else kind) or None,
        "commodity": commodity or None,
        "weight": f"{_plain(weight)} lbs" if weight else None,
        "piece_count": _plain(pieces) if pieces else None,
        "temperature": _temperature(ref),
        "hazmat": None if hazmat is None else ("yes" if hazmat else "no"),
        "bol_number": ref.get("billOfLading") or None,
        "seal_number": ref.get("sealNumber") or None,
        "carrier_assigned": "not yet",
    }
    live = live_dispatch(dispatches)
    assigned = (live.assigned_to if live else None) or {}
    carrier = assigned.get("carrier") or {}
    if live is not None:
        phones = {p.get("type"): p.get("value") for p in carrier.get("phoneNumbers") or []}
        driver: dict[str, Any] = next(
            (c for c in assigned.get("contacts") or [] if (c.get("type") or "") == "DRIVER"), {}
        )
        facts.update(
            {
                "carrier_assigned": "yes",
                "carrier": carrier.get("companyName"),
                "carrier_mc": carrier.get("mcNumber"),
                "carrier_dot": carrier.get("usDOT"),
                "carrier_phone": _phone(phones.get("DISPATCH") or phones.get("MAIN")),
                "driver_name": driver.get("name"),
                "driver_phone": _phone(driver.get("phoneNumber")),
                "truck_number": assigned.get("tractorNumber"),
                "trailer_number": assigned.get("trailerNumber"),
            }
        )
        if notes:
            facts.update(_tracking(notes, live, now or datetime.now(tz=UTC)))
    return {k: v for k, v in facts.items() if k in FACT_KEYS}


def read_load_facts(case: BookingCase, source: FactsSource) -> dict[str, Any]:
    """The load's facts from Transport Pro, or nothing when it cannot be read.

    A load that cannot be read leaves the case's own facts: the writer then answers what the
    case knows and says it will get back to them on the rest.
    """
    try:
        load = source.get_load(case.load_id)
        dispatches = source.search_dispatches(case.load_id)
    except Exception as exc:
        log.warning("booking.facts_unread", case=case.id, load=case.load_id, error=str(exc))
        return {}
    notes: list[TrackingNote] = []
    tracking = getattr(source, "get_tracking_load_notes", None)  # a source may not track
    if callable(tracking):
        try:
            notes = list(tracking(case.load_id))
        except Exception as exc:  # no tracking leaves the rest of the facts
            log.warning("booking.tracking_unread", case=case.id, error=str(exc))
    return load_facts(load, dispatches, waypoint_index=case.waypoint_index, notes=notes)


def with_load(facts: dict[str, Any], loaded: dict[str, Any]) -> dict[str, Any]:
    """The case's facts with the load's added; the case's own values stay where it has them."""
    merged = dict(facts)
    for key, value in loaded.items():
        if merged.get(key) is None:
            merged[key] = value
    return merged


def gather_facts(
    case: BookingCase, settings: Settings, source: FactsSource | None
) -> dict[str, Any]:
    """The case's facts, plus the load's read fresh from Transport Pro when a source is given."""
    facts = case_facts(case, settings)
    return with_load(facts, read_load_facts(case, source)) if source is not None else facts
