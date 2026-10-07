"""Which pickup time to ask for, and whether any time can still make the delivery.

A request asks for one time. :func:`recommend_time` decides it from what is known, in order, and
keeps each step so a person can see how the agent got there:

1. the day: the tendered pickup date, else back from the delivery by the transit days (a weekend
   rolls back to Friday);
2. the floor: a desk that reads the customer's PO date as the earliest pickup is never asked for
   an earlier day (a weekend rolls forward to Monday);
3. the time: the tendered time; else the time this facility usually gives, when one time is most
   of its confirmed appointments (Transport Pro's, from the harvest, and the agent's own
   bookings); else the pod's default;
4. the hours: a time outside the facility's hours that day moves to its opening, or to an hour
   before it closes;
5. the delivery: a time that would arrive too late moves earlier the same day, to the latest that
   still makes it, but not before the facility opens (without its hours, not before
   FP_BOOKING_EARLIEST_PICKUP_TIME, 05:00). When no time that day makes it, the load cannot make
   its delivery as tendered: the recommendation says so, with the latest pickup that would have.

The notice window, the desk's cut-off and how far ahead it books are checked afterwards
(``booking/rules.py``); this module only chooses the time.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import local_dt, transit_hours
from facility_profiles.booking.rules import VendorProfile
from facility_profiles.booking.templates import short_vendor
from facility_profiles.booking.timers import fmt_slot
from facility_profiles.business_days import holiday, is_business_day, why_closed
from facility_profiles.clock import local_to_eastern, stamp
from facility_profiles.config import Settings
from facility_profiles.customers import customer_of
from facility_profiles.domain.schema import Role, SourceType
from facility_profiles.storage.models import SourceDocument
from facility_profiles.storage.repository import as_utc
from facility_profiles.tpro.models import Waypoint

# A facility's usual time counts once it is at least this many of its confirmed appointments,
# and at least this share of them.
USUAL_MIN_COUNT = 3
USUAL_MIN_SHARE = 0.5
# Asked for no later than this before the facility closes.
BEFORE_CLOSING = timedelta(hours=1)
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


@dataclass(frozen=True)
class Step:
    """One thing that decided the time: which rule, what it said, and whether it moved the ask."""

    rule: str  # tender | delivery | floor | usual | default | hours | fit
    note: str
    moved: bool = False

    def as_dict(self) -> dict[str, Any]:
        """For an event's detail."""
        return {"rule": self.rule, "note": self.note, "moved": self.moved}


@dataclass(frozen=True)
class Recommendation:
    """The time to ask for, the steps that chose it, and whether it can make the delivery."""

    local: str | None  # "YYYY-MM-DD HH:MM", vendor local
    steps: tuple[Step, ...]
    feasible: bool
    verdict: str
    latest: str | None = None  # the latest pickup that makes the delivery, when this one cannot
    floor: date | None = None  # the PO date the desk loads from, when it moved the day
    timezone: str | None = None  # the facility's zone, for showing its slots

    @property
    def moved(self) -> list[Step]:
        """The steps that changed the ask (the floor among them)."""
        return [s for s in self.steps if s.moved]


@dataclass(frozen=True)
class Usual:
    """The time a facility usually gives."""

    clock: str
    count: int
    total: int


# ------------------------------------------------------------------ what the facility tells us


def usual_time(clocks: Iterable[str]) -> Usual | None:
    """The time most of a facility's appointments were at, when it is most of them."""
    counted = Counter(c for c in clocks if c and c != "00:00")
    if not counted:
        return None
    clock, count = counted.most_common(1)[0]
    total = sum(counted.values())
    if count < USUAL_MIN_COUNT or count / total < USUAL_MIN_SHARE:
        return None
    return Usual(clock, count, total)


def facility_history(session: Session, key: str | None) -> list[str]:
    """The pickup times this facility confirmed before, as "HH:MM" in its own time zone.

    Two sources: the confirmed appointments on its stops in the harvest (Transport Pro's
    appointment status), and the pickups the agent booked there itself. Tendered times are not
    evidence of what a desk gives, so they are left out.
    """
    if not key:
        return []
    clocks: list[str] = []
    docs = session.scalars(
        select(SourceDocument).where(
            SourceDocument.facility_key == key,
            SourceDocument.role == Role.SHIPPER.value,
            SourceDocument.source_type == SourceType.STOP_STRUCTURED.value,
        )
    )
    for doc in docs:
        try:
            stop = Waypoint.model_validate(json.loads(doc.text))
        except (ValueError, TypeError):
            continue
        appt = stop.appointment_time
        if appt is None or (appt.appointment_status or "").strip().lower() != "confirmed":
            continue
        start = appt.open_at
        if start is None:
            continue
        tz = ZoneInfo(
            (stop.location.iana_timezone if stop.location else None) or "America/New_York"
        )
        clocks.append(start.astimezone(tz).strftime("%H:%M"))
    booked = session.scalars(
        select(BookingCase.confirmed_local).where(
            BookingCase.facility_key == key,
            BookingCase.status == CaseStatus.SCHEDULED.value,
            BookingCase.confirmed_local.is_not(None),
        )
    )
    clocks.extend(clock for local in booked if local and (clock := local.partition(" ")[2]))
    return clocks


