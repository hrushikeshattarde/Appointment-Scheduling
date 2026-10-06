"""Replay the pod's real Morgan Foods thread (Sept 2026) through the agent.

The fixture is the thread as ``scripts/lidl_mail_patterns.py`` would pull it, Outlook link cruft
("115802102660<tel:(580)%20210-2660>") and all. The classifier is scripted with the readings
the production model (anthropic/claude-sonnet-4.6) gave for these messages on 2026-09-29, so the
test pins what the validator and the case policy must do with a real model's output.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from facility_profiles.booking.classify import (
    FakeReplyClassifier,
    ReplyClassification,
    ReplyContext,
    ReplyStatus,
    clean_mail_text,
    validate_classification,
)
from facility_profiles.booking.mail import RecordingMailer, load_messages_jsonl
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.service import (
    approve,
    draft_case,
    ingest,
    list_cases,
    mark_sent,
    reschedule_case,
    scan,
)
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.domain.schema import FacilityIdentity, FieldState, Role
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc
from tests.conftest import FakeTPro, make_load, stop
from tests.test_booking import PYE, awaiting_approval

FIXTURE = Path(__file__).parent / "fixtures" / "lidl_morgan_foods_thread.jsonl"
THREAD = "1a0e9fc7679c2963"
INTERNAL = ["circledelivers.com"]
MORGAN = dict(
    location_id=None,
    name="Morgan Foods, INC.",
    address="90 West Morgan Street",
    city="Austin",
    state="IN",
    postal="47102",
    lat=38.758,
    lon=-85.808,
)

# What the production model returned for each vendor message, keyed by the start of its own words.
RECORDED: dict[str, ReplyClassification] = {
    "115829092660": ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date="2026-10-02",
        pickup_time="09:00",
        pickup_number="20463798",
        conditions=["we cannot schedule early pickup's"],
        quotes=[
            "115802102660 & 115802102661-10/2 @ 9am pickup# 20463798",
            "this is showing a pickup date of 10/2 we cannot schedule early pickup's",
        ],
        confidence=0.95,
    ),
    "Both orders?": ReplyClassification(
        status=ReplyStatus.QUESTION,
        question="Both orders?",
        quotes=["Both orders?"],
        confidence=0.85,
    ),
    "115802102660": ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-05",
        pickup_time="09:00",
        pickup_number="20463798",
        quotes=["115802102660 & 115802102661-10/5 @ 9am pickup# 20463798"],
        confidence=0.99,
    ),
    "Latest is 9pm": ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-09-28",
        pickup_time="09:00",
        pickup_time_end="21:00",
        conditions=["Keep vendor updated on driver's ETA"],
        quotes=["Latest is 9pm tonight, please keep us updated on drivers ETA", "on 09/28 @ 0900"],
        confidence=0.82,
    ),
    "Did this get resolved?": ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-05",
        pickup_time="09:00",
        pickup_number="20463798",
        question="Did this get resolved?",
        quotes=["115802102660 & 115802102661-10/5 @ 9am pickup# 20463798"],
        confidence=0.82,
    ),
}


def recorded(ctx: ReplyContext) -> ReplyClassification:
    for prefix, result in RECORDED.items():
        if ctx.body.startswith(prefix):
            return result
    return ReplyClassification(status=ReplyStatus.UNRELATED)


def morgan_load(load_id: int, *, pos: tuple[str, str], pickup_open: str) -> dict[str, Any]:
    load = make_load(
        load_id,
        stop("SH", status="Not Required", open_=pickup_open, close=pickup_open, **MORGAN),  # type: ignore[arg-type]
        stop(
            "CN",
            status="Not Required",
            open_="2026-10-06T11:30:00Z",
            close="2026-10-06T11:30:00Z",
            notes="DELIVERY# PYE_061026919",
            **PYE,  # type: ignore[arg-type]
        ),
        terminal=1089,
        load_status="Ready To Dispatch",
    )
    load["reference"] = {
        "equipmentType": "Van",
        "miles": 687,
        "poNumber": pos[0],
        "referenceNumber": pos[1],
    }
    load["waypoints"][0]["location"]["ianaTimezone"] = "America/Indiana/Indianapolis"
    load["waypoints"][1]["location"]["ianaTimezone"] = "America/New_York"
    load["billingInfo"] = {
        "customerId": 7211,
        "customer": {"id": 7211, "companyName": "Lidl - Inbound"},
    }
    return load


def seed_morgan(sessions) -> None:  # type: ignore[no-untyped-def]
    from facility_profiles.domain.resolution import StopIdentity

    identity = StopIdentity(
        location_id=None,
        company_name=MORGAN["name"],  # type: ignore[arg-type]
        address=MORGAN["address"],  # type: ignore[arg-type]
        city=MORGAN["city"],  # type: ignore[arg-type]
        state=MORGAN["state"],  # type: ignore[arg-type]
        postal_code=MORGAN["postal"],  # type: ignore[arg-type]
        latitude=None,
        longitude=None,
    )
    key = f"candidate:{identity.candidate_key()}"
    with session_scope(sessions) as session:
        repo = Repository(session)
        repo.upsert_facility(
            FacilityIdentity(
                facility_id=None,
                candidate_key=identity.candidate_key(),
                company_name=MORGAN["name"],  # type: ignore[arg-type]
                address=MORGAN["address"],  # type: ignore[arg-type]
                city=MORGAN["city"],  # type: ignore[arg-type]
                state=MORGAN["state"],  # type: ignore[arg-type]
                postal_code=MORGAN["postal"],  # type: ignore[arg-type]
                iana_timezone="America/Indiana/Indianapolis",
            ),
            latitude=None,
            longitude=None,
        )
        for name, value in (
            ("booking_method", "email"),
            ("contact_email", "shipping.appointments@morganfoods.com"),
            ("contact_name", "Morgan Foods appointments desk"),
            ("time_granularity", "exact"),
        ):
            repo.set_field_human(key, Role.SHIPPER, name, value, state=FieldState.HUMAN_SET)


def test_fixture_parses_like_the_mail_pull():
    messages = load_messages_jsonl(FIXTURE)
    assert len(messages) == 17 and all(m.thread_id == THREAD for m in messages)
    vendor = [m for m in messages if m.from_domain == "morganfoods.com"]
    assert len(vendor) == 7
    # Every reply splits into its own words and the history under it.
    assert all(m.quoted for m in messages[1:])
    confirmation = next(m for m in vendor if m.body.startswith("115802102660<tel:"))
    assert "<tel:(580)%20210-2660>" in confirmation.body
    assert clean_mail_text(confirmation.body).startswith(
        "115802102660 & 115802102661-10/5 @ 9am pickup# 20463798"
    )


def test_outlook_link_cruft_does_not_hide_a_real_confirmation():
    messages = load_messages_jsonl(FIXTURE)
    confirmation = next(m for m in messages if m.body.startswith("115802102660<tel:"))
    kept, issues = validate_classification(
        RECORDED["115802102660"], confirmation.body, confirmation.quoted
    )
    assert kept.status == ReplyStatus.CONFIRMED and not issues
    assert (kept.pickup_date, kept.pickup_time, kept.pickup_number) == (
        "2026-10-05",
        "09:00",
        "20463798",
    )


def test_replay_of_the_morgan_foods_thread(settings, sessions):
    # The fixture replays the day the pod asked Morgan Foods for 10/01 and was pushed to 10/02;
    # the PO-date floor that now prevents that ask is switched off so the replay stays faithful.
    settings = settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "pilot_customer_ids": [7211],
            "booking_po_date_floor_desks": [],
        }
    )
    seed_morgan(sessions)
    messages = load_messages_jsonl(FIXTURE)
    client = FakeTPro(
        [
            morgan_load(
                2591072, pos=("115802102660", "115802102661"), pickup_open="2026-10-01T13:00:00Z"
            )
        ],
        {},
    )
    stats = scan(
        client, sessions, settings, days_ahead=14, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
    )  # type: ignore[arg-type]
    assert stats.created == 1
    mailer = RecordingMailer()
    classifier = FakeReplyClassifier(recorded)

    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.requested_local == "2026-10-01 09:00"
        assert case.po_numbers == ["115802102660", "115802102661"]
        draft = draft_case(
            session, case, mailer, settings, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
        )
        assert "PO# 115802102660 & 115802102661 (ALL IN ONE TRUCK) on 10/01 @ 0900" in (
            draft.body or ""
        )
        mark_sent(session, case, by="megan", thread_id=THREAD, sent_at=messages[0].sent_at)
        case_id = case.id

    # Day 1: Megan's request (internal, skipped); Vera books one line and pushes ours to 10/2
    # ("cannot schedule early pickups"); Megan's "Thank you!" (internal).
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        responder = Responder(settings, mailer, now=datetime(2026, 9, 24, 12, 40, tzinfo=UTC))
        stats = ingest(
            session, messages[:3], classifier, internal_domains=INTERNAL, responder=responder
        )
        assert stats.skipped_internal == 2 and stats.classified == 1
        assert stats.needs_human == 1 and stats.responded == 1
        # 10/02 09:00 still makes the 10/06 delivery, so the agent accepts, pickup number kept.
        assert awaiting_approval(case)
        assert case.confirmed_local == "2026-10-02 09:00" and case.pickup_number == "20463798"
        assert mailer.drafts[-1].body.startswith("Yes, 10/02 @ 0900 works. Thank you!")

        # Day 2: the receiver moved, so a person re-requests Monday 10/05 in the same thread.
        reschedule_case(
            session,
            case,
            mailer,
            settings,
            requested_local="2026-10-05 09:00",
            by="megan",
            note="Due to the receiver's availability, we need to pick this up on Monday 10/05.",
        )
        assert case.status == CaseStatus.PENDING.value and case.confirmed_local is None
        assert (
            "Can we please reschedule PO# 115802102660 & 115802102661 (ALL IN ONE TRUCK) on 10/05 @ 0900?"
            in (mailer.drafts[-1].body)
        )

        responder = Responder(settings, mailer, now=datetime(2026, 9, 25, 14, 0, tzinfo=UTC))
        stats = ingest(
            session, messages[3:7], classifier, internal_domains=INTERNAL, responder=responder
        )
        assert stats.skipped_internal == 2 and stats.classified == 2
        outbound = [m for m in case.messages if m.direction == "out"]
        # "Both orders?" is answered from the case, the way the pod answered it.
        answer = next(m for m in outbound if m.kind == "answer_question")
        assert answer.body is not None and answer.body.startswith(
            "Yes, both orders: PO# 115802102660 & PO# 115802102661."
        )
        # The confirmation with Outlook cruft books the slot and gets the pod's "Thank you!".
        assert stats.proposed == 1 and awaiting_approval(case)
        assert case.confirmed_local == "2026-10-05 09:00" and case.pickup_number == "20463798"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
        last_in = [m for m in case.messages if m.direction == "in"][-1]
        assert last_in.classification["status"] == "confirmed"
        assert not last_in.classification["issues"]
        assert [m.kind for m in outbound].count("acknowledge") == 1

        payload, _written = approve(session, case, by="megan")
        assert payload["start_utc"] == "2026-10-05T13:00:00Z"
        calls_so_far = len(classifier.calls)

        # Day 4 onwards: a missed pickup, ETAs, securement, "did this get resolved?". A booked
        # pickup's mail is read now, because a facility can move a booking. None of this does:
        # the work-in note ("Latest is 9pm tonight", about a slot already past) and the chaser
        # become one question for a person; the rest is kept; the booking stands.
        responder = Responder(settings, mailer, now=datetime(2026, 9, 29, 13, 0, tzinfo=UTC))
        stats = ingest(
            session, messages[7:], classifier, internal_domains=INTERNAL, responder=responder
        )
        assert stats.skipped_internal == 6 and stats.after_decision == 0 and stats.classified == 4
        assert stats.booked_changed == 0
        assert len(classifier.calls) == calls_so_far + 4
        assert case.status == CaseStatus.SCHEDULED.value
        assert case.confirmed_local == "2026-10-05 09:00"
        assert open_kinds(case) == ["facility_question"]
        assert [m.kind for m in case.messages if m.direction == "out"].count("acknowledge") == 1
        assert [e.action for e in case.events].count("reply_after_decision") == 2
        assert len([m for m in case.messages if m.direction == "in"]) == 7


def test_a_slot_already_past_when_the_vendor_wrote_is_not_a_confirmation(settings, sessions):
    """ "Latest is 9pm tonight" after a missed 09:00 pickup is a work-in note, not a booking."""
    # The fixture replays the day the pod asked Morgan Foods for 10/01 and was pushed to 10/02;
    # the PO-date floor that now prevents that ask is switched off so the replay stays faithful.
    settings = settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "pilot_customer_ids": [7211],
            "booking_po_date_floor_desks": [],
        }
    )
    seed_morgan(sessions)
    messages = load_messages_jsonl(FIXTURE)
    work_in = next(m for m in messages if m.body.startswith("Latest is 9pm"))
    client = FakeTPro(
        [
            morgan_load(
                2591060, pos=("115829092660", "115829092661"), pickup_open="2026-09-28T13:00:00Z"
            )
        ],
        {},
    )
    scan(client, sessions, settings, days_ahead=14, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC))  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.requested_local == "2026-09-28 09:00"
        draft_case(session, case, mailer, settings, now=datetime(2026, 9, 23, 12, 0, tzinfo=UTC))
        mark_sent(session, case, by="megan", thread_id="t-first", sent_at=messages[0].sent_at)
        responder = Responder(settings, mailer, now=work_in.sent_at)
        stats = ingest(
            session,
            [replace(work_in, thread_id="t-first", message_id="work-in")],
            FakeReplyClassifier(recorded),
            internal_domains=INTERNAL,
            responder=responder,
        )
        assert stats.classified == 1 and stats.needs_human == 1 and stats.proposed == 0
        assert case.status == CaseStatus.PENDING.value
        assert [e.kind for e in case.open_exceptions] == ["stale_confirmation"]
        assert "already past" in case.open_exceptions[0].description
        assert case.confirmed_local is None
        assert case.events[-1].action == "stale_confirmation"
        # The quote lifted from the quoted request under the reply was rejected as evidence.
        issues = case.messages[-1].classification["issues"]
        assert any("quoted history" in i["reason"] for i in issues)
        # Nothing was drafted back to the vendor.
        assert len(mailer.drafts) == 1
