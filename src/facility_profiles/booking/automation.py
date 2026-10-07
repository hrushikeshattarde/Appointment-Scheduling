"""The agent on its own: each customer's rules say what it does with each pickup, and when.

One pass (:func:`run_once`: ``booking run``, or ``serve --autopilot-every``):

1. the timers run (a desk's silence, a pickup that slipped);
   then, with an inbox (FP_BOOKING_INBOX, ``booking/inbox.py``), the new replies are read and
   answered: each reply is matched to its pickup, read, and answered by the conversation policy
   (``booking/respond.py``), sent where the customer's rule says ``replies = "send"`` and booked
   by the agent itself where it says ``confirm = "auto"``. One reply that fails is retried on
   the next pass and does not stop the others;
2. every unscheduled case without a request gets a request job from the first rule in its
   customer's file that covers it (``customers.profile.Rule``; without one, the default: draft
   now). The job waits for the delivery slot when the rule says so, is held for a person, waits
   on a person when something is open on the case, or is planned for its time: ``lead_days``
   business days before the pickup, at ``batch_at`` in the pod's time zone;
3. the jobs that are due are written, one email per desk the way the pod batches them: drafts
   for a person to send, or sent when the rule says ``send`` and FP_BOOKING_MODE is send. Each
   desk's rules are checked first, exactly as for ``booking draft``. A batch that fails leaves
   nothing behind; it is retried an hour later, and after three tries raised as
   ``automation_failed``;
4. a sent request with no reply gets its one follow-up, unless the rule says not to;
5. with a Transport Pro client (only while FP_BOOKING_TPRO_WRITEBACK is on), the booked pickups
   are written back to their loads (``booking/writeback.py``).

Nothing happens between passes, and a pass never redoes what a person did: a request drafted, a
booking made or a case canceled by hand closes the job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.classify import ReplyClassifier
from facility_profiles.booking.facts import FactsSource
from facility_profiles.booking.inbox import Inbox
from facility_profiles.booking.mail import Mailer, Sender
from facility_profiles.booking.models import (
    AutomationJob,
    BookingCase,
    BookingEvent,
    CaseStatus,
    ExceptionType,
    JobStatus,
)
from facility_profiles.booking.outbox import SendRefusedError
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.rules import check_desk_rules, request_opens, vendor_profile
from facility_profiles.booking.service import (
    IngestStats,
    draft_batch,
    has_request,
    ingest,
    list_cases,
    plan_request,
)
from facility_profiles.booking.timers import fmt_slot, slot_at, sweep
from facility_profiles.booking.worklist import flag, open_kinds
from facility_profiles.booking.writeback import LoadWriter, write_appointments
from facility_profiles.booking.writer import ReplyWriter
from facility_profiles.business_days import is_business_day
from facility_profiles.clock import stamp
from facility_profiles.config import Settings
from facility_profiles.customers import customers
from facility_profiles.customers.profile import Customer, Rule
from facility_profiles.logging import get_logger
from facility_profiles.storage.repository import Repository, as_utc

log = get_logger(__name__)

ACTOR = "automation"
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=1)
CLOSED = frozenset({JobStatus.DONE.value, JobStatus.CANCELED.value})


@dataclass
class RunReport:
    """What one pass did, in counts and one line per case it touched."""

    planned: int = 0
    waiting: int = 0
    blocked: int = 0
    held: int = 0
    drafted: int = 0
    sent: int = 0
    followed_up: int = 0
    failed: int = 0
    closed: int = 0
    written_to_tpro: int = 0
    mail_read: int = 0  # new replies read from the inbox
    mail_by_person: int = 0  # emails people at Circle sent on the group, kept on their pickups
    mail_answered: int = 0  # answers the agent wrote to them (sent or drafted)
    auto_confirmed: int = 0  # confirmations it booked itself
    mail_failed: int = 0  # replies that failed (retried on the next pass) or an unreadable inbox
    mail_unmatched: int = 0  # booking mail no pickup matched, kept for a person this pass
    booked_changed: int = 0  # replies that moved, dropped or put off a booked pickup
    lines: list[str] = field(default_factory=list)

    def say(self, case: BookingCase, text: str) -> None:
        """Add a line about one case."""
        self.lines.append(f"#{case.id} {text}")

    def counts(self) -> dict[str, int]:
        """The counts, for JSON."""
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


# ------------------------------------------------------------------ when a job is due


def business_days_before(day: date, count: int) -> date:
    """``count`` business days before ``day`` (weekends and freight holidays do not count)."""
    while count > 0:
        day -= timedelta(days=1)
        if is_business_day(day):
            count -= 1
    return day


def next_batch(clock: str, tz: ZoneInfo, after: datetime) -> datetime:
    """The first ``clock`` on a business day, in ``tz``, at or after ``after``."""
    at = time.fromisoformat(clock)
    local = after.astimezone(tz)
    day = local.date()
    while True:
        moment = datetime.combine(day, at, tzinfo=tz)
        if moment >= local and is_business_day(day):
            return moment
        day += timedelta(days=1)


def due_time(case: BookingCase, rule: Rule, tz: ZoneInfo, now: datetime) -> datetime:
    """When the rule wants the request written: its lead days before the pickup, at its hour."""
    start = now
    if rule.lead_days is not None:
        pickup = slot_at(case.requested_local, case.vendor_timezone)
        if pickup is not None:
            day = business_days_before(pickup.date(), rule.lead_days)
            start = max(now, datetime.combine(day, time(0), tzinfo=tz))
    if rule.batch_at:
        return next_batch(rule.batch_at, tz, start)
    return start


# ------------------------------------------------------------------ planning


def _close(job: AutomationJob, status: JobStatus, reason: str, now: datetime) -> None:
    job.status = status.value
    job.reason = reason[:255]
    job.done_at = now


def _request_job(session: Session, case: BookingCase, rule: Rule) -> AutomationJob:
    job = next((j for j in reversed(case.jobs) if j.kind == "request"), None)
    if job is None:
        job = AutomationJob(
            case_id=case.id,
            kind="request",
            rule=rule.name,
            action=rule.do,
            status=JobStatus.PLANNED.value,
        )
        case.jobs.append(job)
        session.flush()
    elif job.status not in CLOSED:
        job.rule, job.action = rule.name, rule.do  # the customer's file may have changed
    return job


def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
    session.add(BookingEvent(case_id=case.id, action=action, actor=ACTOR, detail=detail))


def _due(
    job: AutomationJob, case: BookingCase, rule: Rule, tz: ZoneInfo, now: datetime
) -> datetime:
    """When the job runs: the rule's time, kept once it has come.

    Never before the desk opens (``not_before``) or the next try (``retry_at``).
    """
    due = due_time(case, rule, tz, now)
    planned = as_utc(job.due_at) if job.status == JobStatus.PLANNED.value else None
    if planned is not None:
        due = min(due, planned)  # a batch hour that came keeps the job due; it never slides on
    for later in (job.result.get("not_before"), job.result.get("retry_at")):
        if later:
            due = max(due, datetime.fromisoformat(later))
    return due


def _plan(
    session: Session,
    case: BookingCase,
    job: AutomationJob,
    rule: Rule,
    *,
    settings: Settings,
    tz: ZoneInfo,
    now: datetime,
    report: RunReport,
) -> None:
    """Bring one case's request job up to date with its rule and the case."""
    if job.status in CLOSED:
        return
    if has_request(case):
        _close(job, JobStatus.DONE, "a request was already written", now)
        report.closed += 1
        return
    if rule.do == "skip":
        _close(job, JobStatus.CANCELED, rule.why or f"rule {rule.name!r}: not ours to book", now)
        report.closed += 1
        return
    if rule.do == "hold":
        if job.status != JobStatus.HELD.value:
            job.status = JobStatus.HELD.value
            job.reason = (rule.why or f"rule {rule.name!r}: a person books this")[:255]
            note = f"rule {rule.name!r}: a person books this" + (
                f" ({rule.why})" if rule.why else ""
            )
            flag(session, case, ExceptionType.HANDOFF, note[:255], actor=ACTOR, rule=rule.name)
            report.say(case, f"held for a person ({rule.name})")
        report.held += 1
        return
    if rule.wait_for == "delivery_slot" and not (case.delivery_ref and case.delivery_at_utc):
        if job.status != JobStatus.WAITING.value:
            _event(session, case, "request_waiting", reason="waiting for the delivery slot")
            report.say(case, "waits for the delivery slot")
        job.status = JobStatus.WAITING.value
        job.reason = "waiting for the delivery slot and its reference"
        report.waiting += 1
        return
    if job.status == JobStatus.FAILED.value and job.attempts >= MAX_ATTEMPTS:
        return  # raised as automation_failed; a person takes it from here
    if rule.pickup_from == "delivery" and not job.result.get("planned_from_delivery"):
        plan_request(session, case, settings, now=now, use_tender=False)
        job.result = {**job.result, "planned_from_delivery": True}
    if case.open_exceptions:
        job.status = JobStatus.BLOCKED.value
        job.reason = f"waiting on a person: {', '.join(open_kinds(case))}"[:255]
        report.blocked += 1
        return
    due = _due(job, case, rule, tz, now)
    job.status = JobStatus.PLANNED.value
    job.due_at = due.astimezone(UTC)  # SQLite keeps the wall clock and drops the zone
    job.reason = f"rule {rule.name!r}: {rule.do} at {stamp(due, '%a %m/%d %H:%M')}"
    report.planned += 1