def hours_on(profile: VendorProfile | None, day: date) -> list[tuple[str, str]] | None:
    """The facility's open spans on ``day`` as ("HH:MM", "HH:MM"); None when hours are unknown."""
    spans = profile.hours if profile is not None else None
    if not spans:
        return None
    weekday = _WEEKDAYS[day.weekday()]
    found: list[tuple[str, str]] = []
    for span in spans:
        if not isinstance(span, dict) or weekday not in (span.get("days") or []):
            continue
        opens, closes = str(span.get("open") or ""), str(span.get("close") or "")
        if len(opens) == 5 and len(closes) == 5 and opens < closes:
            found.append((opens, closes))
    return sorted(found)


def open_on(profile: VendorProfile | None, day: date) -> bool:
    """Whether the facility ships on ``day``.

    Never on a freight holiday; on a Saturday or Sunday only when its hours say so; on a weekday
    unless its hours list no opening that day.
    """
    if holiday(day) is not None:
        return False
    spans = hours_on(profile, day)
    return bool(spans) if spans is not None else is_business_day(day)


def _closed_why(day: date) -> str:
    return why_closed(day) or f"a {day:%A}, which the facility's hours list closed"


def _open_day(profile: VendorProfile | None, day: date, *, earliest: date) -> date | None:
    """The nearest day the facility ships before ``day``, else after it.

    Not before ``earliest`` (a driver could not make it); None when none is open within two
    weeks.
    """
    before = day - timedelta(days=1)
    while before >= earliest and day - before <= timedelta(days=7):
        if open_on(profile, before):
            return before
        before -= timedelta(days=1)
    after = day + timedelta(days=1)
    while after - day <= timedelta(days=14):
        if open_on(profile, after):
            return after
        after += timedelta(days=1)
    return None


def _fmt_hours(spans: Sequence[tuple[str, str]]) -> str:
    return ", ".join(f"{a.replace(':', '')}-{b.replace(':', '')}" for a, b in spans)


def _minus(clock: str, delta: timedelta) -> str:
    moment = datetime.combine(date(2000, 1, 1), time.fromisoformat(clock)) - delta
    return moment.strftime("%H:%M")


def _within(clock: str, spans: Sequence[tuple[str, str]]) -> bool:
    return any(opens <= clock < closes for opens, closes in spans)


def _into_hours(clock: str, spans: Sequence[tuple[str, str]]) -> str:
    """The nearest workable time inside the spans: the next opening, else an hour before close."""
    later = [opens for opens, _ in spans if opens > clock]
    if later:
        return later[0]
    _, closes = spans[-1]
    return max(spans[-1][0], _minus(closes, BEFORE_CLOSING))


# ------------------------------------------------------------------ the floor


def pickup_floor(case: BookingCase, settings: Settings) -> date | None:
    """The earliest day the desk will load, for desks that read the PO date as the pickup date.

    Only for a customer whose PO numbers carry a date (its file's ``[numbers] po_date``).
    """
    desk = (case.contact_email or "").lower()
    if desk not in {d.lower() for d in settings.booking_po_date_floor_desks}:
        return None
    customer = customer_of(case, settings)
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    anchor = as_utc(case.delivery_at_utc) or as_utc(case.tendered_pickup_utc)
    near = (anchor or datetime.now(tz=UTC)).astimezone(tz).date()
    found = [
        d for d in (customer.po_embedded_date(str(p), near=near) for p in case.po_numbers) if d
    ]
    return max(found) if found else None


# ------------------------------------------------------------------ the recommendation


