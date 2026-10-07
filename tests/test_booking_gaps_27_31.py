"""Gaps 27 to 31 of the email edge-case review, closed one by one.

27 the follow-up counts silence as the to-dos do (weekday hours; only the desk's answer ends it),
28 one nudge per silence and check-backs after an earlier nudge, 29 "check back later" with no
day is chased, 30 a delivery moved under a booked pickup is checked, 31 ETA requests, late
arrivals and holds have their own to-dos, and an ETA is answered from the driver's check call.
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

from facility_profiles.api.booking import _email_update
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.facts import load_facts
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, BookingMessage, CaseStatus
from facility_profiles.booking.respond import MAX_NUDGES, Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus, ReplyTopic
from facility_profiles.booking.service import ingest, list_cases, mark_booked, scan
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.booking.writer import FakeReplyWriter, ReplySituation
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from facility_profiles.tpro.models import Dispatch, Load, TrackingNote
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, reply, seed_vendor
from tests.test_booking_respond import _prepared_case
from tests.test_booking_situation_replies import DISPATCH, LOAD, _written, freight_load
from tests.test_booking_writeback import FakeLoads

INTERNAL = ["circledelivers.com"]


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"pilot_terminal_ids": [1089], "booking_follow_up_hours": 24})


def _quiet_case(settings: Settings, sessions, mailer: RecordingMailer) -> int:  # type: ignore[no-untyped-def]
    """A request sent on Tuesday 09/29 08:00 ET for a pickup a week later."""
    case_id = _prepared_case(settings, sessions, mailer)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.requested_local = "2026-10-08 09:00"
    return case_id


def _nudge(settings, sessions, mailer, case_id: int, at: datetime) -> BookingMessage | None:  # type: ignore[no-untyped-def]
    """The follow-up the agent writes at ``at``, stamped with that time (drafts carry the clock)."""
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        message = Responder(settings, mailer, now=at).follow_up(session, case)
        if message is not None:
            message.created_at = at
        return message


def _read(sessions, case_id: int, message: InboundMessage, reading: ReplyClassification) -> None:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as session:
        ingest(
            session, [message], FakeReplyClassifier(lambda _c: reading), internal_domains=INTERNAL
        )


def _at(text: str, when: datetime, mid: str) -> InboundMessage:
    return InboundMessage(**{**reply(text, mid=mid).__dict__, "sent_at": when})


# ------------------------------------------------------------------ 27. silence as the to-dos count it


def test_the_follow_up_counts_weekday_hours_like_the_to_dos(settings, sessions) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    friday_noon = datetime(2026, 10, 2, 16, 0, tzinfo=UTC)  # 12:00 ET
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.messages[0].sent_at = friday_noon
        # A note from the customer's desk is not the facility answering.
        case.messages.append(
            BookingMessage(
                case_id=case.id,
                direction="in",
                kind="customer_desk",
                subject="RE: 226321092660",
                body="New delivery slot coming",
                sent_at=friday_noon + timedelta(hours=2),
            )
        )
    saturday = datetime(2026, 10, 3, 18, 0, tzinfo=UTC)  # 26 clock hours, no weekday hours
    monday_10 = datetime(2026, 10, 5, 14, 0, tzinfo=UTC)  # 12 + 10 = 22 weekday hours
    monday_13 = datetime(2026, 10, 5, 17, 0, tzinfo=UTC)  # 25 weekday hours
    assert _nudge(settings, sessions, mailer, case_id, saturday) is None
    assert _nudge(settings, sessions, mailer, case_id, monday_10) is None
    nudge = _nudge(settings, sessions, mailer, case_id, monday_13)
    assert nudge is not None and nudge.kind == "follow_up"
    assert "Following up on this." in mailer.drafts[-1].body


# ------------------------------------------------------------------ 28. once per silence; check-backs


def test_a_check_back_goes_even_after_a_nudge_and_each_silence_is_nudged_once(
    settings, sessions
) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    first = _nudge(settings, sessions, mailer, case_id, NOW + timedelta(hours=30))
    assert first is not None
    assert _nudge(settings, sessions, mailer, case_id, NOW + timedelta(hours=60)) is None
    # The desk answers: check back Monday.
    deferral = ReplyClassification(
        status=ReplyStatus.DEFERRED,
        pickup_date="2026-10-05",
        quotes=["Please check back on Monday, 10/5"],
    )
    text = "PO is unconfirmed. Please check back on Monday, 10/5"
    _read(sessions, case_id, _at(text, NOW + timedelta(hours=62), "d1"), deferral)
    friday = datetime(2026, 10, 2, 15, 0, tzinfo=UTC)
    monday = datetime(2026, 10, 5, 13, 0, tzinfo=UTC)
    assert _nudge(settings, sessions, mailer, case_id, friday) is None  # not their day yet
    check_back = _nudge(settings, sessions, mailer, case_id, monday)
    assert check_back is not None and check_back.kind == "follow_up"
    assert _nudge(settings, sessions, mailer, case_id, monday + timedelta(hours=1)) is None
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        reasons = [e.detail.get("reason") for e in case.events if e.action == "follow_up"]
        assert reasons[-1] == "vendor said to check back on 10/05"


def test_no_more_than_three_nudges_on_one_pickup(settings, sessions) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        for n in range(MAX_NUDGES):
            case.messages.append(
                BookingMessage(
                    case_id=case.id,
                    direction="out",
                    kind="follow_up",
                    subject="Re: x",
                    body="Following up on this.",
                    sent_at=NOW + timedelta(hours=1 + n),
                )
            )
    later = ReplyClassification(status=ReplyStatus.DEFERRED, quotes=["check back later"])
    _read(
        sessions,
        case_id,
        _at("Not released, check back later", NOW + timedelta(hours=5), "d2"),
        later,
    )
    assert _nudge(settings, sessions, mailer, case_id, NOW + timedelta(days=5)) is None


# ------------------------------------------------------------------ 29. "check back later"


def test_check_back_later_with_no_day_is_chased_after_a_day_of_weekday_hours(
    settings, sessions
) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    said_at = datetime(2026, 9, 30, 15, 30, tzinfo=UTC)  # Wednesday 11:30 ET
    undated = ReplyClassification(status=ReplyStatus.DEFERRED, quotes=["check back later"])
    _read(
        sessions,
        case_id,
        _at("Order not released yet, please check back later", said_at, "u1"),
        undated,
    )
    assert _nudge(settings, sessions, mailer, case_id, said_at + timedelta(hours=23)) is None
    nudge = _nudge(settings, sessions, mailer, case_id, said_at + timedelta(hours=25))
    assert nudge is not None
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        reason = next(e.detail["reason"] for e in reversed(case.events) if e.action == "follow_up")
        assert reason == "vendor said to check back later; 24 weekday hours since"


# ------------------------------------------------------------------ 30. delivery moved under a booking


def _booked(settings: Settings, sessions) -> tuple[int, dict[str, Any]]:  # type: ignore[no-untyped-def]
    """A pickup booked for Thu 10/01 09:00 ET (13:00 UTC); the load's delivery is 10/02 13:30 UTC."""
    seed_vendor(sessions)
    load = lidl_load(6100, po="226321092660")
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        mark_booked(session, case, by="megan", via="phone", local="2026-10-01 09:00")
        return case.id, load


