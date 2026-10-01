"""The appointments board: its page, its API (overview, filters, one case) and the decisions."""

from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.api.booking import stage
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.worklist import flag
from facility_profiles.cli import app as cli_app
from facility_profiles.config import get_settings
from facility_profiles.storage.db import (
    init_db,
    make_engine,
    session_factory,
    session_scope,
)

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)  # Tuesday, 08:00 in New York
ROOT = Path(__file__).resolve().parents[1]


def _case(
    session,  # type: ignore[no-untyped-def]
    n: int,
    *,
    status: str,
    requested: str,
    vendor: str = "Koch Foods, Inc.",
    customer: str = "Lidl - Inbound",
    confirmed: str | None = None,
    request: bool = False,
    reason: str | None = None,
) -> BookingCase:
    case = BookingCase(
        load_id=2_590_000 + n,
        waypoint_index=0,
        customer_name=customer,
        vendor_name=vendor,
        vendor_city="Erlanger, KY",
        vendor_timezone="America/New_York",
        po_numbers=[f"11580{n}102660"],
        booking_method="email",
        contact_email=f"desk{n}@vendor.example",
        delivery_site="PYE RDC (Perryville, MD)",
        delivery_ref=f"PYE_02102690{n}",
        delivery_at_utc=datetime(2026, 10, 2, 11, 30, tzinfo=UTC),
        requested_local=requested,
        confirmed_local=confirmed,
        confirmed_start_utc=(
            datetime.strptime(confirmed, "%Y-%m-%d %H:%M")
            .replace(tzinfo=ZoneInfo("America/New_York"))
            .astimezone(UTC)
            if confirmed
            else None
        ),
        pickup_number="20463798" if confirmed else None,
        status=status,
        reason=reason,
    )
    session.add(case)
    session.flush()
    case.events.append(BookingEvent(action="scanned", detail={"status": status}))
    if request:
        case.messages.append(
            BookingMessage(direction="out", kind="request", subject=f"Pick Up Appointment {n}")
        )
    session.flush()  # the scan comes before anything raised on the case
    return case


@pytest.fixture
def board(settings, tmp_path: Path):  # type: ignore[no-untyped-def]
    """A store with one case in each situation the board shows, and a client on it."""
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    ids: dict[str, int] = {}
    with session_scope(session_factory(engine)) as s:
        drafted = _case(s, 1, status="unscheduled", requested="2026-09-30 09:00", request=True)
        late = _case(s, 2, status="pending", requested="2026-09-28 09:00", vendor="Morgan Foods")
        review = _case(
            s, 3, status="pending", requested="2026-10-01 09:00", confirmed="2026-10-01 11:00"
        )
        flag(s, review, ExceptionType.CONFIRMATION_REVIEW, "vendor confirmed 2026-10-01 11:00")
        booked = _case(
            s, 4, status="scheduled", requested="2026-10-02 08:00", confirmed="2026-10-02 08:00"
        )
        no_desk = _case(
            s, 5, status="unscheduled", requested="2026-10-03 09:00", customer="Other Co"
        )
        flag(s, no_desk, ExceptionType.MISSING_METHOD, "no verified email booking desk")
        gone = _case(s, 6, status="canceled", requested="2026-09-30 10:00", reason="not needed")
        declined = _case(
            s,
            7,
            status="declined",
            requested="2026-10-01 07:00",
            reason="vendor cannot book: PO not ready",
        )
        flag(s, declined, ExceptionType.FACILITY_DECLINED, "vendor cannot book: PO not ready")
        for name, case in [
            ("drafted", drafted),
            ("late", late),
            ("review", review),
            ("booked", booked),
            ("no_desk", no_desk),
            ("gone", gone),
            ("declined", declined),
        ]:
            ids[name] = case.id
    engine.dispose()
    application = create_app(settings.model_copy(update={"database_url": url}))
    application.state.clock = lambda: NOW
    with TestClient(application) as client:
        yield client, ids


def test_the_page_is_served_and_home_goes_to_it(board):  # type: ignore[no-untyped-def]
    client, _ = board
    home = client.get("/", follow_redirects=False)
    assert home.status_code in (302, 307) and home.headers["location"] == "/app/"
    page = client.get("/app/")
    assert page.status_code == 200 and '<script type="module" src="app.js">' in page.text
    assert client.get("/app/app.js").status_code == 200
    assert client.get("/app/app.css").status_code == 200
    labels = {k["kind"]: k["label"] for k in client.get("/api/booking/kinds").json()}
    assert len(labels) == len(ExceptionType)
    assert labels["confirmation_review"] == "Approve confirmation"
    assert client.get("/api/booking/customers").json() == ["Lidl - Inbound", "Other Co"]


