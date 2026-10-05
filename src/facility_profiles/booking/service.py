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
from dataclasses import dataclass, field, replace
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
from facility_profiles.booking.links import (
    create_offer,
    html_body,
    link_lines,
    links_enabled,
    offer_url,
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
    SlotOffer,
)
from facility_profiles.booking.outbox import (
    check_send_gate,
    dispatch,
    is_sender,
    record_delivery,
)
from facility_profiles.booking.recommend import (
    Recommendation,
    facility_history,
    infeasible_note,
    recommend_time,
)
from facility_profiles.booking.references import (
    ReferenceSource,
    active,
    case_numbers,
    record_load_numbers,
    record_reference,
)
from facility_profiles.booking.respond import (
    Responder,
    local_dt,
)
from facility_profiles.booking.rules import (
    REFERENCE_LABELS,
    REFERENCE_NAMES,
    VendorProfile,
    check_desk_rules,
    missing_references,
    slot_is_stale,
    too_early,
    vendor_profile,
)
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.templates import (
    BUILT_IN,
    Template,
    TemplateKind,
    pick,
    render,
    request_values,
    reschedule_values,
    with_links,
)
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
from facility_profiles.booking.writeback import appointment_payload, queue_write
from facility_profiles.config import Settings
from facility_profiles.customers import Customer, built_in_customers, customer_of, customers
from facility_profiles.domain.resolution import FacilityResolver
from facility_profiles.domain.schema import ReferenceType
from facility_profiles.logging import get_logger
from facility_profiles.mailarchive.filters import normalize_subject, participants
from facility_profiles.pipeline.harvest import iter_terminal_loads, stop_identity
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc
from facility_profiles.tpro.client import TransportProClient
from facility_profiles.tpro.models import Load, Waypoint

log = get_logger(__name__)

PO_RE = re.compile(r"\b\d{9,15}\b")
# A PO field can list several, the way the pod writes them: "115806102630 & 115806102631".
PO_LIST_RE = re.compile(r"^\d+(?:\s*[&,;/+]\s*\d+)*$")
REFRESH_ACTOR = "agent"
# Transport Pro's load search leaves canceled loads out unless asked for this status (checked
# live on 2026-10-05: pod 1089, Lidl inbound), so the scan asks for them separately.
CANCELED_STATUS = "Canceled"
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
    skipped_by_rule: int = 0  # a customer rule says the agent does not book these
    canceled_loads: int = 0  # canceled in Transport Pro before any case was opened
    refreshed: int = 0  # existing cases that took a change from Transport Pro
    cases_canceled: int = 0  # load canceled before anything was written: case canceled
    load_canceled: int = 0  # load canceled after a request or a booking: raised for a person
    booked_in_tpro: int = 0  # a pickup number or a confirmed stop appeared in Transport Pro
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
    """Customer order numbers on the load (PO and reference number).

    A field may list several ("115806102630 & 115806102631"); a value with anything but digits
    and those separators is not a PO.
    """
    ref = load.reference or {}
    seen: list[str] = []
    for name in ("poNumber", "referenceNumber"):
        value = str(ref.get(name) or "").strip()
        if not PO_LIST_RE.match(value):
            continue
        for part in re.findall(r"\d+", value):
            if part not in seen:
                seen.append(part)
    return seen


def load_canceled(load: Load) -> bool:
    """True when Transport Pro shows the load canceled (its load status "Canceled")."""
    status = (load.status.load_status if load.status else None) or ""
    return status.strip().lower().startswith("cancel")


