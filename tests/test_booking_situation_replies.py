"""Replies written for the situation: the load's facts, the writer, and the check on its draft."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import pytest

from facility_profiles.booking.classify import (
    FakeReplyClassifier,
    validate_classification,
)
from facility_profiles.booking.facts import FACT_KEYS, load_facts
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.booking.writer import (
    FOLLOW_UP_LINE,
    HOLD_LINE,
    Answer,
    FakeReplyWriter,
    ReplySituation,
    WrittenReply,
    check_reply,
    render_situation,
)
from facility_profiles.extraction.llm import ExtractionError
from facility_profiles.storage.db import session_scope
from facility_profiles.tpro.models import Dispatch, Load
from tests.test_booking import NOW, lidl_load, reply
from tests.test_booking_respond import _prepared_case
from tests.test_booking_writeback import FakeLoads

LOAD = 7001
# An invented carrier and driver; the money and the insurance must never reach a reply.
DISPATCH: dict[str, Any] = {
    "id": 501,
    "status": "Dispatched",
    "dateCreated": "2026-09-30T14:00:00Z",
    "assignedTo": {
        "type": "brokerCarrier",
        "carrier": {
            "companyName": "Ridgeway Freight LLC",
            "mcNumber": "765432",
            "usDOT": "3210987",
            "phoneNumbers": [{"type": "DISPATCH", "value": "(555) 201-0100"}],
            "insurance": [{"type": "CARGO", "policyNumber": "POL-998877", "limit": 100000}],
        },
        "contacts": [
            {"type": "DRIVER", "name": "Sam Rivera", "phoneNumber": "5552013344"},
            {"type": "DISPATCHER", "name": "Pat Dispatch", "phoneNumber": "5552019999"},
        ],
        "tractorNumber": "T118",
        "trailerNumber": "R5320",
    },
}


def freight_load() -> dict[str, Any]:
    load = lidl_load(LOAD, po="226321092660")
    load["reference"].update(
        {
            "weight": 40000,
            "numberOfPieces": 22,
            "commodity": "Food-Dry Goods",
            "commodityDesc": "Canned Goods",
            "dimensions": {"length": "53.00"},
            "hazmat": False,
        }
    )
    load["postingInfo"] = {"loadBoardRate": 2200, "maxBuy": 2300}
    load["billingInfo"]["charges"] = {"totalFreight": 2650}
    return load


def tpro(*, dispatched: bool = True) -> FakeLoads:
    loads = FakeLoads(freight_load())
    if dispatched:
        loads.dispatches[LOAD] = [DISPATCH]
    return loads


# ------------------------------------------------------------------ the fact sheet


def test_the_load_facts_hold_the_freight_and_who_hauls_it_and_never_money() -> None:
    load = Load.model_validate(freight_load())
    facts = load_facts(load, [Dispatch.model_validate(DISPATCH)])
    assert facts["weight"] == "40,000 lbs" and facts["piece_count"] == "22"
    assert facts["equipment"] == "53 ft Reefer" and facts["hazmat"] == "no"
    assert facts["commodity"] == "Food-Dry Goods: Canned Goods"
    assert facts["delivery_notes"] == (
        "Please ensure driver has a load bar; DELIVERY# PYE_021026123"
    )
    assert (facts["carrier"], facts["carrier_mc"], facts["carrier_dot"]) == (
        "Ridgeway Freight LLC",
        "765432",
        "3210987",
    )
    assert facts["carrier_phone"] == "555-201-0100"
    assert (facts["driver_name"], facts["driver_phone"]) == ("Sam Rivera", "555-201-3344")
    assert (facts["truck_number"], facts["trailer_number"]) == ("T118", "R5320")
    assert set(facts) <= set(FACT_KEYS)
    text = json.dumps(facts)
    for never in ("2200", "2300", "2650", "POL-998877", "100000", "Pat Dispatch", "5552019999"):
        assert never not in text

    canceled = {**DISPATCH, "status": "Canceled"}
    facts = load_facts(load, [Dispatch.model_validate(canceled)])
    assert facts["carrier_assigned"] == "not yet" and facts.get("carrier") is None


# ------------------------------------------------------------------ the check on a draft

FACTS: dict[str, Any] = {
    "po_numbers": ["226321092660"],
    "load_id": 7001,
    "requested_pickup": "10/01 @ 0900",
    "delivery_date": "10/02",
    "delivery_ref": "PYE_021026123",
    "weight": "40,000 lbs",
    "piece_count": "22",
    "carrier": "Ridgeway Freight LLC",
    "carrier_mc": "765432",
    "driver_phone": "555-201-3344",
    "trailer_number": None,
}


def _situation(*questions: str, intent: str = "answer_question", **more: Any) -> ReplySituation:
    return ReplySituation(
        intent=intent,
        decision=more.pop("decision", "Answer their questions."),
        questions=questions,
        facts=more.pop("facts", FACTS),
        **more,
    )


def _written(body: str, *answers: tuple[str, bool, list[str]]) -> WrittenReply:
    return WrittenReply(
        body=body,
        answers=[Answer(question=q, answered=ok, facts_used=keys) for q, ok, keys in answers],
    )


@pytest.mark.parametrize(
    ("body", "problem"),
    [
        ("It is 40,000 lbs, 22 pieces. Thank you!", None),
        ("It is 40000 lbs. Thank you!", None),
        ("It is 41,000 lbs. Thank you!", "values not in the facts: 41,000"),
        ("It is 40,000 lbs; pickup is 10/03. Thank you!", "values not in the facts: 10/03"),
        ("It is 40,000 lbs; pickup at 9am. Thank you!", None),
        ("It is 40,000 lbs; pickup at 10am. Thank you!", "values not in the facts: 10am"),
        ("It is 40,000 lbs; the driver is at (555) 201-3344. Thank you!", None),
        ("It is 40,000 lbs; the driver is at 555-201-3345. Thank you!", "555-201-3345"),
        ("It is 40,000 lbs; write to loads@ridgeway.example. Thank you!", "loads@ridgeway"),
        ("It is 40,000 lbs and the rate stays the same. Thank you!", "money"),
        ("It is 40,000 lbs over 3 stops. Thank you!", "values not in the facts: 3"),
    ],
)
def test_every_value_in_a_draft_must_come_from_the_facts(body: str, problem: str | None) -> None:
    checked = check_reply(
        _written(body, ("What is the weight?", True, ["weight"])),
        _situation("What is the weight?"),
    )
    if problem is None:
        assert checked.ok, checked.problems
        assert checked.answered == ["What is the weight?"]
    else:
        assert not checked.ok and problem in "; ".join(checked.problems)


def test_a_draft_must_carry_the_decision_and_cite_known_facts() -> None:
    accept = _situation(
        intent="accept_offer",
        decision="Accept the pickup time they offered, 10/01 @ 1100: tell them it works.",
        must_include=("10/01", "1100"),
    )
    checked = check_reply(_written("Yes, 10/01 works. Thank you!"), accept)
    assert not checked.ok and "1100 is not in the draft" in checked.problems[0]
    assert check_reply(_written("Yes, 10/01 @ 1100 works. Thank you!"), accept).ok

    unknown = check_reply(
        _written("The trailer is R5320. Thank you!", ("Trailer?", True, ["trailer_number"])),
        _situation("Trailer?"),
    )
    assert not unknown.ok and "not known: trailer_number" in "; ".join(unknown.problems)
    invented = check_reply(
        _written("Sure. Thank you!", ("License?", True, ["driver_license"])),
        _situation("License?"),
    )
    assert not invented.ok and "do not exist: driver_license" in "; ".join(invented.problems)
    # Nothing answerable: the reply only holds, and the question waits for a person.
    holding = check_reply(
        _written("No trailer is assigned yet. Thank you!", ("Trailer?", False, [])),
        _situation("Trailer?"),
    )
    assert holding.ok and holding.unanswered == ["Trailer?"] and holding.answered == []
    assert holding.body.endswith(HOLD_LINE)


def test_what_a_draft_leaves_out_is_said_to_be_followed_up() -> None:
    checked = check_reply(
        _written(
            "It is 40,000 lbs. Thank you!",
            ("What is the weight?", True, ["weight"]),
            ("What is the trailer number?", False, []),
        ),
        _situation("What is the weight?", "What is the trailer number?"),
    )
    assert checked.ok
    assert checked.unanswered == ["What is the trailer number?"]
    assert checked.body.endswith(FOLLOW_UP_LINE)
    said = check_reply(
        _written(
            "It is 40,000 lbs. I will get back to you on the trailer. Thank you!",
            ("What is the weight?", True, ["weight"]),
            ("What is the trailer number?", False, []),
        ),
        _situation("What is the weight?", "What is the trailer number?"),
    )
    assert FOLLOW_UP_LINE not in said.body


def test_the_writer_is_told_the_facts_and_never_the_money() -> None:
    load = Load.model_validate(freight_load())
    facts = {**FACTS, **load_facts(load, [Dispatch.model_validate(DISPATCH)])}
    text = render_situation(_situation("What is the weight?", facts=facts))
    assert "Ridgeway Freight LLC" in text and "40,000 lbs" in text
    for never in ("2200", "2300", "2650", "POL-998877"):
        assert never not in text


# ------------------------------------------------------------------ the responder end to end


@dataclass
class View:
    """What the pass left on the case, read inside its session."""

    open: list[str]
    said: list[str]
    events: list[tuple[str, dict[str, Any]]]

    def event(self, action: str) -> dict[str, Any]:
        return next(detail for name, detail in reversed(self.events) if name == action)


def _run(
    settings,  # type: ignore[no-untyped-def]
    sessions,  # type: ignore[no-untyped-def]
    reading: ReplyClassification,
    writer: FakeReplyWriter,
    *,
    text: str,
    facts: FakeLoads | None = None,
) -> tuple[RecordingMailer, View, int]:
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    drafted = len(mailer.drafts)
    responder = Responder(settings, mailer, writer=writer, now=NOW, facts=facts or tpro())
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(text, mid="s1")],
            FakeReplyClassifier(lambda _ctx: reading),
            internal_domains=["circledelivers.com"],
            responder=responder,
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None
        view = View(
            open=open_kinds(case),
            said=[e.description for e in case.open_exceptions],
            events=[(e.action, dict(e.detail or {})) for e in case.events],
        )
    return mailer, view, drafted


QUESTIONS = (
    "What is the weight?",
    "Who is the carrier and their MC?",
    "Please send the driver name and cell.",
)


def test_one_reply_answers_every_question_from_the_load(settings, sessions) -> None:
    text = (
        "What is the weight? Who is the carrier and their MC? Please send the driver name and cell."
    )
    reading = ReplyClassification(
        status=ReplyStatus.QUESTION, question=QUESTIONS[0], questions=list(QUESTIONS)
    )

    def script(situation: ReplySituation) -> WrittenReply:
        return _written(
            "It is 40,000 lbs. The carrier is Ridgeway Freight LLC, MC 765432. "
            "The driver is Sam Rivera, 555-201-3344. Thank you!",
            (QUESTIONS[0], True, ["weight"]),
            (QUESTIONS[1], True, ["carrier", "carrier_mc"]),
            (QUESTIONS[2], True, ["driver_name", "driver_phone"]),
        )

    writer = FakeReplyWriter(script)
    mailer, view, drafted = _run(settings, sessions, reading, writer, text=text)
    assert len(mailer.drafts) == drafted + 1
    body = mailer.drafts[-1].body
    assert body.startswith("It is 40,000 lbs. The carrier is Ridgeway Freight LLC, MC 765432.")
    assert view.open == []
    event = view.event("answer_question")
    assert event["written"] is True and event["unanswered"] == []
    situation = writer.calls[0]
    assert situation.questions == QUESTIONS and situation.facts["weight"] == "40,000 lbs"


def test_before_a_driver_is_assigned_it_answers_what_it_can_and_raises_the_rest(
    settings, sessions
) -> None:
    asked = ("What is the weight?", "Please send the driver name and cell.")
    reading = ReplyClassification(
        status=ReplyStatus.QUESTION, question=asked[0], questions=list(asked)
    )

    def script(situation: ReplySituation) -> WrittenReply:
        assert situation.facts["carrier_assigned"] == "not yet"
        assert situation.facts["driver_phone"] is None
        return _written(
            "It is 40,000 lbs. Thank you!",
            (asked[0], True, ["weight"]),
            (asked[1], False, []),
        )

    mailer, view, drafted = _run(
        settings,
        sessions,
        reading,
        FakeReplyWriter(script),
        text=" ".join(asked),
        facts=tpro(dispatched=False),
    )
    assert len(mailer.drafts) == drafted + 1
    assert FOLLOW_UP_LINE in mailer.drafts[-1].body
    assert view.open == ["facility_question"]
    said = view.said[0]
    assert "driver name and cell" in said and "someone will get back to them" in said


def test_a_confirmation_that_asks_something_is_thanked_and_answered_in_one_reply(
    settings, sessions
) -> None:
    text = "Confirmed for 10/1 @ 0900. What is the trailer number?"
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="09:00",
        quotes=["Confirmed for 10/1 @ 0900"],
        questions=["What is the trailer number?"],
    )

    def script(situation: ReplySituation) -> WrittenReply:
        assert situation.intent == "acknowledge"
        return _written(
            "Thank you! The trailer is R5320.",
            ("What is the trailer number?", True, ["trailer_number"]),
        )

    mailer, view, drafted = _run(settings, sessions, reading, FakeReplyWriter(script), text=text)
    assert [d.body.split("\n\n")[0] for d in mailer.drafts[drafted:]] == [
        "Thank you! The trailer is R5320."
    ]
    assert view.open == ["confirmation_review"]


def test_a_confirmation_held_for_a_person_raises_its_question_instead(settings, sessions) -> None:
    text = "We can do 10/1 @ 1500. Is it a reefer?"
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="15:00",
        quotes=["10/1 @ 1500"],
        questions=["Is it a reefer?"],
    )
    writer = FakeReplyWriter(lambda s: _written("unused"))
    mailer, view, drafted = _run(settings, sessions, reading, writer, text=text)
    assert len(mailer.drafts) == drafted and writer.calls == []  # not thanked: a different time
    assert "facility_question" in view.open
    assert any("Is it a reefer?" in said for said in view.said)


def test_a_draft_that_fails_its_check_sends_the_fixed_wording(settings, sessions) -> None:
    text = "I can do 10/1 at 1100 instead. Is it a reefer?"
    reading = ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date="2026-10-01",
        pickup_time="11:00",
        quotes=["10/1 at 1100"],
        questions=["Is it a reefer?"],
    )

    def script(situation: ReplySituation) -> WrittenReply:
        assert situation.must_include == ("10/01", "1100")
        return _written(  # the wrong day: never sent
            "Yes, 10/03 @ 1100 works. It is a 53 ft Reefer. Thank you!",
            ("Is it a reefer?", True, ["equipment"]),
        )

    mailer, view, _drafted = _run(settings, sessions, reading, FakeReplyWriter(script), text=text)
    assert mailer.drafts[-1].body.startswith("Yes, 10/01 @ 1100 works. Thank you!")
    assert "facility_question" in view.open  # the reefer question waits for a person
    event = view.event("accept_offer")
    assert "fixed wording" in event["reason"] and event["written"] is False


def test_a_writer_that_fails_hands_an_unanswerable_question_to_a_person(settings, sessions) -> None:
    def broken(situation: ReplySituation) -> WrittenReply:
        raise ExtractionError("OpenRouter returned empty content")

    reading = ReplyClassification(status=ReplyStatus.QUESTION, question="Is it a reefer?")
    mailer, view, drafted = _run(
        settings, sessions, reading, FakeReplyWriter(broken), text="Is it a reefer?"
    )
    assert len(mailer.drafts) == drafted
    assert view.open == ["facility_question"]
    assert "the writer failed" in view.said[0]


def test_money_in_their_email_never_reaches_the_writer(settings, sessions) -> None:
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question="Who pays the lumper?")
    writer = FakeReplyWriter(lambda s: _written("unused"))
    mailer, _view, drafted = _run(settings, sessions, reading, writer, text="Who pays the lumper?")
    assert writer.calls == [] and len(mailer.drafts) == drafted


def test_a_load_that_cannot_be_read_leaves_the_case_facts(settings, sessions) -> None:
    class Down(FakeLoads):
        def get_load(self, load_id: int) -> Load:
            raise OSError("Transport Pro is down")

    reading = ReplyClassification(status=ReplyStatus.QUESTION, question="Both orders?")

    def script(situation: ReplySituation) -> WrittenReply:
        assert situation.facts["weight"] is None and situation.facts["po_numbers"]
        return _written("Just PO# 226321092660. Thank you!", ("Both orders?", True, ["po_numbers"]))

    mailer, _view, _drafted = _run(
        settings,
        sessions,
        reading,
        FakeReplyWriter(script),
        text="Both orders?",
        facts=Down(freight_load()),
    )
    assert mailer.drafts[-1].body.startswith("Just PO# 226321092660. Thank you!")


# ------------------------------------------------------------------ reading every question


def test_only_questions_from_their_own_words_are_kept() -> None:
    result = ReplyClassification(
        status=ReplyStatus.QUESTION,
        question="What is the trailer number?",
        questions=["What is the trailer number?", "Did this get resolved?"],
    )
    kept, issues = validate_classification(
        result, "Morning. What is the trailer number?", quoted="Did this get resolved?"
    )
    assert kept.questions == ["What is the trailer number?"]
    assert any(i.field_name == "questions" for i in issues)


def test_a_holding_draft_never_replaces_a_fixed_answer(settings, sessions) -> None:
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question="Both orders?")

    def script(situation: ReplySituation) -> WrittenReply:
        return _written("Let me check. Thank you!", ("Both orders?", False, []))

    mailer, view, _ = _run(
        settings, sessions, reading, FakeReplyWriter(script), text="Both orders?"
    )
    assert mailer.drafts[-1].body.startswith("Just PO# 226321092660. Thank you!")
    assert view.open == []