# ------------------------------------------------------------------ doing


def _failed(
    session: Session, job: AutomationJob, error: Exception, now: datetime, report: RunReport
) -> None:
    job.attempts = (job.attempts or 0) + 1
    job.last_error = f"{type(error).__name__}: {error}"[:2000]
    job.status = JobStatus.FAILED.value
    job.result = {**job.result, "retry_at": (now + RETRY_AFTER).isoformat()}
    report.failed += 1
    if job.attempts >= MAX_ATTEMPTS:
        job.reason = f"gave up after {job.attempts} tries"
        flag(
            session,
            job.case,
            ExceptionType.AUTOMATION_FAILED,
            f"the request could not be written on its own: {error}"[:255],
            actor=ACTOR,
            error=job.last_error,
        )
        report.say(job.case, f"gave up after {job.attempts} tries: {error}")
    else:
        job.reason = f"failed ({job.attempts} of {MAX_ATTEMPTS}); retried after {RETRY_AFTER}"
        report.say(job.case, f"failed, will retry: {error}")


def _execute(
    session: Session,
    jobs: list[AutomationJob],
    *,
    settings: Settings,
    mailer: Mailer,
    sender: Sender | None,
    now: datetime,
    report: RunReport,
) -> None:
    """Write the due requests: each desk's rules first, then one email per desk."""
    known = customers(settings)
    repo = Repository(session)
    groups: dict[tuple[str, str, bool], list[AutomationJob]] = {}
    for job in jobs:
        case = job.case
        profile = vendor_profile(repo, case.facility_key) if case.facility_key else None
        wait = check_desk_rules(session, case, settings, now=now, profile=profile)
        if case.open_exceptions:
            job.status = JobStatus.BLOCKED.value
            job.reason = f"waiting on a person: {', '.join(open_kinds(case))}"[:255]
            report.blocked += 1
            continue
        if wait:
            opens = request_opens(case.requested_local, case.vendor_timezone, profile)
            if opens is not None:
                job.due_at = opens.astimezone(UTC)
                job.result = {**job.result, "not_before": opens.isoformat()}
            job.reason = wait[:255]
            report.say(case, wait)
            continue
        send = job.action == "send" and settings.booking_mode == "send" and sender is not None
        key = (known.for_case(case).key, (case.contact_email or "").lower(), send)
        groups.setdefault(key, []).append(job)
    session.flush()
    for (_, desk, send), group in groups.items():
        outbox: Mailer | Sender = sender if send and sender is not None else mailer
        try:
            with session.begin_nested():  # a batch that fails leaves nothing behind
                messages = draft_batch(
                    session, [j.case for j in group], outbox, settings, by=ACTOR, now=now
                )
        except (SendRefusedError, ValueError, RuntimeError, OSError) as exc:
            for job in group:
                _failed(session, job, exc, now, report)
            continue
        written = {m.case_id: m for m in messages}
        for job in group:
            message = written.get(job.case_id)
            _close(job, JobStatus.DONE, "sent" if send else "drafted for a person to send", now)
            job.result = {
                **job.result,
                "message_id": message.id if message else None,
                "draft_ref": message.draft_ref if message else None,
                "sent": send,
            }
            if send:
                report.sent += 1
            else:
                report.drafted += 1
            when = fmt_slot(job.case.requested_local, job.case.vendor_timezone)
            report.say(job.case, f"{'sent' if send else 'drafted'} to {desk} for {when}")


