"""The pickup time to ask for: tender, PO floor, usual time, hours, delivery; and infeasible loads."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest
from typer.testing import CliRunner

from facility_profiles.api.booking import case_detail
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.recommend import (
    facility_history,
    recommend_time,
    usual_time,
)
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.rules import VendorProfile
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases, scan
from facility_profiles.booking.worklist import KINDS, open_kinds
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.domain.schema import FacilityIdentity, Role, SourceType
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.models import SourceDocument
from facility_profiles.storage.repository import Repository
from tests.conftest import FakeTPro
from tests.test_booking import NOW, koch_key, lidl_load, seed_vendor

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"]


def a_case(**changes: Any) -> BookingCase:
    """The Koch Foods pickup: tendered Thu 10/01 09:00 ET New York, delivered Fri 10/02 09:30 ET."""
    values: dict[str, Any] = {
        "load_id": 1,
        "customer_id": 7211,
        "customer_name": "Lidl - Inbound",
        "vendor_name": "Koch Foods, Inc.",
        "vendor_timezone": "America/New_York",
        "po_numbers": ["226321092660"],
        "contact_email": "cci@udfinc.com",
        "tendered_pickup_utc": datetime(2026, 10, 1, 13, 0, tzinfo=UTC),
        "delivery_at_utc": datetime(2026, 10, 2, 13, 30, tzinfo=UTC),
        "miles": 559,  # 13.2 h with loading, so the latest pickup is Thu 20:19
    }
    values.update(changes)
    return BookingCase(**values)


def hours(opens: str, closes: str, days: list[str] = WEEKDAYS) -> VendorProfile:
    return VendorProfile(
        key="k",
        booking_method="email",
        contact_email="cci@udfinc.com",
        contact_name=None,
        appointment_required=True,
        summary=None,
        hours=[{"days": days, "open": opens, "close": closes, "by_appointment": False}],
    )


# ------------------------------------------------------------------ the rules, one by one


def test_the_tendered_time_is_asked_for_as_before(settings: Settings) -> None:
    rec = recommend_time(a_case(), settings, None, now=NOW)
    assert rec.local == "2026-10-01 09:00" and rec.feasible and rec.moved == []
    assert [s.rule for s in rec.steps] == ["tender"]
    # No tender: back from the delivery by the transit days, at the pod's default time.
    rec = recommend_time(a_case(tendered_pickup_utc=None), settings, None, now=NOW)
    assert (
        rec.local == "2026-09-30 09:00"
        and rec.steps[0].note == "2 days before the delivery on 10/02"
    )
    # Backed off to a day that has passed: the earliest pickup a driver can still make.
    later = datetime(2026, 9, 30, 15, 10, tzinfo=UTC)  # 11:10 New York; four hours' notice
    rec = recommend_time(a_case(tendered_pickup_utc=None), settings, None, now=later)
    assert rec.local == "2026-09-30 15:30" and rec.feasible
    assert (
        rec.moved[0].note
        == "that day has passed; the earliest pickup a driver can still make is Wed 09/30 15:30 ET"
    )


def test_without_a_tendered_time_the_facilitys_usual_time_is_asked_for(settings: Settings) -> None:
    midnight = a_case(tendered_pickup_utc=datetime(2026, 10, 1, 4, 0, tzinfo=UTC))
    rec = recommend_time(
        midnight, settings, None, now=NOW, history=["07:00", "07:00", "07:00", "10:00"]
    )
    assert rec.local == "2026-10-01 07:00"
    assert rec.moved[0].note == "3 of the facility's 4 confirmed appointments were at 07:00"
    # Too few, or no clear majority: the pod's default.
    assert recommend_time(
        midnight, settings, None, now=NOW, history=["07:00", "07:00"]
    ).local.endswith("09:00")  # type: ignore[union-attr]
    assert usual_time(["07:00", "07:00", "07:00", "10:00", "11:00", "12:00", "13:00"]) is None
    # A tendered time always wins over the usual one.
    assert (
        recommend_time(a_case(), settings, None, now=NOW, history=["07:00"] * 5).local
        == "2026-10-01 09:00"
    )


def test_a_time_outside_the_facilitys_hours_moves_into_them(settings: Settings) -> None:
    early = recommend_time(a_case(), settings, now=NOW, profile=hours("10:00", "16:00"))
    assert early.local == "2026-10-01 10:00"
    assert early.moved[0].note == (
        "09:00 is outside the facility's hours on Thu (1000-1600); asking for 10:00"
    )
    late = a_case(tendered_pickup_utc=datetime(2026, 10, 1, 21, 30, tzinfo=UTC))  # 17:30
    assert (
        recommend_time(late, settings, now=NOW, profile=hours("10:00", "16:00")).local
        == "2026-10-01 15:00"
    )
    closed = recommend_time(
        a_case(), settings, now=NOW, profile=hours("08:00", "12:00", days=["sat"])
    )
    # Closed on the day asked (its hours list only Saturdays): moved to the day it ships.
    assert closed.local == "2026-10-03 09:00" and closed.moved[0].rule == "closed"
    assert closed.moved[0].note == (
        "Thu 10/01 09:00 ET is a Thursday, which the facility's hours list closed; asking for "
        "Sat 10/03 09:00 ET"
    )


def test_a_time_too_late_for_the_delivery_moves_earlier_the_same_day(settings: Settings) -> None:
    evening = a_case(
        tendered_pickup_utc=datetime(2026, 10, 1, 22, 0, tzinfo=UTC),  # 18:00
        delivery_at_utc=datetime(2026, 10, 2, 8, 0, tzinfo=UTC),  # 04:00: latest pickup 14:49
    )
    rec = recommend_time(evening, settings, None, now=NOW)
    assert rec.local == "2026-10-01 14:30" and rec.feasible
    assert rec.moved[-1].rule == "fit"
    assert rec.moved[-1].note == (
        "18:00 ET would arrive after the delivery at 10/02 04:00 ET; 14:30 ET is the latest "
        "that makes it"
    )
    # Not before the facility opens, though: then no time that day makes it.
    rec = recommend_time(evening, settings, now=NOW, profile=hours("15:00", "22:00"))
    assert not rec.feasible and rec.latest == "2026-10-01 14:30"
    # Without its hours, not before the pod's start of day: never a 00:30 ask.
    night = a_case(
        tendered_pickup_utc=datetime(2026, 10, 1, 22, 0, tzinfo=UTC),  # 18:00
        delivery_at_utc=datetime(2026, 10, 1, 21, 30, tzinfo=UTC),  # 17:30: latest pickup 04:19
    )
    rec = recommend_time(night, settings, None, now=NOW)
    assert not rec.feasible and rec.latest == "2026-10-01 04:00"


def test_a_pickup_after_its_delivery_cannot_make_it(settings: Settings) -> None:
    late = a_case(tendered_pickup_utc=datetime(2026, 10, 2, 15, 0, tzinfo=UTC))  # Fri 11:00
    rec = recommend_time(late, settings, None, now=NOW)
    assert not rec.feasible and rec.local == "2026-10-02 11:00"
    assert rec.verdict == (
        "a pickup Fri 10/02 11:00 ET arrives Sat 10/03 00:10 ET, after the delivery slot Fri 10/02 09:30 ET"
    )
    assert rec.latest == "2026-10-01 20:00"


# ------------------------------------------------------------------ what the facility tells us


def test_confirmed_appointments_and_bookings_teach_the_usual_time(
    settings: Settings, session
) -> None:  # type: ignore[no-untyped-def]
    key = koch_key()
    Repository(session).upsert_facility(
        FacilityIdentity(candidate_key=key.split(":")[1], company_name="Koch Foods, Inc."),
        latitude=None,
        longitude=None,
    )

    def stop(open_utc: str, status: str) -> SourceDocument:
        text = json.dumps(
            {
                "type": "SH",
                "location": {"companyName": "Koch Foods, Inc.", "ianaTimezone": "America/New_York"},
                "appointmentTime": {
                    "open": open_utc,
                    "close": open_utc,
                    "appointmentStatus": status,
                },
            }
        )
        return SourceDocument(
            facility_key=key,
            role=Role.SHIPPER.value,
            source_type=SourceType.STOP_STRUCTURED.value,
            text=text,
            text_hash=str(hash(text + open_utc)),
        )

    session.add_all(
        [
            stop("2026-09-01T11:00:00Z", "Confirmed"),  # 07:00 New York
            stop("2026-09-08T11:00:00Z", "Confirmed"),
            stop("2026-09-15T20:00:00Z", "Not Required"),  # a tender, not evidence
            BookingCase(
                load_id=9, facility_key=key, status=CaseStatus.SCHEDULED.value,
                confirmed_local="2026-09-22 07:00",
            ),
            BookingCase(
                load_id=10, facility_key=key, status=CaseStatus.PENDING.value,
                confirmed_local="2026-09-23 12:00",  # not booked yet: not evidence either
            ),
        ]
    )  # fmt: skip
    session.flush()
    assert sorted(facility_history(session, key)) == ["07:00", "07:00", "07:00"]
    assert usual_time(facility_history(session, key)).clock == "07:00"  # type: ignore[union-attr]
    assert facility_history(session, None) == []


# ------------------------------------------------------------------ in the agent


def test_the_scan_asks_for_the_usual_time_and_says_why(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    key = seed_vendor(sessions)
    with session_scope(sessions) as session:
        for i in range(3):
            session.add(
                BookingCase(
                    load_id=100 + i,
                    facility_key=key,
                    status=CaseStatus.SCHEDULED.value,
                    confirmed_local=f"2026-09-{10 + i} 07:00",
                )
            )
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][0]["appointmentTime"]["open"] = "2026-10-01T04:00:00Z"  # midnight: no time
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = next(c for c in list_cases(session) if c.load_id == 2001)
        assert case.requested_local == "2026-10-01 07:00" and open_kinds(case) == []
        event = case.events[-1]
        assert event.action == "time_recommended"
        assert (
            event.detail["reason"] == "3 of the facility's 3 confirmed appointments were at 07:00"
        )
        detail = case_detail(case, now=NOW)
        assert detail["requested_why"] == event.detail["reason"]
        mailer = RecordingMailer()
        draft_case(session, case, mailer, settings, now=NOW)
        assert "PO# 226321092660 on 10/01 @ 0700" in mailer.drafts[0].body


def test_a_load_that_cannot_make_its_delivery_goes_to_a_person(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][0]["appointmentTime"]["open"] = "2026-10-02T15:00:00Z"  # after the delivery
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert open_kinds(case) == ["load_infeasible"]
        todo = case.open_exceptions[0]
        assert todo.description == (
            "cannot make the delivery: a pickup Fri 10/02 11:00 ET arrives Sat 10/03 00:10 ET, after "
            "the delivery slot Fri 10/02 09:30 ET; the latest pickup that makes it is Thu 10/01 20:00 ET"
        )
        assert todo.detail["latest"] == "2026-10-01 20:00"
        assert KINDS["load_infeasible"][0] == "Cannot make the delivery"
        assert [c.id for c in list_cases(session, exception="load_infeasible")] == [case.id]
        with pytest.raises(ValueError, match="load_infeasible"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)


def test_a_delivery_moved_out_of_reach_and_back(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    scan(
        FakeTPro([lidl_load(2001, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]

    def desk(mid: str, body: str) -> InboundMessage:
        return InboundMessage(
            message_id=mid,
            thread_id=f"desk-{mid}",
            sent_at=NOW,
            from_addr="Inbound <inbound@lidl.us>",
            to_addr="lidl@circledelivers.com",
            cc_addr="",
            subject="RESCHEDULE 226321092660",
            body=body,
        )

    unrelated = FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED))
    responder = Responder(settings, RecordingMailer(), now=NOW)
    with session_scope(sessions) as session:
        # The delivery moves to Tuesday 05:00: the pickup would have to leave today.
        ingest(
            session,
            [desk("d1", "9/29 1700 - PYE_290926111")],
            unrelated,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        case = list_cases(session)[0]
        assert open_kinds(case) == ["load_infeasible"]
        # Then to Monday next week: the request backs off it again and the to-do clears.
        ingest(
            session,
            [desk("d2", "10/5 0730 - PYE_051026222")],
            unrelated,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        assert open_kinds(case) == [] and case.requested_local == "2026-10-02 09:00"
        assert case.delivery_ref == "PYE_051026222"


def test_booking_recommend_explains_without_changing_anything(tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", db)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        from facility_profiles.storage.db import init_db, make_engine, session_factory

        engine = make_engine(db)
        init_db(engine)
        with session_scope(session_factory(engine)) as session:
            session.add(
                a_case(
                    tendered_pickup_utc=datetime(2026, 10, 2, 15, 0, tzinfo=UTC),
                    requested_local="2026-10-02 11:00",
                )
            )
        engine.dispose()
        result = CliRunner().invoke(app, ["booking", "recommend", "1"])
        assert result.exit_code == 0, result.output
        assert "#1 asks for Fri 10/02 11:00 ET now" in result.output
        assert "after the delivery slot Fri 10/02 09:30 ET" in result.output
        assert "latest pickup that makes the delivery: Thu 10/01 20:00 ET" in result.output
        assert "facility history: 0 confirmed time(s); usual none yet" in result.output
    finally:
        get_settings.cache_clear()
