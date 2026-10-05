"""Booked pickups written back to Transport Pro: queued once, checked first, read back, audited."""

from __future__ import annotations

import contextlib
import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from typer.testing import CliRunner

from facility_profiles.api.booking import case_detail
from facility_profiles.booking.automation import run_once
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus, JobStatus
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    apply_reply,
    approve,
    close_case,
    list_cases,
    mark_booked,
    scan,
)
from facility_profiles.booking.timers import fmt_slot
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.booking.writeback import OFF, queue_write, write_appointments
from facility_profiles.config import Settings, get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.tpro.errors import TransportProApiError
from facility_profiles.tpro.models import Load
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

# The Lidl load at Koch Foods: tendered Thu 10/01 09:00 New York, booked for 10:00 (14:00Z).
BOOKED = "2026-10-01 10:00"
START = "2026-10-01T14:00:00Z"


class FakeLoads:
    """Transport Pro's loads, in memory: reads what it holds, records and applies writes."""

    def __init__(self, *loads: dict[str, Any], fail: int = 0, ignore_writes: bool = False) -> None:
        self.loads = {load["id"]: copy.deepcopy(load) for load in loads}
        self.fail = fail
        self.ignore_writes = ignore_writes
        self.writes: list[tuple[Any, ...]] = []
        self.notes: list[tuple[int, str]] = []
        self.note_fails = 0
        self.reads = 0

    def get_load(self, load_id: int) -> Load:
        self.reads += 1
        return Load.model_validate(self.loads[load_id])

    def set_appointment(
        self,
        load_id: int,
        waypoint_index: str,
        start_utc: str,
        end_utc: str,
        status: str | None = None,
    ) -> Any:
        if self.fail:
            self.fail -= 1
            raise TransportProApiError(503, "busy", f"POST /load/{load_id}/set_appointment")
        self.writes.append((load_id, waypoint_index, start_utc, end_utc, status))
        if not self.ignore_writes:
            stop = next(w for w in self.loads[load_id]["waypoints"] if w["type"] == waypoint_index)
            stop["appointmentTime"] = {
                "open": start_utc,
                "close": end_utc,
                "appointmentStatus": status,
            }
        return {"success": True}

    def add_load_note(self, load_id: int, content: str, *, priority: bool = False) -> Any:
        if self.note_fails:
            self.note_fails -= 1
            raise TransportProApiError(503, "busy", f"POST /load/{load_id}/note")
        self.notes.append((load_id, content))
        return {"success": True}


def on(settings: Settings) -> Settings:
    return settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_tpro_writeback": True}
    )


def booked(settings: Settings, sessions) -> tuple[int, FakeLoads]:  # type: ignore[no-untyped-def]
    """The pickup scanned and booked by phone for 10:00; Transport Pro holds the tendered stop."""
    load = lidl_load(2001, po="226321092660")
    seed_vendor(sessions)
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        mark_booked(session, case, by="pod", via="phone", local=BOOKED)
        return case.id, FakeLoads(load)


def tpro_job(case: BookingCase):  # type: ignore[no-untyped-def]
    return [j for j in case.jobs if j.kind == "tpro_write"]


# ------------------------------------------------------------------ the queue


