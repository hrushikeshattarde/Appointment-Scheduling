"""Timers: what the agent raises on its own as time passes.

The clocks run over the cases still being booked (:func:`sweep`; ``booking timers`` and
``booking today`` run it, and ``serve`` runs it every few minutes):

- **No reply.** Once a request (or any later message) has gone to the vendor and nothing has
  come back, ``unanswered_24h`` is raised after 24 hours and ``unanswered_48h``, which replaces
  it, after 48. Only Monday-to-Friday hours in the vendor's time zone count: desks do not answer
  at weekends, so a request sent on Friday afternoon is not "48 hours unanswered" on Monday
  morning. The clock starts at the first message the vendor has not answered; a follow-up does
  not restart it. A case that waits on us (a confirmation to approve, a question to answer) is
  not the vendor's silence, and an out-of-office or unrelated reply is not an answer.
- **Expiry.** ``pickup_expired`` is raised when the pickup the case is working towards has
  passed and the case is still not booked. That pickup is the confirmed slot, else a time the
  vendor offered that still waits for a person, else the slot asked for.
- **The desk's rules.** With settings, a request that has not gone out yet is checked against
  its desk's rules again (``booking/rules.py``): a cut-off or notice that passed overnight is
  raised as ``slot_unworkable``, a number the desk now requires as ``missing_reference``; a
  case still waiting for a desk takes the one the profile has since learned.
- **The carrier's steps.** A booked pickup at a facility that sets the carrier a task (gate
  registration) raises it for a person to pass on (``booking/steps.py``).

Each raise remembers what started its clock (the message that went unanswered, the slot that
passed), so a person's resolution sticks: the same silence or the same slot is never raised
twice. When the situation clears (the vendor answers, the pickup moves to a later time, the case
is booked or canceled) the sweep resolves the exception itself.

The timers only write to-dos on the store. They never send mail and never write to Transport Pro.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, selectinload

from facility_profiles.booking.memory import take_desk
from facility_profiles.booking.models import (
    PERSON_MAIL,
    BookingCase,
    BookingMessage,
    CaseException,
    CaseStatus,
    ExceptionType,
    is_person_request,
)
from facility_profiles.booking.rules import check_desk_rules, vendor_profile
from facility_profiles.booking.worklist import (
    TIMER_KINDS,
    UNANSWERED,
    flag,
    open_exceptions,
    resolve,
)
from facility_profiles.business_days import is_business_day
from facility_profiles.clock import slot_text, stamp
from facility_profiles.config import Settings
from facility_profiles.logging import get_logger
from facility_profiles.storage.repository import Repository, as_utc

log = get_logger(__name__)

TIMER = "timer"  # who raised it, as the board and the summary show it
# Still to be booked: the statuses the timers watch (and the board calls past due).
UNBOOKED = (CaseStatus.UNSCHEDULED.value, CaseStatus.PENDING.value, CaseStatus.DECLINED.value)
# Longest silence first: the first limit reached is the one raised.
UNANSWERED_LIMITS: tuple[tuple[ExceptionType, int], ...] = (
    (ExceptionType.UNANSWERED_48H, 48),
    (ExceptionType.UNANSWERED_24H, 24),
)
_UNANSWERED = frozenset(k.value for k in UNANSWERED)
# Notes to the customer's desk are not a question to the vendor.
NOT_TO_VENDOR = frozenset({"escalate_to_customer", "notify_customer_desk", PERSON_MAIL})
# Open exceptions that already say what to do about a slot that will not happen: an expiry on
# top of them would only repeat it.
EXPIRY_DEFERS_TO = frozenset(
    {
        ExceptionType.STALE_CONFIRMATION.value,
        ExceptionType.FACILITY_DECLINED.value,
        ExceptionType.DELIVERY_MOVED.value,
        ExceptionType.LOAD_INFEASIBLE.value,
        ExceptionType.ON_HOLD.value,
        ExceptionType.WORK_IN_OFFERED.value,
    }
)
_SENT_WHAT = {
    "request": "the request",
    "reschedule": "the reschedule request",
    "follow_up": "the follow-up",
}


# ------------------------------------------------------------------ slots and clocks


def tz_of(case: BookingCase) -> ZoneInfo:
    """The vendor's time zone (pickups are written in it)."""
    return ZoneInfo(case.vendor_timezone or "America/New_York")


