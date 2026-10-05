"""The sixteen vendor replies from the 2026-10-05 readiness check, replayed with the agent on its own.

Each reply was first run through the production model; the readings scripted here are what the
model gives with the reply-v4 prompt (times as written, the zone the reply names, a reason for a
decline). The agent runs as autonomously as a customer file allows: answers sent, confirmations
booked, Transport Pro written back. Everything is invented and in memory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.booking.automation import RunReport, run_once
from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.mail import InboundMessage, RecordingMailer, RecordingSender
from facility_profiles.booking.models import BookingCase, CaseStatus, UnmatchedMail
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import approve, reschedule_case
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from tests.test_booking import NOW, lidl_load
from tests.test_booking_autonomy import AUTO, PO, _sent_request, _settings
from tests.test_booking_writeback import FakeLoads

DAY = NOW + timedelta(hours=2)  # Tue 09/29 10:00 ET, two hours after the request went out
EVENING = datetime(2026, 9, 30, 0, 30, tzinfo=UTC)  # Tue 09/29 20:30 ET
LOAD = 7001
ASKED = "PO# 115802102660 on 10/01 @ 0900"
QUOTED = (
    f"From: lidl@circledelivers.com\nSent: Tuesday\n\nCan I please schedule the following?\n{ASKED}"
)


@pytest.fixture(autouse=True)
def _forget_customer_files():  # type: ignore[no-untyped-def]
    from facility_profiles.customers import registry

    yield
    registry.reload()


class FakeInbox:
    def __init__(self, messages: list[InboundMessage]) -> None:
        self.messages = messages

    def fetch(self) -> list[InboundMessage]:
        return list(self.messages)


def reply(
    body: str,
    sent_id: str | None,
    at: datetime = DAY,
    *,
    mid: str,
    sender: str = "Desk Staff <desk.staff@udfinc.com>",
    subject: str = "Re: Pick Up Appointment: 115802102660",
) -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        thread_id=None,
        sent_at=at,
        from_addr=sender,
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject=subject,
        body=body,
        in_reply_to=sent_id,
        rfc_message_id=f"<{mid}@udfinc.example>",
        references=sent_id,
        quoted=QUOTED if sent_id else "",
    )


@dataclass
class World:
    """One pickup asked for 10/01 09:00 ET by a sent request; the agent runs on its own."""

    settings: Settings
    sessions: sessionmaker[Session]
    case_id: int
    sent_id: str
    sender: RecordingSender
    tpro: FakeLoads = field(default_factory=lambda: FakeLoads(lidl_load(LOAD, po=PO)))
    contexts: list[ReplyContext] = field(default_factory=list)

    def run(
        self, messages: list[InboundMessage], readings: dict[str, ReplyClassification], at: datetime
    ) -> tuple[RunReport, list[str]]:
        """One pass; returns the report and the bodies of what was sent in it."""

        def script(ctx: ReplyContext) -> ReplyClassification:
            self.contexts.append(ctx)
            return next(r for start, r in readings.items() if ctx.body.startswith(start))

        before = len(self.sender.drafts)
        with session_scope(self.sessions) as s:
            report = run_once(
                s,
                self.settings,
                now=at,
                mailer=RecordingMailer(),
                sender=self.sender,
                inbox=FakeInbox(messages),
                classifier=FakeReplyClassifier(script),
                client=self.tpro,
            )
        return report, [d.body.split("\n\n")[0] for d in self.sender.drafts[before:]]

    def case(self) -> dict[str, Any]:
        with session_scope(self.sessions) as s:
            case = s.get(BookingCase, self.case_id)
            assert case is not None
            return {
                "status": case.status,
                "confirmed": case.confirmed_local,
                "open": open_kinds(case),
                "said": [e.description for e in case.open_exceptions],
                "events": [e.action for e in case.events],
            }


@pytest.fixture
def world(settings: Settings, sessions: sessionmaker[Session], tmp_path: Path) -> World:
    on = _settings(settings, tmp_path, AUTO).model_copy(update={"booking_tpro_writeback": True})
    case_id, sent_id, sender = _sent_request(on, sessions)
    return World(on, sessions, case_id, sent_id, sender)


def confirmed(day: str, clock: str | None, quote: str, **more: Any) -> ReplyClassification:
    return ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date=day,
        pickup_time=clock,
        quotes=[quote],
        confidence=0.95,
        **more,
    )


def offered(day: str, clock: str, quote: str) -> ReplyClassification:
    return ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date=day,
        pickup_time=clock,
        quotes=[quote],
        confidence=0.9,
    )


# ------------------------------------------------------------------ booked as asked


def test_s1_an_exact_confirmation_is_booked_thanked_and_written_with_a_note(world: World) -> None:
    body = "Confirmed for 10/1 at 0900. PU# 20463798"
    reading = confirmed("2026-10-01", "09:00", body, pickup_number="20463798")
    report, sent = world.run([reply(body, world.sent_id, mid="s1")], {"Confirmed": reading}, DAY)
    case = world.case()
    assert (case["status"], case["open"], sent) == (CaseStatus.SCHEDULED.value, [], ["Thank you!"])
    assert report.auto_confirmed == 1 and report.written_to_tpro == 1
    assert world.tpro.writes == [
        (LOAD, "SH", "2026-10-01T13:00:00Z", "2026-10-01T13:00:00Z", "Confirmed")
    ]
    [(load, note)] = world.tpro.notes
    assert load == LOAD and "Thu 10/01 09:00 ET" in note and "Pickup number: 20463798." in note


def test_s5_a_window_with_conditions_is_booked_as_a_window_and_the_note_carries_them(
    world: World,
) -> None:
    body = "Booked 10/1 between 8:00 and 10:00. Driver must check in at the guard shack."
    reading = confirmed(
        "2026-10-01",
        "08:00",
        "Booked 10/1 between 8:00 and 10:00",
        pickup_time_end="10:00",
        conditions=["check in at the guard shack"],
    )
    _, sent = world.run([reply(body, world.sent_id, mid="s5")], {"Booked": reading}, DAY)
    assert world.case()["status"] == CaseStatus.SCHEDULED.value and sent == ["Thank you!"]
    assert world.tpro.writes == [
        (LOAD, "SH", "2026-10-01T12:00:00Z", "2026-10-01T14:00:00Z", "Confirmed")
    ]
    note = world.tpro.notes[0][1]
    assert "Thu 10/01 08:00 ET to 10:00 ET" in note
    assert "Facility says: check in at the guard shack." in note


def test_s6_outlook_link_cruft_around_the_po_does_not_stop_a_booking(world: World) -> None:
    body = "115802102660<tel:(580)%20210-2660> confirmed 10/1 @ 9am pickup# 20463798"
    reading = confirmed(
        "2026-10-01",
        "09:00",
        "115802102660 confirmed 10/1 @ 9am pickup# 20463798",
        pickup_number="20463798",
    )
    world.run([reply(body, world.sent_id, mid="s6")], {"1158": reading}, DAY)
    assert world.case()["status"] == CaseStatus.SCHEDULED.value
    assert len(world.tpro.writes) == 1


# ------------------------------------------------------------------ held for a person, not thanked


def test_s2_a_pickup_number_alone_is_held_and_not_thanked(world: World) -> None:
    reading = confirmed("2026-10-01", "09:00", "PU# 20463798", pickup_number="20463798")
    _, sent = world.run([reply("PU# 20463798", world.sent_id, mid="s2")], {"PU#": reading}, DAY)
    case = world.case()
    assert case["status"] == CaseStatus.PENDING.value and case["open"] == ["confirmation_review"]
    assert sent == [] and "thanks_held" in case["events"] and world.tpro.writes == []


def test_s12_a_different_time_is_held_and_not_thanked(world: World) -> None:
    body = "Confirmed 10/1 @ 1500."
    reading = confirmed("2026-10-01", "15:00", body)
    _, sent = world.run([reply(body, world.sent_id, mid="s12")], {"Confirmed": reading}, DAY)
    case = world.case()
    assert case["open"] == ["confirmation_review", "confirmed_outside_window"]
    assert case["said"][1] == (
        "vendor confirmed Thu 10/01 15:00 ET; we asked for Thu 10/01 09:00 ET (6 h later)"
    )
    assert sent == [] and world.tpro.writes == []


def test_s7a_tomorrow_written_at_two_in_the_afternoon_is_the_next_day(world: World) -> None:
    body = "Tomorrow at 9am works for us."
    reading = confirmed("2026-09-30", "09:00", body)
    at = NOW + timedelta(hours=6)
    _, sent = world.run([reply(body, world.sent_id, at, mid="s7a")], {"Tomorrow": reading}, at)
    assert "Reply written: 2026-09-29 Tuesday 14:00 ET" in _prompt(world)
    case = world.case()
    assert case["open"] == ["confirmation_review", "confirmed_outside_window"] and sent == []


def test_s7b_tomorrow_written_at_eight_thirty_in_the_evening_is_still_the_next_day(
    world: World,
) -> None:
    """The model was told the reply came on Wednesday (UTC); "tomorrow" became Thursday."""
    body = "Tomorrow at 9am works for us."
    reading = confirmed("2026-09-30", "09:00", body)  # what the model reads from the prompt now
    world.run([reply(body, world.sent_id, EVENING, mid="s7b")], {"Tomorrow": reading}, EVENING)
    prompt = _prompt(world)
    assert "Reply written: 2026-09-29 Tuesday 20:30 ET (the facility's local time)" in prompt
    assert "Wednesday" not in prompt
    case = world.case()
    assert case["status"] == CaseStatus.PENDING.value and world.tpro.writes == []
    assert case["open"] == ["confirmation_review", "confirmed_outside_window"]


def _prompt(world: World) -> str:
    from facility_profiles.booking.classify import render_user_message

    return render_user_message(world.contexts[-1])


# ------------------------------------------------------------------ offers, questions, declines


def test_s3_a_later_time_that_makes_the_delivery_is_accepted_and_booked(world: World) -> None:
    body = "We are full at 9. Can do 10/1 at 1:30 PM instead."
    reading = offered("2026-10-01", "13:30", "Can do 10/1 at 1:30 PM instead")
    _, sent = world.run([reply(body, world.sent_id, mid="s3")], {"We are full": reading}, DAY)
    assert sent == ["Yes, 10/01 @ 1330 works. Thank you!"]
    assert world.case()["status"] == CaseStatus.SCHEDULED.value
    assert world.tpro.writes[0][2] == "2026-10-01T17:30:00Z"


def test_s4_an_offer_that_misses_the_delivery_asks_for_other_days(world: World) -> None:
    body = "Earliest we have is Friday 10/2 at 7am."
    reading = offered("2026-10-02", "07:00", "Friday 10/2 at 7am")
    _, sent = world.run([reply(body, world.sent_id, mid="s4")], {"Earliest": reading}, DAY)
    assert sent[0].startswith(
        "That would not make our delivery appointment on 10/02 (PYE_021026123)."
    )
    assert world.case()["status"] == CaseStatus.PENDING.value and world.tpro.writes == []


def test_s8_check_back_on_the_pickup_day_is_raised(world: World) -> None:
    body = "This order isn't released yet, please check back Thursday."
    reading = ReplyClassification(
        status=ReplyStatus.DEFERRED, pickup_date="2026-10-01", quotes=["check back Thursday"]
    )
    _, sent = world.run([reply(body, world.sent_id, mid="s8")], {"This order": reading}, DAY)
    case = world.case()
    assert case["open"] == ["check_back_too_late"] and sent == []


def test_s9_the_delivery_number_question_gets_the_delivery_number(world: World) -> None:
    body = "What is the delivery appointment number for this PO?"
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question=body, quotes=[body])
    _, sent = world.run([reply(body, world.sent_id, mid="s9")], {"What is": reading}, DAY)
    assert sent == ["The delivery number is PYE_021026123. Thank you!"]
    assert world.case()["open"] == []


def test_s10_a_question_the_case_cannot_answer_goes_to_a_person(world: World) -> None:
    body = "Please send the driver name, cell number and trailer number for gate registration."
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question=body, quotes=[body])
    _, sent = world.run([reply(body, world.sent_id, mid="s10")], {"Please send": reading}, DAY)
    assert sent == [] and world.case()["open"] == ["facility_question"]


def test_s11_a_po_the_facility_does_not_have_goes_to_a_person_not_the_customer(
    world: World,
) -> None:
    body = "We do not have this PO in our system. Please verify with the customer."
    reading = ReplyClassification(
        status=ReplyStatus.REJECTED,
        reject_reason=RejectReason.PO_NOT_FOUND,
        quotes=["We do not have this PO in our system"],
    )
    _, sent = world.run([reply(body, world.sent_id, mid="s11")], {"We do not": reading}, DAY)
    case = world.case()
    assert sent == []  # nothing to inbound@lidl.us: moving the delivery would not help
    assert case["status"] == CaseStatus.DECLINED.value and case["open"] == ["facility_declined"]
    assert case["said"][0].startswith(
        "vendor cannot book (the PO is not in their system): We do not have this PO in our system."
    )
    assert "check the PO with the customer" in case["said"][0]


# ------------------------------------------------------------------ a booked pickup


def _book(world: World) -> None:
    body = "Confirmed for 10/1 at 0900. PU# 20463798"
    reading = confirmed("2026-10-01", "09:00", body, pickup_number="20463798")
    world.run([reply(body, world.sent_id, mid="b1")], {"Confirmed for": reading}, DAY)
    assert world.case()["status"] == CaseStatus.SCHEDULED.value


def test_s13_a_facility_moving_a_booked_pickup_goes_to_a_person(world: World) -> None:
    _book(world)
    later = DAY + timedelta(hours=3)
    body = "We need to move your pickup on 10/1 to 2:00 PM due to a line issue. Please confirm."
    reading = offered("2026-10-01", "14:00", "move your pickup on 10/1 to 2:00 PM")
    report, sent = world.run(
        [reply(body, world.sent_id, later, mid="b2")], {"We need": reading}, later
    )
    case = world.case()
    assert sent == [] and report.booked_changed == 1
    assert case["status"] == CaseStatus.PENDING.value
    assert case["open"] == ["proposed_time_review", "booked_slot_changed"]
    assert case["said"][1] == (
        "booked for Thu 10/01 09:00 ET: the facility wants to move it to Thu 10/01 14:00 ET; "
        "Transport Pro still shows the booked time"
    )
    assert len(world.tpro.writes) == 1  # still the 09:00 written at booking, until a person decides


def test_s14_a_correction_in_the_same_pass_is_not_lost(world: World) -> None:
    first = "Confirmed for 10/1 at 0900."
    second = "Sorry, correction: 10/1 at 11:00 AM, not 9."
    readings = {
        "Confirmed for": confirmed("2026-10-01", "09:00", first),
        "Sorry": confirmed("2026-10-01", "11:00", "10/1 at 11:00 AM"),
    }
    messages = [
        reply(first, world.sent_id, DAY, mid="c1"),
        reply(second, world.sent_id, DAY + timedelta(minutes=4), mid="c2"),
    ]
    _, sent = world.run(messages, readings, DAY + timedelta(minutes=10))
    case = world.case()
    assert case["status"] == CaseStatus.PENDING.value and case["confirmed"] == "2026-10-01 11:00"
    assert case["open"] == ["confirmation_review", "booked_slot_changed"]
    assert sent == ["Thank you!"]  # for the 09:00, before the correction came
    assert world.tpro.writes == []  # the 09:00 was never written: the case moved on first


def test_s16_approving_a_new_time_replaces_the_agents_own_earlier_write(world: World) -> None:
    _book(world)
    with session_scope(world.sessions) as s:
        case = s.get(BookingCase, world.case_id)
        assert case is not None
        message = reschedule_case(
            s,
            case,
            world.sender,
            world.settings,
            requested_local="2026-10-01 14:00",
            by="megan",
            now=DAY + timedelta(hours=1),
        )
        new_id = message.rfc_message_id
    at = DAY + timedelta(hours=2)
    body = "Confirmed 10/1 at 1400."
    world.run(
        [reply(body, new_id, at, mid="d2")],
        {"Confirmed 10/1": confirmed("2026-10-01", "14:00", body)},
        at,
    )
    case = world.case()
    assert case["status"] == CaseStatus.SCHEDULED.value and case["open"] == []
    assert [w[2] for w in world.tpro.writes] == ["2026-10-01T13:00:00Z", "2026-10-01T18:00:00Z"]
    assert "Pickup appointment moved" in world.tpro.notes[-1][1]


def test_a_booked_pickup_confirmed_again_changes_nothing(world: World) -> None:
    _book(world)
    later = DAY + timedelta(hours=1)
    body = "Confirmed again for 10/1 @ 0900, see you then."
    world.run(
        [reply(body, world.sent_id, later, mid="b3")],
        {"Confirmed again": confirmed("2026-10-01", "09:00", "10/1 @ 0900")},
        later,
    )
    case = world.case()
    assert case["status"] == CaseStatus.SCHEDULED.value and case["open"] == []
    assert "vendor_reconfirmed" in case["events"] and len(world.tpro.writes) == 1


def test_a_question_on_a_booked_pickup_is_answered_and_the_booking_stands(world: World) -> None:
    _book(world)
    later = DAY + timedelta(hours=1)
    body = "Which carrier is picking this up?"
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question=body, quotes=[body])
    _, sent = world.run([reply(body, world.sent_id, later, mid="b4")], {"Which": reading}, later)
    assert sent == ["The carrier is Circle Logistics, Inc. Thank you!"]
    case = world.case()
    assert case["status"] == CaseStatus.SCHEDULED.value and case["open"] == []


# ------------------------------------------------------------------ mail that finds no thread


def test_s15_another_person_at_the_desks_company_finds_the_pickup_but_is_not_booked_on(
    world: World,
) -> None:
    body = "Your pickup for 10/1 is set for 9am."
    msg = reply(body, None, mid="s15", sender="Dock Office <dock@udfinc.com>", subject="Pickup")
    _, sent = world.run([msg], {"Your pickup": confirmed("2026-10-01", "09:00", body)}, DAY)
    case = world.case()
    # Tied by the company's domain only: too weak to book on, so a person approves it.
    assert case["status"] == CaseStatus.PENDING.value and case["open"] == ["confirmation_review"]
    assert sent == [] and world.tpro.writes == []


def test_mail_no_pickup_matches_is_kept_once_for_a_person(world: World) -> None:
    body = "Is your truck still coming at 9 tomorrow? We have not heard back."
    msg = reply(
        body,
        None,
        mid="u1",
        sender="Ridgeline Dispatch <dispatch@ridgeline-trucking.example>",
        subject="Pickup appointment tomorrow?",
    )
    first, _ = world.run([msg], {}, DAY)
    again, _ = world.run([msg], {}, DAY + timedelta(minutes=15))
    assert (first.mail_unmatched, first.mail_read) == (1, 1)
    assert (again.mail_unmatched, again.mail_read) == (0, 0)
    with session_scope(world.sessions) as s:
        [item] = s.query(UnmatchedMail).all()
        assert (item.status, item.reason) == ("open", "subject:pickup-appointment")
        assert item.customer_key == "lidl"  # it went to lidl@, the Lidl file's group


def test_mail_that_is_not_about_booking_is_not_kept(world: World) -> None:
    msg = reply(
        "Tours for Thursday attached.",
        None,
        mid="u2",
        sender="Goods Out <goods.out@lidl.example>",
        subject="CIR Capacity 10/01, DD 10/02",
    )
    report, _ = world.run([msg], {}, DAY)
    assert report.mail_unmatched == 0
    with session_scope(world.sessions) as s:
        assert s.query(UnmatchedMail).count() == 0


def test_approve_after_a_booked_change_settles_it(world: World) -> None:
    _book(world)
    later = DAY + timedelta(hours=3)
    body = "Your pickup is now 10/1 @ 1000."
    world.run(
        [reply(body, world.sent_id, later, mid="b5")],
        {"Your pickup": confirmed("2026-10-01", "10:00", "10/1 @ 1000")},
        later,
    )
    assert world.case()["open"] == ["confirmation_review", "booked_slot_changed"]
    with session_scope(world.sessions) as s:
        case = s.get(BookingCase, world.case_id)
        assert case is not None
        approve(s, case, by="megan")
        assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
