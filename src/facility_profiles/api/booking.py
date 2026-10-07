"""Booking cases over HTTP, for the appointments board (``/app``) and anything else that reads them.

Read: an overview (what needs a person, what is past due, what is coming up), a filtered list,
one case in full (open and resolved exceptions, messages, one timeline), and the daily summary.
Write: the same decisions the CLI offers (approve, resolve, booked, cancel), each recorded with
who made it. Nothing here sends mail or writes to Transport Pro.

With sign-in on (api/auth.py), every route answers with the signed-in person's customers only:
another customer's case is "not found", and a decision needs "act" access to its customer.
Decisions are recorded under the signed-in name; without sign-in, under the ``by`` sent.

Every pickup slot here is on the Eastern clock ("YYYY-MM-DD HH:MM", the facility's own zone in
``timezone``); instants are ISO-8601 in UTC, which the board shows in Eastern. A time sent in a
decision (marking a pickup booked) is Eastern too.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime, timedelta
from email.utils import getaddresses
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, StringConstraints
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from facility_profiles.access import Viewer
from facility_profiles.api.auth import ViewerDep, decided_by
from facility_profiles.booking.inbox import reader_tools
from facility_profiles.booking.links import offer_state
from facility_profiles.booking.memory import desk_history
from facility_profiles.booking.models import (
    PERSON_MAIL,
    AutomationJob,
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseException,
    CaseStatus,
    DeskMemory,
    ExceptionType,
    SlotOffer,
    UnmatchedMail,
)
from facility_profiles.booking.references import case_numbers
from facility_profiles.booking.rules import REFERENCE_LABELS, REFERENCE_NAMES
from facility_profiles.booking.service import (
    add_reference,
    approve,
    close_case,
    has_request,
    mark_booked,
    picked_up,
)
from facility_profiles.booking.timers import fmt_slot, pickup_passed
from facility_profiles.booking.today import pickup_slot, render_today, stage, today_summary
from facility_profiles.booking.unmatched import dismiss_unmatched, link_unmatched, open_unmatched
from facility_profiles.booking.worklist import KINDS, resolve
from facility_profiles.clock import eastern_to_local, local_to_eastern, to_eastern
from facility_profiles.config import Settings, get_settings
from facility_profiles.customers import customers
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
    "delivery_from_tpro": "Delivery slot from Transport Pro",
    "booked_in_tpro": "Booked in Transport Pro",
    "picked_up_in_tpro": "Picked up (Transport Pro shows the load delivered)",
    "auto_confirmed": "Booked by the agent",
    "auto_confirm_held": "Left for a person to approve",
    "reply_not_sent": "Answer kept as a draft",
    "tender_changed": "Tender time changed in Transport Pro",
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
    "vendor_reconfirmed": "Vendor confirmed the booked time again",
    "booked_changed": "Vendor changed a booked pickup",
    "thanks_held": "No thank-you sent",
    "mail_linked": "Email linked to this pickup",
    "sent_by_person": "Email sent by a person",
    "send_unconfirmed": "Gmail did not confirm the email went out",
    "email_bounced": "Email sent back by the mail server",
    "auto_reply": "Automatic reply",
    "delivery_delayed": "Delivery delayed",
    "eta_requested": "Facility asked for the driver's ETA",
    "work_in_offered": "Facility offered a late arrival",
    "on_hold": "Facility put the pickup on hold",
}
# Transport Pro changes the board's "Latest updates" lists beside the emails.
LOAD_UPDATES = frozenset(
    {
        "scanned",
        "delivery_from_tpro",
        "tender_changed",
        "booked_in_tpro",
        "picked_up_in_tpro",
        "closed",
        "written_to_tpro",
    }
)
UPDATES_DAYS = 14
UPDATES_SHOWN = 25
# What the agent made of a vendor's email, in a few words.
READ_AS = {
    "confirmed": "confirmed",
    "counter_offer": "offered another time",
    "question": "asked a question",
    "rejected": "cannot book",
    "deferred": "asked to check back",
}
# What the facility's email was about besides the slot (schema.ReplyTopic), in a few words.
READ_TOPIC = {
    "eta": "asked for the driver's ETA",
    "work_in": "offered a late arrival",
    "hold": "put the pickup on hold",
}
# What the agent's own emails did, in a few words: "The agent asked for the pickup".
AGENT_MAIL = {
    "request": "asked for the pickup",
    "reschedule": "asked for a new time",
    "follow_up": "sent a reminder",
    "acknowledge": "thanked the vendor",
    "accept_offer": "accepted the vendor's time",
    "ask_alternative": "asked for other days",
    "answer_question": "answered a question",
    "escalate_to_customer": "wrote to the customer's desk",
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
        # What Transport Pro last said about the load, and whether that means the truck has
        # picked up: the board lists those apart, as done.
        "load_status": (case.tpro_seen or {}).get("load_status"),
        "picked_up": picked_up((case.tpro_seen or {}).get("load_status")),
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


def _in_order(messages: list[BookingMessage]) -> list[BookingMessage]:
    """A pickup's emails as one chain, oldest first: by when each was sent (or written)."""
    epoch = datetime.min.replace(tzinfo=UTC)
    return sorted(
        messages, key=lambda m: (as_utc(m.sent_at) or as_utc(m.created_at) or epoch, m.id)
    )


