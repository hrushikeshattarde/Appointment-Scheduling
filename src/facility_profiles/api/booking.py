"""Booking cases over HTTP, for the appointments board (``/app``) and anything else that reads them.

Read: an overview (what needs a person, what is past due, what is coming up), a filtered list,
one case in full (open and resolved exceptions, messages, one timeline), and the daily summary.
Write: the same decisions the CLI offers (approve, resolve, booked, cancel), each recorded with
who made it. Nothing here sends mail or writes to Transport Pro.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from facility_profiles.booking.links import offer_state
from facility_profiles.booking.memory import desk_history
from facility_profiles.booking.models import (
    AutomationJob,
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseException,
    CaseStatus,
    DeskMemory,
    ExceptionType,
    SlotOffer,
)
from facility_profiles.booking.references import case_numbers
from facility_profiles.booking.rules import REFERENCE_LABELS, REFERENCE_NAMES
from facility_profiles.booking.service import (
    add_reference,
    approve,
    close_case,
    has_request,
    mark_booked,
)
from facility_profiles.booking.timers import fmt_slot, pickup_passed
from facility_profiles.booking.today import pickup_slot, render_today, stage, today_summary
from facility_profiles.booking.worklist import KINDS, resolve
from facility_profiles.config import Settings, get_settings
from facility_profiles.storage.repository import as_utc

EVENTS: dict[str, str] = {
    "scanned": "Found on a load",
    "po_date_floor": "Moved up to the PO date",
    "stale_slot": "Slot too soon or passed",
    "drafted": "Request drafted",
    "sent": "Sent",
    "reschedule": "Asked for a new slot",
    "vendor_confirmed": "Vendor confirmed",
    "stale_confirmation": "Late confirmation",
    "counter_offer": "Vendor offered another time",
    "question": "Vendor asked a question",
    "deferred": "Vendor asked to check back",
    "rejected_by_vendor": "Vendor cannot book",
    "reply_unrelated": "Reply not about this pickup",
    "reply_ignored": "Mail after the decision",
    "reply_after_decision": "Mail after the decision",
    "reply_not_about_this_po": "Reply about other POs",
    "customer_desk_message": "Customer desk message",
    "delivery_updated": "Delivery moved",
    "approved": "Approved",
    "closed": "Closed",
    "marked_booked": "Marked booked",
    "handoff": "Handed to a person",
    "accept_offer": "Agent accepted the offer",
    "ask_alternative": "Agent asked for other days",
    "answer_question": "Agent answered",
    "follow_up": "Follow-up drafted",
    "acknowledge": "Thanked the vendor",
    "escalate_to_customer": "Note to the customer desk",
    "status_migrated": "Moved to the new statuses",
    "reference_added": "Reference added",
    "desk_remembered": "Desk remembered",
    "desk_learned": "Desk on file now",
    "time_recommended": "Pickup time chosen",
    "request_waiting": "Request waits for the delivery slot",
    "written_to_tpro": "Written to Transport Pro",
    "tpro_already_set": "Already in Transport Pro",
    "confirmed_by_link": "Vendor picked a time from the link",
    "proposed_by_link": "Vendor proposed a time from the link",
}


def _iso(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None


def _draft_ready(case: BookingCase) -> bool:
    return case.status == CaseStatus.UNSCHEDULED.value and has_request(case)


def exception_view(exc: CaseException) -> dict[str, Any]:
    """One exception, with its plain-language label and what to do about it."""
    label, hint = KINDS.get(exc.kind, (exc.kind.replace("_", " ").capitalize(), ""))
    return {
        "id": exc.id,
        "kind": exc.kind,
        "label": label,
        "hint": hint,
        "description": exc.description,
        "detail": exc.detail or {},
        "open": exc.resolved_at is None,
        "raised_by": exc.raised_by,
        "raised_at": _iso(exc.raised_at),
        "resolved_by": exc.resolved_by,
        "resolved_at": _iso(exc.resolved_at),
        "resolution": exc.resolution,
    }


def case_summary(case: BookingCase, *, now: datetime) -> dict[str, Any]:
    """What a row of the board shows."""
    local, source = pickup_slot(case)
    events = case.events
    last = max(
        [t for t in [as_utc(case.updated_at), *(as_utc(e.created_at) for e in events)] if t],
        default=None,
    )
    return {
        "id": case.id,
        "load_id": case.load_id,
        "customer": case.customer_name,
        "vendor": case.vendor_name,
        "vendor_city": case.vendor_city,
        "timezone": case.vendor_timezone,
        "po_numbers": [str(p) for p in case.po_numbers],
        "status": case.status,
        "stage": stage(case),
        "pickup_local": local,
        "pickup_source": source,
        "pickup_date": local.partition(" ")[0] if local else None,
        "past_due": pickup_passed(case, now),
        "pickup_number": case.pickup_number,
        # Every number the case has carried, so any of them finds it.
        "numbers": sorted({r.value for r in case.references}),
        "desk": case.contact_email,
        "method": case.booking_method,
        "delivery_site": case.delivery_site,
        "delivery_ref": case.delivery_ref,
        "delivery_at": _iso(case.delivery_at_utc),
        "draft_ready": _draft_ready(case),
        "reschedule_count": case.reschedule_count or 0,
        "open_exceptions": [exception_view(e) for e in case.open_exceptions],
        "last_activity": last.isoformat() if last else None,
    }


def _message_view(message: BookingMessage) -> dict[str, Any]:
    reading = message.classification or {}
    keep = (
        "status",
        "pickup_date",
        "pickup_time",
        "pickup_time_end",
        "pickup_number",
        "question",
        "skipped",
    )
    return {
        "id": message.id,
        "direction": message.direction,
        "kind": message.kind,
        "from": message.from_addr,
        "to": message.to_addr,
        "cc": message.cc_addr,
        "subject": message.subject,
        "body": message.body,
        "sent_at": _iso(message.sent_at),
        "created_at": _iso(message.created_at),
        "draft_ref": message.draft_ref if message.sent_at is None else None,
        "reading": {k: reading[k] for k in keep if reading.get(k)},
    }


def _event_summary(event: BookingEvent) -> str:
    detail = event.detail or {}
    for key in ("reason", "note"):
        if detail.get(key):
            return str(detail[key])
    if detail.get("local"):
        number = f", pickup# {detail['pickup_number']}" if detail.get("pickup_number") else ""
        return f"{detail['local']}{number}"
    if event.action == "status_migrated":
        return f"{detail.get('from')} to {detail.get('to')}"
    if detail.get("to"):
        template = detail.get("template")
        wording = f", {template} wording" if template and template != "built-in" else ""
        return f"to {detail['to']}{wording}"
    return ""


def timeline(case: BookingCase) -> list[dict[str, Any]]:
    """Events and exceptions in one list, oldest first."""
    rows: list[dict[str, Any]] = [
        {
            "at": _iso(e.created_at),
            "type": "event",
            "action": e.action,
            "title": EVENTS.get(e.action, e.action.replace("_", " ").capitalize()),
            "actor": e.actor,
            "summary": _event_summary(e),
        }
        for e in case.events
    ]
    for exc in case.exceptions:
        label = KINDS.get(exc.kind, (exc.kind, ""))[0]
        rows.append(
            {
                "at": _iso(exc.raised_at),
                "type": "raised",
                "action": exc.kind,
                "title": f"To-do: {label}",
                "actor": exc.raised_by,
                "summary": exc.description,
            }
        )
        if exc.resolved_at is not None:
            rows.append(
                {
                    "at": _iso(exc.resolved_at),
                    "type": "resolved",
                    "action": exc.kind,
                    "title": f"Done: {label}",
                    "actor": exc.resolved_by,
                    "summary": exc.resolution or "",
                }
            )
    return sorted(rows, key=lambda r: r["at"] or "")


def requested_why(case: BookingCase) -> str | None:
    """Why the case asks for the time it does, from the latest event that set it."""
    for event in reversed(case.events):
        detail = event.detail or {}
        if event.action in ("time_recommended", "po_date_floor") and detail.get("reason"):
            return str(detail["reason"])
        if event.action == "reschedule":
            note = f": {detail['note']}" if detail.get("note") else ""
            return f"asked again by {event.actor}{note}"
    if not case.requested_local:
        return None
    return "the tendered pickup" if case.tendered_pickup_utc else "backed off the delivery slot"


def job_view(job: AutomationJob) -> dict[str, Any]:
    """One thing the agent planned or did on its own for the case."""
    return {
        "id": job.id,
        "kind": job.kind,
        "rule": job.rule,
        "action": job.action,
        "status": job.status,
        "due_at": _iso(job.due_at),
        "reason": job.reason,
        "attempts": job.attempts or 0,
        "done_at": _iso(job.done_at),
    }


def offer_view(offer: SlotOffer, *, now: datetime) -> dict[str, Any]:
    """The times one request offered by link, and what became of them."""
    detail = offer.answer_detail or {}
    answer = offer.answer
    if answer == "proposed":
        proposed = f"{detail.get('date') or ''} {detail.get('time') or ''}".strip()
        answer = f"proposed {fmt_slot(proposed)}"
    elif answer:
        answer = fmt_slot(answer)
    return {
        "id": offer.id,
        "slots": [fmt_slot(s) for s in offer.slots],
        "state": offer_state(offer, now),
        "answer": answer,
        "answered_at": _iso(offer.answered_at),
        "expires_at": _iso(offer.expires_at),
    }


def desk_view(row: DeskMemory) -> dict[str, Any]:
    """One way the facility was booked before."""
    return {
        "method": row.method,
        "desk": row.desk or None,
        "worked_count": row.worked_count,
        "last_worked_at": _iso(row.last_worked_at),
        "last_case_id": row.last_case_id,
    }


def case_detail(
    case: BookingCase, *, now: datetime, desks: list[DeskMemory] | None = None
) -> dict[str, Any]:
    """Everything about one case; ``desks`` is how its facility was booked before."""
    return {
        "desk_history": [desk_view(d) for d in desks or []],
        **case_summary(case, now=now),
        "requested_local": case.requested_local,
        "requested_why": requested_why(case),
        "confirmed_local": case.confirmed_local,
        "tendered_pickup_at": _iso(case.tendered_pickup_utc),
        "miles": case.miles,
        "reason": case.reason,
        "facility_key": case.facility_key,
        "references": [n.as_dict() for n in case_numbers(case)],
        "exceptions": [exception_view(e) for e in case.exceptions],
        "messages": [_message_view(m) for m in case.messages],
        "offers": [offer_view(o, now=now) for o in case.offers],
        "jobs": [job_view(j) for j in case.jobs],
        "timeline": timeline(case),
        "can_approve": case.status == CaseStatus.PENDING.value
        and any(e.kind == ExceptionType.CONFIRMATION_REVIEW for e in case.open_exceptions),
    }


def _sort_key(row: dict[str, Any]) -> tuple[int, str, int]:
    local = row["pickup_local"]
    return (0 if local else 1, local or "", row["id"])


def overview(cases: list[BookingCase], *, now: datetime, days: int) -> dict[str, Any]:
    """The board's front page: counts, what needs a person, what slipped, what is coming up."""
    rows = [case_summary(c, now=now) for c in cases]
    live = [r for r in rows if r["status"] != CaseStatus.CANCELED.value]
    today = now.date().isoformat()
    horizon = (now.date() + timedelta(days=days)).isoformat()
    # What is still ahead: a pickup that has slipped is listed as past due, not as coming up.
    upcoming = [
        r
        for r in live
        if r["pickup_date"] and today <= r["pickup_date"] < horizon and not r["past_due"]
    ]
    todos = [
        {**exc, "case": {k: r[k] for k in ("id", "vendor", "po_numbers", "pickup_local", "status")}}
        for r in rows
        for exc in r["open_exceptions"]
    ]
    todos.sort(key=lambda t: (t["case"]["pickup_local"] or "9999", t["raised_at"] or ""))
    unscheduled = [r for r in live if r["status"] == CaseStatus.UNSCHEDULED.value]
    pending = [r for r in live if r["status"] == CaseStatus.PENDING.value]
    scheduled = [r for r in live if r["status"] == CaseStatus.SCHEDULED.value]
    return {
        "generated_at": now.isoformat(),
        "days": days,
        "counts": {
            "total": len(rows),
            "needs_action": sum(1 for r in rows if r["open_exceptions"]),
            "past_due": sum(1 for r in rows if r["past_due"]),
            "not_requested": sum(1 for r in unscheduled if not r["open_exceptions"]),
            "drafts_waiting": sum(1 for r in unscheduled if r["draft_ready"]),
            "waiting_on_vendor": sum(1 for r in pending if not r["open_exceptions"]),
            "booked_upcoming": sum(
                1 for r in upcoming if r["status"] == CaseStatus.SCHEDULED.value
            ),
            "booked": len(scheduled),
            "declined": sum(1 for r in live if r["status"] == CaseStatus.DECLINED.value),
            "upcoming": len(upcoming),
        },
        "todos_by_kind": [
            {"kind": kind, "label": KINDS.get(kind, (kind, ""))[0], "count": count}
            for kind, count in Counter(t["kind"] for t in todos).most_common()
        ],
        "todos": todos,
        "past_due": sorted((r for r in rows if r["past_due"]), key=_sort_key),
        "upcoming": sorted(upcoming, key=_sort_key),
    }