def _start(
    case: BookingCase, settings: Settings, *, use_tender: bool
) -> tuple[date, str | None, Step] | None:
    """The day (and time, when the tender has one) to start from."""
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    tender = as_utc(case.tendered_pickup_utc) if use_tender else None
    if tender is not None:
        local = tender.astimezone(tz)
        clock = None if local.strftime("%H:%M") == "00:00" else local.strftime("%H:%M")
        shown = fmt_slot(f"{local:%Y-%m-%d} {clock or ''}".strip(), case.vendor_timezone)
        return local.date(), clock, Step("tender", f"the tendered pickup, {shown}")
    delivery = as_utc(case.delivery_at_utc)
    if delivery is None:
        return None
    days = max(1, math.ceil((case.miles or 0) / settings.booking_transit_miles_per_day))
    day = delivery.astimezone(tz).date() - timedelta(days=days)
    while not is_business_day(day):  # vendors ship on business days
        day -= timedelta(days=1)
    plural = "" if days == 1 else "s"
    note = f"{days} day{plural} before the delivery on {stamp(delivery, '%m/%d', label=False)}"
    return day, None, Step("delivery", note)


def earliest_pickup(now: datetime, settings: Settings, tz: ZoneInfo) -> datetime:
    """The earliest pickup a driver can still make: past the notice window, on a business day.

    Never before the pod's start of day (FP_BOOKING_EARLIEST_PICKUP_TIME).
    """
    soonest = (now + timedelta(hours=settings.booking_min_notice_hours)).astimezone(tz)
    if soonest.minute or soonest.second or soonest.microsecond:
        bump = 30 - soonest.minute % 30
        soonest = (soonest + timedelta(minutes=bump)).replace(second=0, microsecond=0)
    starts = time.fromisoformat(settings.booking_earliest_pickup_time)
    if soonest.time() < starts:
        soonest = datetime.combine(soonest.date(), starts, tzinfo=tz)
    if not is_business_day(soonest.date()):
        day = soonest.date()
        while not is_business_day(day):
            day += timedelta(days=1)
        clock = time.fromisoformat(settings.booking_default_pickup_time)
        soonest = datetime.combine(day, clock, tzinfo=tz)
    return soonest


def _floor(
    case: BookingCase,
    settings: Settings,
    day: date,
    clock: str,
    profile: VendorProfile | None = None,
) -> tuple[date, Step] | None:
    """The PO date the desk loads from, when it is later than ``day`` (closed: the next open)."""
    floor = pickup_floor(case, settings)
    if floor is None or day >= floor:
        return None
    target = floor
    while not open_on(profile, target) and target - floor <= timedelta(days=14):
        target += timedelta(days=1)
    tz = case.vendor_timezone
    note = (
        f"moved from {fmt_slot(f'{day:%Y-%m-%d} {clock}', tz)} to "
        f"{fmt_slot(f'{target:%Y-%m-%d} {clock}', tz)}: "
        f"{short_vendor(case.vendor_name)} reads the PO date {floor:%m/%d} as the earliest pickup"
    )
    return target, Step("floor", note, moved=True)


def _clock(history: Iterable[str], default: str) -> tuple[str, Step]:
    """The time to ask for when none was tendered: the facility's usual one, else the default."""
    usual = usual_time(history)
    if usual is None:
        return default, Step("default", f"no time tendered; the pod's default {default}")
    note = (
        f"{usual.count} of the facility's {usual.total} confirmed appointments were at "
        f"{usual.clock}"
    )
    return usual.clock, Step("usual", note, moved=usual.clock != default)


def _hours(clock: str, spans: Sequence[tuple[str, str]], day: date) -> tuple[str, Step]:
    """The time moved into the facility's hours that day, with what was done."""
    if not spans:
        return clock, Step("hours", f"the facility's hours list no opening on {day:%a}")
    if _within(clock, spans):
        return clock, Step("hours", f"{clock} is within the facility's hours ({_fmt_hours(spans)})")
    moved = _into_hours(clock, spans)
    note = (
        f"{clock} is outside the facility's hours on {day:%a} ({_fmt_hours(spans)}); "
        f"asking for {moved}"
    )
    return moved, Step("hours", note, moved=True)