def _follow_ups(
    session: Session,
    *,
    settings: Settings,
    mailer: Mailer,
    sender: Sender | None,
    now: datetime,
    report: RunReport,
) -> None:
    """One nudge for each sent request the desk has not answered, where the rule allows it."""
    known = customers(settings)
    responder = Responder(settings, mailer, now=now, sender=sender)
    for case in list_cases(session, CaseStatus.PENDING.value):
        rule = known.for_case(case).rule_for(case)
        if rule.do not in ("draft", "send") or not rule.follow_up:
            continue
        message = responder.follow_up(session, case)
        if message is None:
            continue
        send = message.sent_at is not None
        case.jobs.append(
            AutomationJob(
                case_id=case.id,
                kind="follow_up",
                rule=rule.name,
                action=rule.do,
                status=JobStatus.DONE.value,
                due_at=now,
                done_at=now,
                reason="no reply from the desk",
                result={"message_id": message.id, "draft_ref": message.draft_ref, "sent": send},
            )
        )
        report.followed_up += 1
        report.say(case, f"follow-up {'sent' if send else 'drafted'} to {message.to_addr}")


# ------------------------------------------------------------------ one pass


def _zone(customer: Customer, settings: Settings) -> ZoneInfo:
    return ZoneInfo(customer.timezone or settings.booking_timezone)


