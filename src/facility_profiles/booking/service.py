"""Booking cases: find loads to book, compose the request, read the reply, propose the slot.

Draft mode: the agent never sends and never writes to Transport Pro. A person sends the draft
(the archive links it to the case, or ``booking sent``) and approves the vendor's confirmation
(``booking approve``). Every step is recorded on the case.

A case's status says where the appointment stands (unscheduled, pending, scheduled, declined,
canceled). Whatever needs a person is an exception on the case (``booking/worklist.py``), raised
here where the agent stops and resolved where the situation clears.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.booking.classify import (
    ClassificationIssue,
    ReplyClassifier,
    ReplyContext,
    for_case,
    validate_classification,
)
from facility_profiles.booking.mail import (
    InboundMessage,
    Mailer,
    OutboundDraft,
    Sender,
    deliver,
)
from facility_profiles.booking.memory import VIA_METHODS, Learned, remember_booking
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseException,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.outbox import (
    check_send_gate,
    dispatch,
    is_sender,
    record_delivery,
)
from facility_profiles.booking.respond import (
    Responder,
    customer_label,
    local_dt,
    offer_is_feasible,
)
from facility_profiles.booking.rules import (
    REFERENCE_LABELS,
    REFERENCE_NAMES,
    VendorProfile,
    check_desk_rules,
    extra_references,
    missing_references,
    slot_is_stale,
    too_early,
    vendor_profile,
)
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.timers import fmt_slot
from facility_profiles.booking.worklist import (
    QUESTION_SUPERSEDES,
    SLOT_REPLY_SUPERSEDES,
    TIMER_KINDS,
    flag,
    method_exception,
    open_exceptions,
    open_kinds,
    resolve,
    resolve_all,
)
from facility_profiles.config import Settings
from facility_profiles.domain.resolution import FacilityResolver
from facility_profiles.logging import get_logger
from facility_profiles.mailarchive.filters import normalize_subject, participants
from facility_profiles.pipeline.harvest import iter_terminal_loads, stop_identity
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc
from facility_profiles.tpro.client import TransportProClient
from facility_profiles.tpro.models import Load, Waypoint

log = get_logger(__name__)

DELIVERY_REF_RE = re.compile(r"\b[A-Z]{3}_\d{6,}\b")
PO_RE = re.compile(r"\b\d{9,15}\b")
# Lidl's inbound desk writes a delivery slot as "8/20 7AM - GRM_200826926", as "10/6 730AM -
# PYE_061026919", or on two lines ("9/30 at 1100" then "PYE_300926723"); the pod writes its own
# DCT bookings as "FRG_200526615 05/20 @ 1100". All forms are read.
DELIVERY_SLOT_RE = re.compile(
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\s*(?:@|at)?\s*(?P<h>\d{1,2})(?::?(?P<min>\d{2}))?"
    r"\s*(?P<ampm>AM|PM)?[\s,;:\-\u2013\u2014]*(?P<ref>[A-Z]{3}_\d{6,})",
    re.I,
)
# Lidl PO numbers are twelve digits with the delivery date as DDMMYY in digits five to ten
# ("115802102660" is 2 October 2026). Morgan Foods reads that date as the earliest pickup.
LIDL_PO_RE = re.compile(r"^\d{12}$")
PO_DATE_WINDOW_DAYS = 60
DELIVERY_SLOT_REF_FIRST_RE = re.compile(
    r"(?P<ref>[A-Z]{3}_\d{6,})\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\s*(?:@|at)?\s*"
    r"(?P<h>\d{1,2})(?::?(?P<min>\d{2}))?\s*(?P<ampm>AM|PM)?",
    re.I,
)
OPEN_STATUSES = (
    CaseStatus.UNSCHEDULED.value,
    CaseStatus.PENDING.value,
    CaseStatus.DECLINED.value,
)
# Booked or no longer needed; later mail in the thread (ETAs, securement, "did this get
# resolved?") is kept on the case but never read as a new answer to the request.
DECIDED_STATUSES = (CaseStatus.SCHEDULED.value, CaseStatus.CANCELED.value)
# A "confirmation" of a slot that had already passed when the vendor wrote is a work-in or a
# late-arrival note ("latest is 9pm tonight"), not a booking. Same-day replies a few minutes
# after the slot still count.
STALE_CONFIRMATION_GRACE = timedelta(minutes=30)
# A confirmation on another day, or more than this far from the time asked for, is raised for the
# person approving it. Requests ask for one exact time; desks round it to their dock schedule.
CONFIRM_WINDOW = timedelta(hours=2)


@dataclass
class ScanStats:
    """What a scan did."""

    loads: int = 0
    created: int = 0
    already_confirmed: int = 0
    existing: int = 0
    no_pickup_stop: int = 0
    needs_profile: int = 0
    already_booked: int = 0
    case_ids: list[int] = field(default_factory=list)


@dataclass
class IngestStats:
    """What an inbox pass did."""

    messages: int = 0
    skipped_internal: int = 0
    duplicates: int = 0
    unmatched: int = 0
    classified: int = 0
    proposed: int = 0
    needs_human: int = 0
    unrelated: int = 0
    responded: int = 0
    deferred: int = 0
    delivery_updates: int = 0
    after_decision: int = 0  # mail on scheduled or canceled cases, recorded but not classified
    linked_outbound: int = 0  # a person's own send of a drafted request, recognised and linked
    not_about_case: int = 0  # a reply that named other POs than this case's
    own_outbound: int = 0  # the agent's own sent mail seen again in the archive


# ------------------------------------------------------------------ profile and load helpers


def pickup_waypoint(load: Load) -> tuple[int, Waypoint] | None:
    """Index and waypoint of the first pickup stop."""
    for index, wp in enumerate(load.waypoints):
        if wp.role == "shipper":
            return index, wp
    return None


def delivery_waypoint(load: Load) -> Waypoint | None:
    """The first delivery stop."""
    return next((wp for wp in load.waypoints if wp.role == "receiver"), None)


def po_numbers(load: Load) -> list[str]:
    """Customer order numbers on the load (PO and reference number)."""
    ref = load.reference or {}
    seen: list[str] = []
    for name in ("poNumber", "referenceNumber"):
        value = str(ref.get(name) or "").strip()
        if value and value.isdigit() and value not in seen:
            seen.append(value)
    return seen


def requested_local(
    *,
    tendered_pickup_utc: datetime | None,
    delivery_at_utc: datetime | None,
    timezone: str | None,
    miles: int | None,
    settings: Settings,
) -> str | None:
    """The slot to ask for, in vendor local time: the tendered date, else back from delivery."""
    tz = ZoneInfo(timezone or "America/New_York")
    if tendered_pickup_utc is not None:
        local = tendered_pickup_utc.astimezone(tz)
        if local.strftime("%H:%M") == "00:00":
            return f"{local:%Y-%m-%d} {settings.booking_default_pickup_time}"
        return local.strftime("%Y-%m-%d %H:%M")
    if delivery_at_utc is None:
        return None
    days = max(1, math.ceil((miles or 0) / settings.booking_transit_miles_per_day))
    day = delivery_at_utc.astimezone(tz).date() - timedelta(days=days)
    while day.weekday() >= 5:  # vendors ship Monday to Friday
        day -= timedelta(days=1)
    return f"{day:%Y-%m-%d} {settings.booking_default_pickup_time}"


def po_embedded_date(po: str, *, near: date) -> date | None:
    """The DDMMYY date inside a Lidl PO, when it is a real date within two months of ``near``."""
    if not LIDL_PO_RE.match(po):
        return None
    try:
        found = date(2000 + int(po[8:10]), int(po[6:8]), int(po[4:6]))
    except ValueError:
        return None
    return found if abs((found - near).days) <= PO_DATE_WINDOW_DAYS else None


def pickup_floor(case: BookingCase, settings: Settings) -> date | None:
    """The earliest day the desk will load, for desks that read the PO date as the pickup date."""
    desk = (case.contact_email or "").lower()
    if desk not in {d.lower() for d in settings.booking_po_date_floor_desks}:
        return None
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    anchor = as_utc(case.delivery_at_utc) or as_utc(case.tendered_pickup_utc)
    near = (anchor or datetime.now(tz=UTC)).astimezone(tz).date()
    found = [d for d in (po_embedded_date(str(p), near=near) for p in case.po_numbers) if d]
    return max(found) if found else None


def floor_requested(
    case: BookingCase, requested: str | None, settings: Settings, *, now: datetime
) -> tuple[str | None, str | None, bool]:
    """Apply the desk's PO-date floor to a requested slot.

    Returns the slot to use, why it moved (or None), and whether that slot can still make the
    customer's delivery. A weekend floor rolls forward to Monday.
    """
    floor = pickup_floor(case, settings)
    if floor is None or not requested:
        return requested, None, True
    day_text, _, clock = requested.partition(" ")
    try:
        day = date.fromisoformat(day_text)
    except ValueError:
        return requested, None, True
    if day >= floor:
        return requested, None, True
    target = floor
    while target.weekday() >= 5:
        target += timedelta(days=1)
    moved = f"{target:%Y-%m-%d} {clock}".strip()
    feasible, verdict = offer_is_feasible(
        case, local_dt(f"{target:%Y-%m-%d}", clock or None, case.vendor_timezone), settings, now=now
    )
    why = (
        f"moved from {requested} to {moved}: {short_vendor(case.vendor_name)} reads the PO date "
        f"{floor:%m/%d} as the earliest pickup"
    )
    if not feasible:
        why += f"; that {verdict}"
    return moved, why, feasible


def parse_delivery_slot(
    text: str, *, year: int, timezone: str | None
) -> tuple[datetime, str] | None:
    """Read a Lidl delivery slot and DCT reference out of a message; returns (UTC start, ref)."""
    match = DELIVERY_SLOT_RE.search(text) or DELIVERY_SLOT_REF_FIRST_RE.search(text)
    if match is None:
        return None
    hour = int(match.group("h"))
    minute = int(match.group("min") or 0)
    ampm = (match.group("ampm") or "").upper()
    if ampm == "PM" and hour < 12:
        hour += 12
    if ampm == "AM" and hour == 12:
        hour = 0
    if not ampm and match.group("min") is None and hour > 24:
        return None
    if not ampm and match.group("min") is None and len(match.group("h")) == 4:
        hour, minute = int(match.group("h")[:2]), int(match.group("h")[2:])
    raw_year = match.group("y")
    yr = year if not raw_year else (int(raw_year) + 2000 if len(raw_year) == 2 else int(raw_year))
    try:
        local = datetime(
            yr,
            int(match.group("m")),
            int(match.group("d")),
            hour,
            minute,
            tzinfo=ZoneInfo(timezone or "America/New_York"),
        )
    except ValueError:
        return None
    return local.astimezone(UTC), match.group("ref").upper()


def outside_request(
    case: BookingCase, day: str, clock: str | None, *, date_only: bool = False
) -> str | None:
    """Why a confirmed slot is not what was asked for, or None when it is close enough.

    Another day is always outside. On the same day, a time more than :data:`CONFIRM_WINDOW`
    from the one asked for is outside, unless the desk is asked for a date only.
    """
    if not case.requested_local:
        return None
    asked_day, _, asked_clock = case.requested_local.partition(" ")
    confirmed = f"{day} {clock or ''}".strip()
    said = f"vendor confirmed {fmt_slot(confirmed)}; we asked for {fmt_slot(case.requested_local)}"
    if day != asked_day:
        return said
    if date_only or not clock or not asked_clock:
        return None
    gap = local_dt(day, clock, case.vendor_timezone) - local_dt(
        asked_day, asked_clock, case.vendor_timezone
    )
    if abs(gap) <= CONFIRM_WINDOW:
        return None
    hours = abs(gap).total_seconds() / 3600
    return f"{said} ({hours:g} h {'later' if gap > timedelta(0) else 'earlier'})"


def _event(
    session: Session, case: BookingCase, action: str, actor: str = "agent", **detail: Any
) -> None:
    session.add(BookingEvent(case_id=case.id, action=action, actor=actor, detail=detail))


# ------------------------------------------------------------------ scan


def scan(
    client: TransportProClient,
    sessions: sessionmaker[Session],
    settings: Settings,
    *,
    terminal_ids: list[int] | None = None,
    customer_ids: list[int] | None = None,
    days_ahead: int | None = None,
    now: datetime | None = None,
) -> ScanStats:
    """Open a case for every pickup stop that still needs an appointment."""
    now = now or datetime.now(tz=UTC)
    start = now.date()
    end = start + timedelta(days=days_ahead or settings.booking_days_ahead)
    terminals: list[int | None] = list(terminal_ids or settings.pilot_terminal_ids) or [None]
    customers: list[int | None] = list(customer_ids or settings.pilot_customer_ids) or [None]
    stats = ScanStats()
    resolver = FacilityResolver([])
    with session_scope(sessions) as session:
        repo = Repository(session)
        for load in iter_terminal_loads(
            client, terminal_ids=terminals, customer_ids=customers, start=start, end=end
        ):
            stats.loads += 1
            found = pickup_waypoint(load)
            if found is None:
                stats.no_pickup_stop += 1
                continue
            index, wp = found
            appt = wp.appointment_time
            if appt and (appt.appointment_status or "").lower() == "confirmed":
                stats.already_confirmed += 1
                continue
            existing = session.scalar(
                select(BookingCase).where(
                    BookingCase.load_id == load.id, BookingCase.waypoint_index == index
                )
            )
            if existing is not None:
                stats.existing += 1
                continue
            key = resolver.resolve(stop_identity(wp)).key
            profile = vendor_profile(repo, key)
            drop = delivery_waypoint(load)
            delivery_at = drop.appointment_time.open_at if drop and drop.appointment_time else None
            ref_match = DELIVERY_REF_RE.search(drop.notes or "") if drop else None
            loc = wp.location
            miles = (load.reference or {}).get("miles")
            pickup_no = str((load.reference or {}).get("pickupNumber") or "").strip()
            blocker: tuple[ExceptionType, str] | None = None
            if pickup_no:
                status, reason = (
                    CaseStatus.SCHEDULED.value,
                    f"load already carries vendor pickup number {pickup_no}",
                )
            else:
                status, reason = CaseStatus.UNSCHEDULED.value, None
                if not profile.can_email:
                    blocker = method_exception(
                        profile.booking_method,
                        portal_vendor=profile.portal_vendor,
                        portal_url=profile.portal_url,
                    )
            case = BookingCase(
                load_id=load.id,
                waypoint_index=index,
                customer_id=load.billing_info.customer_id if load.billing_info else None,
                customer_name=load.customer_name,
                facility_key=key,
                vendor_name=loc.company_name if loc else None,
                vendor_city=f"{loc.city}, {loc.state}" if loc and loc.city else None,
                vendor_timezone=loc.iana_timezone if loc else None,
                po_numbers=po_numbers(load),
                booking_method=profile.booking_method,
                contact_email=profile.contact_email,
                contact_name=profile.contact_name,
                delivery_site=(
                    f"{drop.location.company_name} ({drop.location.city}, {drop.location.state})"
                    if drop and drop.location and drop.location.company_name
                    else None
                ),
                delivery_ref=ref_match.group(0) if ref_match else None,
                delivery_at_utc=delivery_at,
                tendered_pickup_utc=appt.open_at if appt else None,
                miles=int(miles) if isinstance(miles, int | float) else None,
                pickup_number=pickup_no or None,
                status=status,
                reason=reason,
            )
            case.requested_local = requested_local(
                tendered_pickup_utc=case.tendered_pickup_utc,
                delivery_at_utc=case.delivery_at_utc,
                timezone=case.vendor_timezone,
                miles=case.miles,
                settings=settings,
            )
            session.add(case)
            session.flush()
            if blocker is not None:
                flag(session, case, blocker[0], blocker[1], method=profile.booking_method)
            _event(
                session,
                case,
                "scanned",
                status=case.status,
                reason=case.reason or (blocker[1] if blocker else None),
            )
            _check_requested_slot(session, case, settings, now=now, profile=profile)
            stats.created += 1
            stats.case_ids.append(case.id)
            if pickup_no:
                stats.already_booked += 1
            elif blocker is not None:
                stats.needs_profile += 1
    return stats


def _check_requested_slot(
    session: Session,
    case: BookingCase,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
) -> None:
    """Apply the PO-date floor, the notice window and the desk's rules to a freshly scanned case.

    Only a case the agent could email is affected. A floor that still makes the delivery just
    moves the ask; one that does not, a slot already inside the notice window or past the desk's
    cut-off, or a number the desk needs that the load lacks, is raised for a person before any
    email is written. A desk that does not book that far ahead yet makes the request wait.
    """
    if case.status != CaseStatus.UNSCHEDULED.value or case.open_exceptions:
        return
    moved, why, feasible = floor_requested(case, case.requested_local, settings, now=now)
    if why:
        case.requested_local = moved
        _event(session, case, "po_date_floor", reason=why, feasible=feasible)
        if not feasible:
            flag(session, case, ExceptionType.SLOT_UNWORKABLE, why, requested=moved)
            return
    check_desk_rules(session, case, settings, now=now, profile=profile)


# ------------------------------------------------------------------ compose and draft


def _fmt_local(value: str | None) -> tuple[str, str]:
    """Turn "YYYY-MM-DD HH:MM" into ("MM/DD", "HHMM"), the way the pod writes it."""
    if not value:
        return ("(date to confirm)", "")
    day, _, clock = value.partition(" ")
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return (day, clock.replace(":", ""))
    return (parsed.strftime("%m/%d"), clock.replace(":", ""))


def request_lines(
    case: BookingCase, *, date_only: bool = False, extra: Iterable[str] = ()
) -> list[str]:
    """The PO lines exactly as the pod writes them: "PO# X on MM/DD @ HHMM".

    ``extra`` holds the numbers the desk needs besides the PO ("Shipment# 7781234"); they follow
    the PO: "PO# X / Shipment# 7781234 on MM/DD @ HHMM".
    """
    mmdd, clock = _fmt_local(case.requested_local)
    when = f"on {mmdd}" + (f" @ {clock}" if clock and not date_only else "")
    refs = "".join(f" / {ref}" for ref in extra)
    pos = [str(p) for p in case.po_numbers]
    if not pos:
        return [f"Load {case.load_id}{refs} {when}"]
    if len(pos) > 1:
        return [f"PO# {' & '.join(pos)} (ALL IN ONE TRUCK){refs} {when}"]
    return [f"PO# {pos[0]}{refs} {when}"]


def short_vendor(name: str | None) -> str:
    """Drop the corporate suffix: "Koch Foods, Inc." becomes "Koch Foods"."""
    return re.sub(r",?\s*\b(inc|llc|corp|co)\b\.?$", "", name or "the shipper", flags=re.I).strip()


def compose_request(
    case: BookingCase, settings: Settings, profile: VendorProfile | None = None
) -> OutboundDraft:
    """The request email, in the shape the pod already uses."""
    pos = [str(p) for p in case.po_numbers]
    shared = (case.contact_email or "").lower() in {
        d.lower() for d in settings.booking_shared_desks
    }
    ask = (
        f"Can I please schedule the following for {short_vendor(case.vendor_name)} going to "
        f"{customer_label(case.customer_name)}?"
        if shared
        else "Can I please schedule the following?"
    )
    subject = (
        f"Pick Up Appointment: {' & '.join(pos)}"
        if pos
        else f"Pick Up Appointment: load {case.load_id}"
    )
    lines = [
        "Hello,",
        "",
        ask,
        "",
        *request_lines(
            case,
            date_only=bool(profile and profile.date_only),
            extra=extra_references(case, profile),
        ),
        "",
        "Thank you!",
        "",
        settings.booking_signature,
    ]
    return OutboundDraft(
        to_addr=case.contact_email or "",
        cc_addr=", ".join(settings.booking_cc),
        subject=subject,
        body="\n".join(lines),
    )


def draft_case(
    session: Session,
    case: BookingCase,
    mailer: Mailer | Sender,
    settings: Settings,
    *,
    by: str = "agent",
    now: datetime | None = None,
) -> BookingMessage:
    """Compose the request and hand it to the outbox: a draft for a person, or a send.

    With a :class:`Sender` the request goes out only when the send gate passes (send mode on,
    the recipient is the profile's trusted desk, the daily cap not reached) and the case moves
    straight to ``pending`` with the ids a reply will point back at; a draft leaves it
    ``unscheduled`` until a person sends it. A slot that has gone stale since the scan is
    refused either way: a same-day ask needs a person.
    """
    profile = vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
    _check_draftable(case, settings, now=now or datetime.now(tz=UTC), profile=profile)
    draft = compose_request(case, settings, profile)
    if is_sender(mailer):
        trusted = profile.contact_email if profile and profile.can_email else None
        check_send_gate(session, case, draft, settings, trusted_desk=trusted)
    message = BookingMessage(
        case_id=case.id,
        direction="out",
        kind="request",
        to_addr=draft.to_addr,
        cc_addr=draft.cc_addr,
        subject=draft.subject,
        body=draft.body,
    )
    case.messages.append(message)
    session.flush()
    result = dispatch(session, case, message, mailer, draft, actor=by)
    case.status = CaseStatus.PENDING.value if result.sent else CaseStatus.UNSCHEDULED.value
    case.reason = None
    session.flush()
    _event(session, case, "drafted", draft_ref=result.ref, to=draft.to_addr, subject=draft.subject)
    return message


def _check_draftable(
    case: BookingCase,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
) -> None:
    """Refuse a request the agent must not write.

    That is: a case that is not unscheduled, has open exceptions or no desk, whose slot has
    passed or is inside the notice window or the desk's cut-off, that lacks a number the desk
    needs, or that the desk would not book yet.
    """
    if case.status != CaseStatus.UNSCHEDULED.value:
        msg = f"case {case.id} is {case.status}; only unscheduled cases can be drafted"
        raise ValueError(msg)
    blocking = open_kinds(case)
    if blocking:
        msg = f"case {case.id} has open exceptions ({', '.join(blocking)}); resolve them first"
        raise ValueError(msg)
    if not case.contact_email:
        msg = f"case {case.id} has no booking email"
        raise ValueError(msg)
    stale = slot_is_stale(
        case.requested_local, case.vendor_timezone, settings, now=now, profile=profile
    )
    if stale:
        msg = f"case {case.id}: {stale}; not drafted"
        raise ValueError(msg)
    missing = missing_references(case, profile)
    if missing:
        names = ", ".join(REFERENCE_NAMES.get(m, m) for m in missing)
        msg = f"case {case.id}: the desk needs the {names}; add it with booking ref"
        raise ValueError(msg)
    wait = too_early(case.requested_local, case.vendor_timezone, profile, now=now)
    if wait:
        msg = f"case {case.id}: {wait}; not drafted yet"
        raise ValueError(msg)


def has_request(case: BookingCase) -> bool:
    """True once a request for the case has been drafted or sent."""
    return any(m.direction == "out" and m.kind == "request" for m in case.messages)


def ready_to_draft(session: Session) -> list[BookingCase]:
    """Unscheduled cases with a desk, nothing open and no request yet.

    This is what ``booking draft`` and ``booking send`` pick up when no case is named.
    """
    return [
        c
        for c in list_cases(session, CaseStatus.UNSCHEDULED.value)
        if c.contact_email and not c.open_exceptions and not has_request(c)
    ]


def prepare_drafts(
    session: Session, settings: Settings, *, now: datetime
) -> tuple[list[BookingCase], list[tuple[BookingCase, str]]]:
    """The cases to draft now, and those that wait, after checking each desk's rules.

    A case the rules stop (past the cut-off, a number missing) gets its exception and drops out;
    a case whose desk does not book that far ahead yet waits, with the day it can be asked.
    """
    repo = Repository(session)
    ready: list[BookingCase] = []
    waiting: list[tuple[BookingCase, str]] = []
    for case in ready_to_draft(session):
        profile = vendor_profile(repo, case.facility_key) if case.facility_key else None
        wait = check_desk_rules(session, case, settings, now=now, profile=profile)
        if case.open_exceptions:
            continue
        if wait:
            waiting.append((case, wait))
        else:
            ready.append(case)
    return ready, waiting


def add_reference(
    session: Session, case: BookingCase, kind: str, value: str, *, by: str
) -> list[str]:
    """A person adds a number the desk needs (the customer's shipment or SO number).

    The number goes into the request line. When the case now has every number its desk needs,
    the ``missing_reference`` to-do is resolved. Returns the numbers still missing.
    """
    if kind not in REFERENCE_LABELS:
        msg = f"reference type must be one of {', '.join(REFERENCE_LABELS)}"
        raise ValueError(msg)
    value = value.strip()
    if not value:
        msg = "the reference needs a value"
        raise ValueError(msg)
    previous = (case.reference_numbers or {}).get(kind)
    case.reference_numbers = {**(case.reference_numbers or {}), kind: value}
    profile = vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
    missing = missing_references(case, profile)
    label = f"{REFERENCE_LABELS[kind]} {value}"
    if missing:
        names = " and ".join(REFERENCE_NAMES.get(m, m) for m in missing)
        flag(
            session,
            case,
            ExceptionType.MISSING_REFERENCE,
            f"the desk needs the {names} before it books",
            actor=by,
            missing=missing,
        )
    else:
        resolve(
            session, case, [ExceptionType.MISSING_REFERENCE], resolution=f"{label} added", by=by
        )
    _event(session, case, "reference_added", actor=by, kind=kind, value=value, previous=previous)
    return missing


def reschedule_case(
    session: Session,
    case: BookingCase,
    mailer: Mailer | Sender,
    settings: Settings,
    *,
    requested_local: str,
    by: str,
    note: str | None = None,
) -> BookingMessage:
    """Ask the vendor for a new slot in the same thread (a Circle-side miss, most often).

    Works from pending, scheduled or declined. The case goes back to pending with the count of
    reschedules raised, and whatever was open on it is resolved: asking again is the answer.
    """
    if case.status == CaseStatus.UNSCHEDULED.value:
        msg = f"case {case.id} is unscheduled; nothing to reschedule yet"
        raise ValueError(msg)
    if case.status == CaseStatus.CANCELED.value:
        msg = f"case {case.id} is canceled; nothing to reschedule"
        raise ValueError(msg)
    if not case.contact_email:
        msg = f"case {case.id} has no booking email"
        raise ValueError(msg)
    previous = case.confirmed_local or case.requested_local
    case.requested_local = requested_local
    case.confirmed_local = None
    case.confirmed_start_utc = None
    case.confirmed_end_utc = None
    profile = vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
    line = request_lines(
        case,
        date_only=bool(profile and profile.date_only),
        extra=extra_references(case, profile),
    )[0]
    body_lines = ["Hello,", ""]
    if note:
        body_lines.extend([note.strip(), ""])
    body_lines.extend(
        [f"Can we please reschedule {line}?", "", "Thank you!", "", settings.booking_signature]
    )
    original = next((m.subject for m in case.messages if m.direction == "out" and m.subject), None)
    subject = original or f"Pick Up Appointment: {' & '.join(str(p) for p in case.po_numbers)}"
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    last_in = next((m for m in reversed(case.messages) if m.direction == "in"), None)
    draft = OutboundDraft(
        to_addr=case.contact_email,
        cc_addr=", ".join(settings.booking_cc),
        subject=subject,
        body="\n".join(body_lines),
        thread_id=case.thread_id,
        in_reply_to=last_in.rfc_message_id if last_in else None,
        references=last_in.references_header if last_in else None,
    )
    if is_sender(mailer):
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        trusted = profile.contact_email if profile and profile.can_email else None
        check_send_gate(session, case, draft, settings, trusted_desk=trusted)
    message = BookingMessage(
        case_id=case.id,
        direction="out",
        kind="reschedule",
        to_addr=draft.to_addr,
        cc_addr=draft.cc_addr,
        subject=subject,
        body=draft.body,
        thread_id=case.thread_id,
    )
    case.messages.append(message)
    session.flush()
    result = dispatch(session, case, message, mailer, draft, actor=by)
    case.status = CaseStatus.PENDING.value
    case.reason = None
    case.reschedule_count = (case.reschedule_count or 0) + 1
    resolve_all(session, case, resolution=f"pickup asked for again: {requested_local}", by=by)
    session.flush()
    _event(
        session,
        case,
        "reschedule",
        actor=by,
        previous=previous,
        requested=requested_local,
        note=note,
        draft_ref=result.ref,
    )
    return message


def mark_sent(
    session: Session,
    case: BookingCase,
    *,
    by: str,
    thread_id: str | None = None,
    message_id: str | None = None,
    rfc_message_id: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    """A person sent the draft; remember the thread and Message-ID so replies can be matched.

    Also accepted for a pending case without a thread, which is how a manual ``booking sent``
    gets its ids once the archive shows the message.
    """
    unlinked = case.status == CaseStatus.PENDING.value and not case.thread_id
    if case.status != CaseStatus.UNSCHEDULED.value and not unlinked:
        msg = f"case {case.id} is {case.status}; nothing to mark as sent"
        raise ValueError(msg)
    request = next((m for m in reversed(case.messages) if m.direction == "out"), None)
    if request is not None:
        request.sent_at = sent_at or request.sent_at or datetime.now(tz=UTC)
        request.thread_id = thread_id or request.thread_id
        request.message_id = message_id or request.message_id
        request.rfc_message_id = rfc_message_id or request.rfc_message_id
    case.thread_id = thread_id or case.thread_id
    case.status = CaseStatus.PENDING.value
    _event(
        session,
        case,
        "sent",
        actor=by,
        thread_id=thread_id,
        message_id=message_id,
        rfc_message_id=rfc_message_id,
    )


# ------------------------------------------------------------------ replies


def list_cases(
    session: Session, status: str | None = None, *, exception: str | None = None
) -> list[BookingCase]:
    """Cases, newest first, optionally by status and by open exception (a kind, or ``any``)."""
    stmt = select(BookingCase).order_by(BookingCase.id.desc())
    if status:
        stmt = stmt.where(BookingCase.status == status)
    if exception:
        flagged = select(CaseException.case_id).where(CaseException.resolved_at.is_(None))
        if exception != "any":
            flagged = flagged.where(CaseException.kind == exception)
        stmt = stmt.where(BookingCase.id.in_(flagged))
    return list(session.scalars(stmt))


def match_cases(session: Session, message: InboundMessage) -> list[BookingCase]:
    """Every case a reply belongs to, oldest first.

    A batched request covers several cases with one email, so one reply can answer several
    cases. In order: the Message-IDs the reply points at (In-Reply-To, then References) against
    what the agent or a person sent; the Gmail thread, when the reply was read from the mailbox
    that sent the request; PO numbers in the reply's own words; a lone open case for the sender.
    """
    referenced = message.referenced_ids
    if referenced:
        answered = list(
            session.scalars(
                select(BookingMessage).where(
                    BookingMessage.direction == "out",
                    func.lower(BookingMessage.rfc_message_id).in_(referenced),
                )
            )
        )
        if answered:
            return _distinct(m.case for m in answered)
    if message.thread_id:
        in_thread = list(
            session.scalars(
                select(BookingCase)
                .where(BookingCase.thread_id == message.thread_id)
                .order_by(BookingCase.id)
            )
        )
        if in_thread:
            return in_thread
    open_cases = list(
        session.scalars(
            select(BookingCase)
            .where(BookingCase.status.in_(OPEN_STATUSES))
            .order_by(BookingCase.id)
        )
    )
    numbers = set(PO_RE.findall(f"{message.subject} {message.body}"))
    if numbers:
        hits = [c for c in open_cases if numbers & {str(p) for p in c.po_numbers}]
        if hits:
            return hits
    sender = message.from_email
    by_sender = [c for c in open_cases if (c.contact_email or "").lower() == sender]
    return by_sender if len(by_sender) == 1 else []


def _distinct(cases: Iterable[BookingCase]) -> list[BookingCase]:
    seen: dict[int, BookingCase] = {}
    for case in cases:
        seen.setdefault(case.id, case)
    return [seen[k] for k in sorted(seen)]


def match_case(session: Session, message: InboundMessage) -> BookingCase | None:
    """The first case a reply belongs to, or None."""
    cases = match_cases(session, message)
    return cases[0] if cases else None


def _local_to_utc(day: str, clock: str | None, timezone: str | None) -> datetime:
    tz = ZoneInfo(timezone or "America/New_York")
    parsed = datetime.strptime(f"{day} {clock or '00:00'}", "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=tz).astimezone(UTC)


def _new_reading(
    session: Session, case: BookingCase, outcome: str, *, about_slot: bool = True
) -> None:
    """A new reply supersedes what earlier ones left open about the same thing.

    A reply about the slot replaces the slot's state: the case is pending again and an
    unreviewed confirmation goes, its slot no longer the vendor's last word. A question only
    replaces an earlier question; the slot stays where it was.
    """
    superseded = SLOT_REPLY_SUPERSEDES if about_slot else QUESTION_SUPERSEDES
    if about_slot and open_exceptions(case, ExceptionType.CONFIRMATION_REVIEW):
        case.confirmed_local = None
        case.confirmed_start_utc = None
        case.confirmed_end_utc = None
    resolve(session, case, superseded, resolution=f"superseded by a later reply ({outcome})")
    if about_slot or case.status == CaseStatus.UNSCHEDULED.value:
        case.status = CaseStatus.PENDING.value
        case.reason = None


def apply_reply(
    session: Session,
    case: BookingCase,
    result: ReplyClassification,
    issues: list[ClassificationIssue],
    *,
    actor: str = "agent",
    reply_sent_at: datetime | None = None,
) -> str:
    """Move the case according to the classified reply; return the event recorded.

    The case is pending afterwards (declined when the vendor cannot book), and what a person
    has to look at is raised: the confirmation to approve, the offer, the question, the
    decline, a "confirmation" of a slot already past. A reply that says nothing about the
    request leaves the case as it was.
    """
    if case.status in DECIDED_STATUSES:
        _event(session, case, "reply_ignored", actor=actor, reason=f"case is {case.status}")
        return "reply_ignored"
    requested_day, _, requested_clock = (case.requested_local or "").partition(" ")
    if result.status == ReplyStatus.CONFIRMED and (result.pickup_date or requested_day):
        day = result.pickup_date or requested_day
        clock = result.pickup_time or requested_clock or None
        start = _local_to_utc(day, clock, case.vendor_timezone)
        end = (
            _local_to_utc(day, result.pickup_time_end, case.vendor_timezone)
            if result.pickup_time_end
            else start
        )
        written_at = as_utc(reply_sent_at)
        if written_at is not None and start < written_at - STALE_CONFIRMATION_GRACE:
            local = f"{day} {clock or ''}".strip()
            _new_reading(session, case, "stale_confirmation")
            flag(
                session,
                case,
                ExceptionType.STALE_CONFIRMATION,
                f"vendor 'confirmed' {local}, already past when they wrote; "
                "read it as a work-in or late-arrival note",
                local=local,
                reply_sent_at=written_at.isoformat(),
            )
            _event(
                session,
                case,
                "stale_confirmation",
                actor=actor,
                local=local,
                reply_sent_at=written_at.isoformat(),
                issues=[i.__dict__ for i in issues],
            )
            return "stale_confirmation"
        _new_reading(session, case, "vendor_confirmed")
        case.confirmed_local = f"{day} {clock or ''}".strip()
        case.confirmed_start_utc = start
        case.confirmed_end_utc = end
        case.pickup_number = result.pickup_number or case.pickup_number
        pickup = f", pickup# {case.pickup_number}" if case.pickup_number else ""
        flag(
            session,
            case,
            ExceptionType.CONFIRMATION_REVIEW,
            f"vendor confirmed {case.confirmed_local}{pickup}; approve to accept",
            local=case.confirmed_local,
            pickup_number=case.pickup_number,
            conditions=result.conditions,
        )
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        outside = outside_request(case, day, clock, date_only=bool(profile and profile.date_only))
        if outside:
            flag(
                session,
                case,
                ExceptionType.CONFIRMED_OUTSIDE_WINDOW,
                outside,
                requested=case.requested_local,
                confirmed=case.confirmed_local,
            )
        _event(
            session,
            case,
            "vendor_confirmed",
            actor=actor,
            local=case.confirmed_local,
            pickup_number=case.pickup_number,
            conditions=result.conditions,
            outside_window=outside,
            issues=[i.__dict__ for i in issues],
        )
        return "vendor_confirmed"
    if result.status == ReplyStatus.COUNTER_OFFER:
        _new_reading(session, case, "counter_offer")
        # Morgan Foods assigns the pickup number with the counter ("10/2 @ 9am pickup# 20463798").
        case.pickup_number = result.pickup_number or case.pickup_number
        offered = f"vendor offered {result.pickup_date or '?'} {result.pickup_time or ''}".strip()
        offered += f" to {result.pickup_time_end}" if result.pickup_time_end else ""
        flag(
            session,
            case,
            ExceptionType.PROPOSED_TIME_REVIEW,
            offered,
            date=result.pickup_date,
            time=result.pickup_time,
            time_end=result.pickup_time_end,
            pickup_number=result.pickup_number,
        )
        _event(session, case, "counter_offer", actor=actor, reason=offered)
        return "counter_offer"
    if result.status == ReplyStatus.QUESTION:
        _new_reading(session, case, "question", about_slot=False)
        asked = f"vendor asked: {result.question or 'see reply'}"[:255]
        flag(session, case, ExceptionType.FACILITY_QUESTION, asked, question=result.question)
        _event(session, case, "question", actor=actor, reason=asked)
        return "question"
    if result.status == ReplyStatus.DEFERRED:
        _new_reading(session, case, "deferred")
        case.reason = (
            f"vendor asked to check back on {result.pickup_date}"
            if result.pickup_date
            else "vendor asked to check back later"
        )
        _event(
            session,
            case,
            "deferred",
            actor=actor,
            reason=case.reason,
            check_back=result.pickup_date,
        )
        return "deferred"
    if result.status == ReplyStatus.REJECTED:
        _new_reading(session, case, "rejected_by_vendor")
        said = result.question or "; ".join(result.conditions) or "see reply"
        why = f"vendor cannot book: {said}"[:255]
        case.status = CaseStatus.DECLINED.value
        case.reason = why
        flag(
            session,
            case,
            ExceptionType.FACILITY_DECLINED,
            why,
            question=result.question,
            conditions=result.conditions,
        )
        _event(session, case, "rejected_by_vendor", actor=actor, reason=why)
        return "rejected_by_vendor"
    _event(session, case, "reply_unrelated", actor=actor, issues=[i.__dict__ for i in issues])
    return "reply_unrelated"


def ingest(  # noqa: PLR0912 - one branch per reply outcome
    session: Session,
    messages: list[InboundMessage],
    classifier: ReplyClassifier,
    *,
    internal_domains: list[str],
    responder: Responder | None = None,
    customer_desk: str | None = None,
) -> IngestStats:
    """Match inbound mail to cases, classify the replies, move the cases, draft answers.

    Mail from the customer's inbound desk is not a vendor reply: it is read only for a new
    delivery slot (date, time and DCT reference), which moves the pickup request.
    """
    stats = IngestStats()
    internal = {d.lower() for d in internal_domains}
    for message in messages:
        stats.messages += 1
        if message.from_domain in internal:
            outcome = link_outbound(session, message)
            if outcome == "own":
                stats.own_outbound += 1
            elif outcome == "linked":
                stats.linked_outbound += 1
            else:
                stats.skipped_internal += 1
            continue
        if _already_recorded(session, message):
            stats.duplicates += 1
            continue
        cases = match_cases(session, message)
        if not cases:
            stats.unmatched += 1
            continue
        if customer_desk and message.from_email == customer_desk.lower():
            moved = [_apply_customer_desk_message(session, c, message, responder) for c in cases]
            if any(moved):
                stats.delivery_updates += 1
            continue
        live: list[BookingCase] = []
        for case in cases:
            if case.status in DECIDED_STATUSES:
                _record_after_decision(session, case, message)
                stats.after_decision += 1
            else:
                live.append(case)
        if not live:
            continue
        _ingest_reply(
            session, message, cases=live, classifier=classifier, responder=responder, stats=stats
        )
        session.flush()
    return stats


def _ingest_reply(
    session: Session,
    message: InboundMessage,
    *,
    cases: list[BookingCase],
    classifier: ReplyClassifier,
    responder: Responder | None,
    stats: IngestStats,
) -> None:
    """Classify one vendor reply once and apply it to every live case it answers.

    A reply that answers PO lines separately is applied line by line; a case whose POs the
    reply never names is left where it was. Whatever happens, at most one message goes back
    for one reply: the conversation policy's answers first, else a single "Thank you!".
    """
    first = cases[0]
    context = ReplyContext(
        vendor_name=first.vendor_name or "",
        po_numbers=[str(p) for c in cases for p in c.po_numbers],
        requested_local=first.requested_local,
        reply_sent_at=message.sent_at,
        subject=message.subject,
        body=message.body,
        quoted=message.quoted,
        requests=[([str(p) for p in c.po_numbers], c.requested_local) for c in cases],
    )
    output = classifier.classify(context)
    result, issues = validate_classification(output.result, message.body, message.quoted)
    stats.classified += 1
    answered: list[tuple[BookingCase, BookingMessage, ReplyClassification]] = []
    for case in cases:
        reading = for_case(result, [str(p) for p in case.po_numbers])
        if reading is None:
            case.messages.append(
                _inbound_record(
                    case,
                    message,
                    kind="reply",
                    classification={"skipped": "reply names other POs", "model": output.model},
                )
            )
            session.flush()
            _event(session, case, "reply_not_about_this_po", subject=message.subject)
            stats.not_about_case += 1
            continue
        inbound = _inbound_record(
            case,
            message,
            kind="reply",
            classification={
                **reading.model_dump(mode="json"),
                "line_items": len(result.items),
                "issues": [i.__dict__ for i in issues],
                "model": output.model,
            },
        )
        case.messages.append(inbound)
        session.flush()
        if case.thread_id is None and message.thread_id:
            case.thread_id = message.thread_id
        action = apply_reply(session, case, reading, issues, reply_sent_at=message.sent_at)
        if action == "vendor_confirmed":
            stats.proposed += 1
        elif action in ("counter_offer", "question", "rejected_by_vendor", "stale_confirmation"):
            stats.needs_human += 1
        elif action == "deferred":
            stats.deferred += 1
        else:
            stats.unrelated += 1
        answered.append((case, inbound, reading))
    if responder is not None:
        _answer_once(session, responder, answered, stats)


def _answer_once(
    session: Session,
    responder: Responder,
    answered: list[tuple[BookingCase, BookingMessage, ReplyClassification]],
    stats: IngestStats,
) -> None:
    """At most one message back for one reply: policy answers first, else a single thanks."""
    drafted_any = False
    for case, inbound, reading in answered:
        if reading.status in (
            ReplyStatus.COUNTER_OFFER,
            ReplyStatus.QUESTION,
            ReplyStatus.REJECTED,
        ):
            plan, drafted = responder.respond(session, case, inbound, reading)
            if drafted is not None:
                stats.responded += 1
                drafted_any = True
            log.info(
                "booking.responded", case=case.id, intent=plan.intent.value, reason=plan.reason
            )
    if drafted_any:
        return
    for case, inbound, reading in answered:
        if reading.status == ReplyStatus.CONFIRMED and open_exceptions(
            case, ExceptionType.CONFIRMATION_REVIEW
        ):
            if responder.acknowledge(session, case, inbound) is not None:
                stats.responded += 1
            return


def _inbound_record(
    case: BookingCase, message: InboundMessage, *, kind: str, classification: dict[str, Any]
) -> BookingMessage:
    """A stored copy of an inbound message, threading headers included."""
    return BookingMessage(
        case_id=case.id,
        direction="in",
        kind=kind,
        from_addr=message.from_addr,
        to_addr=message.to_addr,
        cc_addr=message.cc_addr,
        subject=message.subject,
        body=message.body,
        message_id=message.message_id,
        thread_id=message.thread_id,
        rfc_message_id=message.rfc_message_id,
        in_reply_to=message.in_reply_to,
        references_header=message.references,
        sent_at=message.sent_at,
        classification=classification,
    )


def _already_recorded(session: Session, message: InboundMessage) -> bool:
    """Seen before, by the source's id or by the RFC Message-ID (two sources, one email)."""
    if session.scalar(
        select(BookingMessage.id).where(BookingMessage.message_id == message.message_id)
    ):
        return True
    if message.rfc_message_id:
        return (
            session.scalar(
                select(BookingMessage.id).where(
                    BookingMessage.direction == "in",
                    func.lower(BookingMessage.rfc_message_id) == message.rfc_message_id.lower(),
                )
            )
            is not None
        )
    return False


def link_outbound(session: Session, message: InboundMessage) -> str:
    """Recognise Circle's own mail in the archive: ``own``, ``linked`` or ``""``.

    ``own``: a message the agent sent, seen again through the archive; its Gmail id and thread
    from that mailbox are recorded if missing. ``linked``: a person sent a drafted request
    themselves; the case is marked sent with the message's thread and Message-ID, so the reply
    can be matched without anyone typing ids in.
    """
    if message.rfc_message_id:
        own = session.scalar(
            select(BookingMessage).where(
                BookingMessage.direction == "out",
                func.lower(BookingMessage.rfc_message_id) == message.rfc_message_id.lower(),
            )
        )
        if own is not None:
            own.message_id = own.message_id or message.message_id
            own.thread_id = own.thread_id or message.thread_id
            if not own.case.thread_id and message.thread_id:
                own.case.thread_id = message.thread_id
            return "own"
    recipients = participants(message.to_addr, message.cc_addr)
    if not recipients:
        return ""
    candidates = list(
        session.scalars(
            select(BookingCase).where(
                BookingCase.status.in_((CaseStatus.UNSCHEDULED.value, CaseStatus.PENDING.value)),
                func.lower(BookingCase.contact_email).in_(sorted(recipients)),
            )
        )
    )
    subject = normalize_subject(message.subject).lower()
    numbers = set(PO_RE.findall(f"{message.subject} {message.body}"))
    linked = 0
    for case in candidates:
        if case.status == CaseStatus.PENDING.value and case.thread_id:
            continue
        if case.status == CaseStatus.UNSCHEDULED.value and not has_request(case):
            continue  # only a drafted request is linked to a person's send
        same_subject = any(
            normalize_subject(m.subject).lower() == subject
            for m in case.messages
            if m.direction == "out" and m.subject
        )
        pos = {str(p) for p in case.po_numbers}
        if same_subject or (pos and pos <= numbers):
            mark_sent(
                session,
                case,
                by="archive",
                thread_id=message.thread_id,
                message_id=message.message_id,
                rfc_message_id=message.rfc_message_id,
                sent_at=message.sent_at,
            )
            linked += 1
    return "linked" if linked else ""


def _record_after_decision(session: Session, case: BookingCase, message: InboundMessage) -> None:
    """Keep a message that arrived after approval or closure without reading it as an answer."""
    case.messages.append(
        _inbound_record(
            case, message, kind="reply", classification={"skipped": f"case is {case.status}"}
        )
    )
    session.flush()
    _event(session, case, "reply_after_decision", subject=message.subject, status=case.status)


def _apply_customer_desk_message(
    session: Session, case: BookingCase, message: InboundMessage, responder: Responder | None
) -> bool:
    """Record a customer-desk message; on a new delivery slot, move the pickup request."""
    slot = parse_delivery_slot(
        message.full_text, year=message.sent_at.year, timezone=case.vendor_timezone
    )
    inbound = _inbound_record(
        case,
        message,
        kind="customer_desk",
        classification=(
            {"delivery_ref": slot[1], "delivery_at_utc": slot[0].isoformat()} if slot else {}
        ),
    )
    case.messages.append(inbound)
    session.flush()
    if slot is None:
        _event(session, case, "customer_desk_message", subject=message.subject)
        return False
    start, ref = slot
    previous_ref, previous_at = case.delivery_ref, as_utc(case.delivery_at_utc)
    case.delivery_ref = ref
    case.delivery_at_utc = start
    _event(
        session,
        case,
        "delivery_updated",
        previous_ref=previous_ref,
        previous_at=previous_at.isoformat() if previous_at else None,
        delivery_ref=ref,
        delivery_at=start.isoformat(),
    )
    if case.status in DECIDED_STATUSES:
        return True
    settings = responder.settings if responder is not None else None
    if settings is None:
        flag(
            session,
            case,
            ExceptionType.DELIVERY_MOVED,
            f"delivery moved to {ref}; re-request the pickup",
            delivery_ref=ref,
            delivery_at=start.isoformat(),
        )
        return True
    new_request = requested_local(
        tendered_pickup_utc=None,
        delivery_at_utc=start,
        timezone=case.vendor_timezone,
        miles=case.miles,
        settings=settings,
    )
    if new_request is None:
        return True
    now = responder.now if responder is not None and responder.now else datetime.now(tz=UTC)
    floored, why, feasible = floor_requested(case, new_request, settings, now=now)
    new_request = floored or new_request
    if why:
        _event(session, case, "po_date_floor", reason=why, feasible=feasible)
    if not feasible:
        case.requested_local = new_request
        flag(
            session,
            case,
            ExceptionType.SLOT_UNWORKABLE,
            why or f"delivery moved to {ref}; the pickup cannot be re-requested",
            requested=new_request,
            delivery_ref=ref,
        )
        return True
    if case.status != CaseStatus.UNSCHEDULED.value and responder is not None:
        reschedule_case(
            session,
            case,
            responder.mailer,
            settings,
            requested_local=new_request,
            by="agent",
            note="Due to the receiver's availability, we will need to move this pickup.",
        )
    else:
        # Nothing has gone out yet: the next request simply asks for the new day. A missing
        # desk stays open; only what the old delivery made unworkable is cleared.
        case.requested_local = new_request
        case.reason = None
        resolve(
            session,
            case,
            [
                ExceptionType.SLOT_UNWORKABLE,
                ExceptionType.DELIVERY_MOVED,
                ExceptionType.PICKUP_EXPIRED,
            ],
            resolution=f"delivery moved to {ref}; pickup request now {new_request}",
        )
    return True


def draft_batch(
    session: Session,
    cases: list[BookingCase],
    mailer: Mailer | Sender,
    settings: Settings,
    *,
    by: str = "agent",
    now: datetime | None = None,
) -> list[BookingMessage]:
    """Draft (or send) new cases, one email per vendor desk, the way the pod batches requests."""
    now = now or datetime.now(tz=UTC)
    groups: dict[str, list[BookingCase]] = {}
    for case in cases:
        groups.setdefault((case.contact_email or "").lower(), []).append(case)
    messages: list[BookingMessage] = []
    repo = Repository(session)
    for desk, group in groups.items():
        if len(group) == 1 or not desk:
            for case in group:
                messages.append(draft_case(session, case, mailer, settings, by=by, now=now))
            continue
        profile = vendor_profile(repo, group[0].facility_key) if group[0].facility_key else None
        for case in group:
            _check_draftable(case, settings, now=now, profile=profile)
        group.sort(key=lambda c: c.requested_local or "")
        date_only = bool(profile and profile.date_only)
        first = compose_request(group[0], settings, profile)
        ask = first.body.split("\n")[2]
        lines = ["Hello,", "", ask, ""]
        for case in group:
            lines.extend(
                request_lines(case, date_only=date_only, extra=extra_references(case, profile))
            )
        lines.extend(["", "Thank you!", "", settings.booking_signature])
        pos = [str(p) for c in group for p in c.po_numbers]
        subject = f"Pick Up Appointments: {' & '.join(pos)}" if pos else "Pick Up Appointments"
        draft = OutboundDraft(
            to_addr=desk,
            cc_addr=", ".join(settings.booking_cc),
            subject=subject,
            body="\n".join(lines),
        )
        if is_sender(mailer):
            trusted = profile.contact_email if profile and profile.can_email else None
            for case in group:
                check_send_gate(session, case, draft, settings, trusted_desk=trusted)
        result = deliver(mailer, draft)
        for case in group:
            message = BookingMessage(
                case_id=case.id,
                direction="out",
                kind="request",
                to_addr=draft.to_addr,
                cc_addr=draft.cc_addr,
                subject=subject,
                body=draft.body,
            )
            case.messages.append(message)
            session.flush()
            record_delivery(session, case, message, draft, result, actor=by)
            case.status = CaseStatus.PENDING.value if result.sent else CaseStatus.UNSCHEDULED.value
            case.reason = None
            session.flush()
            _event(
                session,
                case,
                "drafted",
                draft_ref=result.ref,
                to=desk,
                subject=subject,
                batched_with=[c.id for c in group if c.id != case.id],
            )
            messages.append(message)
    return messages


# ------------------------------------------------------------------ decisions


def appointment_payload(case: BookingCase) -> dict[str, Any]:
    """What would be written to Transport Pro for the proposed slot."""
    if case.confirmed_start_utc is None:
        msg = f"case {case.id} has no confirmed slot"
        raise ValueError(msg)
    start = as_utc(case.confirmed_start_utc)
    assert start is not None
    end = as_utc(case.confirmed_end_utc) or start
    return {
        "load_id": case.load_id,
        "waypoint_index": str(case.waypoint_index),
        "start_utc": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_utc": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": "Confirmed",
    }


def approve(
    session: Session, case: BookingCase, *, by: str, client: TransportProClient | None = None
) -> tuple[dict[str, Any], bool]:
    """A person approves the vendor's confirmation; write it only when the client allows writes.

    The case becomes scheduled. Its confirmation review is resolved, with what approving settles
    too: a confirmation outside the window asked for, and the timers (the pickup is booked).
    Anything else still open (a later question, say) stays open.
    """
    if case.status != CaseStatus.PENDING.value or not open_exceptions(
        case, ExceptionType.CONFIRMATION_REVIEW
    ):
        msg = f"case {case.id} is {case.status} with no confirmation to review; nothing to approve"
        raise ValueError(msg)
    payload = appointment_payload(case)
    written = False
    if client is not None and client.allow_writes:
        client.set_appointment(**payload)
        written = True
    case.status = CaseStatus.SCHEDULED.value
    case.reason = None
    resolve(
        session,
        case,
        [ExceptionType.CONFIRMATION_REVIEW, ExceptionType.CONFIRMED_OUTSIDE_WINDOW, *TIMER_KINDS],
        resolution="approved",
        by=by,
    )
    _event(session, case, "approved", actor=by, payload=payload, written_to_tpro=written)
    # The vendor confirmed by email: the desk the request went to worked.
    remember_booking(session, case, method="email", desk=case.contact_email, by=by)
    return payload, written


def mark_booked(
    session: Session,
    case: BookingCase,
    *,
    by: str,
    via: str,
    local: str | None = None,
    pickup_number: str | None = None,
    note: str | None = None,
    desk: str | None = None,
) -> Learned:
    """A person booked the pickup outside the agent: by phone, on a portal, by their own email.

    The case becomes scheduled (with the slot, when given, as ``YYYY-MM-DD`` or
    ``YYYY-MM-DD HH:MM`` vendor-local) and everything open on it is resolved as booked. The way
    it was booked is remembered for the facility: ``desk`` is the email address, phone number or
    portal address used (for an email booking, the desk on file when none is given). What the
    vendor profile learned from it is returned.
    """
    if case.status == CaseStatus.CANCELED.value:
        msg = f"case {case.id} is canceled; nothing to mark as booked"
        raise ValueError(msg)
    if local:
        day, _, clock = local.partition(" ")
        start = _local_to_utc(day, clock or None, case.vendor_timezone)
        case.confirmed_local = local
        case.confirmed_start_utc = start
        case.confirmed_end_utc = start
    case.pickup_number = pickup_number or case.pickup_number
    case.status = CaseStatus.SCHEDULED.value
    case.reason = (f"booked by {via}" + (f": {note}" if note else ""))[:255]
    resolve_all(session, case, resolution=f"booked by {via}", by=by)
    _event(
        session,
        case,
        "marked_booked",
        actor=by,
        via=via,
        local=local,
        pickup_number=case.pickup_number,
        note=note,
        desk=desk,
    )
    method = VIA_METHODS.get(via.strip().lower())
    used = desk or (case.contact_email if method == "email" else None)
    return remember_booking(session, case, method=method, desk=used, by=by)


def close_case(session: Session, case: BookingCase, *, by: str, reason: str) -> None:
    """Cancel a case that is no longer needed; everything open on it is resolved with it.

    A pickup booked some other way is not canceled: use :func:`mark_booked`.
    """
    case.status = CaseStatus.CANCELED.value
    case.reason = reason[:255]
    resolve_all(session, case, resolution=f"case canceled: {reason}", by=by)
    _event(session, case, "closed", actor=by, reason=reason)


def summary_line(case: BookingCase) -> str:
    """One line per case for ``booking list``: status, what is open, vendor, PO, slot."""
    kinds = open_kinds(case)
    flags = " ".join(f"!{k}" for k in kinds)
    if not flags and case.status == CaseStatus.UNSCHEDULED.value and has_request(case):
        flags = "draft ready"
    note = case.open_exceptions[0].description if kinds else case.reason
    return (
        f"#{case.id:<4} load {case.load_id:<9} {case.status:<11} {flags:<22} "
        f"{(case.vendor_name or '?')[:32]:<32} "
        f"PO {', '.join(str(p) for p in case.po_numbers) or '-'} "
        f"req {case.requested_local or '?'}" + (f"  ({note})" if note else "")
    )


def describe(case: BookingCase) -> str:
    """One-screen summary for the CLI."""
    lines = [
        f"case #{case.id}  load {case.load_id}  status {case.status}"
        + (f"  ({case.reason})" if case.reason else ""),
    ]
    for exc in case.open_exceptions:
        lines.append(
            f"  OPEN      {exc.kind}: {exc.description}  "
            f"[{exc.raised_by} {exc.raised_at:%m/%d %H:%M}]"
        )
    lines += [
        f"  vendor    {case.vendor_name or '?'} {case.vendor_city or ''}  [{case.facility_key}]",
        f"  desk      {case.contact_email or 'none'}  ({case.booking_method or 'unknown method'})",
        f"  PO        {', '.join(str(p) for p in case.po_numbers) or 'none'}",
        *(
            f"  ref       {REFERENCE_LABELS.get(k, k)} {v}"
            for k, v in sorted((case.reference_numbers or {}).items())
            if v
        ),
        f"  requested {case.requested_local or '?'} local",
        f"  delivery  {case.delivery_site or '?'}"
        + (f" {case.delivery_ref}" if case.delivery_ref else "")
        + (f" at {case.delivery_at_utc:%Y-%m-%d %H:%MZ}" if case.delivery_at_utc else ""),
    ]
    if case.confirmed_local:
        lines.append(
            f"  confirmed {case.confirmed_local} local"
            + (f", pickup# {case.pickup_number}" if case.pickup_number else "")
        )
    if case.reschedule_count:
        lines.append(f"  rescheduled {case.reschedule_count} time(s)")
    for m in case.messages:
        head = f"  {'->' if m.direction == 'out' else '<-'} {m.kind:<8} {m.subject or ''}"
        if m.direction == "in" and m.classification:
            c = m.classification
            head += (
                f"  => {c.get('status')} {c.get('pickup_date') or ''} {c.get('pickup_time') or ''}"
            )
        lines.append(head)
    for exc in case.exceptions:
        if exc.resolved_at is not None:
            lines.append(
                f"  resolved  {exc.kind}: {exc.resolution}  "
                f"[{exc.resolved_by} {exc.resolved_at:%m/%d %H:%M}]"
            )
    return "\n".join(lines)
