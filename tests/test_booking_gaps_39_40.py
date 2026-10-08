"""Gaps 39 and 40 of the email edge-case review: the carrier, after the facility asked or booked.

39 a facility's question about the carrier or the driver, asked before either was on the load, is
   answered in the thread once Transport Pro shows them,
40 what the carrier must do once the pickup is booked (gate registration) is raised for a person
   to pass on, again for a new carrier, and goes on the load's note.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from facility_profiles.booking.automation import run_once
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import (
    PERSON_MAIL,
    BookingCase,
    BookingMessage,
    ExceptionType,
)
from facility_profiles.booking.respond import Responder, about_hauler, hauler_answer
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest, list_cases, mark_booked, scan
from facility_profiles.booking.steps import step_lines, told_steps
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.worklist import KINDS, open_kinds, resolve
from facility_profiles.booking.writeback import appointment_note, appointment_payload
from facility_profiles.booking.writer import FakeReplyWriter, ReplySituation, WrittenReply
from facility_profiles.config import Settings
from facility_profiles.domain.rules import to_tpro_write
from facility_profiles.domain.schema import (
    FacilityIdentity,
    FacilityProfile,
    FieldState,
    ProfileField,
    Role,
)
from facility_profiles.extraction.validate import coerce
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository
from tests.test_booking import NOW, lidl_load, reply, seed_vendor
from tests.test_booking_gaps_37_38 import BOOKED, CANCELED, LOAD, OTHER, Dispatched
from tests.test_booking_respond import _prepared_case
from tests.test_booking_situation_replies import DISPATCH, _written, tpro
from tests.test_booking_situation_replies import LOAD as ASKED_LOAD

SENDER = "Dana Pike <dpike@kochfoods.test>"
NO_DRIVER = {**DISPATCH, "assignedTo": {**DISPATCH["assignedTo"], "contacts": []}}
GATE = "48h before: Register the driver in the gate system at gate.example.test"


def _pilot(settings: Settings) -> Settings:
    return settings.model_copy(update={"pilot_terminal_ids": [1089]})


# ------------------------------------------------------------------ 39. answered once assigned


@pytest.mark.parametrize(
    ("question", "hauler"),
    [
        ("Who is the carrier?", True),
        ("Please send the carrier name and MC#", True),
        ("Driver name and cell?", True),
        ("Who will the driver be?", True),
        ("What is the trailer number?", True),
        ("When will the driver arrive?", False),
        ("What is the weight?", False),
    ],
)
def test_a_question_about_who_hauls_the_load_is_told_apart(question: str, hauler: bool) -> None:
    assert about_hauler(question) is hauler


def test_the_fixed_answer_says_only_what_transport_pro_holds() -> None:
    facts: dict[str, Any] = {
        "carrier": "Ridgeway Freight LLC",
        "carrier_mc": "765432",
        "carrier_dot": "3210987",
        "driver_name": "Sam Rivera",
        "driver_phone": "555-201-3344",
        "trailer_number": "R5320",
    }
    assert hauler_answer("Who is the carrier?", facts) == (
        "The carrier is Ridgeway Freight LLC (MC 765432, DOT 3210987)."
    )
    assert hauler_answer("Driver name and cell? Trailer #?", facts) == (
        "The carrier is Ridgeway Freight LLC (MC 765432, DOT 3210987). "
        "The driver is Sam Rivera, 555-201-3344. The trailer number is R5320."
    )
    assert hauler_answer("Truck number?", facts) is None  # not on the dispatch
    assert hauler_answer("Driver name?", {**facts, "driver_phone": None}) is None
    assert hauler_answer("Who is the carrier?", {**facts, "carrier": None}) is None


def _asked(settings: Settings, sessions, mailer: RecordingMailer, loads, writer=None) -> int:  # type: ignore[no-untyped-def]
    """A desk asks who the carrier is and the weight before any carrier is on the load."""
    case_id = _prepared_case(settings, sessions, mailer)
    asked = ("What is the weight?", "Who is the carrier and their MC?")
    reading = ReplyClassification(
        status=ReplyStatus.QUESTION, question=asked[0], questions=list(asked)
    )

    def script(situation: ReplySituation) -> WrittenReply:
        assert situation.facts["carrier_assigned"] == "not yet"
        return _written(
            "It is 40,000 lbs. Thank you!", (asked[0], True, ["weight"]), (asked[1], False, [])
        )

    responder = Responder(
        settings, mailer, writer=writer or FakeReplyWriter(script), now=NOW, facts=loads
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(" ".join(asked), mid="q1", sender=SENDER)],
            FakeReplyClassifier(lambda _ctx: reading),
            internal_domains=["circledelivers.com"],
            responder=responder,
            settings=settings,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["facility_question"]
    return case_id


def _later(settings, sessions, mailer, loads, case_id: int, writer=None) -> tuple[Any, list[str]]:  # type: ignore[no-untyped-def]
    responder = Responder(settings, mailer, writer=writer, now=NOW, facts=loads)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        message = responder.answer_later(session, case)
        return (message.body if message else None), open_kinds(case)


def test_the_carrier_question_is_answered_once_the_load_has_a_carrier(settings, sessions) -> None:
    settings = _pilot(settings)
    mailer, loads = RecordingMailer(), tpro(dispatched=False)
    case_id = _asked(settings, sessions, mailer, loads)
    drafted = len(mailer.drafts)
    # Still no carrier: nothing to say, and nothing is written.
    assert _later(settings, sessions, mailer, loads, case_id) == (None, ["facility_question"])
    assert len(mailer.drafts) == drafted

    loads.dispatches[ASKED_LOAD] = [DISPATCH]
    seen: list[ReplySituation] = []

    def script(situation: ReplySituation) -> WrittenReply:
        seen.append(situation)
        return _written(
            "Following up: the carrier is Ridgeway Freight LLC, MC 765432. Thank you!",
            (situation.questions[0], True, ["carrier", "carrier_mc"]),
        )

    body, kinds = _later(settings, sessions, mailer, loads, case_id, FakeReplyWriter(script))
    assert body is not None and body.startswith("Following up: the carrier is Ridgeway Freight")
    assert kinds == []
    assert seen[0].questions == ("Who is the carrier and their MC?",)
    assert seen[0].facts["carrier"] == "Ridgeway Freight LLC"
    draft = mailer.drafts[-1]
    assert draft.to_addr == SENDER and draft.in_reply_to == "<q1@vendor.test>"
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        event = next(e for e in reversed(case.events) if e.action == "answer_question")
        assert "the carrier is on the load now" in event.detail["reason"]
    # Settled: nothing more goes out.
    assert _later(settings, sessions, mailer, loads, case_id) == (None, [])


def test_a_waiting_question_is_tried_once_per_carrier_and_driver_seen(settings, sessions) -> None:
    settings = _pilot(settings)
    mailer, loads = RecordingMailer(), tpro(dispatched=False)
    case_id = _prepared_case(settings, sessions, mailer)
    asked = "Please send the driver name and cell."
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question=asked, questions=[asked])
    calls: list[dict[str, Any]] = []

    def script(situation: ReplySituation) -> WrittenReply:
        calls.append(situation.facts)
        known = situation.facts.get("driver_name") is not None
        body = "The driver is Sam Rivera, 555-201-3344. Thank you!" if known else "Thank you!"
        return _written(body, (asked, known, ["driver_name", "driver_phone"] if known else []))

    writer = FakeReplyWriter(script)
    responder = Responder(settings, mailer, writer=writer, now=NOW, facts=loads)
    with session_scope(sessions) as session:
        ingest(
            session,
            [reply(asked, mid="q2", sender=SENDER)],
            FakeReplyClassifier(lambda _ctx: reading),
            internal_domains=["circledelivers.com"],
            responder=responder,
            settings=settings,
        )
    drafted = len(mailer.drafts)
    # A carrier but no driver yet: the writer cannot answer, so no holding email goes out.
    loads.dispatches[ASKED_LOAD] = [NO_DRIVER]
    assert _later(settings, sessions, mailer, loads, case_id, writer) == (
        None,
        ["facility_question"],
    )
    tried = len(calls)
    # The same carrier and no driver: not tried again.
    assert _later(settings, sessions, mailer, loads, case_id, writer)[0] is None
    assert len(calls) == tried and len(mailer.drafts) == drafted
    # The driver is on the dispatch now: answered.
    loads.dispatches[ASKED_LOAD] = [DISPATCH]
    body, kinds = _later(settings, sessions, mailer, loads, case_id, writer)
    assert body is not None and "Sam Rivera, 555-201-3344" in body and kinds == []


def test_without_a_writer_the_fixed_words_answer_and_a_person_writing_stops_it(
    settings, sessions
) -> None:
    settings = _pilot(settings)
    mailer, loads = RecordingMailer(), tpro(dispatched=False)
    case_id = _asked(settings, sessions, mailer, loads)
    loads.dispatches[ASKED_LOAD] = [DISPATCH]
    body, kinds = _later(settings, sessions, mailer, loads, case_id)
    assert body is not None
    assert body.startswith("The carrier is Ridgeway Freight LLC (MC 765432, DOT 3210987). Thank")
    assert kinds == []

    # The same question on another pickup, and a person at Circle writes in the thread first.
    other, other_loads = RecordingMailer(), tpro(dispatched=False)
    with session_scope(sessions) as session:
        for case in list_cases(session):
            session.delete(case)
    case_id = _asked(settings, sessions, other, other_loads)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.messages.append(
            BookingMessage(
                direction="out",
                kind=PERSON_MAIL,
                body="Carrier info to follow.",
                sent_at=datetime.now(tz=UTC) + timedelta(minutes=1),
            )
        )
    other_loads.dispatches[ASKED_LOAD] = [DISPATCH]
    assert _later(settings, sessions, other, other_loads, case_id) == (None, ["facility_question"])


def test_each_pass_of_the_agent_answers_with_transport_pro_to_read(settings, sessions) -> None:
    settings = _pilot(settings)
    mailer, loads = RecordingMailer(), tpro(dispatched=False)
    case_id = _asked(settings, sessions, mailer, loads)
    loads.dispatches[ASKED_LOAD] = [DISPATCH]
    with session_scope(sessions) as session:
        report = run_once(session, settings, now=NOW, mailer=mailer, timers=False, facts=loads)
    assert report.carrier_answered == 1
    assert f"#{case_id} carrier question answered, drafted to {SENDER}" in report.lines
    assert "Ridgeway Freight LLC" in mailer.drafts[-1].body
    with session_scope(sessions) as session:  # no Transport Pro, no answer
        assert run_once(session, settings, now=NOW, mailer=mailer).carrier_answered == 0


# ------------------------------------------------------------------ 40. the carrier's steps


def test_steps_are_filed_one_per_line_with_the_hours_before_they_open() -> None:
    assert coerce("carrier_steps", GATE) == [
        {"step": "Register the driver in the gate system at gate.example.test", "hours_before": 48}
    ]
    assert coerce(
        "carrier_steps", "Bring load bars | 24 hours before - Send the trailer number"
    ) == [
        {"step": "Bring load bars", "hours_before": None},
        {"step": "Send the trailer number", "hours_before": 24},
    ]
    assert coerce("carrier_steps", '[{"step": "Call ahead", "hours_before": 2}]') == [
        {"step": "Call ahead", "hours_before": 2}
    ]
    assert coerce("carrier_steps", "x" * 201) is None
    assert coerce("carrier_steps", "\n".join(f"step {i}" for i in range(6))) is None
    slot = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
    assert step_lines([{"step": "Call ahead.", "hours_before": None}], slot) == ["Call ahead"]


def _steps(sessions, key: str) -> None:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as session:
        Repository(session).set_field_human(
            key,
            Role.SHIPPER,
            "carrier_steps",
            coerce("carrier_steps", GATE),
            state=FieldState.HUMAN_SET,
        )


def _booked_with_steps(settings: Settings, sessions) -> tuple[int, Dispatched]:  # type: ignore[no-untyped-def]
    _steps(sessions, seed_vendor(sessions))
    tpro_ = Dispatched(lidl_load(LOAD, po="226321092660"))
    scan(tpro_, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        mark_booked(session, case, by="megan", via="phone", local=BOOKED)
        return case.id, tpro_


def _todo(sessions, case_id: int) -> tuple[list[str], list[str]]:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        return open_kinds(case), [e.description for e in case.open_exceptions]


def _sweep(sessions, at: datetime) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as session:
        return sweep(session, now=at).counts()


def test_a_booked_pickup_raises_the_carriers_steps_and_again_for_a_new_carrier(
    settings, sessions
) -> None:
    settings = _pilot(settings)
    case_id, tpro_ = _booked_with_steps(settings, sessions)
    gate = "Register the driver in the gate system at gate.example.test (not before Tue 09/29 09:00 ET)"
    assert _sweep(sessions, NOW)["raised"] == {"carrier_steps": 1}
    assert _todo(sessions, case_id) == (["carrier_steps"], [f"Tell the carrier: {gate}"])
    assert _sweep(sessions, NOW)["raised"] == {}  # once per booked time
    # The scan sees a carrier: the open to-do names it.
    tpro_.dispatches = [DISPATCH]
    scan(tpro_, sessions, settings, days_ahead=7, now=NOW + timedelta(hours=1))  # type: ignore[arg-type]
    assert _sweep(sessions, NOW + timedelta(hours=1))["raised"] == {}
    assert _todo(sessions, case_id)[1] == [f"Tell Ridgeway Freight LLC: {gate}"]
    # Passed on, then the carrier is replaced: the new one is told.
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        resolve(
            session, case, [ExceptionType.CARRIER_STEPS], resolution="sent to dispatch", by="megan"
        )
    tpro_.dispatches = [CANCELED, {**DISPATCH, "status": "Canceled"}, OTHER]
    scan(tpro_, sessions, settings, days_ahead=7, now=NOW + timedelta(hours=2))  # type: ignore[arg-type]
    assert _sweep(sessions, NOW + timedelta(hours=2))["raised"] == {"carrier_steps": 1}
    assert _todo(sessions, case_id)[1] == [
        f"Harbor Road Inc replaced Ridgeway Freight LLC: tell Harbor Road Inc: {gate}"
    ]
    # The pickup time passes: it clears itself.
    swept = _sweep(sessions, datetime(2026, 10, 1, 13, 5, tzinfo=UTC))
    assert swept["resolved"] == {"carrier_steps": 1}
    assert _todo(sessions, case_id)[0] == []


def test_a_moved_pickup_is_told_its_new_time_and_the_note_carries_the_steps(
    settings, sessions
) -> None:
    settings = _pilot(settings)
    case_id, _ = _booked_with_steps(settings, sessions)
    _sweep(sessions, NOW)
    with session_scope(sessions) as session:  # the booked time changed on the case
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.confirmed_local = "2026-10-02 10:00"
    swept = _sweep(sessions, NOW)
    assert swept["resolved"] == {"carrier_steps": 1} and swept["raised"] == {"carrier_steps": 1}
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        done = [e.resolution for e in case.exceptions if e.resolved_at is not None]
        assert done == ["the pickup is now Fri 10/02 10:00 ET"]
    # Booked again by a person: booking settles the open one, and the sweep tells the new time.
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        mark_booked(session, case, by="megan", via="phone", local="2026-10-02 10:00")
    assert _sweep(sessions, NOW)["raised"] == {}  # the same time was told already
    kinds, said = _todo(sessions, case_id)
    assert kinds == ["carrier_steps"] and "(not before Wed 09/30 10:00 ET)" in said[0]
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert told_steps(case) == [
            "Register the driver in the gate system at gate.example.test "
            "(not before Wed 09/30 10:00 ET)"
        ]
        note = appointment_note(case, appointment_payload(case), was=None)
        assert "Carrier must: Register the driver in the gate system" in note
        # Canceled: nothing to pass on.
        case.status = "canceled"
    assert _sweep(sessions, NOW)["resolved"] == {"carrier_steps": 1}


def test_a_facility_without_steps_raises_nothing_and_the_label_reads_plainly(
    settings, sessions
) -> None:
    settings = _pilot(settings)
    seed_vendor(sessions)
    tpro_ = Dispatched(lidl_load(LOAD, po="226321092660"))
    scan(tpro_, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        mark_booked(session, list_cases(session)[0], by="megan", via="phone", local=BOOKED)
    assert _sweep(sessions, NOW)["raised"] == {}
    assert KINDS["carrier_steps"][0] == "Tell the carrier"
    profile = FacilityProfile(
        identity=FacilityIdentity(facility_id=1, candidate_key=None),
        role=Role.SHIPPER,
        fields={
            "carrier_steps": ProfileField(
                name="carrier_steps", value=[{"step": "Register at the gate", "hours_before": 48}]
            )
        },
    )
    assert to_tpro_write(profile).notes == "Carrier must: Register at the gate."
