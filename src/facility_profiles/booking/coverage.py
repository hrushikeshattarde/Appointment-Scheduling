"""Who hauls a booked pickup, and whether the truck came: Transport Pro's dispatch, watched.

A booked pickup is only as good as the truck sent to it. Circle books the facility first and
covers the load with a carrier after, so the scan also reads the dispatch of every booked pickup
due in the next few days, or just past, and raises what a person must act on:

- **Carrier dropped** (``carrier_dropped``): the carrier's dispatch was canceled and nothing
  replaced it. Raised once per canceled dispatch.
- **No carrier yet** (``carrier_missing``): no carrier on the load by FP_BOOKING_CARRIER_BY
  (Eastern time) on the business day before the pickup. Raised once per booked time.
- **Truck not seen at pickup** (``pickup_no_show``): FP_BOOKING_NO_SHOW_HOURS after the booked
  time, Transport Pro shows no arrival at the shipper and the load is not picked up. Raised once
  per booked time.

Each clears itself: a carrier on the load ends the first two, an arrival recorded at the shipper
(or the load picked up) the third. A carrier put on the load, changed or dropped is also kept as
an event, so it shows in Latest updates beside the load's other Transport Pro changes. Nothing
is written to Transport Pro and no one is emailed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.booking.facts import live_dispatch
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.timers import slot_at
from facility_profiles.booking.worklist import flag, resolve
from facility_profiles.business_days import previous_business_day
from facility_profiles.clock import EASTERN_ZONE, slot_text, stamp
from facility_profiles.config import Settings
from facility_profiles.logging import get_logger
from facility_profiles.storage.db import session_scope
from facility_profiles.tpro.models import Dispatch, VoiceAiLoad

log = get_logger(__name__)

ACTOR = "agent"
# Where the scan keeps the last carrier it saw, inside the case's view of Transport Pro.
CARRIER_KEY = "carrier"
# Booked pickups this close are watched, and for this long after their time.
WATCH_AHEAD = timedelta(days=4)
WATCH_AFTER = timedelta(days=2)
_CANCELED = frozenset({"canceled", "cancelled", "void", "voided"})
_PICKED_UP = frozenset({"delivered", "in transit", "intransit", "completed"})


class DispatchSource(Protocol):
    """What the watch reads from Transport Pro: a load's dispatches and its stops' actual times."""

    def search_dispatches(self, load_id: int) -> list[Dispatch]:
        """The load's dispatch records."""
        ...

    def get_voiceai_load(self, load_id: int) -> VoiceAiLoad | None:
        """The load's summary, whose dispatch stops carry actual arrival times."""
        ...


@dataclass(frozen=True)
class Watch:
    """One booked pickup to read the dispatch of."""

    case_id: int
    load_id: int
    slot: datetime  # the booked time, aware
    check_arrival: bool  # past the no-show grace: read the stops' actual times too


@dataclass(frozen=True)
class Coverage:
    """What Transport Pro shows for one booked pickup."""

    dispatches: list[Dispatch]
    arrived: datetime | None = None  # the truck's arrival at the shipper, when recorded


@dataclass
class CoverageStats:
    """What one watch did, for the scan's report."""

    watched: int = 0
    carrier_alerts: int = 0  # carrier dropped or none yet, raised
    no_shows: int = 0


def booked_slot(case: BookingCase) -> tuple[str | None, datetime | None]:
    """The booked time, as written on the facility's clock and as an aware datetime."""
    local = case.confirmed_local or case.requested_local
    return local, slot_at(local, case.vendor_timezone)


def carrier_due(day: date, settings: Settings) -> datetime:
    """When a carrier should be on the load: FP_BOOKING_CARRIER_BY, ET, the business day before."""
    hour, _, minute = settings.booking_carrier_by.partition(":")
    clock = time(int(hour), int(minute or 0))
    return datetime.combine(previous_business_day(day), clock, tzinfo=ZoneInfo(EASTERN_ZONE))


def watched(session: Session, now: datetime, settings: Settings) -> list[Watch]:
    """The booked pickups whose dispatch the scan reads: due within WATCH_AHEAD or just past."""
    grace = timedelta(hours=settings.booking_no_show_hours)
    out: list[Watch] = []
    query = select(BookingCase).where(BookingCase.status == CaseStatus.SCHEDULED.value)
    for case in session.scalars(query):
        if (str((case.tpro_seen or {}).get("load_status") or "")).lower() in _PICKED_UP:
            continue
        _, at = booked_slot(case)
        if at is None or not (now - WATCH_AFTER <= at <= now + WATCH_AHEAD):
            continue
        out.append(Watch(case.id, case.load_id, at, check_arrival=now >= at + grace))
    return out


def arrived_at_pickup(summary: VoiceAiLoad | None) -> datetime | None:
    """When the truck arrived at the shipper, from the dispatch's stops; None when not recorded."""
    if summary is None:
        return None
    for stop in summary.dispatch_waypoints:
        if stop.role == "shipper" and stop.actual_date and stop.actual_date.start_at:
            return stop.actual_date.start_at
    return None


def read_coverage(source: DispatchSource, watch: Watch) -> Coverage:
    """The dispatches of one booked pickup, and its arrival at the shipper when it is due."""
    dispatches = source.search_dispatches(watch.load_id)
    arrived = None
    reader = getattr(source, "get_voiceai_load", None)  # a source may not have the summary
    if watch.check_arrival and callable(reader):
        arrived = arrived_at_pickup(reader(watch.load_id))
    return Coverage(dispatches, arrived)