def _read_inbox(
    session: Session,
    inbox: Inbox,
    classifier: ReplyClassifier,
    *,
    settings: Settings,
    responder: Responder | None,
    report: RunReport,
) -> None:
    """Read the new mail, oldest first, one email at a time; a failure leaves that one for later.

    With a ``responder`` each vendor reply is also answered; without one the mail is only read:
    kept on its pickups, which move and raise their to-dos, and nothing is drafted or sent.
    """
    try:
        messages = inbox.fetch()
    except Exception as exc:  # an unreadable inbox must not stop the rest of the pass
        log.exception("booking.inbox_failed")
        report.mail_failed += 1
        report.lines.append(f"inbox: could not read the mail ({exc})")
        return
    for message in sorted(messages, key=lambda m: m.sent_at):
        try:
            with session.begin_nested():
                stats: IngestStats = ingest(
                    session,
                    [message],
                    classifier,
                    internal_domains=settings.internal_email_domains,
                    responder=responder,
                    settings=settings,
                )
        except Exception:  # this reply is tried again on the next pass
            log.exception("booking.reply_failed", subject=message.subject)
            report.mail_failed += 1
            report.lines.append(f"inbox: could not handle {message.subject!r}; trying next pass")
            continue
        report.mail_read += stats.new_mail
        report.mail_by_person += stats.by_person
        report.mail_answered += stats.responded
        report.auto_confirmed += stats.auto_confirmed
        report.mail_unmatched += stats.unmatched_kept
        report.booked_changed += stats.booked_changed
        if stats.auto_confirmed:
            report.lines.append(f"inbox: booked {message.subject!r} from the vendor's confirmation")
        if stats.unmatched_kept:
            report.lines.append(
                f"inbox: no pickup matched {message.subject!r}; kept for a person to link"
            )
        if stats.booked_changed:
            report.lines.append(
                f"inbox: {message.subject!r} changes a booked pickup; left for a person"
            )


