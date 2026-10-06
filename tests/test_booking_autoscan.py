"""The board checks Transport Pro for new pickups on its own (``serve --scan-every``).

The load is the invented Lidl inbound load from tests/test_booking.py (Koch Foods to the PYE
RDC), moved to a few days from now, since the server scans on the real clock.
"""

from __future__ import annotations

import sqlite3
import time as wall
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.booking.service import scan
from facility_profiles.cli import app as cli_app
from facility_profiles.config import Settings, get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory
from facility_profiles.tpro import client as tpro_client
from facility_profiles.tpro.models import Load
from tests.conftest import FakeTPro
from tests.test_booking import lidl_load, seed_vendor

LIDL = ([1089], [7211])  # Lidl's pod and its inbound customer, as `--scan-customer lidl` gives


def upcoming_load(load_id: int = 7001, *, days: int = 2) -> dict[str, Any]:
    """The Lidl test load with its pickup ``days`` from now and delivery a day later."""
    load = lidl_load(load_id, po="115802102660")
    pickup, delivery = load["waypoints"]
    day = datetime.now(tz=UTC).replace(hour=13, minute=0, second=0, microsecond=0)
    for wp, when in (
        (pickup, day + timedelta(days=days)),
        (delivery, day + timedelta(days=days + 1)),
    ):
        wp["appointmentTime"]["open"] = wp["appointmentTime"]["close"] = when.strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
    return load


class ContextTPro(FakeTPro):
    """The fake Transport Pro, usable as ``with TransportProClient.from_settings(...)``."""

    def __enter__(self) -> ContextTPro:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


@pytest.fixture
def store(settings: Settings, tmp_path: Path) -> Iterator[tuple[Settings, Any]]:
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    seed_vendor(sessions)  # Koch Foods books by email
    yield settings.model_copy(update={"database_url": url}), sessions
    engine.dispose()


def _wait_for(client: TestClient, check: Any) -> Any:
    deadline = wall.monotonic() + 10
    while wall.monotonic() < deadline:
        found = check(client.get("/api/booking/overview").json())
        if found:
            return found
        wall.sleep(0.05)
    raise AssertionError("the scan did not run")


def test_the_server_scans_transport_pro_on_its_own(store, monkeypatch: pytest.MonkeyPatch) -> None:  # type: ignore[no-untyped-def]
    settings, _ = store
    fake = ContextTPro([upcoming_load()], {})
    monkeypatch.setattr(tpro_client.TransportProClient, "from_settings", lambda *a, **k: fake)
    application = create_app(settings, scan_every=60, scan_scope=LIDL)
    with TestClient(application) as client:
        scan_state = _wait_for(client, lambda o: o["scan"] if o["scan"]["ok"] else None)
        assert scan_state["every"] == 60 and scan_state["loads"] == 1 and scan_state["created"] == 1
        rows = client.get("/api/booking/cases").json()
    assert [(r["vendor"], r["status"]) for r in rows] == [("Koch Foods, Inc.", "unscheduled")]
    # Lidl's pod and inbound customer only, every time: the open loads, then the canceled ones.
    assert fake.calls and all("'customer_id'" in c and "'terminal_id'" in c for c in fake.calls)
    canceled = ["load_status" in c for c in fake.calls]
    assert canceled == sorted(canceled) and 0 < sum(canceled) < len(canceled)


def test_a_failed_check_shows_on_the_board_which_keeps_serving(
    store, monkeypatch: pytest.MonkeyPatch
) -> None:  # type: ignore[no-untyped-def]
    settings, _ = store

    def unreachable(*_a: object, **_k: object) -> None:
        raise ConnectionError("Transport Pro did not answer")

    monkeypatch.setattr(tpro_client.TransportProClient, "from_settings", unreachable)
    application = create_app(settings, scan_every=30, scan_scope=LIDL)
    with TestClient(application) as client:
        scan_state = _wait_for(client, lambda o: o["scan"] if o["scan"]["ok"] is False else None)
        assert scan_state["every"] == 30 and scan_state["at"]
        assert "did not answer" not in str(scan_state)  # the reason goes to the log, not the page
        assert client.get("/api/booking/cases").status_code == 200


def test_without_scan_every_the_board_does_not_check(store) -> None:  # type: ignore[no-untyped-def]
    settings, _ = store
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/booking/overview").json()["scan"] is None


class StoreWatchingTPro(FakeTPro):
    """Notes, each time it is asked for loads, whether anyone holds the store for writing."""

    def __init__(self, loads: list[dict[str, Any]], path: str) -> None:
        super().__init__(loads, {})
        self.path = path
        self.store_free: list[bool] = []

    def iter_loads(self, **filters: Any) -> Iterator[Load]:
        con = sqlite3.connect(self.path, timeout=0)
        try:
            con.execute("BEGIN IMMEDIATE")
            con.rollback()
            self.store_free.append(True)
        except sqlite3.OperationalError:  # database is locked
            self.store_free.append(False)
        finally:
            con.close()
        yield from super().iter_loads(**filters)


def test_a_scan_does_not_hold_the_store_while_transport_pro_answers(store) -> None:  # type: ignore[no-untyped-def]
    settings, sessions = store
    fake = StoreWatchingTPro([upcoming_load()], settings.database_url.split("///", 1)[1])
    stats = scan(fake, sessions, settings, terminal_ids=[1089], customer_ids=[7211])  # type: ignore[arg-type]
    assert stats.created == 1  # it wrote to the store...
    assert fake.store_free and all(fake.store_free)  # ...never while Transport Pro answered


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    import uvicorn

    for name in (
        "FP_CUSTOMERS",
        "FP_PILOT_TERMINAL_IDS",
        "FP_PILOT_CUSTOMER_IDS",
        "FP_GOOGLE_CLIENT_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.chdir(tmp_path)
    served: list[Any] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: served.append(app))
    get_settings.cache_clear()
    yield served
    get_settings.cache_clear()


def test_serve_scans_only_when_asked_and_only_named_loads(cli_env: list[Any]) -> None:
    runner = CliRunner()
    nobody = runner.invoke(cli_app, ["serve", "--scan-every", "30"])
    assert nobody.exit_code == 2 and "whose loads to check" in nobody.output
    assert not cli_env  # refused before anything served: it would have taken every load

    lidl = runner.invoke(cli_app, ["serve", "--scan-every", "30", "--scan-customer", "lidl"])
    assert lidl.exit_code == 0, lidl.output
    assert "checked for new pickups every 30 min (terminals 1089; customers 7211)" in lidl.output
    assert cli_env[-1].state.scan == {"every": 30.0, "at": None, "ok": None}

    plain = runner.invoke(cli_app, ["serve"])
    assert plain.exit_code == 0 and "Transport Pro is checked" not in plain.output
    assert cli_env[-1].state.scan is None