def _matches(row: dict[str, Any], q: str) -> bool:
    needle = q.strip().lower()
    haystack = [
        row["vendor"],
        str(row["load_id"]),
        row["pickup_number"],
        row["delivery_ref"],
        row["customer"],
        row["desk"],
        *row["po_numbers"],
        *row["numbers"],
    ]
    return any(needle in (h or "").lower() for h in haystack)


Who = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Note = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]
Day = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Clock = Annotated[str, StringConstraints(pattern=r"^\d{2}:\d{2}$")]


class Approval(BaseModel):
    """Who approves the vendor's confirmation."""

    by: Who


class Resolution(BaseModel):
    """Who resolved which exception, and how."""

    by: Who
    kind: ExceptionType
    note: Note


class Booking(BaseModel):
    """A pickup booked outside the agent."""

    by: Who
    via: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    date: Day | None = None
    time: Clock | None = None
    pickup_number: str | None = None
    note: str | None = None
    desk: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


class Reference(BaseModel):
    """A number the vendor's desk needs, added by a person."""

    by: Who
    kind: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class Cancellation(BaseModel):
    """Who cancels the case, and why."""

    by: Who
    reason: Note


# ------------------------------------------------------------------ routes
#
# The app puts its session factory on ``app.state.sessions`` (and, in tests, a fixed clock on
# ``app.state.clock``); the dependencies below read them from the request.


