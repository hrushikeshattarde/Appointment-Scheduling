"""Reference numbers: every number on a pickup in one place, with where it came from."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, BookingReference
from facility_profiles.booking.references import (
    ReferenceSource,
    active,
    case_numbers,
    find_cases,
    record_reference,
)
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    add_reference,
    draft_case,
    ingest,
    list_cases,
    mark_booked,
    mark_sent,
    scan,
)
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, reply, seed_vendor

INTERNAL = ["circledelivers.com"]


def _scanned(settings, sessions, *, pickup_number: str | None = None) -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    load = lidl_load(7301, po="226321092660")
    if pickup_number:
        load["reference"]["pickupNumber"] = pickup_number
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        return list_cases(session)[0].id


def _rows(case: BookingCase) -> list[tuple[str, str, str, bool]]:
    return [(r.kind, r.value, r.source, r.replaced_at is None) for r in case.references]


def test_a_scanned_load_brings_its_numbers(settings, sessions):
    case_id = _scanned(settings, sessions, pickup_number="CCI-77")
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert _rows(case) == [
            ("po_number", "226321092660", "load", True),
            ("delivery_number", "PYE_021026123", "load", True),
            ("pickup_number", "CCI-77", "load", True),
        ]
        assert [(n.label, n.value, n.said) for n in case_numbers(case)][:2] == [
            ("Load#", "7301", "from the load"),
            ("PO#", "226321092660", "from the load"),
        ]


def test_a_new_pickup_number_from_the_vendor_replaces_the_old_one_and_keeps_it(settings, sessions):
    case_id = _scanned(settings, sessions)
    readings = {
        "SET! PU# 4411": ReplyClassification(
            status=ReplyStatus.CONFIRMED, pickup_number="4411", quotes=["SET! PU# 4411"]
        ),
        "Moved to PU# 4523": ReplyClassification(
            status=ReplyStatus.CONFIRMED, pickup_number="4523", quotes=["Moved to PU# 4523"]
        ),
    }

    def script(ctx: ReplyContext) -> ReplyClassification:
        return readings[ctx.body]

    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)
        classifier = FakeReplyClassifier(script)
        ingest(session, [reply("SET! PU# 4411", mid="a")], classifier, internal_domains=INTERNAL)
        ingest(
            session, [reply("Moved to PU# 4523", mid="b")], classifier, internal_domains=INTERNAL
        )
        pickups = [r for r in case.references if r.kind == "pickup_number"]
        assert [(r.value, r.source, r.replaced_at is None) for r in pickups] == [
            ("4411", "vendor", False),
            ("4523", "vendor", True),
        ]
        replies = [m for m in case.messages if m.direction == "in"]
        assert [r.message_id for r in pickups] == [m.id for m in replies]
        assert case.pickup_number == "4523"  # the case's column follows the current number
        was = [n for n in case_numbers(case) if not n.current]
        assert [(n.label, n.value) for n in was] == [("PU#", "4411")]
        # The old number still finds the case.
        assert [c.id for c in find_cases(session, "4411")] == [case_id]


def test_the_customer_desk_moves_the_delivery_reference(settings, sessions):
    case_id = _scanned(settings, sessions)
    desk_mail = InboundMessage(
        message_id="desk1",
        thread_id="lidl-desk",
        sent_at=datetime(2026, 9, 30, 13, 0, tzinfo=UTC),
        from_addr="Inbound <inbound@lidl.us>",
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="RE: RESCHEDULE 226321092660",
        body="PO 226321092660: 10/2 1100 - PYE_021026777",
        rfc_message_id="<desk1@lidl.test>",
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [desk_mail],
            FakeReplyClassifier(lambda _ctx: ReplyClassification(status=ReplyStatus.UNRELATED)),
            internal_domains=INTERNAL,
            customer_desk="inbound@lidl.us",
        )
        case = session.get(BookingCase, case_id)
        assert case is not None
        delivery = [r for r in case.references if r.kind == "delivery_number"]
        assert [(r.value, r.source, r.replaced_at is None) for r in delivery] == [
            ("PYE_021026123", "load", False),
            ("PYE_021026777", "customer_desk", True),
        ]
        assert delivery[1].message_id == case.messages[-1].id
        assert case.delivery_ref == "PYE_021026777"


def test_people_add_portal_ids_confirmations_and_pickup_numbers(settings, sessions):
    case_id = _scanned(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        add_reference(session, case, "portal_appointment_id", "OD-88123", by="megan")
        add_reference(session, case, "confirmation_number", "326673", by="megan")
        mark_booked(session, case, by="vera", via="phone", pickup_number="TEL-1")
        assert case.reference_numbers == {
            "portal_appointment_id": "OD-88123",
            "confirmation_number": "326673",
        }
        assert case.pickup_number == "TEL-1"
        added = [(n.label, n.value, n.said) for n in case_numbers(case) if n.source == "person"]
        assert added == [
            ("Portal appointment#", "OD-88123", "added by megan"),
            ("Confirmation#", "326673", "added by megan"),
            ("PU#", "TEL-1", "added by vera"),
        ]
        # The same number again is not a new row; a blank one is ignored.
        before = len(case.references)
        record_reference(
            session, case, "portal_appointment_id", " OD-88123 ", source=ReferenceSource.PERSON
        )
        assert (
            record_reference(session, case, "bol_number", "  ", source=ReferenceSource.PERSON)
            is None
        )
        assert len(case.references) == before
        assert [c.id for c in find_cases(session, "od-88123")] == [case_id]  # any case, any kind
        assert [c.id for c in find_cases(session, "7301")] == [case_id]  # the load number
        assert find_cases(session, "nothing-like-it") == [] and find_cases(session, " ") == []
        with pytest.raises(ValueError, match="reference type must be one of"):
            record_reference(session, case, "fax_number", "1", source=ReferenceSource.PERSON)


def test_stores_from_before_get_their_numbers_once(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    with session_scope(session_factory(engine)) as session:
        case = BookingCase(
            load_id=99,
            waypoint_index=0,
            po_numbers=["115802102660", "115802102661"],
            delivery_ref="PYE_061026919",
            pickup_number="20463798",
            reference_numbers={"shipment_number": "TI-55"},
        )
        session.add(case)
        session.flush()
        # A store written before the numbers were kept: drop the rows, keep the columns.
        for row in list(case.references):
            session.delete(row)
    engine.dispose()

    for _ in range(2):  # the second start finds nothing to fill
        engine = make_engine(url)
        init_db(engine)
        engine.dispose()
    engine = make_engine(url)
    with session_scope(session_factory(engine)) as session:
        rows = session.scalars(select(BookingReference).order_by(BookingReference.id)).all()
        assert [(r.kind, r.value, r.source) for r in rows] == [
            ("po_number", "115802102660", "load"),
            ("po_number", "115802102661", "load"),
            ("delivery_number", "PYE_061026919", "load"),
            ("pickup_number", "20463798", "migration"),
            ("shipment_number", "TI-55", "migration"),
        ]
        case = session.scalars(select(BookingCase)).one()
        assert len(active(case, "po_number")) == 2  # both POs are current at once
    engine.dispose()


def test_find_a_case_by_any_number_from_the_command_line_and_the_board(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings
):
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    case_id = _scanned(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        add_reference(session, case, "shipment_number", "TI-4471902", by="megan")
        mark_booked(session, case, by="megan", via="phone", pickup_number="4411")
        mark_booked(session, case, by="megan", via="phone", pickup_number="4523")
    engine.dispose()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        found = runner.invoke(app, ["booking", "find", "ti-4471902"])
        assert found.exit_code == 0 and found.output.startswith(f"#{case_id}")
        assert runner.invoke(app, ["booking", "find", "4411"]).output.startswith(f"#{case_id}")
        missing = runner.invoke(app, ["booking", "find", "000"])
        assert missing.exit_code == 1 and "no case carries 000" in missing.output
        shown = runner.invoke(app, ["booking", "show", str(case_id)]).output
        assert "number    PU# 4523  (added by megan)" in shown
        assert "was       PU# 4411  (added by megan, replaced " in shown
        assert "number    Delivery# PYE_021026123  (from the load)" in shown
    finally:
        get_settings.cache_clear()

    application = create_app(settings.model_copy(update={"database_url": url}))
    with TestClient(application) as client:
        rows = client.get("/api/booking/cases", params={"q": "TI-4471902"}).json()
        assert [r["id"] for r in rows] == [case_id]
        assert [r["id"] for r in client.get("/api/booking/cases", params={"q": "4411"}).json()] == [
            case_id
        ]
        refs = client.get(f"/api/booking/cases/{case_id}").json()["references"]
        pickups = [(r["value"], r["current"]) for r in refs if r["kind"] == "pickup_number"]
        assert pickups == [("4523", True), ("4411", False)]  # current first, then history