def test_a_booking_queues_its_slot_once(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, _ = booked(on(settings), sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        [job] = tpro_job(case)
        assert job.status == JobStatus.PLANNED.value
        assert job.result["payload"] == {
            "load_id": 2001,
            "waypoint_index": "SH",
            "start_utc": START,
            "end_utc": START,
            "status": "Confirmed",
        }
        assert queue_write(session, case, by="pod") is job  # the same slot again: no new job
        mark_booked(session, case, by="pod", via="phone", local="2026-10-01 11:00")
        old, new = tpro_job(case)
        assert old.status == JobStatus.CANCELED.value and old.reason == "replaced by a newer time"
        assert new.result["payload"]["start_utc"] == "2026-10-01T15:00:00Z"
        # A booking marked without a time has nothing to write.
        other = BookingCase(load_id=9, status=CaseStatus.SCHEDULED.value)
        assert queue_write(session, other, by="pod") is None


def test_an_approved_confirmation_is_queued_too(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    scan(
        FakeTPro([lidl_load(2001, po="226321092660")], {}),
        sessions,
        on(settings),
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        case.status = CaseStatus.PENDING.value
        reading = ReplyClassification(
            status=ReplyStatus.CONFIRMED, pickup_date="2026-10-01", pickup_time="10:00"
        )
        apply_reply(session, case, reading, [], reply_sent_at=NOW)
        payload, written = approve(session, case, by="megan")
        assert written is False and tpro_job(case)[0].result["payload"] == payload


# ------------------------------------------------------------------ the writer


def test_with_write_back_off_the_job_waits_and_nothing_is_read(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    off = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    case_id, loads = booked(off, sessions)
    with session_scope(sessions) as session:
        report = write_appointments(session, off, loads, now=NOW)
        [job] = tpro_job(session.get(BookingCase, case_id))  # type: ignore[arg-type]
        assert report.waiting == 1 and job.status == JobStatus.WAITING.value and job.reason == OFF
        assert loads.reads == 0 and loads.writes == []
        assert write_appointments(session, on(settings), None, now=NOW).waiting == 1  # no client


def test_written_once_read_back_and_recorded(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    with session_scope(sessions) as session:
        report = write_appointments(session, on(settings), loads, now=NOW)
        case = session.get(BookingCase, case_id)
        assert case is not None
        [job] = tpro_job(case)
        assert report.written == 1 and loads.writes == [(2001, "SH", START, START, "Confirmed")]
        assert job.status == JobStatus.DONE.value and job.reason == "written to Transport Pro"
        assert loads.reads == 2  # read before, read back after
        session.expire(case, ["events"])
        event = case.events[-1]
        assert event.action == "written_to_tpro" and event.actor == "automation"
        assert event.detail["previous"]["appointment_status"] == "Not Required"
        # Nothing more to do on the next pass.
        assert write_appointments(session, on(settings), loads, now=NOW).written == 0
        assert len(loads.writes) == 1
        assert case_detail(case, now=NOW)["jobs"][-1]["status"] == "done"


def test_a_time_already_in_transport_pro_is_not_sent_again(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    loads.loads[2001]["waypoints"][0]["appointmentTime"] = {
        "open": START,
        "close": START,
        "appointmentStatus": "Confirmed",
    }
    with session_scope(sessions) as session:
        report = write_appointments(session, on(settings), loads, now=NOW)
        [job] = tpro_job(session.get(BookingCase, case_id))  # type: ignore[arg-type]
        assert (
            report.already == 1 and loads.writes == [] and job.reason == "already in Transport Pro"
        )


def test_a_different_confirmed_time_is_never_overwritten(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    loads.loads[2001]["waypoints"][0]["appointmentTime"] = {
        "open": "2026-10-01T12:00:00Z",
        "close": "2026-10-01T12:00:00Z",
        "appointmentStatus": "Confirmed",
    }
    with session_scope(sessions) as session:
        report = write_appointments(session, on(settings), loads, now=NOW)
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert report.mismatched == 1 and loads.writes == []
        assert open_kinds(case) == ["tpro_mismatch"]
        assert case.open_exceptions[0].description == (
            "Transport Pro has Thu 10/01 08:00 ET confirmed for this pickup; the booking is "
            "Thu 10/01 10:00 ET. Nothing was overwritten"
        )
        assert tpro_job(case)[0].status == JobStatus.HELD.value
        write_appointments(session, on(settings), loads, now=NOW)  # held: left alone
        assert loads.writes == [] and len(case.exceptions) == 1


def test_a_stop_off_pickup_is_left_for_a_person(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    loads.loads[2001]["waypoints"][0].update(type="PU", stopoff=True)
    with session_scope(sessions) as session:
        report = write_appointments(session, on(settings), loads, now=NOW)
        [job] = tpro_job(session.get(BookingCase, case_id))  # type: ignore[arg-type]
        assert report.held == 1 and loads.writes == []
        assert job.reason.startswith("not the load's shipper stop")  # type: ignore[union-attr]


def test_failed_writes_are_retried_then_raised(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    loads.fail = 3
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        [job] = tpro_job(case)
        write_appointments(session, on(settings), loads, now=NOW)
        assert job.status == JobStatus.FAILED.value and job.attempts == 1
        assert (
            write_appointments(session, on(settings), loads, now=NOW + timedelta(minutes=10)).failed
            == 0
        )
        for hours in (1, 2):
            write_appointments(
                session, on(settings), loads, now=NOW + timedelta(hours=hours, minutes=1)
            )
        assert job.attempts == 3 and open_kinds(case) == ["automation_failed"]
        assert (
            write_appointments(session, on(settings), loads, now=NOW + timedelta(hours=4)).failed
            == 0
        )
    # A write Transport Pro accepts but does not show is not taken on trust.
    quiet = FakeLoads(lidl_load(2001, po="226321092660"), ignore_writes=True)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        mark_booked(session, case, by="pod", via="phone", local="2026-10-01 11:00")
        write_appointments(session, on(settings), quiet, now=NOW)
        assert "does not show the appointment" in (tpro_job(case)[-1].last_error or "")


def test_past_or_unbooked_appointments_are_not_written(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, loads = booked(on(settings), sessions)
    with session_scope(sessions) as session:
        report = write_appointments(
            session, on(settings), loads, now=datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        )
        [job] = tpro_job(session.get(BookingCase, case_id))  # type: ignore[arg-type]
        assert report.closed == 1 and job.reason == "the appointment has passed; nothing to write"
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        mark_booked(session, case, by="pod", via="phone", local="2026-10-01 11:00")
        close_case(session, case, by="pod", reason="load canceled")
        write_appointments(session, on(settings), loads, now=NOW)
        assert tpro_job(case)[-1].reason == "the case is canceled" and loads.writes == []


def test_the_agents_pass_writes_back_only_with_a_client_and_the_setting(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    _, loads = booked(on(settings), sessions)
    with session_scope(sessions) as session:
        off = on(settings).model_copy(update={"booking_tpro_writeback": False})
        assert (
            run_once(session, off, now=NOW, mailer=RecordingMailer(), client=loads).written_to_tpro
            == 0
        )
        assert loads.reads == 0
        report = run_once(session, on(settings), now=NOW, mailer=RecordingMailer(), client=loads)
        assert report.written_to_tpro == 1 and len(loads.writes) == 1
        assert any("written to Transport Pro" in line for line in report.lines)


def test_booking_writeback_from_the_command_line(settings: Settings, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    import facility_profiles.cli as cli

    db = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(db)
    init_db(engine)
    # The CLI reads the real clock: a load picking up three weekdays from today, booked 10:00.
    pickup = datetime.now(tz=UTC).replace(hour=13, minute=0, second=0, microsecond=0) + timedelta(
        days=3
    )
    while pickup.weekday() >= 5:
        pickup += timedelta(days=1)
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][0]["appointmentTime"].update(open=f"{pickup:%Y-%m-%dT%H:%M:%SZ}")
    delivery = pickup + timedelta(days=1)
    load["waypoints"][1]["appointmentTime"].update(open=f"{delivery:%Y-%m-%dT%H:%M:%SZ}")
    sessions = session_factory(engine)
    seed_vendor(sessions)
    scan(FakeTPro([load], {}), sessions, on(settings), days_ahead=14, now=datetime.now(tz=UTC))  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        mark_booked(session, case, by="pod", via="phone", local=f"{pickup:%Y-%m-%d} 10:00")
        case_id = case.id
        shown = fmt_slot(f"{pickup:%Y-%m-%d} 10:00")  # Eastern, as the CLI says it
    engine.dispose()
    loads = FakeLoads(load)
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", db)
    monkeypatch.chdir(tmp_path)
    # Never the real Transport Pro: the CLI's client is the in-memory one.
    opened: list[bool] = []

    def fake_client(_settings: Settings, *, allow_writes: bool = False):  # type: ignore[no-untyped-def]
        opened.append(allow_writes)
        return contextlib.nullcontext(loads)

    monkeypatch.setattr(cli, "_client", fake_client)
    runner = CliRunner()
    get_settings.cache_clear()
    try:
        off = runner.invoke(cli.app, ["booking", "writeback"])
        assert off.exit_code == 0 and "FP_BOOKING_TPRO_WRITEBACK is off" in off.output
        assert opened == [] and loads.writes == []
        dry = runner.invoke(cli.app, ["booking", "writeback", "--dry-run"])
        assert f"#{case_id} would write SH {shown} on load 2001" in dry.output
        assert opened == [False] and loads.writes == []
        monkeypatch.setenv("FP_BOOKING_TPRO_WRITEBACK", "true")
        get_settings.cache_clear()
        live = runner.invoke(cli.app, ["booking", "writeback"])
        assert live.exit_code == 0, live.output
        assert f"#{case_id} written to Transport Pro: SH {shown}" in live.output
        assert opened == [False, True] and len(loads.writes) == 1
    finally:
        get_settings.cache_clear()