def _session(request: Request) -> Iterator[Session]:
    session: Session = request.app.state.sessions()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _now(request: Request) -> datetime:
    clock: Callable[[], datetime] | None = getattr(request.app.state, "clock", None)
    return clock() if clock is not None else datetime.now(tz=UTC)


def _settings(request: Request) -> Settings:
    settings: Settings | None = getattr(request.app.state, "settings", None)
    return settings if settings is not None else get_settings()


SessionDep = Annotated[Session, Depends(_session)]
NowDep = Annotated[datetime, Depends(_now)]
SettingsDep = Annotated[Settings, Depends(_settings)]
router = APIRouter(prefix="/api/booking", tags=["booking"])


def _load_all(session: Session) -> list[BookingCase]:
    stmt = select(BookingCase).options(
        selectinload(BookingCase.exceptions),
        selectinload(BookingCase.messages),
        selectinload(BookingCase.events),
        selectinload(BookingCase.references),
    )
    return list(session.scalars(stmt))


def _get_case(session: Session, case_id: int) -> BookingCase:
    case = session.get(BookingCase, case_id)
    if case is None:
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    return case


def _detail(session: Session, case: BookingCase, now: datetime) -> dict[str, Any]:
    return case_detail(case, now=now, desks=desk_history(session, case.facility_key))