def test_overview_counts_what_needs_a_person_what_slipped_and_what_is_coming(board):  # type: ignore[no-untyped-def]
    client, ids = board
    data = client.get("/api/booking/overview").json()
    assert data["counts"] == {
        "total": 7,
        "needs_action": 3,
        "past_due": 1,
        "not_requested": 1,
        "drafts_waiting": 1,
        "waiting_on_vendor": 1,
        "booked_upcoming": 1,
        "booked": 1,
        "declined": 1,
        "upcoming": 5,
    }
    # To-dos by the pickup they hold up, soonest first.
    assert [t["kind"] for t in data["todos"]] == [
        "facility_declined",
        "confirmation_review",
        "missing_method",
    ]
    assert data["todos"][1]["case"]["id"] == ids["review"]
    assert data["todos"][1]["label"] == "Approve confirmation" and data["todos"][1]["hint"]
    assert [r["id"] for r in data["past_due"]] == [ids["late"]]
    # A slipped pickup is past due, not coming up; a canceled one is neither.
    upcoming = [r["id"] for r in data["upcoming"]]
    assert ids["late"] not in upcoming and ids["gone"] not in upcoming
    assert upcoming[0] == ids["drafted"] and len(upcoming) == 5
    assert {k["kind"]: k["count"] for k in data["todos_by_kind"]} == {
        "facility_declined": 1,
        "confirmation_review": 1,
        "missing_method": 1,
    }
    other = client.get("/api/booking/overview", params={"customer": "Other Co"}).json()
    assert other["counts"]["total"] == 1 and other["counts"]["needs_action"] == 1


def test_the_list_filters_and_sorts_by_pickup(board):  # type: ignore[no-untyped-def]
    client, ids = board

    def listed(**params: Any) -> list[int]:
        response = client.get("/api/booking/cases", params=params)
        assert response.status_code == 200, response.text
        return [r["id"] for r in response.json()]

    everything = listed()
    assert everything[0] == ids["late"] and len(everything) == 7
    assert listed(status="pending") == [ids["late"], ids["review"]]
    assert set(listed(exception="any")) == {ids["review"], ids["no_desk"], ids["declined"]}
    assert listed(exception="missing_method") == [ids["no_desk"]]
    assert listed(q="morgan") == [ids["late"]]
    assert listed(q="115803") == [ids["review"]]
    assert listed(q=str(2_590_004)) == [ids["booked"]]
    assert listed(start="2026-10-02", end="2026-10-02") == [ids["booked"]]
    assert listed(past_due="true") == [ids["late"]]
    assert listed(customer="Other Co") == [ids["no_desk"]]
    assert client.get("/api/booking/cases", params={"start": "10/02"}).status_code == 422
    row = client.get("/api/booking/cases", params={"status": "unscheduled"}).json()[0]
    assert row["draft_ready"] is True and row["stage"] == "Draft waiting to be sent"
    assert row["pickup_source"] == "requested" and row["pickup_date"] == "2026-09-30"


def test_one_case_in_full(board):  # type: ignore[no-untyped-def]
    client, ids = board
    detail = client.get(f"/api/booking/cases/{ids['review']}").json()
    assert detail["can_approve"] is True
    assert detail["stage"] == "Confirmed by the vendor, needs approval"
    assert detail["pickup_local"] == "2026-10-01 11:00" and detail["pickup_source"] == "confirmed"
    assert [e["kind"] for e in detail["exceptions"]] == ["confirmation_review"]
    assert [t["type"] for t in detail["timeline"]] == ["event", "raised"]
    assert detail["timeline"][1]["title"] == "To-do: Approve confirmation"
    drafted = client.get(f"/api/booking/cases/{ids['drafted']}").json()
    assert drafted["messages"][0]["kind"] == "request" and drafted["can_approve"] is False
    assert client.get("/api/booking/cases/999").status_code == 404