def recommend_time(
    case: BookingCase,
    settings: Settings,
    profile: VendorProfile | None,
    *,
    now: datetime,
    history: Iterable[str] = (),
    use_tender: bool = True,
) -> Recommendation:
    """The pickup time to ask for, and whether any time that day can still make the delivery.

    ``history`` is the facility's confirmed times (:func:`facility_history`). Without a tender
    (``use_tender`` off, after the customer moved the delivery) the day is backed off the
    delivery, and moved up to the earliest pickup a driver can still make when that day has
    passed. A tendered time that has passed is left for the desk's rules to raise.
    """
    started = _start(case, settings, use_tender=use_tender)
    if started is None:
        return Recommendation(None, (), True, "no tender or delivery slot to go by")
    day, clock, first = started
    steps = [first]
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    default = settings.booking_default_pickup_time
    earliest = earliest_pickup(now, settings, tz)
    if not open_on(profile, day):
        # A tendered Saturday, a holiday, a day the facility's hours have it closed: the
        # business day before, when a driver can still make it, else the next open day.
        moved_to = _open_day(profile, day, earliest=earliest.date())
        if moved_to is not None:
            shown = clock or default
            steps.append(
                Step(
                    "closed",
                    f"{fmt_slot(f'{day:%Y-%m-%d} {shown}', case.vendor_timezone)} is "
                    f"{_closed_why(day)}; asking for "
                    f"{fmt_slot(f'{moved_to:%Y-%m-%d} {shown}', case.vendor_timezone)}",
                    moved=True,
                )
            )
            day = moved_to
    floored = _floor(case, settings, day, clock or default, profile)
    floor_day = pickup_floor(case, settings) if floored is not None else None
    if floored is not None:
        target, step = floored
        steps.append(step)
        day = target
    if clock is None:
        clock, step = _clock(history, default)
        steps.append(step)

    if (
        first.rule == "delivery"
        and local_dt(f"{day:%Y-%m-%d}", clock, case.vendor_timezone) < earliest
    ):
        day, clock = earliest.date(), earliest.strftime("%H:%M")
        steps.append(
            Step(
                "soon",
                "that day has passed; the earliest pickup a driver can still make is "
                f"{fmt_slot(f'{day:%Y-%m-%d} {clock}', case.vendor_timezone)}",
                moved=True,
            )
        )

    spans = hours_on(profile, day)
    if spans is not None:
        clock, step = _hours(clock, spans, day)
        steps.append(step)

    delivery = as_utc(case.delivery_at_utc)
    if delivery is None:
        return Recommendation(
            f"{day:%Y-%m-%d} {clock}",
            tuple(steps),
            True,
            "no delivery slot to check against",
            floor=floor_day,
        )
    latest = (delivery - timedelta(hours=transit_hours(case, settings))).astimezone(tz)
    latest = latest.replace(minute=0 if latest.minute < 30 else 30, second=0, microsecond=0)
    pickup = local_dt(f"{day:%Y-%m-%d}", clock, case.vendor_timezone)
    if pickup <= latest:
        return Recommendation(
            f"{day:%Y-%m-%d} {clock}", tuple(steps), True, "makes the delivery", floor=floor_day
        )
    starts = settings.booking_earliest_pickup_time
    fits_today = (
        latest.date() == day
        and latest >= earliest
        and (
            _within(latest.strftime("%H:%M"), spans)
            if spans is not None
            else latest.strftime("%H:%M") >= starts
        )
    )
    if fits_today:
        fit = latest.strftime("%H:%M")
        asked_et = _eastern_clock(day, clock, case.vendor_timezone)
        fit_et = _eastern_clock(day, fit, case.vendor_timezone)
        steps.append(
            Step(
                "fit",
                f"{asked_et} ET would arrive after the delivery at {stamp(delivery)}; "
                f"{fit_et} ET is the latest that makes it",
                moved=True,
            )
        )
        return Recommendation(
            f"{day:%Y-%m-%d} {fit}", tuple(steps), True, "makes the delivery", floor=floor_day
        )
    shown_latest = f"{latest:%Y-%m-%d %H:%M}"
    slot = f"{day:%Y-%m-%d} {clock}"
    arrival = pickup + timedelta(hours=transit_hours(case, settings))
    verdict = (
        f"a pickup {fmt_slot(slot, case.vendor_timezone)} arrives "
        f"{stamp(arrival, '%a %m/%d %H:%M')}, after the delivery slot "
        f"{stamp(delivery, '%a %m/%d %H:%M')}"
    )
    return Recommendation(
        slot,
        tuple(steps),
        False,
        verdict,
        shown_latest,
        floor=floor_day,
        timezone=case.vendor_timezone,
    )


def _eastern_clock(day: date, clock: str, timezone: str | None) -> str:
    """A facility-clock time that day, as the Eastern clock shows it ("HH:MM")."""
    return (local_to_eastern(f"{day:%Y-%m-%d} {clock}", timezone) or "").partition(" ")[2] or clock


def infeasible_note(rec: Recommendation) -> str:
    """The to-do's one line when no pickup that day makes the delivery."""
    floor = f"the PO date {rec.floor:%m/%d} is the earliest pickup; " if rec.floor else ""
    latest = (
        f"; the latest pickup that makes it is {fmt_slot(rec.latest, rec.timezone)}"
        if rec.latest
        else ""
    )
    return f"cannot make the delivery: {floor}{rec.verdict}{latest}"[:255]
