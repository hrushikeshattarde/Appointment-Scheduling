"""What needs a person: raising, resolving and annotating case exceptions, and old-store upgrades.

A case's status says where the appointment stands; this module records what a person has to do
about it. The agent raises an exception wherever it would otherwise park the case for a person,
and resolves it when the situation clears (an answered question, an accepted offer, a pickup
asked for again). A person resolves one with a note (``booking resolve``).

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

# A reply about the slot (a confirmation, an offer, a deferral, a decline) replaces whatever an
# earlier reply left open about it, and an open question with it: the vendor has moved on.
SLOT_REPLY_SUPERSEDES: frozenset[ExceptionType] = frozenset(
    {
        ExceptionType.CONFIRMATION_REVIEW,
        ExceptionType.PROPOSED_TIME_REVIEW,
        ExceptionType.FACILITY_DECLINED,
        ExceptionType.STALE_CONFIRMATION,
        ExceptionType.FACILITY_QUESTION,
        ExceptionType.HANDOFF,
    }
)
# A question replaces an earlier open question only. "Which carrier?" after a confirmation
# leaves the confirmation waiting for approval; after a decline, the decline stays open.
QUESTION_SUPERSEDES: frozenset[ExceptionType] = frozenset(
    {ExceptionType.FACILITY_QUESTION, ExceptionType.HANDOFF}
)
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
    **detail: Any,
) -> CaseException:
    """Raise an exception on the case; an open one of the same kind is refreshed instead."""
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
    case.exceptions.append(exc)
    session.flush()
    return exc


def _close(
    session: Session, exceptions: list[CaseException], *, resolution: str, by: str
) -> list[str]:
    now = datetime.now(tz=UTC)
    for exc in exceptions:
        exc.resolved_at = now
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
) -> list[str]:
    """Resolve the open exceptions of those kinds; return the kinds resolved."""
    return _close(session, open_exceptions(case, *kinds), resolution=resolution, by=by)


def resolve_all(
    session: Session, case: BookingCase, *, resolution: str, by: str = "agent"
) -> list[str]:
    """Resolve every open exception on the case; return the kinds resolved."""
    return _close(session, case.open_exceptions, resolution=resolution, by=by)


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


def method_exception(booking_method: str | None) -> tuple[ExceptionType, str]:
    """Why the agent cannot email this vendor: a method it cannot use, or no desk at all."""
    phrase = MANUAL_METHODS.get(booking_method or "")
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