def _who(header: str | None, *, outside: Iterable[str] = ()) -> str:
    """A name from an address header; with ``outside``, the first address not on those domains.

    "To: _US_Mail_Inbound <inbound@lidl.us>, Lidl Group <lidl@circledelivers.com>" is the
    customer's desk, not our own group.
    """
    pairs = [(n, a) for n, a in getaddresses([header or ""]) if a and "@" in a]
    theirs = [p for p in pairs if p[1].lower().rpartition("@")[2] not in set(outside)]
    name, address = (theirs or pairs or [("", "")])[0]
    return name.strip().strip('"') or address or "someone"


def _email_update(
    message: BookingMessage, internal: Iterable[str]
) -> tuple[datetime, str, str] | None:
    """An email as a line of "Latest updates": when, what happened, about what."""
    at = as_utc(message.sent_at) or as_utc(message.created_at)
    if at is None:
        return None
    subject = message.subject or "(no subject)"
    if message.direction == "out" and message.kind == PERSON_MAIL:
        to = _who(message.to_addr, outside=internal)
        return at, f"{_who(message.from_addr)} emailed {to}", subject
    if message.direction == "out":
        if message.sent_at is None:
            return None  # a draft is not a conversation yet; "Not asked yet" counts the drafts
        what = AGENT_MAIL.get(message.kind, message.kind.replace("_", " "))
        return at, f"The agent {what}", subject
    if message.kind == "customer_desk":
        return at, f"{_who(message.from_addr)} (the customer's desk) wrote", subject
    if message.kind == "link":
        return at, "The vendor answered with the link", subject
    reading = message.classification or {}
    if message.kind == "bounce":
        to = reading.get("recipient") or "the desk"
        return at, f"The mail server sent back the email to {to}", subject
    if message.kind == "auto_reply":
        return at, f"{_who(message.from_addr)} sent an automatic reply", subject
    if message.kind == "delivery_delayed":
        return at, "The mail server is still trying to deliver an email", subject
    said = READ_TOPIC.get(str(reading.get("topic") or "")) or READ_AS.get(
        str(reading.get("status") or "")
    )
    read = f", read as {said}" if said and not reading.get("skipped") else ""
    return at, f"{_who(message.from_addr)} wrote{read}", subject