def read_mail(
    session: Session, settings: Settings, *, inbox: Inbox, classifier: ReplyClassifier
) -> RunReport:
    """Read the new mail onto the board and answer nothing (``serve --mail-every``).

    Every email on the customer's group is kept on its pickup: the vendor's (read, so the
    pickup moves and its to-dos are raised), the customer desk's, and the ones people at Circle
    sent. Nothing is drafted, sent or written to Transport Pro.
    """
    report = RunReport()
    _read_inbox(session, inbox, classifier, settings=settings, responder=None, report=report)
    return report


def run_once(
    session: Session,
    settings: Settings,
    *,
    now: datetime,
    mailer: Mailer,
    sender: Sender | None = None,
    timers: bool = True,
    client: LoadWriter | None = None,
    inbox: Inbox | None = None,
    classifier: ReplyClassifier | None = None,
    writer: ReplyWriter | None = None,
    facts: FactsSource | None = None,
) -> RunReport:
    """One pass of the agent on its own: read new replies, plan open requests, write what's due.

    ``client`` writes the booked pickups to Transport Pro; pass one only while write-back is on.
    ``inbox`` and ``classifier`` read and answer the vendors' replies; without them replies wait
    for ``booking inbox``. ``writer`` writes each answer for its situation from the facts, the
    load's read through ``facts`` (Transport Pro, read only); without it the fixed wording goes.
    """
    report = RunReport()
    if timers:
        sweep(session, now=now, settings=settings)
    if inbox is not None and classifier is not None:
        _read_inbox(
            session,
            inbox,
            classifier,
            settings=settings,
            responder=Responder(
                settings,
                mailer,
                writer=writer,
                now=now,
                sender=sender if settings.booking_mode == "send" else None,
                facts=facts,
            ),
            report=report,
        )
    known = customers(settings)
    for case in sorted(list_cases(session, CaseStatus.UNSCHEDULED.value), key=lambda c: c.id):
        customer = known.for_case(case)
        rule = customer.rule_for(case)
        job = _request_job(session, case, rule)
        _plan(
            session,
            case,
            job,
            rule,
            settings=settings,
            tz=_zone(customer, settings),
            now=now,
            report=report,
        )
    open_jobs = list(
        session.scalars(
            select(AutomationJob)
            .where(AutomationJob.kind == "request", AutomationJob.status.not_in(CLOSED))
            .order_by(AutomationJob.case_id)
        )
    )
    for job in open_jobs:
        if job.case.status != CaseStatus.UNSCHEDULED.value:
            done = job.case.status == CaseStatus.SCHEDULED.value or has_request(job.case)
            status = JobStatus.DONE if done else JobStatus.CANCELED
            _close(job, status, f"the case is {job.case.status}", now)
            report.closed += 1
    session.flush()
    due = [
        job
        for job in open_jobs
        if job.status == JobStatus.PLANNED.value
        and job.due_at is not None
        and (as_utc(job.due_at) or now) <= now
    ]
    _execute(session, due, settings=settings, mailer=mailer, sender=sender, now=now, report=report)
    _follow_ups(session, settings=settings, mailer=mailer, sender=sender, now=now, report=report)
    if client is not None and settings.booking_tpro_writeback:
        written = write_appointments(session, settings, client, now=now)
        report.written_to_tpro = written.written
        report.lines.extend(written.lines)
    return report
