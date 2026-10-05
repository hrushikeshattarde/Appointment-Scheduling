"""The agent on its own: rules from the customer file, request jobs, batches, waits and failures."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from facility_profiles.api.app import run_autopilot
from facility_profiles.api.booking import case_detail
from facility_profiles.booking.automation import (
    MAX_ATTEMPTS,
    business_days_before,
    due_time,
    next_batch,
    run_once,
)
from facility_profiles.booking.mail import OutboundDraft, RecordingMailer, RecordingSender
from facility_profiles.booking.models import AutomationJob, BookingCase, CaseStatus, JobStatus
from facility_profiles.booking.service import (
    add_reference,
    list_cases,
    mark_booked,
    mark_sent,
    scan,
)
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.customers import CustomerFileError, customers, parse_customer
from facility_profiles.customers.profile import DEFAULT_RULE
from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

# NOW is Tue 09/29 08:00 New York. The Lidl load picks up Thu 10/01 09:00 at Koch Foods, whose
# CCI desk books by email, and delivers Fri 10/02 09:30 with DCT reference PYE_021026123.
LIDL_HEAD = """
name = "Lidl"
timezone = "America/New_York"
[transport_pro]
customer_ids = [7211, 6680]
terminal_ids = [1089]
[mail]
group = "lidl@circledelivers.com"
[customer_desk]
email = "inbound@lidl.us"
[numbers]
delivery_ref = '[A-Z]{3}_\\d{6,}'
"""


def with_rules(settings: Settings, tmp_path: Path, rules: str) -> Settings:
    """Lidl with these rules instead of its own (a customer file in FP_CUSTOMERS_DIR)."""
    folder = tmp_path / "customers"
    folder.mkdir(exist_ok=True)
    (folder / "lidl.toml").write_text(LIDL_HEAD + rules, encoding="utf-8")
    return settings.model_copy(update={"customers_dir": str(folder), "pilot_terminal_ids": [1089]})


def scanned(settings: Settings, sessions, *loads: dict[str, Any]) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    scan(
        FakeTPro(list(loads) or [lidl_load(2001, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]


def request_job(case: BookingCase) -> AutomationJob:
    return next(j for j in case.jobs if j.kind == "request")


class FailingMailer:
    """A mail client that is down."""

    calls = 0

    def create_draft(self, draft: OutboundDraft) -> str:
        FailingMailer.calls += 1
        msg = "the drafts folder is not writable"
        raise OSError(msg)


# ------------------------------------------------------------------ the rules


def test_rules_are_read_from_the_customer_file_and_checked() -> None:
    data = tomllib.loads(
        LIDL_HEAD
        + """
[[rules]]
name = "a"
do = "teleport"
[[rules]]
name = "b"
do = "draft"
when = { methods = ["fax"], colour = "blue" }
batch_at = "25:00"
lead_days = 99
wait_for = "the weather"
pickup_from = "moon"
follow_up = "sometimes"
extra = 1
[[rules]]
name = "b"
do = "hold"
"""
    )
    with pytest.raises(CustomerFileError) as caught:
        parse_customer(data, key="lidl", source="lidl.toml")
    text = str(caught.value)
    for expected in (
        "rule 'a': do must be one of draft, send, hold, skip",
        "rules[1] has no setting 'extra'",
        "rule 'b': when has no filter 'colour'",
        "method 'fax' is not one of",
        "batch_at must be HH:MM",
        "lead_days must be a whole number from 0 to 30",
        "wait_for can only be delivery_slot",
        "pickup_from must be tender or delivery",
        "follow_up must be true or false",
        "two rules are named 'b'",
    ):
        assert expected in text, expected


def test_the_first_rule_that_covers_a_pickup_decides(settings: Settings) -> None:
    lidl = customers(settings).get("lidl")
    [rule] = lidl.rules
    assert rule.describe() == (
        "vendor pickups by email: method email -> draft (once the delivery has its slot)"
    )
    email = BookingCase(booking_method="email", vendor_name="Koch Foods", customer_id=7211)
    portal = BookingCase(booking_method="web_portal", customer_id=7211)
    assert lidl.rule_for(email) is rule and lidl.rule_for(portal) is DEFAULT_RULE
    data = tomllib.loads(
        LIDL_HEAD
        + """
