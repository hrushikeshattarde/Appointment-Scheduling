"""The conversational policy: counter-offers, questions, rejections, follow-ups, hand-offs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.facts import case_facts
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.respond import (
    AnswerDraft,
    Responder,
    alternative_days,
    answer_from_rules,
    answer_is_safe,
    offer_is_feasible,
)
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases, mark_sent, scan
from facility_profiles.booking.worklist import open_kinds, resolve
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
        draft_case(session, case, mailer, settings, now=NOW)
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
        # The agent settled the offer; the slot it accepted still waits for a person's approval.
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["confirmation_review"]
        offer = case.exceptions[0]
        assert offer.kind == "proposed_time_review"
        assert offer.resolution == "agent accepted: offer 10/01 @ 1100 makes the delivery slot"
        assert case.confirmed_local == "2026-10-01 11:00"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
        sent = mailer.drafts[-1]
        assert sent.subject.startswith("Re: ") and sent.body.startswith("Yes, 10/01 @ 1100 works.")
        assert sent.thread_id == "t1" and sent.in_reply_to == "<c1@vendor.test>"
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
        # The two offers the agent answered are settled; the third hits the round cap and stays
        # open for a person, with the agent's reason on it. No draft.
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["proposed_time_review"]
        assert all(
            (e.resolution or "").startswith("agent asked for other days: offer would arrive")
            for e in case.exceptions[:2]
        )
        assert case.open_exceptions[0].detail["agent"] == "2 rounds reached"
        assert (
            case.events[-1].action == "handoff"
            and "rounds reached" in case.events[-1].detail["reason"]
        )


def test_without_a_writer_the_fixed_rules_answer_and_the_rest_is_handed_off(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)

    def script(ctx: ReplyContext) -> ReplyClassification:
        return ReplyClassification(status=ReplyStatus.QUESTION, question=ctx.body.strip())

    classifier = FakeReplyClassifier(script)
    responder = Responder(settings, mailer, now=NOW)
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

        ask("Both orders?", "q1")
        assert case.status == CaseStatus.PENDING.value and open_kinds(case) == []
        assert case.exceptions[0].resolution == "agent answered from po_numbers"
        assert mailer.drafts[-1].body.startswith("Just PO# 226321092660. Thank you!")
        ask("Where is this delivering to?", "q2")
        assert "Lidl (Perryville, MD) on 10/02 (PYE_021026123)" in mailer.drafts[-1].body
        drafted = len(mailer.drafts)
        # No trucking company is on the case without the load's dispatch: no fixed answer.
        ask("Which carrier is picking up?", "q3")
        assert len(mailer.drafts) == drafted
        assert open_kinds(case) == ["facility_question"]
        assert "no rule answers" in case.events[-1].detail["reason"]
        resolve(
            session, case, [ExceptionType.FACILITY_QUESTION], resolution="told them", by="megan"
        )
        responder.settings = settings.model_copy(update={"booking_max_rounds": 10})
        ask("Confirm PO 999999999 please?", "q4")
        assert open_kinds(case) == ["facility_question"]
        assert len(mailer.drafts) == drafted


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
            status=ReplyStatus.REJECTED,
            reject_reason=RejectReason.NOT_READY,
            question="PO will not be ready until 10/07",
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
        # Declined, and the decline stays open: a person sends the note and Lidl moves the delivery.
        assert case.status == CaseStatus.DECLINED.value
        assert case.reason == (
            "vendor cannot book (the order is not ready that day): PO will not be ready until 10/07"
        )
        assert open_kinds(case) == ["facility_declined"]
        assert "note to the customer desk drafted" in case.open_exceptions[0].description
        note = mailer.drafts[-1]
        assert note.to_addr == "inbound@lidl.us" and note.thread_id is None
        assert note.subject == "RESCHEDULE 226321092660"
        assert (
            "PO will not be ready until 10/07" in note.body
            and "new delivery appointment" in note.body
        )
        assert case.messages[-1].kind == "escalate_to_customer"

        ingest(
            session,
            [reply("Will you pay detention if the driver waits?", mid="d1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        # A question does not move the slot: still declined, the decline still open, and the
        # money question handed to a person beside it.
        assert case.status == CaseStatus.DECLINED.value
        assert open_kinds(case) == ["facility_declined", "facility_question"]
        assert case.open_exceptions[1].detail["agent"] == "reply mentions money or a claim"
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
        assert case.status == CaseStatus.PENDING.value
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
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["confirmation_review"]
        assert case.confirmed_local == "2026-10-01 09:00" and case.pickup_number == "4119085"
        assert (
            mailer.drafts[-1].body.startswith("Thank you!")
            and case.messages[-1].kind == "acknowledge"
        )

        ingest(
            session,
            [reply("This is good for 1430", mid="t1")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        # A time-only answer means the requested date at that time. The newer confirmation
        # replaces the one still waiting for approval; there is one review, for 14:30, and
        # 14:30 is five and a half hours from the 09:00 asked for, so that is raised with it.
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["confirmation_review", "confirmed_outside_window"]
        assert "Thu 10/01 14:30 ET" in case.open_exceptions[0].description
        assert case.open_exceptions[1].description == (
            "vendor confirmed Thu 10/01 14:30 ET; we asked for Thu 10/01 09:00 ET (5.5 h later)"
        )
        assert case.exceptions[0].resolution == "superseded by a later reply (vendor_confirmed)"
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
        assert stats.deferred == 1 and case.status == CaseStatus.PENDING.value
        assert case.reason == "vendor asked to check back on 2026-10-05"
        # Monday 10/5 is after the pickup asked for (Thu 10/01): the pickup is at risk.
        assert open_kinds(case) == ["check_back_too_late"]
        assert case.open_exceptions[0].description == (
            "facility said to check back Mon 10/05, on or after the pickup Thu 10/01 09:00 ET; "
            "the pickup is at risk"
        )
        assert len(mailer.drafts) == 1  # nothing drafted back

        stats = ingest(
            session,
            [reply("Sounds good.", mid="u2")],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=Responder(settings, mailer, now=NOW),
        )
        # "confirmed" with no quote found in the text is not a confirmation.
        assert stats.unrelated == 1 and case.status == CaseStatus.PENDING.value
        assert case.messages[-1].classification["status"] == "unrelated"


def test_follow_up_waits_for_the_vendor_check_back_day(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_follow_up_hours": 24}
    )
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    classifier = FakeReplyClassifier(
        lambda _c: ReplyClassification(
            status=ReplyStatus.DEFERRED,
            pickup_date="2026-10-05",
            quotes=["check back on Monday, 10/5"],
        )
    )
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        # A pickup a week out, so Monday's check-back still leaves time to book it.
        case.requested_local = "2026-10-08 09:00"
        ingest(
            session,
            [reply("PO is unconfirmed. Please check back on Monday, 10/5", mid="cb1")],
            classifier,
            internal_domains=["circledelivers.com"],
        )
        assert case.status == CaseStatus.PENDING.value
        # Two days later would normally trigger a nudge, but the vendor named a day.
        assert (
            Responder(settings, mailer, now=NOW + timedelta(days=2)).follow_up(session, case)
            is None
        )
        due = Responder(settings, mailer, now=datetime(2026, 10, 5, 13, 0, tzinfo=UTC))
        message = due.follow_up(session, case)
        assert message is not None and message.kind == "follow_up"
        assert mailer.drafts[-1].body.startswith("Good Morning,\n\nChecking in on this!")
        assert "check back on 10/05" in case.events[-1].detail["reason"]


def test_edited_times_in_the_quoted_text_count_as_a_counter_offer(settings, sessions):
    from facility_profiles.booking.classify import validate_classification
    from facility_profiles.booking.mail import split_quoted

    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    raw = (
        "These are good for the adjusted time below..\n\nThank you\n\n"
        "From: Megan Goodwin\nSent: Wednesday, August 12, 2026 3:36 PM\n"
        "Subject: Lidl Pick Up Appointments\n\nPO# 226321092660 on 10/01 @ 1430\n"
    )
    own, quoted = split_quoted(raw)
    assert own.startswith("These are good") and "on 10/01 @ 1430" in quoted

    def script(ctx: ReplyContext) -> ReplyClassification:
        assert "@ 1430" in ctx.quoted  # the classifier is shown the quoted part
        return ReplyClassification(
            status=ReplyStatus.COUNTER_OFFER,
            pickup_date="2026-10-01",
            pickup_time="14:30",
            quotes=["PO# 226321092660 on 10/01 @ 1430"],
        )

    # The quoted history may back a counter-offer (the vendor edited the time inside it)...
    kept, issues = validate_classification(
        script(ReplyContext("", [], None, NOW, "", own, quoted)), own, quoted
    )
    assert kept.status == ReplyStatus.COUNTER_OFFER and not issues
    # ...but never a confirmation: a chaser carrying the old slot underneath must not re-book it.
    as_confirmed = script(ReplyContext("", [], None, NOW, "", own, quoted)).model_copy(
        update={"status": ReplyStatus.CONFIRMED}
    )
    kept, issues = validate_classification(as_confirmed, "Did this get resolved?", quoted)
    assert kept.status == ReplyStatus.UNRELATED and kept.pickup_date is None
    assert any("quoted history" in i.reason for i in issues)

    classifier = FakeReplyClassifier(script)
    responder = Responder(settings, mailer, now=NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        message = InboundMessage(
            message_id="q9",
            thread_id="t1",
            sent_at=datetime(2026, 9, 30, 15, 0, tzinfo=UTC),
            from_addr="gweaver@delgrossos.com",
            to_addr="",
            cc_addr="",
            subject="Re: Pick Up Appointment: 226321092660",
            body=own,
            quoted=quoted,
        )
        ingest(
            session,
            [message],
            classifier,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        # 14:30 on 10/01 still makes the delivery, so the agent accepts it.
        assert (
            case.status == CaseStatus.PENDING.value and case.confirmed_local == "2026-10-01 14:30"
        )
        assert open_kinds(case) == ["confirmation_review"]
        assert mailer.drafts[-1].body.startswith("Yes, 10/01 @ 1430 works.")
