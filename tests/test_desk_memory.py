"""Desk memory: a booking that worked teaches the vendor profile its desk and method."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.memory import desk_history, normalize_desk, remember_booking
from facility_profiles.booking.models import BookingCase, CaseStatus, DeskMemory
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    approve,
    draft_case,
    ingest,
    list_cases,
    mark_booked,
    mark_sent,
    scan,
)
from facility_profiles.booking.timers import sweep
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.models import AuditEntry
from facility_profiles.storage.repository import Repository, unwrap
from tests.conftest import FakeTPro
from tests.test_booking import NOW, koch_key, lidl_load, reply, seed_vendor

INTERNAL = ["circledelivers.com"]


def _two_cases(settings, sessions, *, with_email: bool) -> tuple[int, int]:  # type: ignore[no-untyped-def]
    """Two Koch Foods pickups scanned together; without a desk both wait for one."""
    seed_vendor(sessions, with_email=with_email)
    loads = [lidl_load(7301, po="226321092660"), lidl_load(7302, po="226322092660")]
    scan(FakeTPro(loads, {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        first, second = sorted(c.id for c in list_cases(session))
    return first, second


def _profile(session) -> dict[str, tuple[object, str]]:  # type: ignore[no-untyped-def]
    fields = Repository(session).fields(koch_key(), Role.SHIPPER)
    return {name: (unwrap(f.value), f.state) for name, f in fields.items()}


def test_a_desk_is_kept_the_way_the_profile_keeps_it():
    assert normalize_desk("email", " Shipping@Vendor.Example ") == "shipping@vendor.example"
    assert normalize_desk("phone", "(812) 794 1152") == "812-794-1152"
    assert normalize_desk("web_portal", "schedule.opendock.com/w/1") == (
        "https://schedule.opendock.com/w/1"
    )
    assert normalize_desk("phone", "call Natosha") is None
    assert normalize_desk("fcfs", "anything") is None


def test_an_approved_email_booking_is_counted_and_a_trusted_profile_is_left_alone(
    settings, sessions
):
    case_id, _ = _two_cases(settings, sessions, with_email=True)
    confirm = FakeReplyClassifier(
        lambda _ctx: ReplyClassification(
            status=ReplyStatus.CONFIRMED, pickup_number="CCI-1", quotes=["SET! PU# CCI-1"]
        )
    )
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)
        ingest(session, [reply("SET! PU# CCI-1")], confirm, internal_domains=INTERNAL)
        approve(session, case, by="megan")
        rows = desk_history(session, koch_key())
        assert [(r.method, r.desk, r.worked_count, r.last_case_id) for r in rows] == [
            ("email", "cci@udfinc.com", 1, case_id)
        ]
        assert _profile(session)["contact_email"] == ("cci@udfinc.com", FieldState.HUMAN_SET.value)
        audit = session.scalars(select(AuditEntry).where(AuditEntry.action == "learned")).all()
        assert audit == []  # nothing was missing


def test_a_desk_a_person_booked_with_is_learned_and_unblocks_the_vendors_other_case(
    settings, sessions
):
    first, second = _two_cases(settings, sessions, with_email=False)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, first)
        other = session.get(BookingCase, second)
        assert case is not None and other is not None
        assert [e.kind for e in other.open_exceptions] == ["missing_method"]

        learned = mark_booked(
            session,
            case,
            by="megan",
            via="email",
            local="2026-10-01 09:00",
            desk="CCI@udfinc.com",
        )
        assert learned.filled == ["booking_method", "contact_email"]
        assert learned.unblocked == [second]
        assert _profile(session) == {
            "booking_method": ("email", FieldState.HUMAN_SET.value),
            "contact_email": ("cci@udfinc.com", FieldState.HUMAN_SET.value),
        }
        reasons = {
            a.reason
            for a in session.scalars(select(AuditEntry).where(AuditEntry.action == "learned"))
        }
        assert reasons == {f"booked case #{first} by email with cci@udfinc.com"}

        # The other pickup at this vendor now has the desk, and the agent can request it.
        assert other.contact_email == "cci@udfinc.com" and other.booking_method == "email"
        assert other.open_exceptions == []
        assert other.exceptions[0].resolution == (
            f"desk on file now: cci@udfinc.com (learned from case #{first})"
        )
        mailer = RecordingMailer()
        draft_case(session, other, mailer, settings, now=NOW)
        assert mailer.drafts[0].to_addr == "cci@udfinc.com"


def test_a_phone_booking_fills_only_what_the_profile_lacks_and_counts_once_per_case(
    settings, sessions
):
    first, second = _two_cases(settings, sessions, with_email=True)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, first)
        assert case is not None
        learned = mark_booked(session, case, by="megan", via="phone", desk="(812) 794-1152")
        assert learned.filled == ["contact_phone"]  # the email method stays as it was
        assert learned.unblocked == []
        profile = _profile(session)
        assert profile["booking_method"] == ("email", FieldState.HUMAN_SET.value)
        assert profile["contact_phone"] == ("812-794-1152", FieldState.HUMAN_SET.value)

        mark_booked(session, case, by="megan", via="phone", desk="812-794-1152", note="moved")
        other = session.get(BookingCase, second)
        assert other is not None
        mark_booked(session, other, by="vera", via="phone", desk="812 794 1152")
        row = session.scalars(select(DeskMemory)).one()
        assert (row.method, row.desk, row.worked_count, row.last_by) == (
            "phone",
            "812-794-1152",
            2,
            "vera",
        )
        mark_booked(session, other, by="vera", via="other")  # nothing to learn from "other"
        assert session.scalars(select(DeskMemory)).one().worked_count == 2


def test_a_portal_booking_files_the_url_and_its_system(settings, sessions):
    first, _ = _two_cases(settings, sessions, with_email=False)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, first)
        assert case is not None
        learned = mark_booked(
            session, case, by="megan", via="portal", desk="schedule.opendock.com/w/220"
        )
        assert learned.filled == ["booking_method", "portal_url", "portal_vendor"]
        assert learned.unblocked == []  # the agent cannot book on a portal
        assert _profile(session) == {
            "booking_method": ("web_portal", FieldState.HUMAN_SET.value),
            "portal_url": ("https://schedule.opendock.com/w/220", FieldState.HUMAN_SET.value),
            "portal_vendor": ("opendock", FieldState.HUMAN_SET.value),
        }


def test_a_facility_the_harvest_never_stored_is_created_from_the_case(session):
    case = BookingCase(
        load_id=1,
        waypoint_index=0,
        po_numbers=["1"],
        facility_key="candidate:abc123",
        vendor_name="New Vendor Co",
        vendor_city="Austin, IN",
        vendor_timezone="America/Indiana/Indianapolis",
        status=CaseStatus.SCHEDULED.value,
    )
    other = BookingCase(load_id=2, waypoint_index=0, po_numbers=["2"], facility_key="demo:2")
    session.add_all([case, other])
    session.flush()
    learned = remember_booking(session, case, method="email", desk="desk@new.example", by="m")
    assert learned.filled == ["booking_method", "contact_email"]
    record = Repository(session).get_facility("candidate:abc123")
    assert record is not None and (record.company_name, record.city, record.state) == (
        "New Vendor Co",
        "Austin",
        "IN",
    )
    # A key that is not a facility key is remembered without a profile to teach.
    kept = remember_booking(session, other, method="phone", desk=None, by="m")
    assert (kept.worked_count, kept.filled) == (1, [])
    assert remember_booking(session, other, method=None, desk=None, by="m").worked_count == 0


def test_a_desk_filed_by_hand_later_reaches_the_waiting_case(settings, sessions):  # type: ignore[no-untyped-def]
    first, _ = _two_cases(settings, sessions, with_email=False)
    with session_scope(sessions) as session:
        repo = Repository(session)
        for field, value in (("booking_method", "email"), ("contact_email", "cci@udfinc.com")):
            repo.set_field_human(koch_key(), Role.SHIPPER, field, value, state=FieldState.HUMAN_SET)
        result = sweep(session, now=NOW, settings=settings)
        assert sorted((k, why) for _, k, why in result.resolved) == [
            ("missing_method", "desk on file now: cci@udfinc.com"),
            ("missing_method", "desk on file now: cci@udfinc.com"),
        ]
        case = session.get(BookingCase, first)
        assert case is not None and case.contact_email == "cci@udfinc.com"


# ------------------------------------------------------------------ CLI and board


def _cli_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings) -> tuple[str, int, int]:  # type: ignore[no-untyped-def]
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    first, second = _two_cases(settings, session_factory(engine), with_email=False)
    engine.dispose()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    return url, first, second


def test_booked_with_a_desk_and_the_desk_list_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings
):
    _, first, second = _cli_store(tmp_path, monkeypatch, settings)
    runner = CliRunner()
    try:
        assert "nothing booked yet" in runner.invoke(app, ["booking", "desks"]).output
        booked = ["booking", "booked", str(first), "--by", "megan", "--via", "email"]
        result = runner.invoke(app, [*booked, "--desk", "cci@udfinc.com"])
        assert result.exit_code == 0, result.output
        assert "the vendor profile learned its booking_method, contact_email" in result.output
        assert f"case(s) #{second} now have a desk" in result.output
        listed = runner.invoke(app, ["booking", "desks"]).output
        assert "Koch Foods, Inc." in listed and "email" in listed and "cci@udfinc.com" in listed
        assert f"(case #{first})" in listed
    finally:
        get_settings.cache_clear()


def test_profile_set_gives_the_waiting_cases_their_desk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings
):
    _, first, second = _cli_store(tmp_path, monkeypatch, settings)
    runner = CliRunner()
    try:
        set_method = ["profile", "set", koch_key(), "shipper", "booking_method"]
        result = runner.invoke(app, [*set_method, "--value", "email", "--by", "megan"])
        assert "now have a desk" not in result.output  # no address yet
        set_desk = ["profile", "set", koch_key(), "shipper", "contact_email"]
        result = runner.invoke(app, [*set_desk, "--value", "cci@udfinc.com", "--by", "megan"])
        assert result.exit_code == 0, result.output
        assert f"case(s) #{first}, #{second} now have a desk" in result.output
    finally:
        get_settings.cache_clear()


def test_the_board_takes_the_desk_and_shows_how_the_vendor_was_booked(settings, tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    first, second = _two_cases(
        settings.model_copy(update={"database_url": url}),
        session_factory(engine),
        with_email=False,
    )
    engine.dispose()
    application = create_app(settings.model_copy(update={"database_url": url}))
    with TestClient(application) as client:
        booked = client.post(
            f"/api/booking/cases/{first}/booked",
            json={"by": "megan", "via": "phone", "desk": "812 794 1152"},
        ).json()
        assert booked["status"] == "scheduled"
        assert booked["desk_history"] == [
            {
                "method": "phone",
                "desk": "812-794-1152",
                "worked_count": 1,
                "last_worked_at": booked["desk_history"][0]["last_worked_at"],
                "last_case_id": first,
            }
        ]
        titles = [t["title"] for t in booked["timeline"]]
        assert "Desk remembered" in titles
        other = client.get(f"/api/booking/cases/{second}").json()
        assert other["desk_history"][0]["method"] == "phone"  # same vendor, same history