def latest_updates(
    cases: list[BookingCase], *, now: datetime, internal: Iterable[str] = ("circledelivers.com",)
) -> list[dict[str, Any]]:
    """The newest emails and Transport Pro changes across the pickups, newest first.

    Every email on a pickup counts, whoever sent it (the vendor, the agent, a person at Circle,
    the customer's desk), at the time it was sent; a change Transport Pro showed counts at the
    time the board saw it.
    """
    since = now - timedelta(days=UPDATES_DAYS)
    domains = {d.lower() for d in internal}
    rows: list[dict[str, Any]] = []
    # One email that covered several pickups (a batched request, its reply) is one row.
    by_email: dict[str, dict[str, Any]] = {}
    for case in cases:
        pos = [str(p) for p in case.po_numbers]
        for message in case.messages:
            line = _email_update(message, domains)
            if line is None or line[0] < since:
                continue
            key = message.rfc_message_id or message.message_id or f"#{message.id}"
            if key in by_email:
                by_email[key]["po"] += [p for p in pos if p not in by_email[key]["po"]]
                continue
            by_email[key] = _update(line[0], case, pos, source="email", what=line[1], about=line[2])
            rows.append(by_email[key])
        for event in case.events:
            at = as_utc(event.created_at)
            if event.action in LOAD_UPDATES and at is not None and at >= since:
                said = EVENTS.get(event.action, event.action.replace("_", " "))
                reason = str((event.detail or {}).get("reason") or "")
                if reason.lower().startswith(("picked up", "booked in transport pro")):
                    reason = ""  # the title already says it
                rows.append(_update(at, case, pos, source="load", what=said, about=reason))
    rows.sort(key=lambda row: (row["at"], row["case_id"]), reverse=True)
    for row in rows:
        row["po"] = ", ".join(row["po"])
    return rows[:UPDATES_SHOWN]


def _update(
    at: datetime, case: BookingCase, pos: list[str], *, source: str, what: str, about: str
) -> dict[str, Any]:
    return {
        "at": _iso(at),
        "case_id": case.id,
        "vendor": case.vendor_name,
        "po": list(pos),
        "source": source,
        "what": what,
        "about": about,
    }


def _message_view(message: BookingMessage, timezone: str | None = None) -> dict[str, Any]:
    reading = dict(message.classification or {})
    if reading.get("pickup_date") and reading.get("pickup_time"):  # read on the facility's clock
        eastern = local_to_eastern(f"{reading['pickup_date']} {reading['pickup_time']}", timezone)
        reading["pickup_date"], _, reading["pickup_time"] = (eastern or "").partition(" ")
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
    tz = offer.case.vendor_timezone
    if answer == "proposed":
        proposed = f"{detail.get('date') or ''} {detail.get('time') or ''}".strip()
        answer = f"proposed {fmt_slot(proposed, tz)}"
    elif answer:
        answer = fmt_slot(answer, tz)
    return {
        "id": offer.id,
        "slots": [fmt_slot(s, tz) for s in offer.slots],
        "state": offer_state(offer, now),
        "answer": answer,
        "answered_at": _iso(offer.answered_at),
        "expires_at": _iso(offer.expires_at),
    }


def desk_view(row: DeskMemory, *, show_case: bool = True) -> dict[str, Any]:
    """One way the facility was booked before (the last case only when the viewer may see it)."""
    return {
        "method": row.method,
        "desk": row.desk or None,
        "worked_count": row.worked_count,
        "last_worked_at": _iso(row.last_worked_at),
        "last_case_id": row.last_case_id if show_case else None,
    }


