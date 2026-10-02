"""What needs a person: raising, resolving and annotating case exceptions, and old-store upgrades.

A case's status says where the appointment stands; this module records what a person has to do
about it. The agent raises an exception wherever it would otherwise park the case for a person,
and resolves it when the situation clears (an answered question, an accepted offer, a pickup
asked for again). Its timers (``booking/timers.py``) raise what time alone brings: a vendor's
silence, a pickup time that passed unbooked. A person resolves one with a note (``booking
resolve``).

Stores written before the split kept those attention states in the status column
(``needs_profile``, ``needs_human``, ``proposed`` ...). :func:`migrate_legacy_statuses` moves such
cases onto the current statuses and opens the matching exceptions; ``init_db`` runs it.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    CaseException,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.logging import get_logger

log = get_logger(__name__)

# What each exception means to the person who has to act on it, and what to do about it. The
# board and the daily summary both use these words.
KINDS: dict[str, tuple[str, str]] = {
    "missing_method": (
        "No booking desk",
        "Find the vendor's appointment desk, or book by phone and mark it booked.",
    ),
    "method_not_supported": (
        "Books by portal or phone",
        "Book it on the vendor's portal or by phone, then mark it booked.",
    ),
    "missing_reference": (
        "Reference needed",
        "The desk will not book without this number. Get it from the customer and add it to "
        "the case; the request is drafted with it.",
    ),
    "tpro_mismatch": (
        "Transport Pro has another time",
        "Transport Pro already shows a different confirmed appointment for this pickup, and the "
        "agent does not overwrite it. Check with the vendor which is right, then fix Transport Pro "
        "or update the booking.",
    ),
    "automation_failed": (
        "Automation failed",
        "The agent could not write this request on its own. Read the error, fix what it names, "
        "then draft it with booking draft or resolve this to let the agent try again.",
    ),
    "load_infeasible": (
        "Cannot make the delivery",
        "No pickup the desk would take gets the load there in time. Ask the customer to move the "
        "delivery, or the vendor for an earlier pickup, then reschedule or mark it booked.",
    ),
    "slot_unworkable": (
        "Slot will not work",
        "Pick a new pickup slot with the vendor, or ask the customer to move the delivery.",
    ),
    "confirmation_review": (
        "Approve confirmation",
        "Check the vendor's reply below, then approve the slot.",
    ),
    "proposed_time_review": (
        "Vendor offered another time",
        "Reply to the vendor to accept or ask for another time; mark it booked once agreed.",
    ),
    "facility_question": ("Vendor question", "Answer the vendor, then resolve this."),
    "facility_declined": (
        "Vendor cannot book",
        "Ask the customer to move the delivery or find the vendor another day.",
    ),
    "stale_confirmation": (
        "Late confirmation",
        "The slot had passed when the vendor wrote; read it as a work-in note and rebook.",
    ),
    "delivery_moved": (
        "Delivery moved",
        "Ask the vendor for a pickup that makes the new delivery.",
    ),
    "handoff": ("Handed to a person", "Read the thread and decide the next step."),
    "unanswered_24h": (
        "No reply in 24 h",
        "Chase the vendor: send the follow-up or call the desk. This clears when they answer.",
    ),
    "unanswered_48h": (
        "No reply in 48 h",
        "Call the vendor's desk; if they book by phone, mark it booked with the slot.",
    ),
    "pickup_expired": (
        "Pickup time passed",
        "If the truck picked up, mark it booked; if it still has to move, agree a new day "
        "with the vendor; if it is no longer needed, cancel it.",
    ),
    "confirmed_outside_window": (
        "Confirmed a different time",
        "The vendor confirmed a time we did not ask for. Check it still makes the delivery "
        "before you approve it.",
    ),
}
# The vendor's silence: raised by the no-reply timers, cleared by any answer from the vendor.
UNANSWERED: frozenset[ExceptionType] = frozenset(
    {ExceptionType.UNANSWERED_24H, ExceptionType.UNANSWERED_48H}
)
# Everything the timers raise. Booking the pickup settles all of them.
TIMER_KINDS: frozenset[ExceptionType] = UNANSWERED | {ExceptionType.PICKUP_EXPIRED}
# A reply about the slot (a confirmation, an offer, a deferral, a decline) replaces whatever an
# earlier reply left open about it, and an open question with it: the vendor has moved on. Any
# answer also ends the vendor's silence.
SLOT_REPLY_SUPERSEDES: frozenset[ExceptionType] = UNANSWERED | {
    ExceptionType.CONFIRMATION_REVIEW,
    ExceptionType.CONFIRMED_OUTSIDE_WINDOW,
    ExceptionType.PROPOSED_TIME_REVIEW,
    ExceptionType.FACILITY_DECLINED,
    ExceptionType.STALE_CONFIRMATION,
    ExceptionType.FACILITY_QUESTION,
    ExceptionType.HANDOFF,
}
# A question replaces an earlier open question only. "Which carrier?" after a confirmation
# leaves the confirmation waiting for approval; after a decline, the decline stays open.
QUESTION_SUPERSEDES: frozenset[ExceptionType] = UNANSWERED | {
    ExceptionType.FACILITY_QUESTION,
    ExceptionType.HANDOFF,
}
# Booking methods a profile can name that the agent cannot use: it only books by email.
MANUAL_METHODS = {
    "web_portal": "books on a web portal",
    "phone": "books by phone",
    "fcfs": "takes trucks first come, first served",
    "preset_by_customer": "has its appointments set by the customer",
}


def open_exceptions(case: BookingCase, *kinds: ExceptionType) -> list[CaseException]:
    """Open exceptions on the case, oldest first; only those kinds when kinds are given."""
    wanted = {k.value for k in kinds}
    return [e for e in case.open_exceptions if not wanted or e.kind in wanted]


def open_kinds(case: BookingCase) -> list[str]:
    """The kinds of the open exceptions, oldest first."""
    return [e.kind for e in case.open_exceptions]


def flag(
    session: Session,
    case: BookingCase,
    kind: ExceptionType,
    description: str,
    *,
    actor: str = "agent",
    at: datetime | None = None,
    **detail: Any,
) -> CaseException:
    """Raise an exception on the case; an open one of the same kind is refreshed instead.

    ``at`` dates a new exception (the timers pass their clock); the default is now.
    """
    current = open_exceptions(case, kind)
    if current:
        exc = current[0]
        exc.description = description[:255]
        exc.detail = {**exc.detail, **detail}
        session.flush()
        return exc
    exc = CaseException(
        kind=kind.value, description=description[:255], detail=detail, raised_by=actor
    )
    if at is not None:
        exc.raised_at = at
    case.exceptions.append(exc)
    session.flush()
    return exc


def _close(
    session: Session,
    exceptions: list[CaseException],
    *,
    resolution: str,
    by: str,
    at: datetime | None = None,
) -> list[str]:
    when = at or datetime.now(tz=UTC)
    for exc in exceptions:
        exc.resolved_at = when
        exc.resolved_by = by
        exc.resolution = resolution[:255]
    if exceptions:
        session.flush()
    return [e.kind for e in exceptions]


def resolve(
    session: Session,
    case: BookingCase,
    kinds: Iterable[ExceptionType],
    *,
    resolution: str,
    by: str = "agent",
    at: datetime | None = None,
) -> list[str]:
    """Resolve the open exceptions of those kinds; return the kinds resolved."""
    return _close(session, open_exceptions(case, *kinds), resolution=resolution, by=by, at=at)


def resolve_all(
    session: Session,
    case: BookingCase,
    *,
    resolution: str,
    by: str = "agent",
    at: datetime | None = None,
) -> list[str]:
    """Resolve every open exception on the case; return the kinds resolved."""
    return _close(session, case.open_exceptions, resolution=resolution, by=by, at=at)


def annotate(session: Session, case: BookingCase, note: str) -> CaseException | None:
    """Add the agent's note to the most recently raised open exception; None when none is open."""
    current = case.open_exceptions
    if not current:
        return None
    exc = current[-1]
    exc.description = f"{exc.description} | agent: {note}"[:255]
    exc.detail = {**exc.detail, "agent": note}
    session.flush()
    return exc


