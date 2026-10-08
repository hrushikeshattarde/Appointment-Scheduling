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
from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.service import (
    approve,
    close_case,
    compose_request,
    draft_case,
    ingest,
    list_cases,
    mark_sent,
    requested_local,
    reschedule_case,
    scan,
)
from facility_profiles.booking.worklist import flag, open_kinds, resolve
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


def awaiting_approval(case: BookingCase) -> bool:
    """Pending, with the vendor's confirmation open for a person's approval and nothing else."""
    return case.status == CaseStatus.PENDING.value and open_kinds(case) == ["confirmation_review"]


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
        load_status="Ready To Dispatch",  # as live loads still to pick up show
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
        rfc_message_id=f"<{mid}@vendor.test>",
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
        assert case.status == CaseStatus.UNSCHEDULED.value and open_kinds(case) == []
        assert case.contact_email == "cci@udfinc.com"
        assert case.po_numbers == ["226321092660"]
        assert case.delivery_ref == "PYE_021026123"
        assert case.requested_local == "2026-10-01 09:00"
        draft = compose_request(case, settings)
        assert draft.subject == "Pick Up Appointment: 226321092660"
        # The CCI desk serves several shippers, so the pod names the shipper and the customer.
        assert "Can I please schedule the following for Koch Foods going to Lidl?" in draft.body
        assert "PO# 226321092660 on 10/01 @ 0900" in draft.body
        assert "Delivering" not in draft.body and "Carrier:" not in draft.body
        assert draft.body.startswith("Hello,\n\nCan I please schedule")
        assert draft.cc_addr == "lidl@circledelivers.com, megan.goodwin@circledelivers.com"
        message = draft_case(session, case, mailer, settings, now=NOW)
        # Drafted but not sent: nothing has been asked of the vendor yet.
        assert message.draft_ref == "memory:1" and case.status == CaseStatus.UNSCHEDULED.value
        mark_sent(session, case, by="megan", thread_id="t1")
        assert case.status == CaseStatus.PENDING.value
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
        # Megan's own "Thanks!" on the group is kept on the case as sent by a person.
        assert stats2.by_person == 1 and stats2.skipped_internal == 0
        assert stats2.duplicates == 1 and stats2.proposed == 1
        case = session.get(BookingCase, case_id)
        # Confirmed by the vendor, still pending until a person approves what the agent read.
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["confirmation_review"]
        assert case.open_exceptions[0].description == (
            "vendor confirmed Thu 10/01 11:00 ET, pickup# CCI-9389; approve to accept"
        )
        assert case.confirmed_local == "2026-10-01 11:00"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        assert case.pickup_number == "CCI-9389"
        payload, written = approve(session, case, by="megan")
        assert payload == {
            "load_id": 2001,
            "waypoint_index": "SH",  # Transport Pro's name for the shipper stop
            "start_utc": "2026-10-01T15:00:00Z",
            "end_utc": "2026-10-01T15:00:00Z",
            "status": "Confirmed",
        }
        assert written is False and case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == []
        review = case.exceptions[0]
        assert (review.resolution, review.resolved_by) == ("approved", "megan")
        actions = [e.action for e in case.events]
        # The desk that confirmed is remembered for the facility once the booking is approved.
        assert actions == [
            "scanned",
            "drafted",
            "sent",
            "sent_by_person",  # Megan's "Thanks!" on the group, kept on the case
            "vendor_confirmed",
            "approved",
            "desk_remembered",
        ]
        with pytest.raises(ValueError, match="nothing to approve"):
            approve(session, case, by="megan")


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
        assert stats.needs_human == 1 and case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["facility_question"]
        assert "Which carrier" in case.open_exceptions[0].description

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
        # The offer is about the slot, so it supersedes the open question: one thing to decide.
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["proposed_time_review"]
        offer = case.open_exceptions[0]
        assert offer.description == "vendor offered Sat 10/03 16:00 ET"
        assert (offer.detail["date"], offer.detail["time"]) == ("2026-10-03", "16:00")
        assert case.exceptions[0].resolution == "superseded by a later reply (counter_offer)"

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
        # The unbacked confirmation was downgraded to unrelated; the offer is still the open item.
        assert stats3.unrelated == 1 and open_kinds(case) == ["proposed_time_review"]
        assert case.confirmed_local is None
        last = case.messages[-1]
        assert last.classification["status"] == "unrelated"
        assert any(i["reason"] == "quote not found in reply" for i in last.classification["issues"])

        close_case(session, case, by="megan", reason="load canceled by Lidl")
        assert case.status == CaseStatus.CANCELED.value and open_kinds(case) == []
        assert case.exceptions[-1].resolution == "case canceled: load canceled by Lidl"


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
        # Booked outside the agent before the scan: scheduled, and nothing for a person to do.
        assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
        assert case.reason == "load already carries vendor pickup number 20463798"
        assert case.pickup_number == "20463798" and case.po_numbers == ["115802102660"]
        with pytest.raises(ValueError, match="only unscheduled cases"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)


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
        assert case.status == CaseStatus.UNSCHEDULED.value
        assert open_kinds(case) == ["missing_method"]
        assert case.open_exceptions[0].description == (
            "no verified email booking desk on the profile"
        )
        with pytest.raises(ValueError, match=r"open exceptions \(missing_method\)"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)
        case.contact_email = "desk@example.com"
        resolve(session, case, [ExceptionType.MISSING_METHOD], resolution="desk found", by="megan")
        mailer = LocalDraftMailer(tmp_path / "drafts", sender="lidl@circledelivers.com")
        message = draft_case(session, case, mailer, settings, now=NOW)
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
        for command in ("scan", "draft", "sent", "approve", "close", "booked", "resolve"):
            assert runner.invoke(app, ["booking", command, "--help"]).exit_code == 0
        result = runner.invoke(app, ["booking", "list", "--exception", "nonsense"])
        assert result.exit_code == 2
    finally:
        get_settings.cache_clear()


