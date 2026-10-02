"""Booked pickups written back to Transport Pro (``POST /load/{id}/set_appointment``).

When a pickup is booked (approved, picked from a click-to-confirm link, or marked booked with its
time), a write job is queued with the appointment (:func:`queue_write`). The writer
(:func:`write_appointments`: ``booking writeback``, or each pass of the agent on its own) sends
it only while FP_BOOKING_TPRO_WRITEBACK is on. Until then the job waits, and the board says the
time is to be entered in Transport Pro by hand.

Before it writes, the writer reads the load from Transport Pro:

- only the load's shipper stop is written (waypoint ``SH``); a stop-off pickup is left for a
  person;
- the same confirmed time already there means the write was made: nothing is sent again;
- a different confirmed time already there was set by someone: it is never overwritten, and a
  person is asked which is right (``tpro_mismatch``);
- after the write the load is read again, so the write is confirmed rather than assumed.

Every write is recorded on the case (``written_to_tpro``, with what Transport Pro had before) and
on its job. A failed write is retried after an hour and raised after three tries. An appointment
whose time has passed is never written.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import (
    AutomationJob,
    BookingCase,
    BookingEvent,
    CaseStatus,
    ExceptionType,
    JobStatus,
)
from facility_profiles.booking.worklist import flag
from facility_profiles.config import Settings
from facility_profiles.storage.repository import as_utc
from facility_profiles.tpro.errors import TransportProError
from facility_profiles.tpro.models import Load, Waypoint

KIND = "tpro_write"
ACTOR = "automation"
MAX_ATTEMPTS = 3
RETRY_AFTER = timedelta(hours=1)
CLOSED = frozenset({JobStatus.DONE.value, JobStatus.CANCELED.value})
CONFIRMED = "Confirmed"
OFF = "Transport Pro write-back is off (FP_BOOKING_TPRO_WRITEBACK): enter the time there by hand"


class LoadWriter(Protocol):
    """What the writer needs from the Transport Pro client."""

    def get_load(self, load_id: int) -> Load:
        """``GET /load/{id}``."""
        ...

    def set_appointment(
        self,
        load_id: int,
        waypoint_index: str,
        start_utc: str,
        end_utc: str,
        status: str | None = None,
    ) -> Any:
        """``POST /load/{id}/set_appointment``."""
        ...


def _stamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ")


def appointment_payload(case: BookingCase) -> dict[str, Any]:
    """What is written to Transport Pro for the booked slot.

    ``waypoint_index`` is Transport Pro's name for the stop: ``SH`` for the load's shipper, which
    is the first stop; a pickup at any other stop is a stop-off and is not written by the agent.
    """
    if case.confirmed_start_utc is None:
        msg = f"case {case.id} has no confirmed slot"
        raise ValueError(msg)
    start = as_utc(case.confirmed_start_utc)
    assert start is not None
    end = as_utc(case.confirmed_end_utc) or start
    return {
        "load_id": case.load_id,
        "waypoint_index": "SH" if case.waypoint_index == 0 else None,
        "start_utc": _stamp(start),
        "end_utc": _stamp(end),
        "status": CONFIRMED,
    }


def queue_write(session: Session, case: BookingCase, *, by: str) -> AutomationJob | None:
    """Queue the booked slot for Transport Pro; the same slot is never queued twice.

    A job still open for an earlier slot is replaced. Without a confirmed time (a booking marked
    without one) there is nothing to write.
    """
    if case.confirmed_start_utc is None:
        return None
    payload = appointment_payload(case)
    for job in case.jobs:
        if job.kind != KIND:
            continue
        if (job.result or {}).get("payload") == payload and job.status != JobStatus.CANCELED.value:
            return job
        if job.status not in CLOSED:
            job.status = JobStatus.CANCELED.value
            job.reason = "replaced by a newer time"
    job = AutomationJob(
        case_id=case.id,
        kind=KIND,
        rule="tpro writeback",
        action="write",
        status=JobStatus.PLANNED.value,
        result={"payload": payload, "queued_by": by},
        reason="queued; written while FP_BOOKING_TPRO_WRITEBACK is on",
    )
    case.jobs.append(job)
    session.flush()
    return job


# ------------------------------------------------------------------ the writer


@dataclass
class WriteReport:
    """What one writer pass did."""

    written: int = 0
    already: int = 0
    mismatched: int = 0
    waiting: int = 0
    held: int = 0
    failed: int = 0
    closed: int = 0
    lines: list[str] = field(default_factory=list)

    def say(self, case: BookingCase, text: str) -> None:
        """Add a line about one case."""
        self.lines.append(f"#{case.id} {text}")

    def counts(self) -> dict[str, int]:
        """The counts, for JSON."""
        return {k: v for k, v in self.__dict__.items() if isinstance(v, int)}


def _same(stop: Waypoint, payload: dict[str, Any]) -> bool:
    appt = stop.appointment_time
    if appt is None or (appt.appointment_status or "").strip().lower() != CONFIRMED.lower():
        return False
    start, end = appt.open_at, appt.close_at or appt.open_at
    return (
        start is not None
        and _stamp(start.astimezone(UTC)) == payload["start_utc"]
        and end is not None
        and _stamp(end.astimezone(UTC)) == payload["end_utc"]
    )


def _held_by_tpro(stop: Waypoint) -> dict[str, Any] | None:
    """What Transport Pro has for the stop, when it is a confirmed appointment."""
    appt = stop.appointment_time
    if appt is None or (appt.appointment_status or "").strip().lower() != CONFIRMED.lower():
        return None
    return {"open": appt.open, "close": appt.close, "status": appt.appointment_status}


def _shipper(load: Load, position: int) -> Waypoint | None:
    """The stop the case is for, when it is the load's shipper (``SH``) rather than a stop-off."""
    if not 0 <= position < len(load.waypoints):
        return None
    stop = load.waypoints[position]
    if (stop.type or "").upper() != "SH" or stop.stopoff:
        return None
    return stop