def case_detail(
    case: BookingCase,
    *,
    now: datetime,
    desks: list[DeskMemory] | None = None,
    shown_cases: Callable[[int], bool] | None = None,
) -> dict[str, Any]:
    """Everything about one case; ``desks`` is how its facility was booked before.

    ``shown_cases`` says which other cases the reader may see (all, when not given).
    """
    return {
        "desk_history": [
            desk_view(
                d,
                show_case=shown_cases is None
                or d.last_case_id is None
                or shown_cases(d.last_case_id),
            )
            for d in desks or []
        ],
        **case_summary(case, now=now),
        "requested_local": local_to_eastern(case.requested_local, case.vendor_timezone),
        "requested_why": requested_why(case),
        "confirmed_local": local_to_eastern(case.confirmed_local, case.vendor_timezone),
        "tendered_pickup_at": _iso(case.tendered_pickup_utc),
        "miles": case.miles,
        "reason": case.reason,
        "facility_key": case.facility_key,
        "references": [n.as_dict() for n in case_numbers(case)],
        "exceptions": [exception_view(e) for e in case.exceptions],
        "messages": [_message_view(m, case.vendor_timezone) for m in _in_order(case.messages)],
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
    eastern_today = to_eastern(now).date()
    today = eastern_today.isoformat()
    horizon = (eastern_today + timedelta(days=days)).isoformat()
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


def mail_view(item: UnmatchedMail) -> dict[str, Any]:
    """One email the agent could not tie to a pickup, for a person to link or dismiss."""
    return {
        "id": item.id,
        "from": item.from_addr,
        "to": item.to_addr,
        "cc": item.cc_addr,
        "subject": item.subject,
        "body": item.body,
        "sent_at": _iso(item.sent_at),
        "reason": item.reason,
        "customer": item.customer_key,
        "status": item.status,
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
    """Who approves the vendor's confirmation (the signed-in person, when there is sign-in)."""

    by: Who | None = None


class Resolution(BaseModel):
    """Who resolved which exception, and how."""

    by: Who | None = None
    kind: ExceptionType
    note: Note


class Booking(BaseModel):
    """A pickup booked outside the agent."""

    by: Who | None = None
    via: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    date: Day | None = None
    time: Clock | None = None
    pickup_number: str | None = None
    note: str | None = None
    desk: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


class Reference(BaseModel):
    """A number the vendor's desk needs, added by a person."""

    by: Who | None = None
    kind: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=32)]
    value: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)]


class Cancellation(BaseModel):
    """Who cancels the case, and why."""

    by: Who | None = None
    reason: Note


class MailLink(BaseModel):
    """Which pickup an email the agent could not match belongs to."""

    by: Who | None = None
    case_id: int


class MailDismissal(BaseModel):
    """Why an email the agent could not match needs nothing."""

    by: Who | None = None
    note: Note


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


def _key(settings: Settings) -> Callable[[BookingCase], str]:
    """The customer file's key for a case (``default`` when no file claims it)."""
    known = customers(settings)
    return lambda case: known.for_case(case).key


def _visible(session: Session, viewer: Viewer, settings: Settings) -> list[BookingCase]:
    """Every case the viewer may see: all for an admin, else their customers' only."""
    cases = _load_all(session)
    if viewer.admin:
        return cases
    key = _key(settings)
    return [c for c in cases if viewer.sees(key(c))]


def _get_case(
    session: Session, case_id: int, viewer: Viewer, settings: Settings, *, act: bool = False
) -> BookingCase:
    """One case the viewer may see (404 for another customer's, as for none), or act on (403)."""
    case = session.get(BookingCase, case_id)
    key = _key(settings)(case) if case is not None and not viewer.admin else ""
    if case is None or not viewer.sees(key):
        raise HTTPException(status_code=404, detail=f"case {case_id} not found")
    if act and not viewer.acts(key):
        raise HTTPException(
            status_code=403,
            detail="you can see this customer's pickups but not act on them; ask an admin",
        )
    return case


def _sees_case(session: Session, viewer: Viewer, settings: Settings) -> Callable[[int], bool]:
    """Whether the viewer may see the case with this id."""
    key = _key(settings)

    def sees(case_id: int) -> bool:
        other = session.get(BookingCase, case_id)
        return other is not None and viewer.sees(key(other))

    return sees


def _detail(
    session: Session, case: BookingCase, now: datetime, viewer: Viewer, settings: Settings
) -> dict[str, Any]:
    shown = None if viewer.admin else _sees_case(session, viewer, settings)
    detail = case_detail(
        case, now=now, desks=desk_history(session, case.facility_key), shown_cases=shown
    )
    detail["can_act"] = viewer.admin or viewer.acts(_key(settings)(case))
    return detail


