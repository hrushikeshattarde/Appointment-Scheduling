"""Status and exception split: what needs a person is an exception, the status stays truthful."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import text
from typer.testing import CliRunner

from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    approve,
    draft_case,
    ingest,
    list_cases,
    mark_booked,
    mark_sent,
    scan,
    summary_line,
)
from facility_profiles.booking.worklist import (
    annotate,
    flag,
    legacy_plan,
    migrate_legacy_statuses,
    open_kinds,
    resolve,
    resolve_all,
)
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.storage.db import (
    ensure_columns,
    init_db,
    make_engine,
    session_factory,
    session_scope,
)
from facility_profiles.storage.repository import Repository, as_utc
from tests.conftest import FakeTPro
from tests.test_booking import NOW, koch_key, lidl_load, reply, seed_vendor

INTERNAL = ["circledelivers.com"]


def _sent_case(settings, sessions, *, po: str = "226321092660") -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(7301, po=po)], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)
        return case.id


def test_one_open_exception_per_kind_and_resolutions_say_who_and_why(session):
    case = BookingCase(load_id=1, waypoint_index=0, po_numbers=["1"])
    session.add(case)
    session.flush()
    first = flag(session, case, ExceptionType.FACILITY_QUESTION, "vendor asked: which carrier?")
    again = flag(session, case, ExceptionType.FACILITY_QUESTION, "vendor asked: both orders?", n=2)
    assert again is first and first.description == "vendor asked: both orders?"
    assert first.detail == {"n": 2} and first.raised_by == "agent"
    flag(session, case, ExceptionType.SLOT_UNWORKABLE, "slot passed", actor="megan")
    assert open_kinds(case) == ["facility_question", "slot_unworkable"]

    noted = annotate(session, case, "no rule answers")
    assert noted is not None and noted.kind == "slot_unworkable"
    assert noted.description == "slot passed | agent: no rule answers"

    assert resolve(session, case, [ExceptionType.MISSING_METHOD], resolution="x") == []
    done = resolve(session, case, [ExceptionType.FACILITY_QUESTION], resolution="told", by="megan")
    assert done == ["facility_question"] and first.resolved_by == "megan"
    assert first.resolution == "told" and first.resolved_at is not None
    assert resolve_all(session, case, resolution="closed") == ["slot_unworkable"]
    assert open_kinds(case) == [] and annotate(session, case, "late") is None
    # A kind can be raised again once the earlier one is resolved.
    flag(session, case, ExceptionType.FACILITY_QUESTION, "vendor asked: dock hours?")
    assert open_kinds(case) == ["facility_question"] and len(case.exceptions) == 3


def test_a_portal_vendor_is_not_a_missing_desk(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    key = seed_vendor(sessions, with_email=False)
    with session_scope(sessions) as session:
        Repository(session).set_field_human(
            key, Role.SHIPPER, "booking_method", "web_portal", state=FieldState.HUMAN_SET
        )
    stats = scan(
        FakeTPro([lidl_load(7302, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    assert stats.needs_profile == 1
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.UNSCHEDULED.value
        assert open_kinds(case) == ["method_not_supported"]
        portal = case.open_exceptions[0]
        assert portal.description == "books on a web portal; the agent only books by email"
        assert portal.detail == {"method": "web_portal"}
        assert case.events[0].detail["reason"] == portal.description


def test_booked_another_way_is_scheduled_and_clears_what_was_open(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions, with_email=False)
    scan(
        FakeTPro([lidl_load(7303, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert open_kinds(case) == ["missing_method"]
        mark_booked(
            session,
            case,
            by="megan",
            via="phone",
            local="2026-10-01 10:30",
            pickup_number="CCI-1",
            note="called the CCI desk",
        )
        assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
        assert case.reason == "booked by phone: called the CCI desk"
        assert case.exceptions[0].resolution == "booked by phone"
        assert as_utc(case.confirmed_start_utc) == datetime(2026, 10, 1, 14, 30, tzinfo=UTC)
        assert case.pickup_number == "CCI-1"
        booked, remembered = case.events[-2:]
        assert (booked.action, booked.actor) == ("marked_booked", "megan")
        assert (remembered.action, remembered.detail["method"]) == ("desk_remembered", "phone")
        # Scheduled is decided: no draft, and a cancellation is refused the other way round.
        with pytest.raises(ValueError, match="only unscheduled"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)
        case.status = CaseStatus.CANCELED.value
        with pytest.raises(ValueError, match="canceled"):
            mark_booked(session, case, by="megan", via="phone")


def test_follow_up_waits_while_something_is_open_for_a_person(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_follow_up_hours": 24}
    )
    case_id = _sent_case(settings, sessions)
    mailer = RecordingMailer()
    later = Responder(settings, mailer, now=NOW + timedelta(hours=30))
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        flag(session, case, ExceptionType.FACILITY_QUESTION, "vendor asked: by phone, which dock?")
        assert later.follow_up(session, case) is None
        resolve(session, case, [ExceptionType.FACILITY_QUESTION], resolution="called back")
        message = later.follow_up(session, case)
        assert message is not None and message.kind == "follow_up"


def test_a_question_after_a_confirmation_leaves_it_waiting_for_approval(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    case_id = _sent_case(settings, sessions)

    def script(ctx: ReplyContext) -> ReplyClassification:
        if "?" in ctx.body:
            return ReplyClassification(status=ReplyStatus.QUESTION, question=ctx.body.strip())
        return ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_time="11:00",
            pickup_number="CCI-9389",
            quotes=["10/1 11:00 CCI-9389"],
        )

    classifier = FakeReplyClassifier(script)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        ingest(
            session, [reply("10/1 11:00 CCI-9389", mid="c1")], classifier, internal_domains=INTERNAL
        )
        assert open_kinds(case) == ["confirmation_review"]
        ingest(
            session,
            [reply("Which door will the driver use?", mid="q1")],
            classifier,
            internal_domains=INTERNAL,
        )
        # A question is not about the slot: the confirmation stays, the question joins it.
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["confirmation_review", "facility_question"]
        assert case.confirmed_local == "2026-10-01 11:00"
        approve(session, case, by="megan")
        assert case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == ["facility_question"]


def test_a_moved_delivery_before_any_request_keeps_a_missing_desk_open(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions, with_email=False)
    scan(
        FakeTPro([lidl_load(7304, po="104419082630")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    lidl = InboundMessage(
        message_id="z1",
        thread_id="tz",
        sent_at=datetime(2026, 9, 30, 19, 5, tzinfo=UTC),
        from_addr="inbound@lidl.us",
        to_addr="megan.goodwin@circledelivers.com",
        cc_addr="",
        subject="104419082630 NO COVERAGE",
        body="Here is an updated appointment! 10/6 7AM - GRM_061026926.",
    )
    classifier = FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED))
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        stats = ingest(
            session,
            [lidl],
            classifier,
            internal_domains=INTERNAL,
            responder=Responder(settings, RecordingMailer(), now=NOW),
            customer_desk="inbound@lidl.us",
        )
        assert stats.delivery_updates == 1
        # Nothing went out, so the next request just asks for the new day; the desk is still
        # missing, so the case is still not draftable.
        assert case.status == CaseStatus.UNSCHEDULED.value
        assert case.requested_local == "2026-10-02 09:00"
        assert open_kinds(case) == ["missing_method"]


def test_list_filters_by_open_exception_and_flags_drafts(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    loads = [lidl_load(7305, po="226321092660"), lidl_load(7306, po="226321092661")]
    scan(FakeTPro(loads, {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        drafted, flagged = sorted(list_cases(session), key=lambda c: c.load_id)
        draft_case(session, drafted, RecordingMailer(), settings, now=NOW)
        flag(session, flagged, ExceptionType.SLOT_UNWORKABLE, "requested slot has already passed")
        assert [c.id for c in list_cases(session, exception="any")] == [flagged.id]
        assert list_cases(session, exception="slot_unworkable")[0].id == flagged.id
        assert list_cases(session, exception="facility_question") == []
        assert "draft ready" in summary_line(drafted)
        line = summary_line(flagged)
        assert "!slot_unworkable" in line and "(requested slot has already passed)" in line


def _legacy(session, status: str, **fields) -> BookingCase:  # type: ignore[no-untyped-def]
    case = BookingCase(
        load_id=fields.pop("load_id"),
        waypoint_index=0,
        po_numbers=["115802102660"],
        status=status,
        updated_at=datetime(2026, 9, 29, 10, 0, tzinfo=UTC),
        **fields,
    )
    session.add(case)
    session.flush()
    return case


def test_cases_on_the_old_statuses_are_moved_once(session):
    old = datetime(2026, 9, 29, 10, 0, tzinfo=UTC)
    new = _legacy(session, "new", load_id=1)
    drafted = _legacy(session, "drafted", load_id=2)
    drafted.messages.append(BookingMessage(direction="out", kind="request"))
    deferred = _legacy(
        session, "sent", load_id=3, reason="vendor asked to check back on 2026-10-05"
    )
    no_desk = _legacy(
        session, "needs_profile", load_id=4, reason="no verified email booking desk on the profile"
    )
    portal = _legacy(session, "needs_profile", load_id=5, booking_method="web_portal")
    booked = _legacy(session, "already_booked", load_id=6, reason="load already carries 20463798")
    confirmed = _legacy(
        session, "proposed", load_id=7, confirmed_local="2026-10-05 09:00", pickup_number="20463798"
    )
    accepted = _legacy(session, "proposed", load_id=8, confirmed_local="2026-10-02 09:00")
    accepted.events.append(BookingEvent(action="counter_offer"))
    accepted.events.append(BookingEvent(action="accept_offer"))
    approved = _legacy(session, "approved", load_id=9)
    closed_booked = _legacy(
        session,
        "closed",
        load_id=10,
        reason="already booked: vendor pickup number 20463798 is on the load",
    )
    closed = _legacy(session, "closed", load_id=11, reason="load canceled by Lidl")
    question = _legacy(session, "needs_human", load_id=12, reason="vendor asked: which carrier?")
    question.messages.append(BookingMessage(direction="out", kind="request", sent_at=old))
    question.events.append(BookingEvent(action="question"))
    question.events.append(BookingEvent(action="handoff", detail={"reason": "no rule answers"}))
    declined = _legacy(
        session, "needs_human", load_id=13, reason="vendor cannot ship as planned; customer must"
    )
    declined.events.append(BookingEvent(action="rejected_by_vendor"))
    declined.events.append(BookingEvent(action="escalate_to_customer"))
    stale = _legacy(session, "needs_human", load_id=14, reason="requested slot has already passed")
    stale.events.append(BookingEvent(action="scanned"))
    stale.events.append(BookingEvent(action="stale_slot"))
    floored = _legacy(session, "needs_human", load_id=15, reason="moved; misses the delivery")
    floored.events.append(BookingEvent(action="po_date_floor", detail={"feasible": False}))
    unknown = _legacy(session, "needs_human", load_id=16, reason="look at this")
    assert legacy_plan(unknown).exception == (ExceptionType.HANDOFF, "look at this")
    # A floor that still made the delivery only moved the ask; the question before it explains.
    moved_ask = _legacy(session, "needs_human", load_id=17, reason="vendor asked: dock hours?")
    moved_ask.events.append(BookingEvent(action="question"))
    moved_ask.events.append(BookingEvent(action="po_date_floor", detail={"feasible": True}))
    assert legacy_plan(moved_ask).exception == (
        ExceptionType.FACILITY_QUESTION,
        "vendor asked: dock hours?",
    )

    assert migrate_legacy_statuses(session) == 17
    expected = {
        new: ("unscheduled", None, []),
        drafted: ("unscheduled", None, []),
        deferred: ("pending", "vendor asked to check back on 2026-10-05", []),
        no_desk: ("unscheduled", None, ["missing_method"]),
        portal: ("unscheduled", None, ["method_not_supported"]),
        booked: ("scheduled", "load already carries 20463798", []),
        confirmed: ("pending", None, ["confirmation_review"]),
        accepted: ("pending", None, ["confirmation_review"]),
        approved: ("scheduled", None, []),
        closed_booked: (
            "scheduled",
            "already booked: vendor pickup number 20463798 is on the load",
            [],
        ),
        closed: ("canceled", "load canceled by Lidl", []),
        question: ("pending", None, ["facility_question"]),
        declined: (
            "declined",
            "vendor cannot ship as planned; customer must",
            ["facility_declined"],
        ),
        stale: ("unscheduled", None, ["slot_unworkable"]),
        floored: ("unscheduled", None, ["slot_unworkable"]),
        unknown: ("unscheduled", None, ["handoff"]),
    }
    for case, (status, reason, kinds) in expected.items():
        assert (case.status, case.reason, open_kinds(case)) == (status, reason, kinds), case.load_id
        assert case.events[-1].action == "status_migrated" and case.events[-1].actor == "migration"
    assert no_desk.open_exceptions[0].description == "no verified email booking desk on the profile"
    assert portal.open_exceptions[0].description.startswith("books on a web portal")
    assert confirmed.open_exceptions[0].description == (
        "vendor confirmed 2026-10-05 09:00, pickup# 20463798"
    )
    assert accepted.open_exceptions[0].description == (
        "vendor offered 2026-10-02 09:00; the agent accepted it"
    )
    review = question.open_exceptions[0]
    assert review.raised_by == "migration" and review.detail == {"legacy_status": "needs_human"}
    assert as_utc(review.raised_at) == old
    assert question.events[-1].detail == {"from": "needs_human", "to": "pending"}
    # Nothing left on an old status: a second pass is a no-op.
    assert migrate_legacy_statuses(session) == 0


def test_init_db_upgrades_a_store_written_before_the_split(tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'old.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    with engine.begin() as conn:
        # What a store from before the split looks like: no reschedule count, old statuses.
        conn.execute(text("ALTER TABLE booking_cases DROP COLUMN reschedule_count"))
        conn.execute(
            text(
                "INSERT INTO booking_cases (load_id, waypoint_index, po_numbers, status, reason, "
                "created_at, updated_at) VALUES (2582627, 0, '[]', 'needs_profile', "
                "'no verified email booking desk on the profile', '2026-09-29 10:00:00', "
                "'2026-09-29 10:00:00')"
            )
        )
    engine.dispose()  # the new version opens the old file fresh, as the CLI does
    assert ensure_columns(engine) == ["booking_cases.reschedule_count"]
    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE booking_cases DROP COLUMN reschedule_count"))
    engine.dispose()
    init_db(engine)
    with session_scope(session_factory(engine)) as session:
        case = list_cases(session)[0]
        assert case.reschedule_count == 0
        assert case.status == CaseStatus.UNSCHEDULED.value
        assert open_kinds(case) == ["missing_method"]
    engine.dispose()


def test_booking_cli_lists_resolves_and_books(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "fp.db"
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.chdir(tmp_path)
    engine = make_engine(f"sqlite:///{db.as_posix()}")
    init_db(engine)
    with session_scope(session_factory(engine)) as session:
        case = BookingCase(
            load_id=2582627,
            waypoint_index=0,
            po_numbers=["115802102660"],
            facility_key=koch_key(),
            vendor_name="Koch Foods, Inc.",
            vendor_timezone="America/New_York",
            requested_local="2026-10-05 09:00",
        )
        session.add(case)
        session.flush()
        flag(session, case, ExceptionType.MISSING_METHOD, "no verified email booking desk")
    engine.dispose()
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        listed = runner.invoke(app, ["booking", "list"])
        assert listed.exit_code == 0 and "!missing_method" in listed.output
        assert "#1" in runner.invoke(app, ["booking", "list", "--exception", "any"]).output
        none = runner.invoke(app, ["booking", "list", "--exception", "confirmation_review"])
        assert "no cases" in none.output
        shown = runner.invoke(app, ["booking", "show", "1"])
        assert shown.exit_code == 0 and "OPEN      missing_method" in shown.output
        wrong = runner.invoke(
            app, ["booking", "resolve", "1", "confirmation_review", "--by", "megan", "--note", "x"]
        )
        assert wrong.exit_code == 1 and "no open confirmation_review" in wrong.output
        done = runner.invoke(
            app,
            ["booking", "resolve", "1", "missing_method", "--by", "megan", "--note", "desk found"],
        )
        assert done.exit_code == 0 and "missing_method resolved" in done.output
        booked = runner.invoke(
            app,
            [
                "booking",
                "booked",
                "1",
                "--by",
                "megan",
                "--via",
                "phone",
                "--date",
                "2026-10-05",
                "--time",
                "09:00",
                "--pickup-number",
                "20463798",
            ],
        )
        assert booked.exit_code == 0 and "scheduled (booked by phone)" in booked.output
        assert "scheduled" in runner.invoke(app, ["booking", "list"]).output
        bad = runner.invoke(
            app, ["booking", "booked", "1", "--by", "m", "--via", "x", "--time", "9"]
        )
        assert bad.exit_code == 2
    finally:
        get_settings.cache_clear()