def method_exception(
    booking_method: str | None, *, portal_vendor: str | None = None, portal_url: str | None = None
) -> tuple[ExceptionType, str]:
    """Why the agent cannot email this vendor: a method it cannot use, or no desk at all.

    A portal desk is named with its system and address, so the person knows where to book.
    """
    phrase = MANUAL_METHODS.get(booking_method or "")
    if phrase and booking_method == "web_portal" and (portal_vendor or portal_url):
        system = portal_vendor if portal_vendor and portal_vendor != "other" else "a web portal"
        phrase = f"books on {system.replace('_', ' ')}" + (f" ({portal_url})" if portal_url else "")
    if phrase:
        return ExceptionType.METHOD_NOT_SUPPORTED, f"{phrase}; the agent only books by email"
    return ExceptionType.MISSING_METHOD, "no verified email booking desk on the profile"


# ------------------------------------------------------------------ stores from before the split

LEGACY_STATUSES: frozenset[str] = frozenset(
    {
        "new",
        "needs_profile",
        "already_booked",
        "drafted",
        "sent",
        "proposed",
        "needs_human",
        "approved",
        "closed",
    }
)
# What parked a needs_human case, read from the latest event that explains it.
_LEGACY_CAUSES: dict[str, ExceptionType] = {
    "question": ExceptionType.FACILITY_QUESTION,
    "counter_offer": ExceptionType.PROPOSED_TIME_REVIEW,
    "rejected_by_vendor": ExceptionType.FACILITY_DECLINED,
    "escalate_to_customer": ExceptionType.FACILITY_DECLINED,
    "stale_confirmation": ExceptionType.STALE_CONFIRMATION,
    "stale_slot": ExceptionType.SLOT_UNWORKABLE,
    "po_date_floor": ExceptionType.SLOT_UNWORKABLE,
    "delivery_updated": ExceptionType.DELIVERY_MOVED,
}
# "closed" covered both "not needed" and "booked another way"; the second is a booking.
_BOOKED_ELSEWHERE = re.compile(r"\b(?:already booked|booked (?:by|on|via|in|through|with))\b", re.I)


