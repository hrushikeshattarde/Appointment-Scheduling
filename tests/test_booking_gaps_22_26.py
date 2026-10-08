"""Gaps 22 to 26 of the email edge-case review: what a person did, and one safe answer per email.

22 a person's own email to the facility settles the to-dos it answered,
23 one email about several batched pickups gets one answer back,
24 an answer the send gate stopped gets a to-do of its own that points at the draft,
25 an offer the agent accepted is booked on its own only on the checks a confirmation gets,
26 a booked pickup the facility can no longer ship on its day tells the customer's desk.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import InboundMessage
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import Responder, joint_body
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest
from facility_profiles.booking.worklist import KINDS, open_kinds
from facility_profiles.storage.db import session_scope
from tests.test_booking_autonomy import (  # noqa: F401 - the fixture resets the customer files
    AUTO,
    CONFIRMED,
    CONFIRMS,
    INTERNAL,
    PO,
    REPLY_AT,
    _case,
    _forget_customer_files,
    _ingest,
    _reply,
    _sent_request,
    _settings,
)
from tests.test_booking_multi import _batched
from tests.test_booking_multi import _reply as batched_reply

OFFER = ReplyClassification(
    status=ReplyStatus.COUNTER_OFFER,
    pickup_date="2026-10-01",
    pickup_time="11:00",
    quotes=["10/1/26 11:00 works better for us"],
    confidence=0.9,
)


def _person(
    *,
    to: str,
    sent_at: datetime,
    in_reply_to: str,
    mid: str,
    body: str = "We will have a 53 ft reefer there.",
) -> InboundMessage:
    """An email someone at Circle sent with the Lidl group on copy."""
    return InboundMessage(
        message_id=mid,
        thread_id=None,
        sent_at=sent_at,
        from_addr="Jordan Lake <jordan.lake@circledelivers.com>",
        to_addr=to,
        cc_addr="Lidl Group <lidl@circledelivers.com>",
        subject=f"Re: Pick Up Appointment: {PO}",
        body=body,
        in_reply_to=in_reply_to,
        rfc_message_id=f"<{mid}@circledelivers.com>",
        references=in_reply_to,
    )


def _read(sessions, settings, message: InboundMessage) -> int:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as s:
        stats = ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: CONFIRMED),
            internal_domains=INTERNAL,
            settings=settings,
        )
    return stats.by_person


# ------------------------------------------------------------------ 22. a person answered


def test_a_persons_email_to_the_facility_settles_what_they_answered(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    asked = "Are you able to bring a 53 ft reefer?"
    question = ReplyClassification(
        status=ReplyStatus.QUESTION, question=asked, quotes=[asked], confidence=0.9
    )
    _ingest(sessions, settings, _reply(asked, sent_id), question, sender)
    assert _case(sessions, case_id)["open"] == ["facility_question"]
    their = "<r1@udfinc.example>"
    # Written before their question, or only to the customer's desk: it answered nothing.
    early = _person(
        to="desk.staff@udfinc.com",
        sent_at=REPLY_AT - timedelta(hours=1),
        in_reply_to=sent_id,
        mid="p0",
    )
    desk = _person(
        to="inbound@lidl.us", sent_at=REPLY_AT + timedelta(hours=1), in_reply_to=their, mid="p1"
    )
    assert _read(sessions, settings, early) == 1 and _read(sessions, settings, desk) == 1
    assert _case(sessions, case_id)["open"] == ["facility_question"]
    # Their answer to the facility, after its question: settled.
    answer = _person(
        to="Desk Staff <desk.staff@udfinc.com>",
        sent_at=REPLY_AT + timedelta(hours=2),
        in_reply_to=their,
        mid="p2",
    )
    assert _read(sessions, settings, answer) == 1
    view = _case(sessions, case_id)
    assert view["open"] == []
    assert ("answered_by_person", "jordan.lake@circledelivers.com") in view["events"]
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        done = next(e for e in case.exceptions if e.kind == "facility_question")
        assert done.resolution == "jordan.lake@circledelivers.com answered the facility by email"
        assert done.resolved_by == "jordan.lake@circledelivers.com"


# ------------------------------------------------------------------ 23. one answer per email


def test_one_reply_about_two_batched_pickups_gets_one_answer(settings, sessions) -> None:
    settings, sender, (a_id, b_id), sent_id = _batched(settings, sessions)
    asked = "What are your load numbers?"
    reading = ReplyClassification(
        status=ReplyStatus.QUESTION, question=asked, quotes=[asked], confidence=0.9
    )
    at = datetime(2026, 9, 24, 12, 40, tzinfo=UTC)
    with session_scope(sessions) as session:
        stats = ingest(
            session,
            [batched_reply(f"{asked}\n\nThanks", sent_id)],
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=INTERNAL,
            responder=Responder(settings, sender, now=at),
        )
        a, b = session.get(BookingCase, a_id), session.get(BookingCase, b_id)
        assert a is not None and b is not None
        answers = [[m for m in c.messages if m.kind == "answer_question"] for c in (a, b)]
        assert [len(x) for x in answers] == [1, 1]
        assert answers[0][0].body == answers[1][0].body
        together = [e.detail.get("answered_with") for c in (a, b) for e in c.events]
        assert [b_id] in together and [a_id] in together
        assert open_kinds(a) == [] and open_kinds(b) == []
    assert len(sender.drafts) == 2  # the batched request, and one answer
    assert sender.drafts[-1].body.startswith(
        "PO# 115829092660 & 115829092661: Our load number is 8001.\n\n"
        "PO# 115802102660 & 115802102661: Our load number is 8002.\n\nThank you!"
    )
    assert sender.drafts[-1].in_reply_to == "<vera-1@outlook.com>"
    assert stats.responded == 2


def test_the_same_answer_for_every_pickup_is_written_once() -> None:
    a = BookingCase(id=1, load_id=1, po_numbers=["1"])
    b = BookingCase(id=2, load_id=2, po_numbers=["2"])
    from facility_profiles.booking.respond import ResponseIntent, ResponsePlan

    plan = ResponsePlan(
        ResponseIntent.ANSWER_QUESTION, "r", body="This is a Lidl order. Thank you!"
    )
    assert joint_body([(a, None, plan), (b, None, plan)]) == "This is a Lidl order. Thank you!"


# ------------------------------------------------------------------ 24. a refused answer


def test_an_answer_the_gate_stops_gets_a_to_do_pointing_at_its_draft(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        case.contact_email = "someone@elsewhere.example"  # not the desk the profile trusts
    question = ReplyClassification(
        status=ReplyStatus.QUESTION,
        question="What is the delivery number?",
        quotes=["What is the delivery number?"],
        confidence=0.9,
    )
    _, mailer = _ingest(
        sessions,
        settings,
        _reply("What is the delivery number?", sent_id, sender="Ann <ann@elsewhere.example>"),
        question,
        sender,
    )
    assert len(mailer.drafts) == 1
    view = _case(sessions, case_id)
    # The question the draft answers is settled; the draft itself is what waits.
    assert view["open"] == ["draft_not_sent"]
    said = next(d for k, d in view["exceptions"] if k == "draft_not_sent")
    assert said.startswith("the agent's answer to someone@elsewhere.example was not sent (")
    assert said.endswith("it is in the drafts")
    assert KINDS["draft_not_sent"][0] == "Send the agent's draft"
    # Someone sends an answer to the facility themselves: done.
    answer = _person(
        to="ann@elsewhere.example",
        sent_at=REPLY_AT + timedelta(hours=1),
        in_reply_to="<r1@udfinc.example>",
        mid="p3",
        body="PYE_021026123",
    )
    assert _read(sessions, settings, answer) == 1
    assert _case(sessions, case_id)["open"] == []


# ------------------------------------------------------------------ 25. an offer accepted


def test_an_offer_tied_only_by_its_sender_is_accepted_but_not_booked_on_its_own(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, _, sender = _sent_request(settings, sessions)
    # No thread, no Message-ID, no PO: tied to the pickup by who sent it only.
    _ingest(
        sessions,
        settings,
        _reply("Can't do 9. 10/1/26 11:00 works better for us", None),
        OFFER,
        sender,
    )
    assert sender.drafts[-1].body.startswith("Yes, 10/01 @ 1100 works.")
    view = _case(sessions, case_id)
    assert view["status"] == CaseStatus.PENDING.value
    assert ("auto_confirm_held", "agent") in view["events"]
    review = next(d for k, d in view["exceptions"] if k == "confirmation_review")
    assert (
        "not booked automatically: the reply was tied to this pickup by its sender only" in review
    )


def test_an_offer_whose_words_do_not_back_its_time_is_not_booked_on_its_own(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    unbacked = OFFER.model_copy(update={"pickup_number": "CCI-7777"})  # in none of its words
    _ingest(
        sessions,
        settings,
        _reply("Can't do 9. 10/1/26 11:00 works better for us", sent_id),
        unbacked,
        sender,
    )
    assert sender.drafts[-1].body.startswith("Yes, 10/01 @ 1100 works.")
    view = _case(sessions, case_id)
    assert view["status"] == CaseStatus.PENDING.value
    review = next(d for k, d in view["exceptions"] if k == "confirmation_review")
    assert "do not back every date, time or number" in review


# ------------------------------------------------------------------ 26. a booked pickup declined


def _booked(settings, sessions):  # type: ignore[no-untyped-def]
    case_id, sent_id, sender = _sent_request(settings, sessions)
    _ingest(sessions, settings, _reply(CONFIRMS, sent_id), CONFIRMED, sender)
    assert _case(sessions, case_id)["status"] == CaseStatus.SCHEDULED.value
    return case_id, sent_id, sender


def test_a_booked_pickup_the_facility_cannot_ship_tells_the_customer_desk(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _booked(settings, sessions)
    to_facility = len([d for d in sender.drafts if "udfinc.com" in d.to_addr])
    words = "This PO is not ready, we cannot ship it this week."
    no = ReplyClassification(
        status=ReplyStatus.REJECTED,
        reject_reason=RejectReason.NOT_READY,
        question=words,
        quotes=[words],
        confidence=0.9,
    )
    _ingest(sessions, settings, _reply(words, sent_id, mid="r2"), no, sender)
    note = sender.drafts[-1]
    assert note.to_addr == "inbound@lidl.us" and note.subject == f"RESCHEDULE {PO}"
    assert words in note.body
    # The facility hears nothing back, and the booking waits for a person.
    assert len([d for d in sender.drafts if "udfinc.com" in d.to_addr]) == to_facility
    view = _case(sessions, case_id)
    assert "booked_slot_changed" in view["open"]
    changed = next(d for k, d in view["exceptions"] if k == "booked_slot_changed")
    assert "note to the customer desk sent" in changed


def test_a_booked_pickup_declined_for_another_reason_sends_no_note(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _booked(settings, sessions)
    before = len(sender.drafts)
    words = "We do not have this PO in our system."
    no = ReplyClassification(
        status=ReplyStatus.REJECTED,
        reject_reason=RejectReason.PO_NOT_FOUND,
        question=words,
        quotes=[words],
        confidence=0.9,
    )
    _ingest(sessions, settings, _reply(words, sent_id, mid="r3"), no, sender)
    assert len(sender.drafts) == before
    assert "booked_slot_changed" in _case(sessions, case_id)["open"]