def _iso_utc(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.astimezone(UTC).isoformat() if aware else None


def tpro_view(load: Load, wp: Waypoint, customer: Customer) -> dict[str, Any]:
    """What Transport Pro says about the load and its pickup now, as a case keeps it."""
    appt = wp.appointment_time
    drop = delivery_waypoint(load)
    delivery_at = drop.appointment_time.open_at if drop and drop.appointment_time else None
    return {
        "load_status": load.status.load_status if load.status else None,
        "appointment_status": appt.appointment_status if appt else None,
        "tender": _iso_utc(appt.open_at if appt else None),
        "pickup_number": str((load.reference or {}).get("pickupNumber") or "").strip() or None,
        "delivery_ref": customer.find_delivery_ref(drop.notes) if drop else None,
        "delivery_at": _iso_utc(delivery_at),
        "po_numbers": po_numbers(load),
    }


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


def parse_delivery_slot(
    text: str, *, year: int, timezone: str | None, customer: Customer
) -> tuple[datetime, str] | None:
    """Read a delivery slot and the customer's delivery reference out of a message.

    Returns (UTC start, reference). Lidl's inbound desk writes "8/20 7AM - GRM_200826926",
    "10/6 730AM - PYE_061026919" or "9/30 at 1100" with the reference on the next line; the pod
    writes its own bookings reference first, "FRG_200526615 05/20 @ 1100". A customer without a
    delivery reference format has no slot to read.
    """
    match = next((m for p in customer.slot_patterns() if (m := p.search(text))), None)
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


def _names(case: BookingCase, settings: Settings) -> list[str]:
    """What a customer-scoped template may be saved under for this case."""
    return customer_of(case, settings).template_matches(case.customer_name)


def _event(
    session: Session, case: BookingCase, action: str, actor: str = "agent", **detail: Any
) -> None:
    session.add(BookingEvent(case_id=case.id, action=action, actor=actor, detail=detail))


# ------------------------------------------------------------------ scan


def scan(  # noqa: PLR0912 - one branch per kind of load
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
    billed_to: list[int | None] = list(customer_ids or settings.pilot_customer_ids) or [None]
    stats = ScanStats()
    resolver = FacilityResolver([])
    known = customers(settings)
    with session_scope(sessions) as session:
        repo = Repository(session)
        seen_loads: set[int] = set()
        for load in iter_terminal_loads(
            client, terminal_ids=terminals, customer_ids=billed_to, start=start, end=end
        ):
            stats.loads += 1
            seen_loads.add(load.id)
            found = pickup_waypoint(load)
            if found is None:
                stats.no_pickup_stop += 1
                continue
            index, wp = found
            customer_id = load.billing_info.customer_id if load.billing_info else None
            customer = known.for_customer(customer_id, load.customer_name)
            existing = _case_for_stop(session, load.id, index)
            if existing is not None:
                stats.existing += 1
                refresh_case(
                    session,
                    existing,
                    load,
                    wp,
                    customer=customer,
                    settings=settings,
                    now=now,
                    stats=stats,
                )
                continue
            if load_canceled(load):
                stats.canceled_loads += 1
                continue
            appt = wp.appointment_time
            if appt and (appt.appointment_status or "").lower() == "confirmed":
                stats.already_confirmed += 1
                continue
            key = resolver.resolve(stop_identity(wp)).key
            profile = vendor_profile(repo, key)
            drop = delivery_waypoint(load)
            delivery_at = drop.appointment_time.open_at if drop and drop.appointment_time else None
            delivery_ref = customer.find_delivery_ref(drop.notes) if drop else None
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
                customer_id=customer_id,
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
                delivery_ref=delivery_ref,
                delivery_at_utc=delivery_at,
                tendered_pickup_utc=appt.open_at if appt else None,
                miles=int(miles) if isinstance(miles, int | float) else None,
                pickup_number=pickup_no or None,
                status=status,
                reason=reason,
                tpro_seen=tpro_view(load, wp, customer),
            )
            case.requested_local = requested_local(
                tendered_pickup_utc=case.tendered_pickup_utc,
                delivery_at_utc=case.delivery_at_utc,
                timezone=case.vendor_timezone,
                miles=case.miles,
                settings=settings,
            )
            if customer.rule_for(case).do == "skip":
                stats.skipped_by_rule += 1
                continue
            session.add(case)
            session.flush()
            record_load_numbers(session, case, at=now)
            if blocker is not None:
                flag(session, case, blocker[0], blocker[1], method=profile.booking_method)
            _event(
                session,
                case,
                "scanned",
                status=case.status,
                reason=case.reason or (blocker[1] if blocker else None),
            )
            if case.status == CaseStatus.UNSCHEDULED.value:
                from_tender = customer.rule_for(case).pickup_from != "delivery"
                plan_request(
                    session, case, settings, now=now, profile=profile, use_tender=from_tender
                )
            _check_requested_slot(session, case, settings, now=now, profile=profile)
            stats.created += 1
            stats.case_ids.append(case.id)
            if pickup_no:
                stats.already_booked += 1
            elif blocker is not None:
                stats.needs_profile += 1
        _canceled_pass(
            client,
            session,
            settings,
            terminals=terminals,
            billed_to=billed_to,
            start=start,
            end=end,
            skip=seen_loads,
            now=now,
            stats=stats,
        )
    return stats


def _case_for_stop(session: Session, load_id: int, index: int) -> BookingCase | None:
    """The case already open for this pickup stop, if any."""
    return session.scalar(
        select(BookingCase).where(
            BookingCase.load_id == load_id, BookingCase.waypoint_index == index
        )
    )


def _canceled_pass(
    client: TransportProClient,
    session: Session,
    settings: Settings,
    *,
    terminals: list[int | None],
    billed_to: list[int | None],
    start: date,
    end: date,
    skip: set[int],
    now: datetime,
    stats: ScanStats,
) -> None:
    """The canceled loads in the window: their cases are canceled or raised, nothing is opened.

    Transport Pro only returns canceled loads when asked for them (:data:`CANCELED_STATUS`).
    """
    known = customers(settings)
    for load in iter_terminal_loads(
        client,
        terminal_ids=terminals,
        customer_ids=billed_to,
        start=start,
        end=end,
        extra_filters={"load_status": CANCELED_STATUS},
    ):
        if load.id in skip or not load_canceled(load):
            continue
        found = pickup_waypoint(load)
        existing = _case_for_stop(session, load.id, found[0]) if found else None
        if found is None or existing is None:
            stats.canceled_loads += 1
            continue
        customer_id = load.billing_info.customer_id if load.billing_info else None
        refresh_case(
            session,
            existing,
            load,
            found[1],
            customer=known.for_customer(customer_id, load.customer_name),
            settings=settings,
            now=now,
            stats=stats,
        )


# ------------------------------------------------------------------ refresh


@dataclass
class _Seen:
    """Transport Pro now (``view``) against the last scan (``before``, None for an older case)."""

    view: dict[str, Any]
    before: dict[str, Any] | None

    def changed(self, key: str, on_case: Any) -> bool:
        """True when Transport Pro has a new value for ``key`` to put on the case.

        Something cleared in Transport Pro is not taken off the case. An older case, from before
        the scans kept what they saw, takes only what it lacks.
        """
        value = self.view[key]
        if value in (None, []):
            return False
        if self.before is None:
            return on_case in (None, [], "")
        return bool(value != self.before.get(key))


def refresh_case(
    session: Session,
    case: BookingCase,
    load: Load,
    wp: Waypoint,
    *,
    customer: Customer,
    settings: Settings,
    now: datetime,
    stats: ScanStats | None = None,
) -> list[str]:
    """Bring a case up to date with its load in Transport Pro; return what changed.

    Only what changed in Transport Pro since the last scan is taken (``tpro_seen``), so a slot a
    person or the customer's desk gave the case stands. A rescan:

    - load canceled: a case nothing was written for is canceled; one with a request written or
      a booking made raises ``load_canceled`` (once) for a person to tell the vendor;
    - a vendor pickup number, or the stop confirmed, in Transport Pro: the pickup was booked
      there; the case is scheduled and nothing is queued to write back (it is there already);
    - a new or moved delivery slot (the delivery reference in the delivery stop's notes, the
      stop's time): kept on the case. Once a request is out, a moved slot raises
      ``delivery_moved``;
    - a new tender time, and new POs while no request is written: kept on the case. Before any
      request, the pickup time is chosen again from what changed, and the desk's rules rechecked.
    """
    stats = stats if stats is not None else ScanStats()
    if case.status == CaseStatus.CANCELED.value:
        return []
    seen = _Seen(tpro_view(load, wp, customer), case.tpro_seen)
    if load_canceled(load):
        done = _refresh_canceled(session, case, seen, now=now, stats=stats)
    else:
        booked = _refresh_booking(session, case, wp, seen, now=now, stats=stats)
        delivery = _refresh_delivery(session, case, seen, now=now)
        request = _refresh_request(
            session, case, seen, customer, settings, now=now, replan=bool(delivery)
        )
        done = [*booked, *delivery, *request]
    case.tpro_seen = seen.view
    if done:
        stats.refreshed += 1
    return done


def _refresh_canceled(
    session: Session, case: BookingCase, seen: _Seen, *, now: datetime, stats: ScanStats
) -> list[str]:
    """The load was canceled: cancel a case nothing went out for, else tell a person once."""
    if case.status == CaseStatus.UNSCHEDULED.value and not has_request(case):
        close_case(session, case, by=REFRESH_ACTOR, reason="the load was canceled in Transport Pro")
        stats.cases_canceled += 1
        return ["canceled"]
    if any(e.kind == ExceptionType.LOAD_CANCELED.value for e in case.exceptions):
        return []  # raised before; a person's resolution stands
    what = (
        "delete the drafted request (it was not sent), then cancel the pickup"
        if case.status == CaseStatus.UNSCHEDULED.value
        else "tell the vendor the pickup is no longer needed, then cancel it"
    )
    flag(
        session,
        case,
        ExceptionType.LOAD_CANCELED,
        f"the load was canceled in Transport Pro; {what}",
        at=now,
        load_status=seen.view["load_status"],
    )
    stats.load_canceled += 1
    return ["load_canceled"]


def _refresh_booking(
    session: Session,
    case: BookingCase,
    wp: Waypoint,
    seen: _Seen,
    *,
    now: datetime,
    stats: ScanStats,
) -> list[str]:
    """A pickup number or a confirmed stop in Transport Pro: keep it, and book an open case."""
    done: list[str] = []
    if seen.changed("pickup_number", case.pickup_number):
        record_reference(
            session,
            case,
            ReferenceType.PICKUP_NUMBER.value,
            seen.view["pickup_number"],
            source=ReferenceSource.LOAD,
            at=now,
        )
        done.append("pickup_number")
    confirmed = (seen.view["appointment_status"] or "").lower() == "confirmed"
    before = (seen.before or {}).get("appointment_status") or ""
    newly_confirmed = confirmed and before.lower() != "confirmed"
    if case.status in OPEN_STATUSES and (done or newly_confirmed):
        _booked_in_tpro(session, case, wp, confirmed=confirmed)
        stats.booked_in_tpro += 1
        done.append("booked")
    return done


def _refresh_delivery(
    session: Session, case: BookingCase, seen: _Seen, *, now: datetime
) -> list[str]:
    """A new or moved delivery slot in Transport Pro; once a request is out, a move is raised."""
    new_ref = seen.changed("delivery_ref", case.delivery_ref)
    new_at = seen.changed("delivery_at", _iso_utc(case.delivery_at_utc))
    if not (new_ref or new_at):
        return []
    had_slot = bool(case.delivery_ref or case.delivery_at_utc)
    previous_ref, previous_at = case.delivery_ref, _iso_utc(case.delivery_at_utc)
    if new_ref:
        record_reference(
            session,
            case,
            ReferenceType.DELIVERY_NUMBER.value,
            seen.view["delivery_ref"],
            source=ReferenceSource.LOAD,
            at=now,
        )
    if new_at:
        case.delivery_at_utc = datetime.fromisoformat(seen.view["delivery_at"])
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    at = as_utc(case.delivery_at_utc)
    when = f"{at.astimezone(tz):%m/%d %H:%M}" if at else ""
    shown = " ".join(p for p in (case.delivery_ref, when) if p)
    _event(
        session,
        case,
        "delivery_from_tpro",
        previous_ref=previous_ref,
        previous_at=previous_at,
        delivery_ref=case.delivery_ref,
        delivery_at=_iso_utc(case.delivery_at_utc),
        reason=f"Transport Pro: delivery {shown}",
    )
    asked = case.status in (CaseStatus.PENDING.value, CaseStatus.DECLINED.value) or (
        case.status == CaseStatus.UNSCHEDULED.value and has_request(case)
    )
    if asked and had_slot:
        flag(
            session,
            case,
            ExceptionType.DELIVERY_MOVED,
            f"Transport Pro moved the delivery to {shown}; check the pickup still makes it",
            at=now,
            delivery_ref=case.delivery_ref,
            delivery_at=_iso_utc(case.delivery_at_utc),
        )
    return ["delivery"]


def _refresh_request(
    session: Session,
    case: BookingCase,
    seen: _Seen,
    customer: Customer,
    settings: Settings,
    *,
    now: datetime,
    replan: bool = False,
) -> list[str]:
    """A new tender time, and new POs before any request; then choose the time again.

    ``replan``: the delivery changed, so the time is chosen again even when nothing else did.
    """
    done: list[str] = []
    if seen.changed("tender", _iso_utc(case.tendered_pickup_utc)):
        previous = _iso_utc(case.tendered_pickup_utc)
        case.tendered_pickup_utc = datetime.fromisoformat(seen.view["tender"])
        tz = ZoneInfo(case.vendor_timezone or "America/New_York")
        tender = case.tendered_pickup_utc.astimezone(tz)
        _event(
            session,
            case,
            "tender_changed",
            previous=previous,
            tender=seen.view["tender"],
            reason=f"Transport Pro's tender is now {tender:%m/%d %H:%M}",
        )
        done.append("tender")
    waiting = case.status == CaseStatus.UNSCHEDULED.value and not has_request(case)
    pos = list(seen.view["po_numbers"])
    if (
        waiting
        and seen.changed("po_numbers", case.po_numbers)
        and pos != [str(p) for p in case.po_numbers]
    ):
        case.po_numbers = pos
        record_load_numbers(session, case, at=now)
        done.append("po_numbers")
    if waiting and (done or replan):
        resolve(
            session,
            case,
            [
                ExceptionType.LOAD_INFEASIBLE,
                ExceptionType.SLOT_UNWORKABLE,
                ExceptionType.DELIVERY_MOVED,
                ExceptionType.PICKUP_EXPIRED,
            ],
            resolution="Transport Pro changed the load; the pickup time was chosen again",
            by=REFRESH_ACTOR,
            at=now,
        )
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        use_tender = customer.rule_for(case).pickup_from != "delivery"
        plan_request(session, case, settings, now=now, profile=profile, use_tender=use_tender)
        _check_requested_slot(session, case, settings, now=now, profile=profile)
    return done


def _booked_in_tpro(session: Session, case: BookingCase, wp: Waypoint, *, confirmed: bool) -> None:
    """The pickup was booked in Transport Pro: schedule the case with the stop's time."""
    appt = wp.appointment_time
    start = appt.open_at if confirmed and appt else None
    if start is not None and appt is not None:
        local = start.astimezone(ZoneInfo(case.vendor_timezone or "America/New_York"))
        clock = f"{local:%H:%M}"
        case.confirmed_local = (
            f"{local:%Y-%m-%d}" if clock == "00:00" else f"{local:%Y-%m-%d} {clock}"
        )
        case.confirmed_start_utc = start
        case.confirmed_end_utc = appt.close_at or start
    how = (
        f"vendor pickup number {case.pickup_number}"
        if case.pickup_number
        else "the stop's appointment is confirmed there"
    )
    case.status = CaseStatus.SCHEDULED.value
    case.reason = f"booked in Transport Pro: {how}"[:255]
    resolve_all(session, case, resolution="booked in Transport Pro", by=REFRESH_ACTOR)
    _event(
        session,
        case,
        "booked_in_tpro",
        local=case.confirmed_local,
        pickup_number=case.pickup_number,
        reason=case.reason,
    )


def _recommend(
    session: Session,
    case: BookingCase,
    settings: Settings,
    profile: VendorProfile | None,
    *,
    now: datetime,
    use_tender: bool = True,
) -> Recommendation:
    """The time to ask for, from the case, the desk and the facility's history."""
    history = facility_history(session, case.facility_key)
    return recommend_time(case, settings, profile, now=now, history=history, use_tender=use_tender)


def _apply_recommendation(session: Session, case: BookingCase, rec: Recommendation) -> None:
    """Ask for the recommended time, say why when it moved, and raise a load that cannot make it.

    A PO-date floor is recorded as ``po_date_floor``, every other move as ``time_recommended``;
    a time that is simply the tender (or the default) records nothing.
    """
    if rec.local is None:
        return
    case.requested_local = rec.local
    for step in rec.moved:
        if step.rule == "floor":
            reason = step.note if rec.feasible else f"{step.note}; {rec.verdict}"
            _event(session, case, "po_date_floor", reason=reason, feasible=rec.feasible)
    others = [s for s in rec.moved if s.rule != "floor"]
    if others:
        _event(
            session,
            case,
            "time_recommended",
            local=rec.local,
            reason="; ".join(s.note for s in others),
            steps=[s.as_dict() for s in rec.steps],
        )
    if not rec.feasible:
        flag(
            session,
            case,
            ExceptionType.LOAD_INFEASIBLE,
            infeasible_note(rec),
            requested=rec.local,
            latest=rec.latest,
            steps=[s.as_dict() for s in rec.steps],
        )


def plan_request(
    session: Session,
    case: BookingCase,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
    use_tender: bool = True,
) -> Recommendation:
    """Choose the time an unscheduled case asks for, and raise it when no time makes the delivery.

    What the scan does for every new case; see ``booking/recommend.py`` for the rules. Without
    the tender (a rule that plans the pickup back from the delivery) the day is backed off the
    delivery slot.
    """
    if profile is None and case.facility_key:
        profile = vendor_profile(Repository(session), case.facility_key)
    rec = _recommend(session, case, settings, profile, now=now, use_tender=use_tender)
    _apply_recommendation(session, case, rec)
    return rec


def _check_requested_slot(
    session: Session,
    case: BookingCase,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
) -> None:
    """Apply the notice window and the desk's rules to a freshly scanned case.

    Only a case the agent could email is affected, and only once the time asked for can make the
    delivery (``_apply_recommendation``). A slot already inside the notice window or past the
    desk's cut-off, or a number the desk needs that the load lacks, is raised for a person before
    any email is written. A desk that does not book that far ahead yet makes the request wait.
    """
    if case.status != CaseStatus.UNSCHEDULED.value or case.open_exceptions:
        return
    check_desk_rules(session, case, settings, now=now, profile=profile)


# ------------------------------------------------------------------ compose and draft


def _offers(
    session: Session,
    cases: list[BookingCase],
    settings: Settings,
    profile: VendorProfile | None,
    *,
    now: datetime,
) -> dict[int, SlotOffer]:
    """The one-click times each case's request offers, by case id; none while links are off."""
    if not links_enabled(settings):
        return {}
    found: dict[int, SlotOffer] = {}
    for case in cases:
        offer = create_offer(session, case, settings, profile, now=now)
        if offer is not None:
            found[case.id] = offer
    return found


def _with_links(
    template: Template,
    cases: list[BookingCase],
    offers: dict[int, SlotOffer] | None,
    settings: Settings,
) -> tuple[Template, str, dict[str, SlotOffer]]:
    """The template to use, the {links} text, and each link's offer by URL."""
    if not offers:
        return template, "", {}
    urls = {case_id: offer_url(offer, settings) for case_id, offer in offers.items()}
    by_url = {urls[case_id]: offers[case_id] for case_id in urls}
    return with_links(template), link_lines(cases, urls), by_url


def compose_request(
    case: BookingCase,
    settings: Settings,
    profile: VendorProfile | None = None,
    template: Template | None = None,
    offers: dict[int, SlotOffer] | None = None,
) -> OutboundDraft:
    """The request email: the built-in template is the pod's own wording, word for word.

    With ``offers`` (click-to-confirm on) the email also carries the links to the times offered,
    and an HTML part where they are buttons.
    """
    chosen, links, by_url = _with_links(
        template or BUILT_IN[TemplateKind.REQUEST], [case], offers, settings
    )
    subject, body = render(chosen, request_values([case], settings, profile, links=links))
    customer = customer_of(case, settings)
    return OutboundDraft(
        to_addr=case.contact_email or "",
        cc_addr=customer.cc_header,
        subject=subject or "",
        body=body,
        from_addr=customer.sender,
        html=html_body(body, by_url) if by_url else None,
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
    now = now or datetime.now(tz=UTC)
    _check_draftable(case, settings, now=now, profile=profile)
    template = pick(
        session, TemplateKind.REQUEST, desk=case.contact_email, customer=_names(case, settings)
    )
    draft = compose_request(case, settings, profile, template)
    if is_sender(mailer):
        trusted = profile.contact_email if profile and profile.can_email else None
        check_send_gate(session, case, draft, settings, trusted_desk=trusted)
    offers = _offers(session, [case], settings, profile, now=now)
    if offers:
        draft = compose_request(case, settings, profile, template, offers)
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
    for offer in offers.values():
        offer.message_id = message.id
    result = dispatch(session, case, message, mailer, draft, actor=by)
    case.status = CaseStatus.PENDING.value if result.sent else CaseStatus.UNSCHEDULED.value
    case.reason = None
    session.flush()
    _event(
        session,
        case,
        "drafted",
        actor=by,
        draft_ref=result.ref,
        to=draft.to_addr,
        subject=draft.subject,
        template=template.source,
        offered=offers[case.id].slots if offers else None,
    )
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
    previous = next((r.value for r in active(case, kind)), None)
    record_reference(session, case, kind, value, source=ReferenceSource.PERSON, by=by)
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
    now: datetime | None = None,
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
    template = pick(
        session, TemplateKind.RESCHEDULE, desk=case.contact_email, customer=_names(case, settings)
    )
    _, body = render(
        template, reschedule_values(case, settings, profile, previous=previous, note=note)
    )
    original = next((m.subject for m in case.messages if m.direction == "out" and m.subject), None)
    subject = original or f"Pick Up Appointment: {' & '.join(str(p) for p in case.po_numbers)}"
    subject = subject if subject.lower().startswith("re:") else f"Re: {subject}"
    last_in = next((m for m in reversed(case.messages) if m.direction == "in"), None)
    customer = customer_of(case, settings)
    draft = OutboundDraft(
        to_addr=case.contact_email,
        cc_addr=customer.cc_header,
        subject=subject,
        body=body,
        thread_id=case.thread_id,
        in_reply_to=last_in.rfc_message_id if last_in else None,
        references=last_in.references_header if last_in else None,
        from_addr=customer.sender,
    )
    if is_sender(mailer):
        trusted = profile.contact_email if profile and profile.can_email else None
        check_send_gate(session, case, draft, settings, trusted_desk=trusted)
    offers = _offers(session, [case], settings, profile, now=now or datetime.now(tz=UTC))
    if offers:
        chosen, links, by_url = _with_links(template, [case], offers, settings)
        values = reschedule_values(
            case, settings, profile, previous=previous, note=note, links=links
        )
        _, body = render(chosen, values)
        draft = replace(draft, body=body, html=html_body(body, by_url))
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
    for offer in offers.values():
        offer.message_id = message.id
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
    message_id: int | None = None,
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
        record_reference(
            session,
            case,
            ReferenceType.PICKUP_NUMBER.value,
            result.pickup_number,
            source=ReferenceSource.VENDOR,
            by=actor,
            message_id=message_id,
        )
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
        record_reference(
            session,
            case,
            ReferenceType.PICKUP_NUMBER.value,
            result.pickup_number,
            source=ReferenceSource.VENDOR,
            by=actor,
            message_id=message_id,
        )
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
    settings: Settings | None = None,
) -> IngestStats:
    """Match inbound mail to cases, classify the replies, move the cases, draft answers.

    Mail from a case's own customer desk (its customer file's ``[customer_desk] email``, or
    ``customer_desk`` here) is not a vendor reply: it is read only for a new delivery slot
    (date, time and the customer's delivery reference), which moves the pickup request.
    """
    stats = IngestStats()
    internal = {d.lower() for d in internal_domains}
    settings = settings or (responder.settings if responder is not None else None)
    known = customers(settings) if settings is not None else built_in_customers()
    extra_desk = (customer_desk or "").strip().lower()
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
        from_desk = [
            c
            for c in cases
            if message.from_email
            and message.from_email in {known.for_case(c).customer_desk, extra_desk or None}
        ]
        if from_desk:
            moved = [
                _apply_customer_desk_message(session, c, message, responder, known.for_case(c))
                for c in from_desk
            ]
            if any(moved):
                stats.delivery_updates += 1
            cases = [c for c in cases if c not in from_desk]
            if not cases:
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
        action = apply_reply(
            session, case, reading, issues, reply_sent_at=message.sent_at, message_id=inbound.id
        )
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
    session: Session,
    case: BookingCase,
    message: InboundMessage,
    responder: Responder | None,
    customer: Customer,
) -> bool:
    """Record a customer-desk message; on a new delivery slot, move the pickup request."""
    slot = parse_delivery_slot(
        message.full_text,
        year=message.sent_at.year,
        timezone=case.vendor_timezone,
        customer=customer,
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
    record_reference(
        session,
        case,
        ReferenceType.DELIVERY_NUMBER.value,
        ref,
        source=ReferenceSource.CUSTOMER_DESK,
        message_id=inbound.id,
    )
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
    # The tender was for the old delivery: the new ask is backed off the new one.
    profile = vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
    now = responder.now if responder is not None and responder.now else datetime.now(tz=UTC)
    rec = _recommend(session, case, settings, profile, now=now, use_tender=False)
    new_request = rec.local
    if new_request is None:
        return True
    if not rec.feasible:
        _apply_recommendation(session, case, rec)
        return True
    if case.status != CaseStatus.UNSCHEDULED.value and responder is not None:
        for step in rec.moved:
            if step.rule == "floor":
                _event(session, case, "po_date_floor", reason=step.note, feasible=True)
        reschedule_case(
            session,
            case,
            responder.mailer,
            settings,
            requested_local=new_request,
            by="agent",
            note="Due to the receiver's availability, we will need to move this pickup.",
            now=now,
        )
    else:
        # Nothing has gone out yet: the next request simply asks for the new day. A missing
        # desk stays open; only what the old delivery made unworkable is cleared.
        _apply_recommendation(session, case, rec)
        case.reason = None
        resolve(
            session,
            case,
            [
                ExceptionType.SLOT_UNWORKABLE,
                ExceptionType.LOAD_INFEASIBLE,
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
    """Draft (or send) new cases, one email per vendor desk, the way the pod batches requests.

    A desk that ships for two customers gets one email per customer: each is copied to and
    signed for its own customer's group.
    """
    now = now or datetime.now(tz=UTC)
    known = customers(settings)
    groups: dict[tuple[str, str], list[BookingCase]] = {}
    for case in cases:
        key = (known.for_case(case).key, (case.contact_email or "").lower())
        groups.setdefault(key, []).append(case)
    messages: list[BookingMessage] = []
    repo = Repository(session)
    for (_, desk), group in groups.items():
        if len(group) == 1 or not desk:
            for case in group:
                messages.append(draft_case(session, case, mailer, settings, by=by, now=now))
            continue
        profile = vendor_profile(repo, group[0].facility_key) if group[0].facility_key else None
        for case in group:
            _check_draftable(case, settings, now=now, profile=profile)
        group.sort(key=lambda c: c.requested_local or "")
        template = pick(
            session, TemplateKind.BATCH_REQUEST, desk=desk, customer=_names(group[0], settings)
        )
        rendered_subject, body = render(template, request_values(group, settings, profile))
        subject = rendered_subject or "Pick Up Appointments"
        customer = known.for_case(group[0])
        draft = OutboundDraft(
            to_addr=desk,
            cc_addr=customer.cc_header,
            subject=subject,
            body=body,
            from_addr=customer.sender,
        )
        if is_sender(mailer):
            trusted = profile.contact_email if profile and profile.can_email else None
            for case in group:
                check_send_gate(session, case, draft, settings, trusted_desk=trusted)
        offers = _offers(session, group, settings, profile, now=now)
        if offers:
            chosen, links, by_url = _with_links(template, group, offers, settings)
            _, body = render(chosen, request_values(group, settings, profile, links=links))
            draft = replace(draft, body=body, html=html_body(body, by_url))
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
            if case.id in offers:
                offers[case.id].message_id = message.id
            record_delivery(session, case, message, draft, result, actor=by)
            case.status = CaseStatus.PENDING.value if result.sent else CaseStatus.UNSCHEDULED.value
            case.reason = None
            session.flush()
            _event(
                session,
                case,
                "drafted",
                actor=by,
                draft_ref=result.ref,
                to=desk,
                subject=subject,
                batched_with=[c.id for c in group if c.id != case.id],
                template=template.source,
            )
            messages.append(message)
    return messages


# ------------------------------------------------------------------ decisions


def approve(session: Session, case: BookingCase, *, by: str) -> tuple[dict[str, Any], bool]:
    """A person approves the vendor's confirmation; the slot is queued for Transport Pro.

    The case becomes scheduled. Its confirmation review is resolved, with what approving settles
    too: a confirmation outside the window asked for, and the timers (the pickup is booked).
    Anything else still open (a later question, say) stays open. Nothing is written here: the
    writer (``booking/writeback.py``) sends the queued slot while write-back is on. Returns the
    payload, and False (not written yet).
    """
    if case.status != CaseStatus.PENDING.value or not open_exceptions(
        case, ExceptionType.CONFIRMATION_REVIEW
    ):
        msg = f"case {case.id} is {case.status} with no confirmation to review; nothing to approve"
        raise ValueError(msg)
    payload = appointment_payload(case)
    written = False
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
    queue_write(session, case, by=by)
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
    record_reference(
        session,
        case,
        ReferenceType.PICKUP_NUMBER.value,
        pickup_number,
        source=ReferenceSource.PERSON,
        by=by,
    )
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
    learned = remember_booking(session, case, method=method, desk=used, by=by)
    if local:
        queue_write(session, case, by=by)
    return learned


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
            f"  {'number' if n.current else 'was':<9} {n.label} {n.value}  ({n.said}"
            + (f", replaced {n.replaced_at:%m/%d}" if n.replaced_at else "")
            + ")"
            for n in case_numbers(case)
            if n.kind not in ("load_number", "po_number")
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