def _decided(
    session: Session, case: BookingCase, now: datetime, viewer: Viewer, settings: Settings
) -> dict[str, Any]:
    session.commit()  # the decision is stored before anyone is told it was made
    return _detail(session, case, now, viewer, settings)


@router.get("/overview")
def get_overview(
    request: Request,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
    *,
    customer: str | None = None,
    days: Annotated[int, Query(ge=1, le=60)] = 7,
) -> dict[str, Any]:
    """Counts, open to-dos, past-due pickups and the next days' pickups.

    ``scan`` is when this server last checked Transport Pro for new pickups and whether that
    worked (``serve --scan-every``); None when it does not check on its own.
    """
    cases = [
        c
        for c in _visible(session, viewer, settings)
        if not customer or c.customer_name == customer
    ]
    data = overview(cases, now=now, days=days)
    data["mail"] = [mail_view(m) for m in open_unmatched(session) if _sees_mail(viewer, m)]
    data["counts"]["unmatched_mail"] = len(data["mail"])
    data["scan"] = getattr(request.app.state, "scan", None)
    data["mail_check"] = getattr(request.app.state, "mail_check", None)
    data["updates"] = latest_updates(cases, now=now, internal=settings.internal_email_domains)
    return data


@router.get("/today")
def get_today(
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
    customer: str | None = None,
) -> dict[str, Any]:
    """The daily summary: what needs a person, today's pickups, drafts to send.

    ``text`` is the same summary as plain text, ready to paste into an email or a chat.
    """
    data = today_summary(
        _visible(session, viewer, settings),
        now=now,
        timezone=settings.booking_timezone,
        customer=customer,
    )
    return {**data, "text": render_today(data)}


@router.get("/kinds")
def get_kinds(_viewer: ViewerDep) -> list[dict[str, str]]:
    """Every exception kind with its label and what to do about it, for the board's filter."""
    return [
        {"kind": kind.value, "label": KINDS[kind.value][0], "hint": KINDS[kind.value][1]}
        for kind in ExceptionType
    ]


@router.get("/customers")
def get_customers(session: SessionDep, settings: SettingsDep, viewer: ViewerDep) -> list[str]:
    """Customer names on the cases the viewer may see, for the board's filter."""
    if viewer.admin:
        names = set(session.scalars(select(BookingCase.customer_name).distinct()))
    else:
        names = {c.customer_name for c in _visible(session, viewer, settings)}
    return sorted(n for n in names if n)


