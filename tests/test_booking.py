"""Booking agent prototype: scan, draft, reply handling, approval."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from facility_profiles.booking.classify import (
    FakeReplyClassifier,
    ReplyClassification,
    ReplyContext,
    ReplyStatus,
    validate_classification,
)
from facility_profiles.booking.mail import InboundMessage, LocalDraftMailer, RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.service import (
    approve,
    close_case,
    compose_request,
    draft_case,
    ingest,
    list_cases,
    mark_sent,
    requested_local,
    scan,
)
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.domain.resolution import StopIdentity
from facility_profiles.domain.schema import FacilityIdentity, FieldState, Role
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc
from tests.conftest import FakeTPro, make_load, stop

NOW = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
KOCH = dict(
    location_id=None,
    name="Koch Foods, Inc.",
    address="2825 Crescent Springs Pike",
    city="Erlanger",
    state="KY",
    postal="41018",
    lat=39.035146,
    lon=-84.588501,
)
PYE = dict(
    location_id=None,
    name="Lidl",
    address="81 Belvidere Rd",
    city="Perryville",
    state="MD",
    postal="21903",
    lat=39.583542,
    lon=-76.026726,
)


def lidl_load(load_id: int, *, po: str, pickup_status: str = "Not Required") -> dict[str, Any]:
    load = make_load(
        load_id,
        stop(
            "SH",
            status=pickup_status,
            open_="2026-10-01T13:00:00Z",
            close="2026-10-01T15:00:00Z",
            **KOCH,
        ),  # type: ignore[arg-type]
        stop(
            "CN",
            status="Not Required",
            open_="2026-10-02T13:30:00Z",
            close="2026-10-02T13:30:00Z",
            notes="Please ensure driver has a load bar.<br/>DELIVERY# PYE_021026123",
            **PYE,  # type: ignore[arg-type]
        ),
        terminal=1089,
    )
    load["reference"] = {
        "equipmentType": "Reefer",
        "miles": 559,
        "poNumber": po,
        "referenceNumber": po,
    }
    for wp in load["waypoints"]:
        wp["location"]["ianaTimezone"] = "America/New_York"
    load["billingInfo"] = {
        "customerId": 7211,
        "customer": {"id": 7211, "companyName": "Lidl - Inbound"},
    }
    return load


def koch_key() -> str:
    identity = StopIdentity(
        location_id=None,
        company_name=KOCH["name"],  # type: ignore[arg-type]
        address=KOCH["address"],  # type: ignore[arg-type]
        city=KOCH["city"],  # type: ignore[arg-type]
        state=KOCH["state"],  # type: ignore[arg-type]
        postal_code=KOCH["postal"],  # type: ignore[arg-type]
        latitude=None,
        longitude=None,
    )
    return f"candidate:{identity.candidate_key()}"


def seed_vendor(sessions, *, with_email: bool = True) -> str:  # type: ignore[no-untyped-def]
    key = koch_key()
    with session_scope(sessions) as session:
        repo = Repository(session)
        repo.upsert_facility(
            FacilityIdentity(
                facility_id=None,
                candidate_key=key.split(":")[1],
                company_name=KOCH["name"],  # type: ignore[arg-type]
                address=KOCH["address"],  # type: ignore[arg-type]
                city=KOCH["city"],  # type: ignore[arg-type]
                state=KOCH["state"],  # type: ignore[arg-type]
                postal_code=KOCH["postal"],  # type: ignore[arg-type]
                iana_timezone="America/New_York",
            ),
            latitude=None,
            longitude=None,
        )
        if with_email:
            repo.set_field_human(
                key, Role.SHIPPER, "booking_method", "email", state=FieldState.HUMAN_SET
            )
            repo.set_field_human(
                key, Role.SHIPPER, "contact_email", "cci@udfinc.com", state=FieldState.HUMAN_SET
            )
            repo.set_field_human(
                key, Role.SHIPPER, "contact_name", "CCI desk", state=FieldState.HUMAN_SET
            )
    return key


def reply(
    body: str,
    *,
    thread: str | None = "t1",
    subject: str = "Re: Pick Up Appointment: 226321092660",
    sender: str = "Shannon Humphrey <shumphre@udfinc.com>",
    mid: str = "m1",
) -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        thread_id=thread,
        sent_at=datetime(2026, 9, 30, 15, 31, tzinfo=UTC),
        from_addr=sender,
        to_addr="Megan Goodwin <megan.goodwin@circledelivers.com>",
        cc_addr="Lidl Group <lidl@circledelivers.com>",
        subject=subject,
        body=body,
    )


def test_requested_local_uses_tender_date_or_backs_off_from_delivery(settings):
    tendered = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)  # 09:00 New York
    assert (
        requested_local(
            tendered_pickup_utc=tendered,
            delivery_at_utc=None,
            timezone="America/New_York",
            miles=559,
            settings=settings,
        )
        == "2026-10-01 09:00"
    )
    delivery = datetime(
        2026, 10, 5, 13, 30, tzinfo=UTC
    )  # Monday: 559 miles -> 2 days -> Saturday -> Friday
    assert (
        requested_local(
            tendered_pickup_utc=None,
            delivery_at_utc=delivery,
            timezone="America/New_York",
            miles=559,
            settings=settings,
        )
        == "2026-10-02 09:00"
    )
    assert (
        requested_local(
            tendered_pickup_utc=None,
            delivery_at_utc=None,
            timezone=None,
            miles=None,
            settings=settings,
        )
        is None
    )


def test_scan_draft_reply_and_approve_round_trip(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "pilot_customer_ids": [7211]}
    )
    seed_vendor(sessions)
    client = FakeTPro(
        [
            lidl_load(2001, po="226321092660"),
            lidl_load(2002, po="226321092661", pickup_status="Confirmed"),
        ],
        {},
    )

    stats = scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert stats.loads == 2 and stats.created == 1 and stats.already_confirmed == 1
    assert "customer_id" in client.calls[0]

    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.NEW.value
        assert case.contact_email == "cci@udfinc.com"
        assert case.po_numbers == ["226321092660"]
        assert case.delivery_ref == "PYE_021026123"
        assert case.requested_local == "2026-10-01 09:00"
        draft = compose_request(case, settings)
        assert draft.subject == "Pick Up Appointment: 226321092660"
        assert "PO# 226321092660 on 10/01 @ 09:00" in draft.body
        assert "Lidl (Perryville, MD) on 10/02 (PYE_021026123)" in draft.body
        assert "pickup for Lidl?" in draft.body
        assert draft.cc_addr == "lidl@circledelivers.com"
        message = draft_case(session, case, mailer, settings)
        assert message.draft_ref == "memory:1" and case.status == CaseStatus.DRAFTED.value
        mark_sent(session, case, by="megan", thread_id="t1")
        assert case.status == CaseStatus.SENT.value
        case_id = case.id

    # Second scan does not duplicate the case.
    again = scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert again.created == 0 and again.existing == 1

    def script(ctx: ReplyContext) -> ReplyClassification:
        assert "226321092660" in ctx.po_numbers
        return ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date="2026-10-01",
            pickup_time="11:00",
            pickup_number="CCI-9389",
            quotes=["10/1/26 11:00 CIRCLE #226321092660 CCI-9389"],
            confidence=0.9,
        )

    classifier = FakeReplyClassifier(script)
    inbound = [
        reply("Thanks!", sender="Megan Goodwin <megan.goodwin@circledelivers.com>", mid="own"),
        reply("10/1/26 11:00 CIRCLE #226321092660 CCI-9389\n\nThank you!", mid="m1"),
        reply("10/1/26 11:00 CIRCLE #226321092660 CCI-9389\n\nThank you!", mid="m1"),  # duplicate
    ]
    with session_scope(sessions) as session:
        stats2 = ingest(session, inbound, classifier, internal_domains=["circledelivers.com"])
        assert stats2.skipped_internal == 1 and stats2.duplicates == 1 and stats2.proposed == 1
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PROPOSED.value
        assert case.confirmed_local == "2026-10-01 11:00"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        assert case.pickup_number == "CCI-9389"
        payload, written = approve(session, case, by="megan", client=None)
        assert payload == {
            "load_id": 2001,
            "waypoint_index": "0",
            "start_utc": "2026-10-01T15:00:00Z",
            "end_utc": "2026-10-01T15:00:00Z",
            "status": "Confirmed",
        }
        assert written is False and case.status == CaseStatus.APPROVED.value
        actions = [e.action for e in case.events]
        assert actions == ["scanned", "drafted", "sent", "vendor_confirmed", "approved"]


def test_questions_counter_offers_and_unbacked_values_go_to_a_person(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    client = FakeTPro([lidl_load(3001, po="226331082660")], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]

    def script(ctx: ReplyContext) -> ReplyClassification:
        body = ctx.body.lower()
        if "which carrier" in body:
            return ReplyClassification(
                status=ReplyStatus.QUESTION, question="Which carrier is picking up?"
            )
        if "full" in body:
            return ReplyClassification(
                status=ReplyStatus.COUNTER_OFFER,
                pickup_date="2026-10-03",
                pickup_time="16:00",
                quotes=["I have the 3rd from 16:00"],
            )
        # A confirmation whose quote is not in the text must not be trusted.
        return ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date="2026-10-01",
            pickup_time="08:00",
            quotes=["confirmed for 10/1 at 8"],
        )

    classifier = FakeReplyClassifier(script)
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        # No thread yet: the reply is matched on the PO number in the subject.
        stats = ingest(
            session,
            [
                reply(
                    "Which carrier is picking up?",
                    thread=None,
                    subject="Re: 226331082660",
                    mid="q1",
                )
            ],
            classifier,
            internal_domains=["circledelivers.com"],
        )
        assert stats.needs_human == 1 and case.status == CaseStatus.NEEDS_HUMAN.value
        assert case.reason is not None and "Which carrier" in case.reason

        ingest(
            session,
            [
                reply(
                    "1st and 2nd is full, I have the 3rd from 16:00",
                    thread=None,
                    subject="Re: 226331082660",
                    mid="c1",
                )
            ],
            classifier,
            internal_domains=["circledelivers.com"],
        )
        assert (
            case.status == CaseStatus.NEEDS_HUMAN.value
            and "vendor offered 2026-10-03 16:00" in (case.reason or "")
        )

        stats3 = ingest(
            session,
            [
                reply(
                    "Sounds good, see you then.", thread=None, subject="Re: 226331082660", mid="u1"
                )
            ],
            classifier,
            internal_domains=["circledelivers.com"],
        )
        # The unbacked confirmation was downgraded to unrelated; the case did not move to proposed.
        assert stats3.unrelated == 1 and case.status == CaseStatus.NEEDS_HUMAN.value
        last = case.messages[-1]
        assert last.classification["status"] == "unrelated"
        assert any(i["reason"] == "quote not found in reply" for i in last.classification["issues"])

        close_case(session, case, by="megan", reason="booked by phone")
        assert case.status == CaseStatus.CLOSED.value


def test_scan_skips_loads_that_already_carry_a_pickup_number(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    load = lidl_load(5001, po="115802102660")
    load["reference"]["pickupNumber"] = "20463798"
    client = FakeTPro([load], {})
    stats = scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert stats.created == 1 and stats.already_booked == 1
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.ALREADY_BOOKED.value
        assert case.pickup_number == "20463798" and case.po_numbers == ["115802102660"]
        with pytest.raises(ValueError, match="only new cases"):
            draft_case(session, case, RecordingMailer(), settings)


def test_validate_classification_keeps_backed_values_only():
    text = "I HAVE 11:00 AM AVAILABLE ON THE 24TH\n\n9/24/26 11:00 CIRCLE #226321092660 CCI-9389"
    result = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-09-24",
        pickup_time="11:00",
        pickup_number="CCI-9389",
        quotes=["9/24/26 11:00 CIRCLE #226321092660 CCI-9389", "not in the text"],
    )
    kept, issues = validate_classification(result, text)
    assert kept.status == ReplyStatus.CONFIRMED and kept.pickup_number == "CCI-9389"
    assert kept.quotes == ["9/24/26 11:00 CIRCLE #226321092660 CCI-9389"]
    assert [i.field_name for i in issues] == ["quotes"]

    bogus = ReplyClassification(
        status=ReplyStatus.CONFIRMED, pickup_date="2026-09-24", pickup_number="ZZ-1"
    )
    kept, issues = validate_classification(bogus, text)
    assert (
        kept.status == ReplyStatus.UNRELATED
        and kept.pickup_number is None
        and kept.pickup_date is None
    )
    assert {i.field_name for i in issues} == {"pickup_number", "pickup_date", "status"}


def test_scan_without_a_verified_desk_needs_profile_and_local_drafts_are_files(
    settings, sessions, tmp_path: Path
):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions, with_email=False)
    client = FakeTPro([lidl_load(4001, po="226304092660")], {})
    stats = scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert stats.needs_profile == 1
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.NEEDS_PROFILE.value
        with pytest.raises(ValueError, match="only new cases"):
            draft_case(session, case, RecordingMailer(), settings)
        case.status = CaseStatus.NEW.value
        case.contact_email = "desk@example.com"
        mailer = LocalDraftMailer(tmp_path / "drafts", sender="lidl@circledelivers.com")
        message = draft_case(session, case, mailer, settings)
        path = Path(message.draft_ref or "")
        assert path.exists() and path.suffix == ".eml"
        raw = path.read_text(encoding="utf-8")
        assert "To: desk@example.com" in raw and "Subject: Pick Up Appointment: 226304092660" in raw


def test_booking_cli_smoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.setenv("FP_BOOKING_CC", "lidl@circledelivers.com, ops@circledelivers.com")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        assert get_settings().booking_cc == ["lidl@circledelivers.com", "ops@circledelivers.com"]
        result = runner.invoke(app, ["booking", "list"])
        assert result.exit_code == 0 and "no cases" in result.output
        result = runner.invoke(app, ["booking", "show", "1"])
        assert result.exit_code == 1 and "not found" in result.output
        result = runner.invoke(app, ["booking", "inbox"])
        assert result.exit_code == 2
        for command in ("scan", "draft", "sent", "approve", "close"):
            assert runner.invoke(app, ["booking", command, "--help"]).exit_code == 0
    finally:
        get_settings.cache_clear()