def test_first_come_first_served_vendors_get_a_date_only_request(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    key = seed_vendor(sessions)
    with session_scope(sessions) as session:
        repo = Repository(session)
        repo.set_field_human(
            key, Role.SHIPPER, "appointment_required", False, state=FieldState.HUMAN_SET
        )
        repo.set_field_human(
            key, Role.SHIPPER, "contact_email", "ncshipping@example.com", state=FieldState.HUMAN_SET
        )
    client = FakeTPro([lidl_load(9001, po="266621042660")], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, settings, now=NOW)
    body = mailer.drafts[-1].body
    assert "PO# 266621042660 on 10/01\n" in body and "@" not in body.split("Thank you!")[0]
    assert "Can I please schedule the following?" in body


def test_reschedule_drafts_in_thread_and_resets_the_slot(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    client = FakeTPro([lidl_load(9002, po="104427082660")], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        with pytest.raises(ValueError, match="nothing to reschedule"):
            reschedule_case(
                session, case, mailer, settings, requested_local="2026-10-02 09:00", by="megan"
            )
        draft_case(session, case, mailer, settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t9")
        case.status = CaseStatus.SCHEDULED.value
        case.confirmed_local = "2026-10-01 09:00"
        message = reschedule_case(
            session,
            case,
            mailer,
            settings,
            requested_local="2026-10-02 14:30",
            by="megan",
            note="Our driver fell off this morning, my apologies.",
        )
        assert message.kind == "reschedule" and case.status == CaseStatus.PENDING.value
        assert case.reschedule_count == 1
        assert case.requested_local == "2026-10-02 14:30" and case.confirmed_local is None
        sent = mailer.drafts[-1]
        assert sent.subject == "Re: Pick Up Appointment: 104427082660" and sent.thread_id == "t9"
        assert "Our driver fell off this morning, my apologies." in sent.body
        assert "Can we please reschedule PO# 104427082660 on 10/02 @ 1430?" in sent.body
        assert (
            case.events[-1].action == "reschedule"
            and case.events[-1].detail["previous"] == "2026-10-01 09:00"
        )


def test_parse_delivery_slot_reads_both_forms_lidl_uses():
    from functools import partial

    from facility_profiles.booking.service import parse_delivery_slot
    from facility_profiles.customers import built_in_customers

    known = built_in_customers()
    parse = partial(parse_delivery_slot, customer=known.get("lidl"))
    got = parse(
        "Here is an updated appointment! 8/20 7AM - GRM_200826926.",
        year=2026,
        timezone="America/New_York",
    )
    assert got == (datetime(2026, 8, 20, 11, 0, tzinfo=UTC), "GRM_200826926")
    got = parse(
        "New Appointment: FRG_200526615 05/20 @ 1100", year=2026, timezone="America/New_York"
    )
    assert got == (datetime(2026, 5, 20, 15, 0, tzinfo=UTC), "FRG_200526615")
    got = parse(
        "your new appointment on 8/20 at 8AM - FRG_200826660",
        year=2026,
        timezone="America/New_York",
    )
    assert got is not None and got[1] == "FRG_200826660" and got[0].hour == 12
    # The desk's wording after the 9/29 delivery miss: time and reference with no dash between.
    got = parse("9/30 at 1100 PYE_300926723", year=2026, timezone="America/New_York")
    assert got == (datetime(2026, 9, 30, 15, 0, tzinfo=UTC), "PYE_300926723")
    assert parse("Thanks for the update!", year=2026, timezone=None) is None
    # A customer whose file names no delivery reference has no slot to read.
    assert (
        parse_delivery_slot(
            "8/20 7AM - GRM_200826926", year=2026, timezone=None, customer=known.fallback
        )
        is None
    )


def test_customer_desk_slot_moves_the_pickup_and_redrafts_in_thread(settings, sessions):
    from facility_profiles.booking.respond import Responder

    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    client = FakeTPro([lidl_load(9101, po="104419082630")], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    classifier = FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED))
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="tv")
        # The vendor could not ship as planned and Lidl was asked to move the delivery.
        case.status = CaseStatus.DECLINED.value
        flag(session, case, ExceptionType.FACILITY_DECLINED, "vendor cannot book: no coverage")
        lidl = InboundMessage(
            message_id="z1",
            thread_id="tz",
            sent_at=datetime(2026, 9, 30, 19, 5, tzinfo=UTC),
            from_addr="inbound @lidl.us <inbound@lidl.us>",
            to_addr="megan.goodwin@circledelivers.com",
            cc_addr="lidl@circledelivers.com",
            subject="104419082630 NO COVERAGE",
            body="Here is an updated appointment! 10/6 7AM - GRM_061026926.\n\nLet me know if you need anything else.",
        )
        stats = ingest(
            session,
            [lidl],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=Responder(settings, mailer, now=NOW),
            customer_desk="inbound@lidl.us",
        )
        assert stats.delivery_updates == 1 and stats.classified == 0
        assert case.delivery_ref == "GRM_061026926"
        assert as_utc(case.delivery_at_utc) == datetime(2026, 10, 6, 11, 0, tzinfo=UTC)
        # The pickup was re-requested in the vendor thread, backed off the new delivery; asking
        # again answers the decline.
        assert case.status == CaseStatus.PENDING.value and open_kinds(case) == []
        assert case.exceptions[0].resolution == "pickup asked for again: Fri 10/02 09:00 ET"
        assert case.requested_local == "2026-10-02 09:00"
        assert mailer.drafts[-1].to_addr == "cci@udfinc.com" and mailer.drafts[-1].thread_id == "tv"
        assert (
            "Can we please reschedule PO# 104419082630 on 10/02 @ 0900?" in mailer.drafts[-1].body
        )
        assert [m.kind for m in case.messages] == ["request", "customer_desk", "reschedule"]
        assert any(e.action == "delivery_updated" for e in case.events)

        # A customer-desk message without a slot is recorded and leaves the case alone.
        stats = ingest(
            session,
            [
                InboundMessage(
                    message_id="z2",
                    thread_id="tz",
                    sent_at=datetime(2026, 9, 30, 19, 30, tzinfo=UTC),
                    from_addr="inbound@lidl.us",
                    to_addr="",
                    cc_addr="",
                    subject="Re: 104419082630 NO COVERAGE",
                    body="Thanks for the update!",
                )
            ],
            classifier,
            internal_domains=["circledelivers.com"],
            customer_desk="inbound@lidl.us",
        )
        assert stats.delivery_updates == 0 and case.status == CaseStatus.PENDING.value


def test_draft_batch_writes_one_email_per_desk(settings, sessions):
    from facility_profiles.booking.service import draft_batch

    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    loads = [lidl_load(9201, po="104419082630"), lidl_load(9202, po="104421082660")]
    loads[1]["waypoints"][0]["appointmentTime"]["open"] = "2026-10-03T15:00:00Z"
    loads[1]["waypoints"][1]["appointmentTime"]["open"] = "2026-10-05T13:30:00Z"  # after it
    client = FakeTPro(loads, {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        cases = list_cases(session, CaseStatus.UNSCHEDULED.value)
        messages = draft_batch(session, cases, mailer, settings, now=NOW)
        assert len(messages) == 2 and len(mailer.drafts) == 1
        draft = mailer.drafts[0]
        assert draft.subject == "Pick Up Appointments: 104419082630 & 104421082660"
        assert "PO# 104419082630 on 10/01 @ 0900\nPO# 104421082660 on 10/02 @ 1100" in draft.body
        assert "ALL IN ONE TRUCK" not in draft.body
        assert all(c.status == CaseStatus.UNSCHEDULED.value for c in cases)
        assert messages[0].draft_ref == messages[1].draft_ref
        assert cases[0].events[-1].detail["batched_with"] == [cases[1].id]