@router.get("/cases")
def get_cases(
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
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
    rows = [case_summary(c, now=now) for c in _visible(session, viewer, settings)]
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
def get_case_detail(
    case_id: int, session: SessionDep, now: NowDep, settings: SettingsDep, viewer: ViewerDep
) -> dict[str, Any]:
    """One case in full, with how its facility was booked before."""
    return _detail(session, _get_case(session, case_id, viewer, settings), now, viewer, settings)


@router.post("/cases/{case_id}/approve")
def post_approve(
    case_id: int,
    body: Approval,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Approve the vendor's confirmation; the slot is queued for Transport Pro."""
    by = decided_by(viewer, body.by)
    case = _get_case(session, case_id, viewer, settings, act=True)
    try:
        approve(session, case, by=by)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decided(session, case, now, viewer, settings)


@router.post("/cases/{case_id}/resolve")
def post_resolve(
    case_id: int,
    body: Resolution,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Resolve one open exception with a note."""
    by = decided_by(viewer, body.by)
    case = _get_case(session, case_id, viewer, settings, act=True)
    if not resolve(session, case, [body.kind], resolution=body.note, by=by):
        raise HTTPException(status_code=409, detail=f"case {case_id} has no open {body.kind.value}")
    return _decided(session, case, now, viewer, settings)


@router.post("/cases/{case_id}/booked")
def post_booked(
    case_id: int,
    body: Booking,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Record a pickup booked outside the agent; the case becomes scheduled."""
    by = decided_by(viewer, body.by)
    if body.time and not body.date:
        raise HTTPException(status_code=422, detail="a pickup time needs a date")
    case = _get_case(session, case_id, viewer, settings, act=True)
    eastern = f"{body.date} {body.time}" if body.date and body.time else body.date
    local = eastern_to_local(eastern, case.vendor_timezone)  # people give times in Eastern
    try:
        mark_booked(
            session,
            case,
            by=by,
            via=body.via,
            local=local,
            pickup_number=(body.pickup_number or "").strip() or None,
            note=(body.note or "").strip() or None,
            desk=body.desk or None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decided(session, case, now, viewer, settings)


@router.post("/cases/{case_id}/reference")
def post_reference(
    case_id: int,
    body: Reference,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Add a number the desk needs (the customer's shipment or SO number) to the case."""
    by = decided_by(viewer, body.by)
    case = _get_case(session, case_id, viewer, settings, act=True)
    try:
        add_reference(session, case, body.kind, body.value, by=by)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _decided(session, case, now, viewer, settings)


@router.get("/references")
def get_references(_viewer: ViewerDep) -> list[dict[str, str]]:
    """Every reference type with how a request writes it and its plain name."""
    return [
        {"kind": kind, "label": label, "name": REFERENCE_NAMES.get(kind, kind)}
        for kind, label in REFERENCE_LABELS.items()
    ]


@router.post("/cases/{case_id}/cancel")
def post_cancel(
    case_id: int,
    body: Cancellation,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Cancel a case that is no longer needed."""
    by = decided_by(viewer, body.by)
    case = _get_case(session, case_id, viewer, settings, act=True)
    if case.status == CaseStatus.CANCELED.value:
        raise HTTPException(status_code=409, detail=f"case {case_id} is already canceled")
    close_case(session, case, by=by, reason=body.reason)
    return _decided(session, case, now, viewer, settings)


# ------------------------------------------------------------------ mail no pickup matched


def _sees_mail(viewer: Viewer, item: UnmatchedMail) -> bool:
    return viewer.sees(item.customer_key or "default")


def _get_mail(session: Session, mail_id: int, viewer: Viewer) -> UnmatchedMail:
    """An email the viewer may act on (404 when they may not see it, 403 when only see it)."""
    item = session.get(UnmatchedMail, mail_id)
    key = (item.customer_key or "default") if item is not None else ""
    if item is None or not viewer.sees(key):
        raise HTTPException(status_code=404, detail=f"mail {mail_id} not found")
    if not viewer.acts(key):
        raise HTTPException(
            status_code=403,
            detail="you can see this customer's mail but not act on it; ask an admin",
        )
    return item


@router.get("/mail")
def get_mail(session: SessionDep, viewer: ViewerDep) -> list[dict[str, Any]]:
    """Booking mail no pickup matched, newest first, for a person to link or dismiss."""
    return [mail_view(m) for m in open_unmatched(session) if _sees_mail(viewer, m)]


@router.post("/mail/{mail_id}/link")
def post_mail_link(
    mail_id: int,
    body: MailLink,
    *,
    session: SessionDep,
    now: NowDep,
    settings: SettingsDep,
    viewer: ViewerDep,
) -> dict[str, Any]:
    """Tie an email to its pickup; the agent reads it as that pickup's reply."""
    by = decided_by(viewer, body.by)
    item = _get_mail(session, mail_id, viewer)
    case = _get_case(session, body.case_id, viewer, settings, act=True)
    classifier, _ = reader_tools(settings)
    try:
        link_unmatched(session, item, case, by=by, classifier=classifier, settings=settings)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _decided(session, case, now, viewer, settings)


@router.post("/mail/{mail_id}/dismiss")
def post_mail_dismiss(
    mail_id: int, body: MailDismissal, *, session: SessionDep, viewer: ViewerDep
) -> dict[str, Any]:
    """Say an email the agent could not match needs nothing."""
    by = decided_by(viewer, body.by)
    item = _get_mail(session, mail_id, viewer)
    try:
        dismiss_unmatched(session, item, by=by, note=body.note)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    session.commit()
    return mail_view(item)