def _rescan_with_delivery(settings, sessions, load: dict[str, Any], open_: str) -> None:  # type: ignore[no-untyped-def]
    moved = copy.deepcopy(load)
    moved["waypoints"][1]["appointmentTime"]["open"] = open_
    moved["waypoints"][1]["appointmentTime"]["close"] = open_
    scan(FakeTPro([moved], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]


def test_a_delivery_moved_too_early_for_the_booked_pickup_is_raised(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, load = _booked(settings, sessions)
    # Later: the booked pickup still makes it, so nothing is raised.
    _rescan_with_delivery(settings, sessions, load, "2026-10-03T13:30:00Z")
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == []
    # Earlier: 13:00 UTC + 559 miles at 50 mph + 2 h of loading lands after 20:00 UTC.
    _rescan_with_delivery(settings, sessions, load, "2026-10-01T20:00:00Z")
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["delivery_moved"]
        said = case.open_exceptions[0].description
        assert "the booked pickup Thu 10/01 09:00 ET would arrive" in said
        assert said.endswith("ask the vendor for an earlier pickup, then reschedule")


def test_the_customer_desk_moving_the_delivery_checks_the_booked_pickup(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, _ = _booked(settings, sessions)
    lidl = InboundMessage(
        message_id="z9",
        thread_id="tz9",
        sent_at=datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        from_addr="inbound@lidl.us",
        to_addr="megan.goodwin@circledelivers.com",
        cc_addr="lidl@circledelivers.com",
        subject="226321092660 MISSED PICK UP",
        body="Here is an updated appointment! 10/1 6PM - PYE_011026926.",
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [lidl],
            FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED)),
            internal_domains=INTERNAL,
            customer_desk="inbound@lidl.us",
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == ["delivery_moved"]
        assert case.open_exceptions[0].description.startswith(
            "the customer's desk moved the delivery to PYE_011026926; the booked pickup"
        )


# ------------------------------------------------------------------ 31. ETA, late arrivals, holds


def _topic(topic: ReplyTopic, question: str, **fields: Any) -> ReplyClassification:
    return ReplyClassification(
        status=fields.pop("status", ReplyStatus.QUESTION),
        topic=topic,
        question=question,
        questions=[question],
        **fields,
    )


def test_an_eta_request_on_a_booked_pickup_has_its_own_to_do(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, _ = _booked(settings, sessions)
    ask = "What is the driver's ETA?"
    _read(sessions, case_id, reply(ask, mid="e1"), _topic(ReplyTopic.ETA, ask))
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == ["eta_requested"]
        assert case.open_exceptions[0].description == (
            "facility asks for the driver's ETA: What is the driver's ETA?"
        )


def test_a_late_arrival_offer_names_the_latest_time_and_goes_to_a_person(
    settings, sessions
) -> None:
    settings = _settings(settings)
    case_id, _ = _booked(settings, sessions)
    said = "We missed you this morning. Latest we can take him is 9pm tonight."
    reading = _topic(
        ReplyTopic.WORK_IN,
        "Latest we can take him is 9pm tonight.",
        pickup_time="21:00",
        quotes=["Latest we can take him is 9pm tonight."],
    )
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(said, mid="w1")],  # written Wed 09/30 11:31 ET
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=INTERNAL,
            responder=Responder(settings, mailer, now=NOW),
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["work_in_offered"]
        assert case.open_exceptions[0].description.startswith(
            "facility will still take the truck until Wed 09/30 21:00 ET"
        )
        assert mailer.drafts == []  # a person checks the driver can make it


def test_a_hold_with_no_day_stops_the_chasing_and_a_booked_one_is_a_change(
    settings, sessions
) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    hold = "This order is on hold, we will let you know."
    reading = _topic(ReplyTopic.HOLD, hold, status=ReplyStatus.DEFERRED)
    _read(sessions, case_id, reply(hold, mid="h1"), reading)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["on_hold"]
    assert _nudge(settings, sessions, mailer, case_id, NOW + timedelta(days=5)) is None


def test_a_hold_read_as_a_decline_is_still_a_hold(settings, sessions) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _quiet_case(settings, sessions, mailer)
    drafted = len(mailer.drafts)
    hold = "This order has been put on hold by our planning team."
    reading = _topic(ReplyTopic.HOLD, hold, status=ReplyStatus.REJECTED, reject_reason="not_ready")
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(hold, mid="h3")],
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=INTERNAL,
            responder=Responder(settings, mailer, now=NOW),
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["on_hold"]
    assert len(mailer.drafts) == drafted  # no note asking the customer to move the delivery


def test_a_hold_on_a_booked_pickup_is_a_change_for_a_person(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, _ = _booked(settings, sessions)
    hold = "Please put this one on hold."
    _read(sessions, case_id, reply(hold, mid="h2"), _topic(ReplyTopic.HOLD, hold))
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["booked_slot_changed"]


def _tracked(now: datetime) -> FakeLoads:
    """The load with its dispatch, and the driver's check calls and positions."""

    class Tracked(FakeLoads):
        def get_tracking_load_notes(self, load_id: int) -> list[TrackingNote]:
            def note(minutes: int, **fields: Any) -> TrackingNote:
                at = (now - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
                return TrackingNote.model_validate({"eventDate": at, "loadId": load_id, **fields})

            return [
                note(300, dataSource="Phone", dispatchId=501, comments="Loaded, rolling"),
                note(40, dataSource="Phone", dispatchId=501, comments="Driver 30 minutes out"),
                note(20, dataSource="Phone", dispatchId=999, comments="Old carrier, ignore"),
                note(10, dataSource="Phone", dispatchId=501, comments="Detention starts at 2h"),
                note(
                    5, dataSource="Phone", dispatchId=501, comments="Called DISP for times at REC"
                ),
                note(4, dataSource="Phone", dispatchId=501, comments="POD indexed"),
                note(
                    15,
                    dataSource="Macropoint",
                    dispatchId=501,
                    location={"city": "Florence", "state": "KY"},
                ),
            ]

    loads = Tracked(freight_load())
    loads.dispatches[LOAD] = [DISPATCH]
    return loads


def test_the_driver_check_call_and_position_are_facts_for_an_eta() -> None:
    now = datetime(2026, 10, 8, 12, 0, tzinfo=UTC)
    notes = _tracked(now).get_tracking_load_notes(LOAD)
    facts = load_facts(
        Load.model_validate(freight_load()),
        [Dispatch.model_validate(DISPATCH)],
        notes=notes,
        now=now,
    )
    # Not the old carrier's note, the one about money, nor Circle's own tracking notes; 11:20 UTC
    # is 07:20 ET.
    assert facts["last_check_call"] == "10/08 @ 0720 ET: Driver 30 minutes out"
    assert facts["last_location"] == "Florence, KY at 10/08 @ 0745 ET"
    stale = load_facts(
        Load.model_validate(freight_load()),
        [Dispatch.model_validate(DISPATCH)],
        notes=notes,
        now=now + timedelta(days=2),
    )
    assert stale["last_check_call"] is None and stale["last_location"] is None


def test_an_eta_is_answered_from_the_drivers_check_call(settings, sessions) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    ask = "When will the driver get here?"

    def script(situation: ReplySituation) -> Any:
        call = situation.facts["last_check_call"]
        return _written(
            f"Our last update from the driver, {call}. Thank you!", (ask, True, ["last_check_call"])
        )

    responder = Responder(
        settings,
        mailer,
        writer=FakeReplyWriter(script),
        now=NOW,
        facts=_tracked(datetime.now(tz=UTC)),
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(ask, mid="e2")],
            FakeReplyClassifier(lambda _c: _topic(ReplyTopic.ETA, ask)),
            internal_domains=INTERNAL,
            responder=responder,
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == []
    assert "Driver 30 minutes out" in mailer.drafts[-1].body


def test_an_eta_the_agent_cannot_give_stays_one_to_do_with_the_rest(settings, sessions) -> None:
    settings = _settings(settings)
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    eta, weight = "What is the driver's ETA?", "What is the weight?"

    def script(_situation: ReplySituation) -> Any:
        return _written(
            "It is 40,000 lbs. We will get back to you on the ETA. Thank you!",
            (eta, False, []),
            (weight, True, ["weight"]),
        )

    loads = FakeLoads(freight_load())
    loads.dispatches[LOAD] = [DISPATCH]
    responder = Responder(settings, mailer, writer=FakeReplyWriter(script), now=NOW, facts=loads)
    reading = ReplyClassification(
        status=ReplyStatus.QUESTION, topic=ReplyTopic.ETA, question=eta, questions=[eta, weight]
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(f"{eta} {weight}", mid="e3")],
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=INTERNAL,
            responder=responder,
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["eta_requested"]
        assert "also asked: What is the driver's ETA?" in case.open_exceptions[0].description


def test_latest_updates_say_what_the_facility_asked() -> None:
    message = BookingMessage(
        direction="in",
        kind="reply",
        from_addr="Dana <dana@vendor.example>",
        subject="RE: Pick Up",
        sent_at=NOW,
        classification={"status": "question", "topic": "eta"},
    )
    line = _email_update(message, ["circledelivers.com"])
    assert line is not None and line[1] == "Dana wrote, read as asked for the driver's ETA"
