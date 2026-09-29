"""Booking cases: find loads to book, compose the request, read the reply, propose the slot.

Draft mode: the agent never sends and never writes to Transport Pro. A person sends the draft,
tells the agent it went out (``booking sent``), and approves the proposed appointment
(``booking approve``). Every step is recorded on the case.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.booking.classify import (
    ClassificationIssue,
    ReplyClassifier,
    ReplyContext,
    validate_classification,
)
from facility_profiles.booking.mail import InboundMessage, Mailer, OutboundDraft
from facility_profiles.booking.models import BookingCase, BookingEvent, BookingMessage, CaseStatus
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.config import Settings
from facility_profiles.domain.resolution import FacilityResolver
from facility_profiles.domain.schema import Role
from facility_profiles.logging import get_logger
from facility_profiles.pipeline.harvest import iter_terminal_loads, stop_identity
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc, unwrap
from facility_profiles.tpro.client import TransportProClient
from facility_profiles.tpro.models import Load, Waypoint

log = get_logger(__name__)

DELIVERY_REF_RE = re.compile(r"\b[A-Z]{3}_\d{6,}\b")
PO_RE = re.compile(r"\b\d{9,15}\b")
TRUSTED_STATES = frozenset({"human_set", "verified", "written"})
OPEN_STATUSES = (
    CaseStatus.NEW.value,
    CaseStatus.DRAFTED.value,
    CaseStatus.SENT.value,
    CaseStatus.PROPOSED.value,
    CaseStatus.NEEDS_HUMAN.value,
)


@dataclass(frozen=True)
class VendorProfile:
    """The trusted booking facts for a pickup facility."""

    key: str
    booking_method: str | None
    contact_email: str | None
    contact_name: str | None
    appointment_required: bool | None
    summary: str | None

    @property
    def can_email(self) -> bool:
        """True when the agent has a verified email desk to write to."""
        return self.booking_method == "email" and bool(self.contact_email)


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


# ------------------------------------------------------------------ profile and load helpers


def vendor_profile(repo: Repository, key: str) -> VendorProfile:
    """Read the trusted shipper-side fields for a facility key."""
    fields = repo.fields(key, Role.SHIPPER)

    def trusted(name: str) -> Any:
        fld = fields.get(name)
        if fld is None or fld.state not in TRUSTED_STATES:
            return None
        return unwrap(fld.value)

    profile = repo.profile(key, Role.SHIPPER)
    return VendorProfile(
        key=key,
        booking_method=trusted("booking_method"),
        contact_email=trusted("contact_email"),
        contact_name=trusted("contact_name"),
        appointment_required=trusted("appointment_required"),
        summary=profile.scheduling_summary if profile else None,
    )


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
            if pickup_no:
                status, reason = (
                    CaseStatus.ALREADY_BOOKED.value,
                    f"load already carries vendor pickup number {pickup_no}",
                )
            elif profile.can_email:
                status, reason = CaseStatus.NEW.value, None
            else:
                status, reason = (
                    CaseStatus.NEEDS_PROFILE.value,
                    "no verified email booking desk on the profile",
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
            _event(session, case, "scanned", status=case.status, reason=case.reason)
            stats.created += 1
            stats.case_ids.append(case.id)
            if status == CaseStatus.ALREADY_BOOKED.value:
                stats.already_booked += 1
            elif status == CaseStatus.NEEDS_PROFILE.value:
                stats.needs_profile += 1
    return stats


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


def request_lines(case: BookingCase) -> list[str]:
    """The PO lines exactly as the pod writes them: "PO# X on MM/DD @ HHMM"."""
    mmdd, clock = _fmt_local(case.requested_local)
    when = f"on {mmdd}" + (f" @ {clock}" if clock else "")
    pos = [str(p) for p in case.po_numbers]
    if not pos:
        return [f"Load {case.load_id} {when}"]
    if len(pos) > 1:
        return [f"PO# {' & '.join(pos)} (ALL IN ONE TRUCK) {when}"]
    return [f"PO# {pos[0]} {when}"]


def compose_request(case: BookingCase, settings: Settings) -> OutboundDraft:
    """The request email, in the shape the pod already uses."""
    pos = [str(p) for p in case.po_numbers]
    subject = (
        f"Pick Up Appointment: {' & '.join(pos)}"
        if pos
        else f"Pick Up Appointment: load {case.load_id}"
    )
    lines = [
        "Hello,",
        "",
        "Can I please schedule the following?",
        "",
        *request_lines(case),
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
    session: Session, case: BookingCase, mailer: Mailer, settings: Settings
) -> BookingMessage:
    """Compose the request and hand it to the mailer as a draft."""
    if case.status not in (CaseStatus.NEW.value, CaseStatus.DRAFTED.value):
        msg = f"case {case.id} is {case.status}; only new cases can be drafted"
        raise ValueError(msg)
    if not case.contact_email:
        msg = f"case {case.id} has no booking email"
        raise ValueError(msg)
    draft = compose_request(case, settings)
    ref = mailer.create_draft(draft)
    message = BookingMessage(
        case_id=case.id,
        direction="out",
        kind="request",
        to_addr=draft.to_addr,
        cc_addr=draft.cc_addr,
        subject=draft.subject,
        body=draft.body,
        draft_ref=ref,
    )
    case.messages.append(message)
    case.status = CaseStatus.DRAFTED.value
    case.reason = None
    session.flush()
    _event(session, case, "drafted", draft_ref=ref, to=draft.to_addr, subject=draft.subject)
    return message


def mark_sent(
    session: Session,
    case: BookingCase,
    *,
    by: str,
    thread_id: str | None = None,
    message_id: str | None = None,
    sent_at: datetime | None = None,
) -> None:
    """A person sent the draft; remember the thread so replies can be matched."""
    if case.status not in (CaseStatus.DRAFTED.value, CaseStatus.NEW.value):
        msg = f"case {case.id} is {case.status}; nothing to mark as sent"
        raise ValueError(msg)
    request = next((m for m in reversed(case.messages) if m.direction == "out"), None)
    if request is not None:
        request.sent_at = sent_at or datetime.now(tz=UTC)
        request.thread_id = thread_id or request.thread_id
        request.message_id = message_id or request.message_id
    case.thread_id = thread_id or case.thread_id
    case.status = CaseStatus.SENT.value
    _event(session, case, "sent", actor=by, thread_id=thread_id, message_id=message_id)


# ------------------------------------------------------------------ replies


def list_cases(session: Session, status: str | None = None) -> list[BookingCase]:
    """Cases, newest first, optionally by status."""
    stmt = select(BookingCase).order_by(BookingCase.id.desc())
    if status:
        stmt = stmt.where(BookingCase.status == status)
    return list(session.scalars(stmt))


def match_case(session: Session, message: InboundMessage) -> BookingCase | None:
    """Match a reply to a case: by thread, then PO number, then a lone open case per sender."""
    if message.thread_id:
        case = session.scalar(select(BookingCase).where(BookingCase.thread_id == message.thread_id))
        if case is not None:
            return case
    open_cases = list(
        session.scalars(select(BookingCase).where(BookingCase.status.in_(OPEN_STATUSES)))
    )
    numbers = set(PO_RE.findall(f"{message.subject} {message.body}"))
    if numbers:
        hits = [c for c in open_cases if numbers & {str(p) for p in c.po_numbers}]
        if len(hits) == 1:
            return hits[0]
    sender = message.from_email
    by_sender = [c for c in open_cases if (c.contact_email or "").lower() == sender]
    return by_sender[0] if len(by_sender) == 1 else None


def _local_to_utc(day: str, clock: str | None, timezone: str | None) -> datetime:
    tz = ZoneInfo(timezone or "America/New_York")
    parsed = datetime.strptime(f"{day} {clock or '00:00'}", "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=tz).astimezone(UTC)


def apply_reply(
    session: Session,
    case: BookingCase,
    result: ReplyClassification,
    issues: list[ClassificationIssue],
    *,
    actor: str = "agent",
) -> str:
    """Move the case according to the classified reply; return the new status."""
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
        case.confirmed_local = f"{day} {clock or ''}".strip()
        case.confirmed_start_utc = start
        case.confirmed_end_utc = end
        case.pickup_number = result.pickup_number or case.pickup_number
        case.status = CaseStatus.PROPOSED.value
        case.reason = None
        _event(
            session,
            case,
            "vendor_confirmed",
            actor=actor,
            local=case.confirmed_local,
            pickup_number=case.pickup_number,
            conditions=result.conditions,
            issues=[i.__dict__ for i in issues],
        )
    elif result.status == ReplyStatus.COUNTER_OFFER:
        case.status = CaseStatus.NEEDS_HUMAN.value
        case.reason = (
            f"vendor offered {result.pickup_date or '?'} {result.pickup_time or ''}".strip()
            + (f" to {result.pickup_time_end}" if result.pickup_time_end else "")
        )
        _event(session, case, "counter_offer", actor=actor, reason=case.reason)
    elif result.status == ReplyStatus.QUESTION:
        case.status = CaseStatus.NEEDS_HUMAN.value
        case.reason = f"vendor asked: {result.question or 'see reply'}"[:255]
        _event(session, case, "question", actor=actor, reason=case.reason)
    elif result.status == ReplyStatus.DEFERRED:
        case.status = CaseStatus.SENT.value
        case.reason = (
            f"vendor asked to check back on {result.pickup_date}"
            if result.pickup_date
            else "vendor asked to check back later"
        )
        _event(session, case, "deferred", actor=actor, reason=case.reason)
    elif result.status == ReplyStatus.REJECTED:
        case.status = CaseStatus.NEEDS_HUMAN.value
        case.reason = (
            f"vendor cannot book: {result.question or '; '.join(result.conditions) or 'see reply'}"[
                :255
            ]
        )
        _event(session, case, "rejected_by_vendor", actor=actor, reason=case.reason)
    else:
        _event(session, case, "reply_unrelated", actor=actor, issues=[i.__dict__ for i in issues])
    return case.status


def ingest(
    session: Session,
    messages: list[InboundMessage],
    classifier: ReplyClassifier,
    *,
    internal_domains: list[str],
    responder: Responder | None = None,
) -> IngestStats:
    """Match inbound mail to cases, classify the replies, move the cases, draft answers."""
    stats = IngestStats()
    internal = {d.lower() for d in internal_domains}
    for message in messages:
        stats.messages += 1
        if message.from_domain in internal:
            stats.skipped_internal += 1
            continue
        if session.scalar(
            select(BookingMessage).where(BookingMessage.message_id == message.message_id)
        ):
            stats.duplicates += 1
            continue
        case = match_case(session, message)
        if case is None:
            stats.unmatched += 1
            continue
        context = ReplyContext(
            vendor_name=case.vendor_name or "",
            po_numbers=[str(p) for p in case.po_numbers],
            requested_local=case.requested_local,
            reply_sent_at=message.sent_at,
            subject=message.subject,
            body=message.body,
        )
        output = classifier.classify(context)
        result, issues = validate_classification(output.result, message.body)
        stats.classified += 1
        inbound = BookingMessage(
            case_id=case.id,
            direction="in",
            kind="reply",
            from_addr=message.from_addr,
            to_addr=message.to_addr,
            cc_addr=message.cc_addr,
            subject=message.subject,
            body=message.body,
            message_id=message.message_id,
            thread_id=message.thread_id,
            sent_at=message.sent_at,
            classification={
                **result.model_dump(mode="json"),
                "issues": [i.__dict__ for i in issues],
                "model": output.model,
            },
        )
        case.messages.append(inbound)
        session.flush()
        if case.thread_id is None and message.thread_id:
            case.thread_id = message.thread_id
        apply_reply(session, case, result, issues)
        if result.status == ReplyStatus.CONFIRMED and case.status == CaseStatus.PROPOSED.value:
            stats.proposed += 1
        elif result.status in (
            ReplyStatus.COUNTER_OFFER,
            ReplyStatus.QUESTION,
            ReplyStatus.REJECTED,
        ):
            stats.needs_human += 1
        elif result.status == ReplyStatus.DEFERRED:
            stats.deferred += 1
        else:
            stats.unrelated += 1
        if responder is not None and result.status in (
            ReplyStatus.COUNTER_OFFER,
            ReplyStatus.QUESTION,
            ReplyStatus.REJECTED,
        ):
            plan, drafted = responder.respond(session, case, inbound, result)
            if drafted is not None:
                stats.responded += 1
            log.info(
                "booking.responded", case=case.id, intent=plan.intent.value, reason=plan.reason
            )
        elif (
            responder is not None
            and result.status == ReplyStatus.CONFIRMED
            and case.status == CaseStatus.PROPOSED.value
        ):
            if responder.acknowledge(session, case, inbound) is not None:
                stats.responded += 1
        session.flush()
    return stats


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
    """A person approves the proposed slot; write it only when the client allows writes."""
    if case.status != CaseStatus.PROPOSED.value:
        msg = f"case {case.id} is {case.status}; only proposed cases can be approved"
        raise ValueError(msg)
    payload = appointment_payload(case)
    written = False
    if client is not None and client.allow_writes:
        client.set_appointment(**payload)
        written = True
    case.status = CaseStatus.APPROVED.value
    _event(session, case, "approved", actor=by, payload=payload, written_to_tpro=written)
    return payload, written


def close_case(session: Session, case: BookingCase, *, by: str, reason: str) -> None:
    """Close a case without booking."""
    case.status = CaseStatus.CLOSED.value
    case.reason = reason[:255]
    _event(session, case, "closed", actor=by, reason=reason)


def describe(case: BookingCase) -> str:
    """One-screen summary for the CLI."""
    lines = [
        f"case #{case.id}  load {case.load_id}  status {case.status}"
        + (f"  ({case.reason})" if case.reason else ""),
        f"  vendor    {case.vendor_name or '?'} {case.vendor_city or ''}  [{case.facility_key}]",
        f"  desk      {case.contact_email or 'none'}  ({case.booking_method or 'unknown method'})",
        f"  PO        {', '.join(str(p) for p in case.po_numbers) or 'none'}",
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
    for m in case.messages:
        head = f"  {'->' if m.direction == 'out' else '<-'} {m.kind:<8} {m.subject or ''}"
        if m.direction == "in" and m.classification:
            c = m.classification
            head += (
                f"  => {c.get('status')} {c.get('pickup_date') or ''} {c.get('pickup_time') or ''}"
            )
        lines.append(head)
    return "\n".join(lines)
