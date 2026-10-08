"""The board writes booked pickups to Transport Pro on its own (``serve --writeback-every``).

A board that only reads the mail and scans (no ``--autopilot-every``) still writes each booking's
time to its load, while FP_BOOKING_TPRO_WRITEBACK is on, with the writer's own checks.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from facility_profiles.api import app as app_module
from facility_profiles.api.app import create_app, run_writeback
from facility_profiles.booking.models import BookingCase, JobStatus
from facility_profiles.cli import app as cli_app
from facility_profiles.config import Settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.tpro import client as client_module
from tests.test_booking import NOW
from tests.test_booking_autoscan import cli_env  # noqa: F401 - the serve fixture
from tests.test_booking_writeback import START, booked, on, tpro_job


def test_a_booked_pickup_is_written_and_read_back(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    settings = on(settings)
    case_id, loads = booked(settings, sessions)
    report = run_writeback(sessions, settings, loads, now=NOW)
    assert report.written == 1
    assert loads.writes == [(2001, "SH", START, START, "Confirmed")]
    assert len(loads.notes) == 1
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        assert tpro_job(case)[0].status == JobStatus.DONE.value
        assert "written_to_tpro" in [e.action for e in case.events]
    assert run_writeback(sessions, settings, loads, now=NOW).written == 0  # written once


def test_nothing_waiting_never_calls_transport_pro(
    settings: Settings, sessions, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("Transport Pro was called with nothing to write")

    monkeypatch.setattr(client_module.TransportProClient, "from_settings", refuse)
    assert run_writeback(sessions, on(settings)).counts() == {
        "written": 0,
        "already": 0,
        "mismatched": 0,
        "waiting": 0,
        "held": 0,
        "failed": 0,
        "closed": 0,
    }


def test_the_writer_refuses_while_write_back_is_off(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(RuntimeError, match="FP_BOOKING_TPRO_WRITEBACK"):
        run_writeback(sessions, settings)


def test_serve_writes_only_with_write_back_on(
    cli_env: list[Any],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runner = CliRunner()
    off = runner.invoke(cli_app, ["serve", "--writeback-every", "1"])
    assert off.exit_code == 2 and "needs FP_BOOKING_TPRO_WRITEBACK=true" in off.output
    assert not cli_env  # refused before anything served
    monkeypatch.setenv("FP_BOOKING_TPRO_WRITEBACK", "true")
    app_module.get_settings.cache_clear()
    live = runner.invoke(cli_app, ["serve", "--writeback-every", "1"])
    assert live.exit_code == 0, live.output
    assert "written to Transport Pro every 1 min" in live.output


def test_the_pickup_says_whether_the_agent_writes_transport_pro(
    settings: Settings, tmp_path: Path
) -> None:
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    case_id, _ = booked(on(settings), sessions)
    engine.dispose()
    for writes in (False, True):
        board = create_app(
            settings.model_copy(update={"database_url": url, "booking_tpro_writeback": writes})
        )
        with TestClient(board) as client:
            detail = client.get(f"/api/booking/cases/{case_id}").json()
        assert detail["tpro_writeback"] is writes