[[rules]]
name = "koch by phone"
when = { vendors = ["Koch"], desks = ["CCI@udfinc.com"] }
do = "hold"
why = "the CCI desk wants a call"
[[rules]]
name = "outbound"
when = { customer_ids = [6680] }
do = "skip"
"""
    )
    custom = parse_customer(data, key="lidl", source="lidl.toml")
    koch = BookingCase(vendor_name="Koch Foods, Inc.", contact_email="cci@udfinc.com")
    assert custom.rule_for(koch).name == "koch by phone"
    assert (
        custom.rule_for(BookingCase(vendor_name="Koch Foods", contact_email="x@y.z"))
        is DEFAULT_RULE
    )
    assert custom.rule_for(BookingCase(customer_id=6680)).do == "skip"


def test_when_a_rule_wants_the_request_written() -> None:
    from zoneinfo import ZoneInfo

    from facility_profiles.customers.profile import Rule

    tz = ZoneInfo("America/New_York")
    case = BookingCase(
        requested_local="2026-10-05 09:00", vendor_timezone="America/New_York"
    )  # Mon
    assert business_days_before(datetime(2026, 10, 5).date(), 1).isoformat() == "2026-10-02"
    assert due_time(case, Rule("now", "draft"), tz, NOW) == NOW
    assert due_time(case, Rule("ten", "draft", batch_at="10:00"), tz, NOW) == datetime(
        2026, 9, 29, 10, 0, tzinfo=tz
    )
    lead = Rule("lead", "draft", lead_days=2, batch_at="10:00")
    assert due_time(case, lead, tz, NOW) == datetime(2026, 10, 1, 10, 0, tzinfo=tz)
    friday_evening = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)  # 19:00 New York
    assert next_batch("10:00", tz, friday_evening) == datetime(2026, 10, 5, 10, 0, tzinfo=tz)


# ------------------------------------------------------------------ the pass


def test_lidl_asks_for_a_pickup_once_its_delivery_slot_is_known(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][1]["notes"] = "Please ensure driver has a load bar."  # no DCT slot yet
    scanned(settings, sessions, load)
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        report = run_once(session, settings, now=NOW, mailer=mailer)
        case = list_cases(session)[0]
        job = request_job(case)
        assert report.waiting == 1 and mailer.drafts == []
        assert job.status == JobStatus.WAITING.value and job.rule == "vendor pickups by email"
        assert case.events[-1].action == "request_waiting"
        # The pod books the DCT slot; the reference reaches the case.
        add_reference(session, case, "delivery_number", "PYE_021026123", by="pod")
        report = run_once(session, settings, now=NOW + timedelta(minutes=15), mailer=mailer)
        assert report.drafted == 1 and len(mailer.drafts) == 1
        assert (
            job.status == JobStatus.DONE.value and job.result["message_id"] == case.messages[0].id
        )
        session.expire(case, ["events"])  # events are added by id, not through the list
        assert case.events[-1].action == "drafted" and case.events[-1].actor == "automation"
        # A second pass does nothing more.
        again = run_once(session, settings, now=NOW + timedelta(minutes=30), mailer=mailer)
        assert again.drafted == 0 and len(mailer.drafts) == 1 and len(case.jobs) == 1


def test_one_email_per_desk_at_the_batch_hour_its_lead_days_ahead(
    settings: Settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = with_rules(
        settings,
        tmp_path,
        '[[rules]]\nname = "daily"\ndo = "draft"\nlead_days = 1\nbatch_at = "10:00"\n',
    )
    scanned(
        settings, sessions, lidl_load(2001, po="226321092660"), lidl_load(2002, po="226321092661")
    )
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        report = run_once(session, settings, now=NOW, mailer=mailer)
        assert report.planned == 2 and mailer.drafts == []
        jobs = [request_job(c) for c in list_cases(session)]
        due = datetime(2026, 9, 30, 14, 0, tzinfo=UTC)  # Wed 10:00 New York, a day before
        assert {j.due_at.replace(tzinfo=UTC) for j in jobs} == {due}  # type: ignore[union-attr]
        assert jobs[0].reason == "rule 'daily': draft at Wed 09/30 10:00 ET"
        assert (
            run_once(session, settings, now=due - timedelta(minutes=1), mailer=mailer).drafted == 0
        )
        report = run_once(session, settings, now=due + timedelta(minutes=5), mailer=mailer)
        assert report.drafted == 2 and len(mailer.drafts) == 1
        assert (
            "PO# 226321092660 on 10/01 @ 0900\nPO# 226321092661 on 10/01 @ 0900"
            in mailer.drafts[0].body
        )


def test_hold_raises_it_for_a_person_and_skip_opens_no_case(
    settings: Settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    rules = (
        '[[rules]]\nname = "outbound"\nwhen = { customer_ids = [6680] }\ndo = "skip"\n'
        '[[rules]]\nname = "koch"\nwhen = { vendors = ["koch"] }\ndo = "hold"\nwhy = "the CCI desk wants a call"\n'
    )
    settings = with_rules(settings, tmp_path, rules)
    outbound = lidl_load(2002, po="226321092661")
    outbound["billingInfo"] = {
        "customerId": 6680,
        "customer": {"id": 6680, "companyName": "Lidl-Outbound"},
    }
    seed_vendor(sessions)
    stats = scan(
        FakeTPro([lidl_load(2001, po="226321092660"), outbound], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    assert stats.created == 1 and stats.skipped_by_rule == 1
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        report = run_once(session, settings, now=NOW, mailer=mailer)
        case = list_cases(session)[0]
        assert report.held == 1 and mailer.drafts == []
        assert request_job(case).status == JobStatus.HELD.value
        assert open_kinds(case) == ["handoff"]
        assert case.open_exceptions[0].description == (
            "rule 'koch': a person books this (the CCI desk wants a call)"
        )
        run_once(session, settings, now=NOW + timedelta(hours=1), mailer=mailer)
        assert len(case.exceptions) == 1  # raised once, not every pass


def test_a_case_waiting_on_a_person_or_a_desk_that_books_later(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    key = seed_vendor(sessions)
    with session_scope(sessions) as session:
        repo = Repository(session)
        repo.set_field_human(
            key, Role.SHIPPER, "required_refs", ["shipment_number"], state=FieldState.HUMAN_SET
        )
        repo.set_field_human(key, Role.SHIPPER, "max_days_ahead", 1, state=FieldState.HUMAN_SET)
    scan(
        FakeTPro([lidl_load(2001, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert open_kinds(case) == ["missing_reference"]
        run_once(session, settings, now=NOW, mailer=mailer)
        job = request_job(case)
        assert (
            job.status == JobStatus.BLOCKED.value
            and job.reason == "waiting on a person: missing_reference"
        )
        add_reference(session, case, "shipment_number", "7781234", by="pod")
        # The desk books one day ahead: the job moves to the day it opens, Wed 00:00.
        run_once(session, settings, now=NOW, mailer=mailer)
        assert mailer.drafts == [] and job.status == JobStatus.PLANNED.value
        opens = datetime(2026, 9, 30, 4, 0, tzinfo=UTC)
        assert job.due_at.replace(tzinfo=UTC) == opens  # type: ignore[union-attr]
        assert job.reason.startswith("the desk books at most 1 day ahead")  # type: ignore[union-attr]
        run_once(session, settings, now=NOW + timedelta(hours=2), mailer=mailer)
        assert mailer.drafts == []
        run_once(session, settings, now=opens + timedelta(minutes=1), mailer=mailer)
        assert len(mailer.drafts) == 1 and "Shipment# 7781234" in mailer.drafts[0].body


def test_a_send_rule_sends_in_send_mode_and_drafts_otherwise(
    settings: Settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = with_rules(settings, tmp_path, '[[rules]]\nname = "send"\ndo = "send"\n')
    scanned(settings, sessions)
    mailer, sender = RecordingMailer(), RecordingSender()
    with session_scope(sessions) as session:
        report = run_once(session, settings, now=NOW, mailer=mailer, sender=sender)
        assert report.drafted == 1 and report.sent == 0 and sender.drafts == []  # draft mode
    scanned_more = settings.model_copy(update={"booking_mode": "send"})
    scan(
        FakeTPro([lidl_load(2002, po="226321092661")], {}),
        sessions,
        scanned_more,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        report = run_once(session, scanned_more, now=NOW, mailer=mailer, sender=sender)
        assert report.sent == 1 and len(sender.drafts) == 1
        case = next(c for c in list_cases(session) if c.load_id == 2002)
        assert case.status == CaseStatus.PENDING.value and request_job(case).result["sent"] is True


def test_a_failed_batch_leaves_nothing_and_gives_up_after_three_tries(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    scanned(settings, sessions)
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        report = run_once(session, settings, now=NOW, mailer=FailingMailer())  # type: ignore[arg-type]
        job = request_job(case)
        assert report.failed == 1 and job.status == JobStatus.FAILED.value and job.attempts == 1
        assert case.messages == [] and case.offers == []  # rolled back with the batch
        assert job.last_error == "OSError: the drafts folder is not writable"
        # Not retried before the hour is up; retried after it; given up after the third try.
        assert (
            run_once(
                session, settings, now=NOW + timedelta(minutes=30), mailer=FailingMailer()
            ).failed
            == 0
        )  # type: ignore[arg-type]
        for hours in (1, 2):
            run_once(
                session,
                settings,
                now=NOW + timedelta(hours=hours, minutes=1),
                mailer=FailingMailer(),
            )  # type: ignore[arg-type]
        assert job.attempts == MAX_ATTEMPTS and open_kinds(case) == ["automation_failed"]
        assert (
            run_once(session, settings, now=NOW + timedelta(hours=5), mailer=FailingMailer()).failed
            == 0
        )  # type: ignore[arg-type]


def test_a_silent_desk_gets_its_follow_up_unless_the_rule_says_not(
    settings: Settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    scanned(settings, sessions)
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        run_once(session, settings, now=NOW, mailer=mailer)
        case = list_cases(session)[0]
        mark_sent(session, case, by="pod", thread_id="t1", sent_at=NOW)
        later = NOW + timedelta(hours=26)
        report = run_once(session, settings, now=later, mailer=mailer)
        assert report.followed_up == 1 and mailer.drafts[-1].body.startswith(
            "Hello,\n\nFollowing up on this."
        )
        assert [j.kind for j in case.jobs] == ["request", "follow_up"]
        assert (
            run_once(session, settings, now=later + timedelta(hours=1), mailer=mailer).followed_up
            == 0
        )
    quiet = with_rules(
        settings, tmp_path, '[[rules]]\nname = "quiet"\ndo = "draft"\nfollow_up = false\n'
    )
    scan(FakeTPro([lidl_load(2002, po="226321092661")], {}), sessions, quiet, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        run_once(session, quiet, now=NOW, mailer=mailer)
        case = next(c for c in list_cases(session) if c.load_id == 2002)
        mark_sent(session, case, by="pod", thread_id="t2", sent_at=NOW)
        assert (
            run_once(session, quiet, now=NOW + timedelta(hours=26), mailer=mailer).followed_up == 0
        )


def test_what_a_person_did_closes_the_job(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][1]["notes"] = None  # waits for the delivery slot
    scanned(settings, sessions, load)
    with session_scope(sessions) as session:
        run_once(session, settings, now=NOW, mailer=RecordingMailer())
        case = list_cases(session)[0]
        mark_booked(session, case, by="pod", via="phone", local="2026-10-01 09:00")
        report = run_once(session, settings, now=NOW + timedelta(hours=1), mailer=RecordingMailer())
        assert report.closed == 1 and request_job(case).status == JobStatus.DONE.value
        assert request_job(case).reason == "the case is scheduled"


def test_a_rule_can_plan_the_pickup_back_from_the_delivery(
    settings: Settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = with_rules(
        settings,
        tmp_path,
        '[[rules]]\nname = "dock first"\ndo = "draft"\npickup_from = "delivery"\n',
    )
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][1]["appointmentTime"]["open"] = "2026-10-05T13:30:00Z"  # Mon 09:30
    scanned(settings, sessions, load)
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        # 559 miles is two transit days before Monday: Saturday, so Friday, not the tendered Thursday.
        assert case.requested_local == "2026-10-02 09:00"
        mailer = RecordingMailer()
        run_once(session, settings, now=NOW, mailer=mailer)
        assert "on 10/02 @ 0900" in mailer.drafts[0].body


# ------------------------------------------------------------------ where people see it


def future_load() -> dict[str, Any]:
    """The Lidl load, picking up three days from the real today (the CLI reads the clock)."""
    today = datetime.now(tz=UTC).replace(hour=13, minute=0, second=0, microsecond=0)
    pickup = today + timedelta(days=3)
    while pickup.weekday() >= 5:
        pickup += timedelta(days=1)
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][0]["appointmentTime"].update(
        open=f"{pickup:%Y-%m-%dT%H:%M:%SZ}",
        close=f"{pickup + timedelta(hours=2):%Y-%m-%dT%H:%M:%SZ}",
    )
    delivery = pickup + timedelta(days=1, minutes=30)
    load["waypoints"][1]["appointmentTime"].update(
        open=f"{delivery:%Y-%m-%dT%H:%M:%SZ}", close=f"{delivery:%Y-%m-%dT%H:%M:%SZ}"
    )
    return load


def test_the_board_the_cli_and_the_server_loop(settings: Settings, tmp_path, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    db = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    drafts = tmp_path / "drafts"
    local = settings.model_copy(
        update={"database_url": db, "booking_drafts_dir": str(drafts), "pilot_terminal_ids": [1089]}
    )
    engine = make_engine(db)
    init_db(engine)
    sessions = session_factory(engine)
    seed_vendor(sessions)
    scan(FakeTPro([future_load()], {}), sessions, local, days_ahead=14, now=datetime.now(tz=UTC))  # type: ignore[arg-type]
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", db)
    monkeypatch.setenv("FP_BOOKING_DRAFTS_DIR", str(drafts))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        dry = runner.invoke(app, ["booking", "run", "--dry-run"])
        assert dry.exit_code == 0, dry.output
        assert "drafted to cci@udfinc.com" in dry.output and "nothing was changed" in dry.output
        assert runner.invoke(app, ["booking", "jobs"]).output.strip() == "no jobs"
        assert not drafts.exists()
        shown = runner.invoke(app, ["customers", "show", "lidl"])
        assert "rule 1" in shown.output and "vendor pickups by email" in shown.output
    finally:
        get_settings.cache_clear()
    # The server's loop: one pass, drafts into the folder.
    run_autopilot(sessions, local)
    assert len(list(drafts.glob("*.eml"))) == 1
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        [job] = case_detail(case, now=NOW)["jobs"]
        assert (
            job["kind"] == "request"
            and job["status"] == "done"
            and job["rule"] == "vendor pickups by email"
        )
    get_settings.cache_clear()
    try:
        listed = runner.invoke(app, ["booking", "jobs", "1"])
        assert "request   done" in listed.output and "vendor pickups by email" in listed.output
    finally:
        get_settings.cache_clear()
    engine.dispose()
