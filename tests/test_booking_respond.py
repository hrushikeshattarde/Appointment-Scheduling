"""The conversational policy: counter-offers, questions, rejections, follow-ups, hand-offs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import (
    AnswerDraft,
    FakeAnswerComposer,
    Responder,
    alternative_days,
    answer_from_rules,
    answer_is_safe,
    case_facts,
    offer_is_feasible,
)
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases, mark_sent, scan
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import as_utc
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, reply, seed_vendor


def _prepared_case(settings, sessions, mailer: RecordingMailer, *, po: str = "226321092660") -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    client = FakeTPro([lidl_load(7001, po=po)], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, settings)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)
        return case.id


def _counter(date: str, time: str, quote: str) -> ReplyClassification:
    return ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER, pickup_date=date, pickup_time=time, quotes=[quote]
    )


def test_feasibility_and_alternative_days(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        # Delivery 2026-10-02 13:30Z, 559 miles at 50 mph + 2 h = 13.2 h transit.
        ok, why = offer_is_feasible(
            case, datetime(2026, 10, 1, 16, 0, tzinfo=UTC), settings, now=NOW
        )
        assert ok and "makes the delivery" in why
        ok, why = offer_is_feasible(
            case, datetime(2026, 10, 2, 2, 0, tzinfo=UTC), settings, now=NOW
        )
        assert not ok and "after the delivery slot" in why
        ok, why = offer_is_feasible(
            case, datetime(2026, 10, 3, 14, 0, tzinfo=UTC), settings, now=NOW
        )
        assert not ok and "weekend" in why
        ok, why = offer_is_feasible(case, NOW + timedelta(hours=1), settings, now=NOW)
        assert not ok and "too soon" in why
        assert alternative_days(case, settings, now=NOW) == ["10/01", "09/30", "09/29"]


def test_counter_offer_is_accepted_when_it_makes_the_delivery(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    classifier = FakeReplyClassifier(
        lambda _c: _counter("2026-10-01", "11:00", "I HAVE 11:00 AM AVAILABLE ON THE 1ST")
    )
    responder = Responder(settings, mailer, now=NOW)
    with session_scope(sessions) as session:
        stats = ingest(
            session,
            [reply("21ST IS FULL AT 9, I HAVE 11:00 AM AVAILABLE ON THE 1ST", mid="c1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        assert stats.needs_human == 1 and stats.responded == 1
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PROPOSED.value
        assert case.confirmed_local == "2026-10-01 11:00"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        sent = mailer.drafts[-1]
        assert sent.subject.startswith("Re: ") and sent.body.startswith("Yes, 10/01 @ 1100 works.")
        assert sent.thread_id == "t1" and sent.in_reply_to == "c1"
        assert [e.action for e in case.events][-1] == "accept_offer"


def test_counter_offer_that_misses_delivery_asks_for_alternatives_then_caps(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089], "booking_max_rounds": 2})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    classifier = FakeReplyClassifier(lambda _c: _counter("2026-10-02", "09:00", "the 2nd at 9"))
    responder = Responder(settings, mailer, now=NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        for i in range(3):
            ingest(
                session,
                [
                    reply(
                        "Only the 2nd at 9",
                        mid=f"o{i}",
                        subject="Re: Pick Up Appointment: 226321092660",
                    )
                ],
                classifier,
                internal_domains=["circledelivers.com"],
                responder=responder,
            )
        outbound = [m for m in case.messages if m.direction == "out"]
        kinds = [m.kind for m in outbound]
        assert kinds == ["request", "ask_alternative", "ask_alternative"]
        assert (
            "would not make our delivery appointment on 10/02 (PYE_021026123)" in outbound[1].body
        )
        assert "10/01 or 09/30 or 09/29" in outbound[1].body
        # Third reply hits the round cap: handed to a person, no draft.
        assert case.status == CaseStatus.NEEDS_HUMAN.value
        assert (
            case.events[-1].action == "handoff"
            and "rounds reached" in case.events[-1].detail["reason"]
        )


def test_questions_are_answered_from_facts_or_handed_off(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    composer = FakeAnswerComposer(
        {
            "Is this a reefer or a dry van?": AnswerDraft(
                answerable=True, message="This is a Reefer load.", facts_used=["equipment"]
            ),
            "Please send the driver name and truck number.": AnswerDraft(
                answerable=False, reason="driver details are not on the case"
            ),
            "Confirm PO 999999999 please?": AnswerDraft(
                answerable=True, message="Yes, PO 999999999 is correct.", facts_used=["po_numbers"]
            ),
        }
    )

    def script(ctx: ReplyContext) -> ReplyClassification:
        return ReplyClassification(status=ReplyStatus.QUESTION, question=ctx.body.strip())

    classifier = FakeReplyClassifier(script)
    responder = Responder(settings, mailer, composer=composer, now=NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None

        def ask(text: str, mid: str) -> None:
            ingest(
                session,
                [reply(text, mid=mid)],
                classifier,
                internal_domains=["circledelivers.com"],
                responder=responder,
            )

        ask("Which carrier is picking up?", "q1")
        assert case.status == CaseStatus.SENT.value
        assert mailer.drafts[-1].body.startswith("The carrier is Circle Logistics, Inc. Thank you!")
        ask("Both orders?", "q2")
        assert mailer.drafts[-1].body.startswith("Just PO# 226321092660.")
        ask("Where is this delivering to?", "q3")
        assert "Lidl (Perryville, MD) on 10/02 (PYE_021026123)" in mailer.drafts[-1].body
        assert composer.calls == []  # rules covered all of those

        settings2 = settings.model_copy(update={"booking_max_rounds": 10})
        responder.settings = settings2
        ask("Is this a reefer or a dry van?", "q4")
        assert composer.calls[-1] == "Is this a reefer or a dry van?"
        assert mailer.drafts[-1].body.startswith("This is a Reefer load.")
        ask("Please send the driver name and truck number.", "q5")
        assert case.status == CaseStatus.NEEDS_HUMAN.value and "driver details" in (
            case.reason or ""
        )
        case.status = CaseStatus.SENT.value
        case.reason = None
        ask("Confirm PO 999999999 please?", "q6")
        assert case.status == CaseStatus.NEEDS_HUMAN.value
        assert "number not on the case: 999999999" in case.events[-1].detail["reason"]


def test_rejection_drafts_a_note_to_the_customer_desk_and_money_talk_is_handed_off(
    settings, sessions
):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)

    def script(ctx: ReplyContext) -> ReplyClassification:
        if "detention" in ctx.body.lower():
            return ReplyClassification(
                status=ReplyStatus.QUESTION, question="Will you pay detention?"
            )
        return ReplyClassification(
            status=ReplyStatus.REJECTED, question="PO will not be ready until 10/07"
        )

    classifier = FakeReplyClassifier(script)
    responder = Responder(settings, mailer, now=NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        ingest(
            session,
            [reply("The PO will not be ready until 10/07, sorry.", mid="r1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        assert case.status == CaseStatus.NEEDS_HUMAN.value
        note = mailer.drafts[-1]
        assert note.to_addr == "inbound@lidl.us" and note.thread_id is None
        assert note.subject == "226321092660 - pickup pushed by Koch Foods, Inc."
        assert (
            "PO will not be ready until 10/07" in note.body
            and "new delivery appointment" in note.body
        )
        assert case.messages[-1].kind == "escalate_to_customer"

        case.status = CaseStatus.SENT.value
        ingest(
            session,
            [reply("Will you pay detention if the driver waits?", mid="d1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        assert case.status == CaseStatus.NEEDS_HUMAN.value
        assert case.events[-1].detail["reason"] == "reply mentions money or a claim"
        assert len(mailer.drafts) == 2  # nothing drafted for the money question


def test_follow_up_once_after_the_configured_silence(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_follow_up_hours": 24}
    )
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert (
            Responder(settings, mailer, now=NOW + timedelta(hours=5)).follow_up(session, case)
            is None
        )
        later = Responder(settings, mailer, now=NOW + timedelta(hours=30))
        message = later.follow_up(session, case)
        assert message is not None and message.kind == "follow_up"
        assert "Hello,\n\nFollowing up on this." in mailer.drafts[-1].body
        assert case.status == CaseStatus.SENT.value
        assert later.follow_up(session, case) is None  # only once


def test_rule_answers_and_safety_checks(settings, sessions):
    facts = {
        "po_numbers": ["115802102660", "115802102661"],
        "carrier": "Circle Logistics, Inc.",
        "equipment": None,
        "customer": "Lidl",
        "delivery_site": "PYE RDC (Perryville, MD)",
        "delivery_date": "10/06",
        "delivery_ref": "PYE_061026919",
        "requested_local": "2026-10-05 09:00",
        "load_id": 2591072,
        "pickup_number": None,
    }
    assert (
        answer_from_rules("Both orders?", facts).message
        == "Yes, both orders: PO# 115802102660 & PO# 115802102661."
    )  # type: ignore[union-attr]
    assert answer_from_rules("Who is the carrier?", facts).facts_used == ["carrier"]  # type: ignore[union-attr]
    assert (
        answer_from_rules("What is your load number?", facts).message
        == "Our load number is 2591072."
    )  # type: ignore[union-attr]
    assert answer_from_rules("Can the driver come at 3?", facts) is None
    ok, why = answer_is_safe(
        AnswerDraft(answerable=True, message="Our load number is 2591072.", facts_used=["load_id"]),
        facts,
    )
    assert ok
    ok, why = answer_is_safe(
        AnswerDraft(answerable=True, message="The rate is $1400.", facts_used=[]), facts
    )
    assert not ok and "forbidden" in why
    ok, why = answer_is_safe(
        AnswerDraft(answerable=True, message="Sure.", facts_used=["driver"]), facts
    )
    assert not ok and "do not exist" in why
    with session_scope(sessions) as session:
        seed_vendor(sessions)
        scan(
            FakeTPro([lidl_load(8001, po="226321092660")], {}),
            sessions,
            settings.model_copy(update={"pilot_terminal_ids": [1089]}),
            days_ahead=7,
            now=NOW,
        )  # type: ignore[arg-type]
        case = list_cases(session)[0]
        built = case_facts(case, settings)
        assert built["customer"] == "Lidl" and built["delivery_date"] == "10/02"
        assert built["po_numbers"] == ["226321092660"]


def test_bare_set_and_time_only_confirmations_use_the_requested_slot(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)

    def script(ctx: ReplyContext) -> ReplyClassification:
        if "SET!" in ctx.body:
            return ReplyClassification(
                status=ReplyStatus.CONFIRMED,
                pickup_number="4119085",
                quotes=["SET!", "PU# 4119085"],
            )
        return ReplyClassification(
            status=ReplyStatus.CONFIRMED, pickup_time="14:30", quotes=["This is good for 1430"]
        )

    classifier = FakeReplyClassifier(script)
    responder = Responder(settings, mailer, now=NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        stats = ingest(
            session,
            [reply("SET!\n\nPU# 4119085", mid="s1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        # Bare "SET!" books the slot we asked for; the pod's "Thank you!" is drafted in the thread.
        assert stats.proposed == 1 and stats.responded == 1
        assert case.status == CaseStatus.PROPOSED.value
        assert case.confirmed_local == "2026-10-01 09:00" and case.pickup_number == "4119085"
        assert (
            mailer.drafts[-1].body.startswith("Thank you!")
            and case.messages[-1].kind == "acknowledge"
        )

        case.status = CaseStatus.SENT.value
        case.confirmed_local = None
        ingest(
            session,
            [reply("This is good for 1430", mid="t1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        # A time-only answer means the requested date at that time.
        assert case.status == CaseStatus.PROPOSED.value
        assert case.confirmed_local == "2026-10-01 14:30"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 18, 30, tzinfo=UTC)


def test_deferred_replies_keep_waiting_and_unbacked_confirmations_are_not_trusted(
    settings, sessions
):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)

    def script(ctx: ReplyContext) -> ReplyClassification:
        if "check back" in ctx.body.lower():
            return ReplyClassification(
                status=ReplyStatus.DEFERRED,
                pickup_date="2026-10-05",
                quotes=["Please check back on Monday, 10/5"],
            )
        return ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["not in the text"])

    classifier = FakeReplyClassifier(script)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        stats = ingest(
            session,
            [reply("PO is unconfirmed in our system. Please check back on Monday, 10/5", mid="d1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=Responder(settings, mailer, now=NOW),
        )
        assert stats.deferred == 1 and case.status == CaseStatus.SENT.value
        assert case.reason == "vendor asked to check back on 2026-10-05"
        assert len(mailer.drafts) == 1  # nothing drafted back

        stats = ingest(
            session,
            [reply("Sounds good.", mid="u2")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=Responder(settings, mailer, now=NOW),
        )
        # "confirmed" with no quote found in the text is not a confirmation.
        assert stats.unrelated == 1 and case.status == CaseStatus.SENT.value
        assert case.messages[-1].classification["status"] == "unrelated"