def test_decisions_are_recorded_with_who_made_them(board):  # type: ignore[no-untyped-def]
    client, ids = board
    base = "/api/booking/cases"
    approved = client.post(f"{base}/{ids['review']}/approve", json={"by": "Megan"}).json()
    assert approved["status"] == "scheduled" and approved["open_exceptions"] == []
    assert approved["exceptions"][0]["resolved_by"] == "Megan"
    again = client.post(f"{base}/{ids['review']}/approve", json={"by": "Megan"})
    assert again.status_code == 409 and "nothing to approve" in again.json()["detail"]

    body = {"by": "Megan", "kind": "missing_method", "note": "desk is ops@vendor.example"}
    resolved = client.post(f"{base}/{ids['no_desk']}/resolve", json=body).json()
    assert resolved["open_exceptions"] == [] and resolved["status"] == "unscheduled"
    assert client.post(f"{base}/{ids['no_desk']}/resolve", json=body).status_code == 409

    booking = {"by": "Megan", "via": "phone", "date": "2026-10-05", "time": "09:00"}
    booked = client.post(f"{base}/{ids['declined']}/booked", json=booking).json()
    assert booked["status"] == "scheduled" and booked["confirmed_local"] == "2026-10-05 09:00"
    assert booked["stage"] == "Booked by phone"
    no_date = {"by": "Megan", "via": "phone", "time": "09:00"}
    assert client.post(f"{base}/{ids['late']}/booked", json=no_date).status_code == 422

    canceled = client.post(f"{base}/{ids['drafted']}/cancel", json={"by": "Megan", "reason": "x"})
    assert canceled.json()["status"] == "canceled"
    assert (
        client.post(
            f"{base}/{ids['drafted']}/cancel", json={"by": "Megan", "reason": "x"}
        ).status_code
        == 409
    )
    assert (
        client.post(f"{base}/{ids['drafted']}/booked", json={"by": "M", "via": "x"}).status_code
        == 409
    )

    # Nobody decides anonymously, and a kind must exist.
    assert client.post(f"{base}/{ids['late']}/approve", json={"by": "  "}).status_code == 422
    wrong = {"by": "Megan", "kind": "nonsense", "note": "x"}
    assert client.post(f"{base}/{ids['late']}/resolve", json=wrong).status_code == 422
    assert client.post(f"{base}/999/approve", json={"by": "Megan"}).status_code == 404

    # Stored, not just answered: a fresh read sees the decisions.
    after = client.get("/api/booking/overview").json()["counts"]
    assert after["needs_action"] == 0 and after["booked"] == 3


def test_stage_says_who_the_case_is_waiting_on():
    def case(status: str, *, reason: str | None = None) -> BookingCase:
        return BookingCase(status=status, reason=reason, po_numbers=[], messages=[], exceptions=[])

    question = case(CaseStatus.PENDING.value)
    question.exceptions.append(
        _exception(ExceptionType.FACILITY_QUESTION, "vendor asked: which door?")
    )
    assert stage(question) == "Waiting on us: vendor question"
    deferred = case(CaseStatus.PENDING.value, reason="vendor asked to check back on 2026-10-05")
    assert stage(deferred) == "Vendor asked to check back on 2026-10-05"
    assert stage(case(CaseStatus.PENDING.value)) == "Waiting on the vendor"
    blocked = case(CaseStatus.UNSCHEDULED.value)
    blocked.exceptions.append(_exception(ExceptionType.MISSING_METHOD, "no desk"))
    assert stage(blocked) == "Not requested: no booking desk"
    assert stage(case(CaseStatus.UNSCHEDULED.value)) == "Not requested yet"
    assert stage(case(CaseStatus.SCHEDULED.value)) == "Booked"
    assert stage(case(CaseStatus.DECLINED.value)) == "Vendor cannot book"
    assert stage(case(CaseStatus.CANCELED.value, reason="not needed")) == "Not needed"


def _exception(kind: ExceptionType, description: str):  # type: ignore[no-untyped-def]
    from facility_profiles.booking.models import CaseException

    return CaseException(kind=kind.value, description=description, detail={})


def test_serve_starts_the_board_on_the_store_asked_for(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import uvicorn

    calls: list[dict[str, Any]] = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append({"app": app, **kw}))
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        db = f"sqlite:///{(tmp_path / 'served.db').as_posix()}"
        result = CliRunner().invoke(cli_app, ["serve", "--port", "8123", "--db", db])
        assert result.exit_code == 0, result.output
        assert "http://127.0.0.1:8123/app/" in result.output and db in result.output
        assert calls[0]["port"] == 8123 and calls[0]["host"] == "127.0.0.1"
        assert (tmp_path / "served.db").exists()
    finally:
        get_settings.cache_clear()


def test_the_demo_seeder_builds_every_situation_and_refuses_a_used_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
):
    spec = importlib.util.spec_from_file_location(
        "seed_demo", ROOT / "scripts" / "seed_booking_demo.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    db = f"sqlite:///{(tmp_path / 'demo.db').as_posix()}"
    monkeypatch.setattr(sys, "argv", ["seed", "--db", db])
    assert module.main() == 0
    assert "17 demo cases" in capsys.readouterr().out
    engine = make_engine(db)
    with session_scope(session_factory(engine)) as s:
        statuses = sorted(c.status for c in s.query(BookingCase))
        kinds = sorted(e.kind for c in s.query(BookingCase) for e in c.open_exceptions)
    engine.dispose()
    assert statuses.count("scheduled") == 2 and statuses.count("declined") == 1
    assert statuses.count("canceled") == 1 and len(statuses) == 17
    assert kinds == sorted(
        [
            "confirmation_review",
            "confirmation_review",
            "facility_question",
            "facility_declined",
            "missing_method",
            "method_not_supported",
            "stale_confirmation",
            "slot_unworkable",
        ]
    )
    assert module.main() == 1  # never mixed into a store that already has cases