@dataclass(frozen=True)
class LegacyPlan:
    """Where a case written with the old statuses lands."""

    status: CaseStatus
    reason: str | None
    exception: tuple[ExceptionType, str] | None = None


def _last_action(case: BookingCase, actions: set[str]) -> str | None:
    return next((e.action for e in reversed(case.events) if e.action in actions), None)


def _legacy_cause(case: BookingCase) -> ExceptionType:
    for event in reversed(case.events):
        kind = _LEGACY_CAUSES.get(event.action)
        if kind is None:
            continue
        if event.action == "po_date_floor" and (event.detail or {}).get("feasible", True):
            continue  # a floor that still made the delivery only moved the ask
        return kind
    return ExceptionType.HANDOFF


def legacy_plan(case: BookingCase) -> LegacyPlan:
    """Map a case on an old status onto a current status, plus an exception if it was parked.

    The exception carries the old reason as its description, so nothing a person wrote or the
    agent explained is lost.
    """
    old, reason = case.status, case.reason
    if old in ("new", "drafted"):
        return LegacyPlan(CaseStatus.UNSCHEDULED, reason)
    if old == "sent":
        return LegacyPlan(CaseStatus.PENDING, reason)
    if old in ("already_booked", "approved"):
        return LegacyPlan(CaseStatus.SCHEDULED, reason)
    if old == "closed":
        booked = bool(reason and _BOOKED_ELSEWHERE.search(reason))
        return LegacyPlan(CaseStatus.SCHEDULED if booked else CaseStatus.CANCELED, reason)
    if old == "needs_profile":
        kind, default = method_exception(case.booking_method)
        return LegacyPlan(CaseStatus.UNSCHEDULED, None, (kind, reason or default))
    if old == "proposed":
        slot = case.confirmed_local or "?"
        what = f"vendor confirmed {slot}"
        if _last_action(case, {"accept_offer", "vendor_confirmed"}) == "accept_offer":
            what = f"vendor offered {slot}; the agent accepted it"
        pickup = f", pickup# {case.pickup_number}" if case.pickup_number else ""
        return LegacyPlan(
            CaseStatus.PENDING, None, (ExceptionType.CONFIRMATION_REVIEW, what + pickup)
        )
    kind = _legacy_cause(case)  # needs_human
    if kind == ExceptionType.FACILITY_DECLINED:
        return LegacyPlan(CaseStatus.DECLINED, reason, (kind, reason or "vendor cannot book"))
    asked = any(m.direction == "in" or m.sent_at is not None for m in case.messages)
    status = CaseStatus.PENDING if asked else CaseStatus.UNSCHEDULED
    return LegacyPlan(status, None, (kind, reason or "handed to a person"))


def migrate_legacy_statuses(session: Session) -> int:
    """Move cases still on the old statuses onto the current ones; return how many moved.

    Each moved case gets the exception its old status implied, dated to when the case last
    changed (when it entered that status), and a ``status_migrated`` event naming both
    statuses. A store with nothing to move is left untouched, so this is safe on every start.
    """
    cases = list(
        session.scalars(select(BookingCase).where(BookingCase.status.in_(sorted(LEGACY_STATUSES))))
    )
    for case in cases:
        old, when = case.status, case.updated_at
        plan = legacy_plan(case)
        case.status = plan.status.value
        case.reason = plan.reason
        if plan.exception is not None:
            kind, description = plan.exception
            exc = flag(session, case, kind, description, actor="migration", legacy_status=old)
            exc.raised_at = when or exc.raised_at
        # Appended, not added by id: legacy_plan may have loaded the collection already.
        case.events.append(
            BookingEvent(
                action="status_migrated",
                actor="migration",
                detail={"from": old, "to": case.status},
            )
        )
    if cases:
        session.flush()
        log.info("booking.statuses_migrated", cases=len(cases))
    return len(cases)