def slot_at(local: str | None, timezone: str | None) -> datetime | None:
    """A vendor-local slot ("YYYY-MM-DD" or "YYYY-MM-DD HH:MM") as an aware datetime.

    A date-only slot counts until the end of its day.
    """
    if not local:
        return None
    day, _, clock = local.partition(" ")
    try:
        naive = datetime.strptime(f"{day} {clock or '23:59'}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(timezone or "America/New_York"))


def fmt_slot(local: str | None, timezone: str | None = None, *, label: bool = True) -> str:
    """A slot for people, on the Eastern clock: "2026-10-01 09:00" becomes "Thu 10/01 09:00 ET".

    ``local`` is on the facility's clock, in ``timezone`` (Eastern when not given).
    """
    return slot_text(local, timezone, label=label)


def target_slot(case: BookingCase) -> tuple[str | None, str]:
    """The pickup the case is working towards, and where it comes from.

    The confirmed slot when there is one; else the time a vendor offered, while the offer waits
    for a person; else the slot asked for.
    """
    if case.confirmed_local:
        return case.confirmed_local, "confirmed"
    for exc in reversed(open_exceptions(case, ExceptionType.PROPOSED_TIME_REVIEW)):
        detail = exc.detail or {}
        if detail.get("date"):
            return f"{detail['date']} {detail.get('time') or ''}".strip(), "offered"
    return case.requested_local, "requested"


def pickup_passed(case: BookingCase, now: datetime) -> bool:
    """True when the case is not booked and the pickup it is working towards has passed."""
    if case.status not in UNBOOKED:
        return False
    local, _ = target_slot(case)
    at = slot_at(local, case.vendor_timezone)
    return at is not None and at <= now


def weekday_hours(start: datetime, end: datetime, tz: ZoneInfo) -> float:
    """Hours between two instants that fall on a business day in ``tz`` (no weekends, holidays)."""
    total = 0.0
    cursor, stop = start.astimezone(UTC), end.astimezone(UTC)
    while cursor < stop:
        local = cursor.astimezone(tz)
        midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
        chunk_end = min(midnight.astimezone(UTC), stop)
        if is_business_day(local.date()):
            total += (chunk_end - cursor).total_seconds() / 3600
        cursor = chunk_end
    return total


def after_weekday_hours(start: datetime, hours: float, tz: ZoneInfo) -> datetime:
    """The instant ``hours`` weekday hours after ``start`` in ``tz``; weekends do not count.

    A link sent on Friday morning with 72 hours to live lasts into Wednesday, not Monday morning.
    """
    left = timedelta(hours=hours)
    cursor = start.astimezone(UTC)
    while left > timedelta(0):
        local = cursor.astimezone(tz)
        midnight = datetime.combine(local.date() + timedelta(days=1), time(0), tzinfo=tz)
        next_day = midnight.astimezone(UTC)
        if is_business_day(local.date()):
            if next_day - cursor >= left:
                return cursor + left
            left -= next_day - cursor
        cursor = next_day
    return cursor


def is_vendor_answer(message: BookingMessage) -> bool:
    """An inbound reply read as being about this case, or a time picked from a link."""
    reading = message.classification or {}
    return (
        message.direction == "in"
        and message.kind in ("reply", "link")
        and reading.get("status") not in (None, "unrelated")
    )


def waiting_since(case: BookingCase) -> BookingMessage | None:
    """The first message sent to the vendor that is still unanswered, or None.

    That is the earliest sent message to the vendor after their last answer. Drafts nobody sent
    do not count: the vendor has not seen them. A request a person emailed themselves counts.
    """
    answers = [
        t for m in case.messages if is_vendor_answer(m) if (t := as_utc(m.sent_at or m.created_at))
    ]
    last_answer = max(answers, default=None)
    unanswered = [
        (sent, m)
        for m in case.messages
        if m.direction == "out" and (m.kind not in NOT_TO_VENDOR or is_person_request(m))
        if (sent := as_utc(m.sent_at)) is not None
        if last_answer is None or sent > last_answer
    ]
    return min(unanswered, key=lambda pair: pair[0])[1] if unanswered else None


# ------------------------------------------------------------------ the sweep


@dataclass
class SweepResult:
    """What one pass of the timers did: (case id, kind, description or why) per change."""

    cases: int = 0
    raised: list[tuple[int, str, str]] = field(default_factory=list)
    resolved: list[tuple[int, str, str]] = field(default_factory=list)

    def counts(self) -> dict[str, object]:
        """Totals per kind, for logs and the CLI."""
        return {
            "cases": self.cases,
            "raised": dict(Counter(kind for _, kind, _ in self.raised)),
            "resolved": dict(Counter(kind for _, kind, _ in self.resolved)),
        }


def sweep(session: Session, *, now: datetime, settings: Settings | None = None) -> SweepResult:
    """Run the timers over every case still being booked, and any with a timer still open.

    With ``settings`` the requests not sent yet are also checked against their desks' rules.
    """
    timed = select(CaseException.case_id).where(
        CaseException.resolved_at.is_(None),
        CaseException.kind.in_(sorted(k.value for k in TIMER_KINDS)),
    )
    stmt = (
        select(BookingCase)
        .where(or_(BookingCase.status.in_(UNBOOKED), BookingCase.id.in_(timed)))
        .options(selectinload(BookingCase.exceptions), selectinload(BookingCase.messages))
        .order_by(BookingCase.id)
    )
    result = SweepResult()
    for case in session.scalars(stmt):
        check_case(session, case, now=now, result=result, settings=settings)
    # The steps read the carrier the scan saw (booking/coverage.py), which reads these timers.
    from facility_profiles.booking.steps import sweep_steps  # noqa: PLC0415

    steps = sweep_steps(session, now=now)
    result.raised += steps.raised
    result.resolved += steps.resolved
    if result.raised or result.resolved:
        log.info("booking.timers", **result.counts())
    return result


def check_case(
    session: Session,
    case: BookingCase,
    *,
    now: datetime,
    result: SweepResult | None = None,
    settings: Settings | None = None,
) -> SweepResult:
    """Run the clocks on one case (and its desk's rules, given settings)."""
    run = _Pass(session, now, result if result is not None else SweepResult(), settings)
    run.result.cases += 1
    _check_expiry(run, case)
    _check_desk(run, case)
    _check_silence(run, case)
    return run.result


@dataclass
class _Pass:
    """One sweep: where it writes, what time it is, and what it did."""

    session: Session
    now: datetime
    result: SweepResult
    settings: Settings | None = None

    def raise_(
        self, case: BookingCase, kind: ExceptionType, description: str, **detail: object
    ) -> None:
        flag(self.session, case, kind, description, actor=TIMER, at=self.now, **detail)
        self.result.raised.append((case.id, kind.value, description))

    def clear(self, case: BookingCase, kinds: list[ExceptionType], why: str) -> None:
        for kind in resolve(self.session, case, kinds, resolution=why, by=TIMER, at=self.now):
            self.result.resolved.append((case.id, kind, why))


def _raised_before(case: BookingCase, kind: ExceptionType, key: str, value: object) -> bool:
    """True when this kind was raised for the same clock already, open or resolved since."""
    return any(e.kind == kind.value and (e.detail or {}).get(key) == value for e in case.exceptions)


def _check_expiry(run: _Pass, case: BookingCase) -> None:
    local, source = target_slot(case)
    if not pickup_passed(case, run.now):
        if case.status not in UNBOOKED:
            why = f"case is {case.status}"
        elif local:
            why = f"the pickup is now {fmt_slot(local, case.vendor_timezone)} ({source})"
        else:
            why = "no pickup slot on the case"
        run.clear(case, [ExceptionType.PICKUP_EXPIRED], why)
        return
    if any(e.kind in EXPIRY_DEFERS_TO for e in case.open_exceptions):
        return
    if _raised_before(case, ExceptionType.PICKUP_EXPIRED, "slot", local):
        return
    run.raise_(
        case, ExceptionType.PICKUP_EXPIRED, _expired_text(case, local), slot=local, source=source
    )
    run.clear(
        case,
        [ExceptionType.UNANSWERED_24H, ExceptionType.UNANSWERED_48H, ExceptionType.SLOT_UNWORKABLE],
        "superseded: the pickup time passed",
    )


def _check_desk(run: _Pass, case: BookingCase) -> None:
    """A request not sent yet, against its desk's rules; a passed pickup is the expiry's."""
    if (
        run.settings is None
        or case.status != CaseStatus.UNSCHEDULED.value
        or pickup_passed(case, run.now)
    ):
        return
    before = {e.id: e for e in case.open_exceptions}
    profile = (
        vendor_profile(Repository(run.session), case.facility_key) if case.facility_key else None
    )
    if profile is not None:  # a desk filed on the profile since the scan unblocks the case
        take_desk(run.session, case, profile, by=TIMER)
    check_desk_rules(run.session, case, run.settings, now=run.now, profile=profile, actor=TIMER)
    for exc in case.open_exceptions:
        if exc.id not in before:
            run.result.raised.append((case.id, exc.kind, exc.description))
    for exc in before.values():
        if exc.resolved_at is not None:
            run.result.resolved.append((case.id, exc.kind, exc.resolution or ""))


def _expired_text(case: BookingCase, local: str | None) -> str:
    when = f"pickup {fmt_slot(local, case.vendor_timezone)} passed"
    if case.status == CaseStatus.UNSCHEDULED.value:
        if any(m.direction == "out" and m.kind == "request" for m in case.messages):
            return f"{when}; the request was drafted but never sent"
        return f"{when}; it was never requested"
    if case.status == CaseStatus.DECLINED.value:
        return f"{when}; the vendor could not book it"
    if open_exceptions(case, ExceptionType.CONFIRMATION_REVIEW):
        return f"{when} while the vendor's confirmation waited for approval"
    return f"{when} with no booking from the vendor"


def _check_silence(run: _Pass, case: BookingCase) -> None:
    message = waiting_since(case) if case.status == CaseStatus.PENDING.value else None
    on_us = [e.kind for e in case.open_exceptions if e.kind not in _UNANSWERED]
    since = as_utc(message.sent_at) if message is not None else None
    if message is None or since is None or on_us:
        if on_us:
            why = f"waiting on us: {on_us[0]}"
        elif case.status == CaseStatus.PENDING.value:
            why = "the vendor answered"
        else:
            why = f"case is {case.status}"
        run.clear(case, sorted(UNANSWERED), why)
        return
    since_iso = since.isoformat()
    for exc in open_exceptions(case, *UNANSWERED):
        if (exc.detail or {}).get("since") != since_iso:
            run.clear(case, [ExceptionType(exc.kind)], "a newer message started a new wait")
    hours = weekday_hours(since, run.now, tz_of(case))
    for kind, limit in UNANSWERED_LIMITS:
        if hours < limit:
            continue
        if not _raised_before(case, kind, "since", since_iso):
            text = _silence_text(case, message, since, limit)
            run.raise_(case, kind, text, since=since_iso, message_id=message.id)
            if kind == ExceptionType.UNANSWERED_48H:
                run.clear(case, [ExceptionType.UNANSWERED_24H], "no reply after 48 h either")
        break


def _silence_text(case: BookingCase, message: BookingMessage, since: datetime, limit: int) -> str:
    what = _SENT_WHAT.get(message.kind, "our message")
    desk = case.contact_email or case.vendor_name or "the vendor"
    return (
        f"no reply from {desk} to {what} sent {stamp(since, '%a %m/%d %H:%M')} "
        f"({limit} weekday hours)"
    )[:255]
