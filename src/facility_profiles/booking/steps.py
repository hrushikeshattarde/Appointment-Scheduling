"""What the carrier must do once a pickup is booked: the facility's steps, put to a person.

Some facilities set the carrier a task besides turning up on time. Morgan Foods refuses a driver
who is not registered in its Eaigle gate system, and registration opens 48 hours before the
pickup. A person files such steps on the facility's profile (``carrier_steps``, with ``profile
set``); a booked pickup there raises **Tell the carrier** (``carrier_steps``) with each step, and
for a step that cannot be done sooner the time it opens, on the Eastern clock:

- once per booked time and carrier: a person passes it on and marks it done;
- naming the carrier once the scan has seen one on the load (``booking/coverage.py``); a carrier
  that replaces the one told raises it again, because the new one was never told;
- cleared on its own when the pickup is moved, canceled or picked up, or its time has passed.

The timers run it (:func:`sweep_steps`, from ``booking/timers.py``). The steps also go on the
load's note when the appointment is written to Transport Pro (``booking/writeback.py``). Nothing
is sent to anyone.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from facility_profiles.booking.coverage import CARRIER_KEY, PICKED_UP, booked_slot
from facility_profiles.booking.models import (
    BookingCase,
    CaseException,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.rules import VendorProfile, vendor_profile
from facility_profiles.booking.worklist import flag, open_exceptions, resolve
from facility_profiles.clock import slot_text, stamp
from facility_profiles.storage.repository import Repository

ACTOR = "agent"
KIND = ExceptionType.CARRIER_STEPS


@dataclass
class StepsResult:
    """What one run did: (case id, kind, description or why) per change."""

    raised: list[tuple[int, str, str]] = field(default_factory=list)
    resolved: list[tuple[int, str, str]] = field(default_factory=list)


def step_lines(steps: list[dict[str, Any]], slot: datetime) -> list[str]:
    """Each step in words, with the time it opens for a step that cannot be done sooner."""
    lines: list[str] = []
    for step in steps:
        words = str(step.get("step") or "").strip().rstrip(".")
        if not words:
            continue
        hours = step.get("hours_before")
        if isinstance(hours, int) and hours > 0:
            opens = stamp(slot - timedelta(hours=hours), "%a %m/%d %H:%M")
            words = f"{words} (not before {opens})"
        lines.append(words)
    return lines


def told_steps(case: BookingCase) -> list[str]:
    """The steps raised for the booked time now on the case, for the load's note."""
    local, _ = booked_slot(case)
    told = _told(case, local)
    return list((told[-1].detail or {}).get("steps") or []) if told else []


def check_steps(
    session: Session,
    case: BookingCase,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
    result: StepsResult | None = None,
) -> StepsResult:
    """Raise, name the carrier on, or clear the carrier's steps for one booked pickup."""
    result = result if result is not None else StepsResult()
    local, at = booked_slot(case)
    done = _not_needed(case, at, now)
    if done is None:
        for exc in open_exceptions(case, KIND):
            if (exc.detail or {}).get("slot") != local:
                done = f"the pickup is now {slot_text(local, case.vendor_timezone)}"
    if done is not None:
        for kind in resolve(session, case, [KIND], resolution=done, by=ACTOR, at=now):
            result.resolved.append((case.id, kind, done))
    if at is None or _not_needed(case, at, now) is not None:
        return result
    if profile is None and case.facility_key:
        profile = vendor_profile(Repository(session), case.facility_key)
    lines = step_lines(profile.carrier_steps, at) if profile else []
    if not lines:
        return result
    seen = (case.tpro_seen or {}).get(CARRIER_KEY)
    dispatch = seen.get("dispatch") if isinstance(seen, dict) else None
    name = seen.get("name") if isinstance(seen, dict) else None
    told = _told(case, local)
    replaced = None
    if told:
        wait, replaced = _since_told(told[-1], dispatch)
        if wait:
            return result
    who = name or "the carrier"
    head = f"{who} replaced {replaced}: tell {who}" if replaced else f"Tell {who}"
    text = f"{head}: " + "; ".join(lines)
    new = not open_exceptions(case, KIND)
    flag(
        session,
        case,
        KIND,
        text,
        actor=ACTOR,
        at=now,
        slot=local,
        dispatch=dispatch,
        carrier=name,
        steps=lines,
    )
    if new or replaced:
        result.raised.append((case.id, KIND.value, text[:255]))
    return result


def sweep_steps(session: Session, *, now: datetime) -> StepsResult:
    """Check the steps of every booked pickup not yet past, and of any with its to-do open."""
    raised = select(CaseException.case_id).where(
        CaseException.resolved_at.is_(None), CaseException.kind == KIND.value
    )
    stmt = (
        select(BookingCase)
        .where(
            or_(
                (BookingCase.status == CaseStatus.SCHEDULED.value)
                & BookingCase.facility_key.is_not(None)
                & (
                    BookingCase.confirmed_start_utc.is_(None)
                    | (BookingCase.confirmed_start_utc >= now - timedelta(days=1))
                ),
                BookingCase.id.in_(raised),
            )
        )
        .order_by(BookingCase.id)
    )
    result = StepsResult()
    profiles: dict[str, VendorProfile] = {}
    repo = Repository(session)
    for case in session.scalars(stmt):
        profile = None
        if case.facility_key:
            if case.facility_key not in profiles:
                profiles[case.facility_key] = vendor_profile(repo, case.facility_key)
            profile = profiles[case.facility_key]
        check_steps(session, case, now=now, profile=profile, result=result)
    return result


def _not_needed(case: BookingCase, at: datetime | None, now: datetime) -> str | None:
    """Why a booked pickup no longer needs its steps passed on, or None when it does."""
    if case.status != CaseStatus.SCHEDULED.value:
        return f"case is {case.status}"
    if str((case.tpro_seen or {}).get("load_status") or "").lower() in PICKED_UP:
        return "the load is picked up"
    if at is None:
        return "no booked time on the case"
    if now >= at:
        return "the pickup time passed"
    return None


def _since_told(last: CaseException, dispatch: Any) -> tuple[bool, str | None]:
    """Whether to leave the steps told last as they are, and the carrier a new one replaced."""
    detail = last.detail or {}
    if dispatch is None or dispatch == detail.get("dispatch"):
        return True, None  # no carrier yet, or the one already told
    if detail.get("dispatch") is None:
        # The first carrier: named on the open to-do; passed on before it, a person put it on
        # the load and it stays done.
        return last.resolved_at is not None, None
    return False, str(detail.get("carrier") or "the carrier told before")


def _told(case: BookingCase, local: str | None) -> list[CaseException]:
    """The steps raised for this booked time, open or done, oldest first."""
    mine = [e for e in case.exceptions if e.kind == KIND.value]
    return sorted(
        (e for e in mine if (e.detail or {}).get("slot") == local), key=lambda e: e.id or 0
    )