def _decided(session: Session, case: BookingCase, now: datetime) -> dict[str, Any]:
    session.commit()  # the decision is stored before anyone is told it was made
    return _detail(session, case, now)


@router.get("/overview")
def get_overview(
    session: SessionDep,
    now: NowDep,
    customer: str | None = None,
    days: Annotated[int, Query(ge=1, le=60)] = 7,
) -> dict[str, Any]:
    """Counts, open to-dos, past-due pickups and the next days' pickups."""
    cases = [c for c in _load_all(session) if not customer or c.customer_name == customer]
    return overview(cases, now=now, days=days)


@router.get("/today")
def get_today(
    session: SessionDep, now: NowDep, settings: SettingsDep, customer: str | None = None
) -> dict[str, Any]:
    """The daily summary: what needs a person, today's pickups, drafts to send.

    ``text`` is the same summary as plain text, ready to paste into an email or a chat.
    """
    data = today_summary(
        _load_all(session), now=now, timezone=settings.booking_timezone, customer=customer
    )
    return {**data, "text": render_today(data)}


@router.get("/kinds")
def get_kinds() -> list[dict[str, str]]:
    """Every exception kind with its label and what to do about it, for the board's filter."""
    return [
        {"kind": kind.value, "label": KINDS[kind.value][0], "hint": KINDS[kind.value][1]}
        for kind in ExceptionType
    ]