def _close(job: AutomationJob, status: JobStatus, reason: str, now: datetime) -> None:
    job.status = status.value
    job.reason = reason[:255]
    job.done_at = now


def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
    session.add(BookingEvent(case_id=case.id, action=action, actor=ACTOR, detail=detail))


def _failed(
    session: Session, job: AutomationJob, error: Exception, now: datetime, report: WriteReport
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
            f"the appointment could not be written to Transport Pro: {error}"[:255],
            actor=ACTOR,
            error=job.last_error,
        )
        report.say(job.case, f"Transport Pro write gave up after {job.attempts} tries: {error}")
    else:
        job.reason = f"failed ({job.attempts} of {MAX_ATTEMPTS}); retried after {RETRY_AFTER}"
        report.say(job.case, f"Transport Pro write failed, will retry: {error}")


def _write_one(
    session: Session,
    job: AutomationJob,
    client: LoadWriter,
    *,
    now: datetime,
    dry_run: bool,
    report: WriteReport,
) -> None:
    case = job.case
    payload = job.result["payload"]
    load = client.get_load(case.load_id)
    stop = _shipper(load, case.waypoint_index)
    if stop is None:
        job.status = JobStatus.HELD.value
        job.reason = "not the load's shipper stop (a stop-off): enter it in Transport Pro by hand"
        report.held += 1
        report.say(case, "held: a stop-off pickup is not written by the agent")
        return
    if _same(stop, payload):
        _close(job, JobStatus.DONE, "already in Transport Pro", now)
        if not dry_run:
            _event(session, case, "tpro_already_set", payload=payload)
        report.already += 1
        report.say(case, "already in Transport Pro")
        return
    there = _held_by_tpro(stop)
    if there is not None:
        job.status = JobStatus.HELD.value
        job.reason = "Transport Pro has another confirmed time; a person decides"
        if not dry_run:
            flag(
                session,
                case,
                ExceptionType.TPRO_MISMATCH,
                (
                    f"Transport Pro has {there['open']} confirmed for this pickup; the booking is "
                    f"{payload['start_utc']}. Nothing was overwritten"
                )[:255],
                actor=ACTOR,
                tpro=there,
                booked=payload,
            )
        report.mismatched += 1
        report.say(case, f"not written: Transport Pro already has {there['open']} confirmed")
        return
    if dry_run:
        report.written += 1
        report.say(case, f"would write SH {payload['start_utc']} to load {case.load_id}")
        return
    previous = stop.appointment_time.model_dump() if stop.appointment_time else None
    client.set_appointment(
        case.load_id, "SH", payload["start_utc"], payload["end_utc"], payload["status"]
    )
    check = _shipper(client.get_load(case.load_id), case.waypoint_index)
    if check is None or not _same(check, payload):
        msg = "Transport Pro took the write but does not show the appointment"
        raise TransportProError(msg)
    _close(job, JobStatus.DONE, "written to Transport Pro", now)
    job.result = {**job.result, "previous": previous, "written_at": now.isoformat()}
    _event(session, case, "written_to_tpro", payload=payload, previous=previous)
    report.written += 1
    report.say(case, f"written to Transport Pro: SH {payload['start_utc']}")


def write_appointments(
    session: Session,
    settings: Settings,
    client: LoadWriter | None,
    *,
    now: datetime,
    dry_run: bool = False,
) -> WriteReport:
    """One writer pass: every queued booking that can be written, checked against the load first.

    With write-back off (or no client) nothing is read or written; the jobs wait and say why.
    ``dry_run`` reads the loads and says what would be written, and writes nothing.
    """
    report = WriteReport()
    jobs = list(
        session.scalars(
            select(AutomationJob)
            .where(AutomationJob.kind == KIND, AutomationJob.status.not_in(CLOSED))
            .order_by(AutomationJob.case_id, AutomationJob.id)
        )
    )
    for job in jobs:
        case = job.case
        payload = (job.result or {}).get("payload") or {}
        if case.status != CaseStatus.SCHEDULED.value:
            _close(job, JobStatus.CANCELED, f"the case is {case.status}", now)
            report.closed += 1
            continue
        start = datetime.strptime(payload["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
        if start <= now:
            _close(job, JobStatus.CANCELED, "the appointment has passed; nothing to write", now)
            report.closed += 1
            continue
        if job.status == JobStatus.FAILED.value and job.attempts >= MAX_ATTEMPTS:
            continue
        if job.status == JobStatus.HELD.value:
            continue  # a stop-off, or a time Transport Pro disagrees with: a person's call
        retry = (job.result or {}).get("retry_at")
        if retry and datetime.fromisoformat(retry) > now:
            continue
        if client is None or not (settings.booking_tpro_writeback or dry_run):
            job.status = JobStatus.WAITING.value
            job.reason = OFF
            report.waiting += 1
            continue
        try:
            _write_one(session, job, client, now=now, dry_run=dry_run, report=report)
        except (TransportProError, OSError, ValueError) as exc:
            _failed(session, job, exc, now, report)
    return report