def watch_coverage(
    source: DispatchSource,
    sessions: sessionmaker[Session],
    settings: Settings,
    *,
    now: datetime,
) -> CoverageStats:
    """Read the dispatch of every watched booked pickup and raise or clear what it shows.

    Transport Pro is read with no session open; a load it cannot read is left for the next scan.
    """
    stats = CoverageStats()
    with session_scope(sessions) as session:
        watches = watched(session, now, settings)
    read: dict[int, Coverage] = {}
    for watch in watches:
        try:
            read[watch.case_id] = read_coverage(source, watch)
        except Exception:  # one unreadable load must not stop the scan
            log.warning("booking.coverage_unread", load_id=watch.load_id, exc_info=True)
    with session_scope(sessions) as session:
        for case_id, coverage in read.items():
            case = session.get(BookingCase, case_id)
            if case is None or case.status != CaseStatus.SCHEDULED.value:
                continue
            stats.watched += 1
            raised = apply_coverage(session, case, coverage, settings=settings, now=now)
            stats.carrier_alerts += sum(
                1 for k in raised if k in ("carrier_dropped", "carrier_missing")
            )
            stats.no_shows += raised.count("pickup_no_show")
    return stats


def apply_coverage(
    session: Session,
    case: BookingCase,
    coverage: Coverage,
    *,
    settings: Settings,
    now: datetime,
) -> list[str]:
    """Raise or clear the carrier and no-show to-dos of a booked pickup; return what was raised."""
    local, at = booked_slot(case)
    if at is None:
        return []
    raised: list[str] = []
    booked = slot_text(local, case.vendor_timezone)
    live = live_dispatch(coverage.dispatches)
    name = _carrier_name(live)
    before = (case.tpro_seen or {}).get(CARRIER_KEY)
    if live is not None:
        resolve(
            session,
            case,
            [ExceptionType.CARRIER_DROPPED, ExceptionType.CARRIER_MISSING],
            resolution=f"carrier on the load: {name or 'assigned'}",
            by=ACTOR,
        )
        if not isinstance(before, dict) or before.get("dispatch") != live.id:
            was = before.get("name") if isinstance(before, dict) else None
            action = "carrier_changed" if was and was != name else "carrier_assigned"
            what = f"{was} replaced by {name}" if action == "carrier_changed" else (name or "")
            _event(session, case, action, reason=what, carrier=name)
    else:
        raised += _uncovered(session, case, coverage, booked=booked, now=now, settings=settings)
    if now >= at + timedelta(hours=settings.booking_no_show_hours):
        raised += _no_show(session, case, coverage, booked=booked, covered=live is not None)
    case.tpro_seen = {
        **(case.tpro_seen or {}),
        CARRIER_KEY: {"dispatch": live.id if live else None, "name": name},
    }
    session.flush()
    return raised


def _uncovered(
    session: Session,
    case: BookingCase,
    coverage: Coverage,
    *,
    booked: str,
    now: datetime,
    settings: Settings,
) -> list[str]:
    """No live dispatch: the carrier dropped (a canceled one), or none came in time."""
    canceled = [d for d in coverage.dispatches if (d.status or "").lower() in _CANCELED]
    if canceled:
        last = max(canceled, key=lambda d: (d.date_created or "", d.id))
        if _raised_before(case, ExceptionType.CARRIER_DROPPED, "dispatch", last.id):
            return []
        who = _carrier_name(last) or "the carrier"
        why = f"{who}'s dispatch was canceled; the pickup booked for {booked} has no carrier now"
        flag(session, case, ExceptionType.CARRIER_DROPPED, why[:255], actor=ACTOR, dispatch=last.id)
        _event(session, case, "carrier_dropped", reason=why[:255], carrier=_carrier_name(last))
        return ["carrier_dropped"]
    local, at = booked_slot(case)
    if at is None or local is None:
        return []
    due = carrier_due(at.date(), settings)
    if now < due or _raised_before(case, ExceptionType.CARRIER_MISSING, "slot", local):
        return []
    why = f"booked for {booked} and no carrier on the load yet (due by {stamp(due)})"
    flag(session, case, ExceptionType.CARRIER_MISSING, why[:255], actor=ACTOR, slot=local)
    return ["carrier_missing"]


def _no_show(
    session: Session, case: BookingCase, coverage: Coverage, *, booked: str, covered: bool
) -> list[str]:
    """Past the grace with no arrival at the shipper: a truck that may have missed it."""
    if coverage.arrived is not None:
        resolve(
            session,
            case,
            [ExceptionType.PICKUP_NO_SHOW],
            resolution=f"Transport Pro shows the truck arrived {stamp(coverage.arrived)}",
            by=ACTOR,
        )
        return []
    local = case.confirmed_local or case.requested_local
    if _raised_before(case, ExceptionType.PICKUP_NO_SHOW, "slot", local):
        return []
    tail = "" if covered else "; no carrier is on the load"
    why = f"booked for {booked}; Transport Pro shows no arrival at the shipper yet{tail}"
    flag(session, case, ExceptionType.PICKUP_NO_SHOW, why[:255], actor=ACTOR, slot=local)
    return ["pickup_no_show"]


def _carrier_name(dispatch: Dispatch | None) -> str | None:
    carrier = ((dispatch.assigned_to or {}) if dispatch else {}).get("carrier") or {}
    name = carrier.get("companyName")
    return str(name).strip() if name else None


def _raised_before(case: BookingCase, kind: ExceptionType, key: str, value: Any) -> bool:
    """Raised already for the same dispatch or booked time, open or resolved since."""
    return any(e.kind == kind.value and (e.detail or {}).get(key) == value for e in case.exceptions)


def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
    session.add(BookingEvent(case_id=case.id, action=action, actor=ACTOR, detail=detail))