@router.get("/customers")
def get_customers(session: SessionDep) -> list[str]:
    """Customer names on the cases, for the board's filter."""
    names = session.scalars(select(BookingCase.customer_name).distinct())
    return sorted(n for n in names if n)


@router.get("/cases")
def get_cases(
    session: SessionDep,
    now: NowDep,
    *,
    status: CaseStatus | None = None,
    exception: str | None = None,
    customer: str | None = None,
    q: str | None = None,
    start: Day | None = None,
    end: Day | None = None,
    past_due: bool = False,
) -> list[dict[str, Any]]:
    """Cases by pickup, filtered; ``exception`` is a kind or ``any``, ``q`` searches ids."""
    rows = [case_summary(c, now=now) for c in _load_all(session)]
    if status is not None:
        rows = [r for r in rows if r["status"] == status.value]
    if exception == "any":
        rows = [r for r in rows if r["open_exceptions"]]
    elif exception:
        rows = [r for r in rows if any(e["kind"] == exception for e in r["open_exceptions"])]
    if customer:
        rows = [r for r in rows if r["customer"] == customer]
    if q and q.strip():
        rows = [r for r in rows if _matches(r, q)]
    if start:
        rows = [r for r in rows if r["pickup_date"] and r["pickup_date"] >= start]
    if end:
        rows = [r for r in rows if r["pickup_date"] and r["pickup_date"] <= end]
    if past_due:
        rows = [r for r in rows if r["past_due"]]
    return sorted(rows, key=_sort_key)


@router.get("/cases/{case_id}")
def get_case_detail(case_id: int, session: SessionDep, now: NowDep) -> dict[str, Any]:
    """One case in full, with how its facility was booked before."""
    return _detail(session, _get_case(session, case_id), now)


@router.post("/cases/{case_id}/approve")
def post_approve(case_id: int, body: Approval, session: SessionDep, now: NowDep) -> dict[str, Any]:
    """Approve the vendor's confirmation; the slot is queued for Transport Pro."""
    case = _get_case(session, case_id)
    try:
        approve(session, case, by=body.by)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decided(session, case, now)


@router.post("/cases/{case_id}/resolve")
def post_resolve(
    case_id: int, body: Resolution, session: SessionDep, now: NowDep
) -> dict[str, Any]:
    """Resolve one open exception with a note."""
    case = _get_case(session, case_id)
    if not resolve(session, case, [body.kind], resolution=body.note, by=body.by):
        raise HTTPException(status_code=409, detail=f"case {case_id} has no open {body.kind.value}")
    return _decided(session, case, now)


@router.post("/cases/{case_id}/booked")
def post_booked(case_id: int, body: Booking, session: SessionDep, now: NowDep) -> dict[str, Any]:
    """Record a pickup booked outside the agent; the case becomes scheduled."""
    if body.time and not body.date:
        raise HTTPException(status_code=422, detail="a pickup time needs a date")
    case = _get_case(session, case_id)
    local = f"{body.date} {body.time}" if body.date and body.time else body.date
    try:
        mark_booked(
            session,
            case,
            by=body.by,
            via=body.via,
            local=local,
            pickup_number=(body.pickup_number or "").strip() or None,
            note=(body.note or "").strip() or None,
            desk=body.desk or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decided(session, case, now)


@router.post("/cases/{case_id}/reference")
def post_reference(
    case_id: int, body: Reference, session: SessionDep, now: NowDep
) -> dict[str, Any]:
    """Add a number the desk needs (the customer's shipment or SO number) to the case."""
    case = _get_case(session, case_id)
    try:
        add_reference(session, case, body.kind, body.value, by=body.by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _decided(session, case, now)


@router.get("/references")
def get_references() -> list[dict[str, str]]:
    """Every reference type with how a request writes it and its plain name."""
    return [
        {"kind": kind, "label": label, "name": REFERENCE_NAMES.get(kind, kind)}
        for kind, label in REFERENCE_LABELS.items()
    ]


@router.post("/cases/{case_id}/cancel")
def post_cancel(
    case_id: int, body: Cancellation, session: SessionDep, now: NowDep
) -> dict[str, Any]:
    """Cancel a case that is no longer needed."""
    case = _get_case(session, case_id)
    if case.status == CaseStatus.CANCELED.value:
        raise HTTPException(status_code=409, detail=f"case {case_id} is already canceled")
    close_case(session, case, by=body.by, reason=body.reason)
    return _decided(session, case, now)
