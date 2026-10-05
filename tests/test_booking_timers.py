"""Timers and the daily summary: what the agent raises as time passes, and the morning page."""

from __future__ import annotations

import time as wall
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from facility_profiles.api.app import create_app, run_timers
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import RecordingMailer
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
    outside_request,
    scan,
)
from facility_profiles.booking.timers import (
    check_case,
    pickup_passed,
    sweep,
    target_slot,
    waiting_since,
    weekday_hours,
)
from facility_profiles.booking.today import render_today, today_summary
from facility_profiles.booking.worklist import flag, open_kinds, resolve
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, reply, seed_vendor

ET = ZoneInfo("America/New_York")
INTERNAL = ["circledelivers.com"]
# NOW is Tuesday 09/29 08:00 in New York; the Lidl test load picks up Thursday 10/01 09:00.
PICKUP = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)


def _sent_case(settings, sessions) -> int:  # type: ignore[no-untyped-def]
    """A request drafted and sent by a person at NOW, waiting on the CCI desk."""
    seed_vendor(sessions)
    scan(
        FakeTPro([lidl_load(7301, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)
        return case.id


def _case(  # type: ignore[no-untyped-def]
    session,
    *,
    status: str = CaseStatus.UNSCHEDULED.value,
    requested: str = "2026-09-28 09:00",
    confirmed: str | None = None,
    vendor: str = "Polar Corporation",
    desk: str | None = "csr@polar.example",
) -> BookingCase:
    case = BookingCase(
        load_id=2_600_000 + (session.query(BookingCase).count() + 1),
        waypoint_index=0,
        customer_name="Lidl - Inbound",
        vendor_name=vendor,
        vendor_timezone="America/New_York",
        po_numbers=["118830092663"],
        booking_method="email" if desk else None,
        contact_email=desk,
        requested_local=requested,
        confirmed_local=confirmed,
        status=status,
    )
    session.add(case)
    session.flush()
    return case


def _request(case: BookingCase, *, sent_at: datetime | None) -> None:
    case.messages.append(
        BookingMessage(
            direction="out",
            kind="request",
            subject="Pick Up Appointment: 118830092663",
            sent_at=sent_at,
        )
    )


# ------------------------------------------------------------------ clocks


def test_weekday_hours_skip_the_weekend_in_the_vendor_time_zone():
    friday = datetime(2026, 9, 25, 15, 0, tzinfo=ET)
    assert weekday_hours(friday, friday + timedelta(days=1), ET) == pytest.approx(9)
    assert weekday_hours(friday, friday + timedelta(days=3), ET) == pytest.approx(24)
    tuesday = datetime(2026, 9, 29, 8, 0, tzinfo=ET)
    assert weekday_hours(tuesday, tuesday + timedelta(hours=30), ET) == pytest.approx(30)
    assert weekday_hours(tuesday, tuesday - timedelta(hours=1), ET) == 0
    # A Sunday night to Monday morning across the end of daylight time still counts real hours.
    sunday = datetime(2026, 11, 1, 23, 0, tzinfo=ET)
    assert weekday_hours(sunday, datetime(2026, 11, 2, 6, 0, tzinfo=ET), ET) == pytest.approx(6)


def test_no_reply_raises_24_then_48_hours_once_each_and_a_resolution_sticks(settings, sessions):
    case_id = _sent_case(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert sweep(session, now=NOW + timedelta(hours=23)).raised == []

        first = sweep(session, now=NOW + timedelta(hours=25))
        assert [(k, d) for _, k, d in first.raised] == [
            (
                "unanswered_24h",
                "no reply from cci@udfinc.com to the request sent Tue 09/29 08:00 ET "
                "(24 weekday hours)",
            )
        ]
        exc = case.open_exceptions[0]
        assert exc.raised_by == "timer" and exc.raised_at == NOW + timedelta(hours=25)
        assert sweep(session, now=NOW + timedelta(hours=30)).raised == []  # once per silence

        second = sweep(session, now=NOW + timedelta(hours=48, minutes=30))
        assert [k for _, k, _ in second.raised] == ["unanswered_48h"]
        assert [(k, why) for _, k, why in second.resolved] == [
            ("unanswered_24h", "no reply after 48 h either")
        ]
        assert open_kinds(case) == ["unanswered_48h"]

        resolve(
            session,
            case,
            [ExceptionType.UNANSWERED_48H],
            resolution="called the desk, they will answer today",
            by="megan",
        )
        again = sweep(session, now=NOW + timedelta(hours=48, minutes=45))
        assert again.raised == [] and open_kinds(case) == []  # the person's call stands


def test_any_answer_stops_the_clock_but_an_unrelated_reply_does_not(settings, sessions):
    case_id = _sent_case(settings, sessions)
    readings = iter(
        [
            ReplyClassification(status=ReplyStatus.UNRELATED),
            ReplyClassification(
                status=ReplyStatus.QUESTION, question="Both orders?", quotes=["Both orders?"]
            ),
        ]
    )
    classifier = FakeReplyClassifier(lambda _ctx: next(readings))
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        sweep(session, now=NOW + timedelta(hours=25))
        assert open_kinds(case) == ["unanswered_24h"]

        ingest(
            session,
            [reply("I am out of the office", mid="ooo")],
            classifier,
            internal_domains=INTERNAL,
        )
        assert waiting_since(case) is case.messages[0]  # still the request
        assert open_kinds(case) == ["unanswered_24h"]

        ingest(session, [reply("Both orders?", mid="q1")], classifier, internal_domains=INTERNAL)
        assert open_kinds(case) == ["facility_question"]
        silence = case.exceptions[0]
        assert silence.resolution == "superseded by a later reply (question)"
        assert waiting_since(case) is None
        assert sweep(session, now=NOW + timedelta(hours=40)).raised == []


def test_the_follow_up_is_still_drafted_and_only_a_sent_one_settles_the_24_hours(
    settings, sessions
):
    settings = settings.model_copy(update={"booking_follow_up_hours": 24})
    case_id = _sent_case(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        later = NOW + timedelta(hours=25)
        responder = Responder(settings, RecordingMailer(), now=later)
        sweep(session, now=later)
        # Something that waits on us is not the vendor's silence: no nudge while it is open.
        flag(session, case, ExceptionType.FACILITY_QUESTION, "vendor asked: which door?")
        assert responder.follow_up(session, case) is None
        resolve(session, case, [ExceptionType.FACILITY_QUESTION], resolution="answered", by="m")

        message = responder.follow_up(session, case)
        assert message is not None and message.kind == "follow_up" and message.sent_at is None
        # A draft has chased nobody yet; the follow-up does not restart the clock either.
        assert open_kinds(case) == ["unanswered_24h"]
        assert waiting_since(case) is case.messages[0]


def test_a_pickup_that_passes_unbooked_expires_once_and_replaces_the_silence(settings, sessions):
    case_id = _sent_case(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        sweep(session, now=NOW + timedelta(hours=48, minutes=30))
        assert open_kinds(case) == ["unanswered_48h"]
        assert not pickup_passed(case, PICKUP - timedelta(minutes=1))

        result = sweep(session, now=PICKUP + timedelta(minutes=1))
        assert [(k, d) for _, k, d in result.raised] == [
            ("pickup_expired", "pickup Thu 10/01 09:00 ET passed with no booking from the vendor")
        ]
        assert [(k, why) for _, k, why in result.resolved] == [
            ("unanswered_48h", "superseded: the pickup time passed")
        ]
        assert sweep(session, now=PICKUP + timedelta(hours=3)).raised == []

        mark_booked(session, case, by="megan", via="phone", local="2026-10-01 11:00")
        assert open_kinds(case) == [] and case.status == CaseStatus.SCHEDULED.value
        assert sweep(session, now=PICKUP + timedelta(hours=4)).raised == []


def test_expiry_says_what_happened_and_leaves_the_other_to_dos(session):
    drafted = _case(session)
    _request(drafted, sent_at=None)
    never = _case(session, desk=None)
    flag(session, never, ExceptionType.MISSING_METHOD, "no verified email booking desk")
    stale = _case(session)
    flag(session, stale, ExceptionType.SLOT_UNWORKABLE, "requested slot has already passed")
    declined = _case(session, status=CaseStatus.DECLINED.value)
    flag(session, declined, ExceptionType.FACILITY_DECLINED, "vendor cannot book: PO not ready")
    review = _case(session, status=CaseStatus.PENDING.value, confirmed="2026-09-28 10:00")
    flag(session, review, ExceptionType.CONFIRMATION_REVIEW, "vendor confirmed 2026-09-28 10:00")
    ahead = _case(session, requested="2026-10-05 09:00")
    date_only = _case(session, requested="2026-09-29")

    sweep(session, now=NOW)
    first_open = {c.id: c.open_exceptions[0].description for c in (drafted, never, stale, review)}
    assert first_open == {
        drafted.id: "pickup Mon 09/28 09:00 ET passed; the request was drafted but never sent",
        never.id: "no verified email booking desk",
        stale.id: "pickup Mon 09/28 09:00 ET passed; it was never requested",
        review.id: "vendor confirmed 2026-09-28 10:00",
    }
    assert open_kinds(never) == ["missing_method", "pickup_expired"]
    assert open_kinds(stale) == ["pickup_expired"]
    assert stale.exceptions[0].resolution == "superseded: the pickup time passed"
    assert open_kinds(review) == ["confirmation_review", "pickup_expired"]
    assert review.open_exceptions[1].description == (
        "pickup Mon 09/28 10:00 ET passed while the vendor's confirmation waited for approval"
    )
    assert open_kinds(declined) == ["facility_declined"]  # it already says the slot is off
    assert open_kinds(ahead) == [] and open_kinds(date_only) == []  # a date counts all day
    sweep(session, now=NOW + timedelta(hours=16))
    assert open_kinds(date_only) == ["pickup_expired"]

    resolve(session, drafted, [ExceptionType.PICKUP_EXPIRED], resolution="driver went", by="m")
    assert sweep(session, now=NOW + timedelta(hours=17)).raised == []


def test_a_later_offer_or_a_new_slot_clears_the_expiry(session):
    case = _case(session, status=CaseStatus.PENDING.value)
    _request(case, sent_at=NOW - timedelta(days=2))
    check_case(session, case, now=NOW)
    assert open_kinds(case) == ["pickup_expired"]

    flag(
        session,
        case,
        ExceptionType.PROPOSED_TIME_REVIEW,
        "vendor offered 2026-10-02 13:00",
        date="2026-10-02",
        time="13:00",
    )
    assert target_slot(case) == ("2026-10-02 13:00", "offered")
    result = check_case(session, case, now=NOW)
    assert [(k, why) for _, k, why in result.resolved] == [
        ("pickup_expired", "the pickup is now Fri 10/02 13:00 ET (offered)")
    ]
    assert open_kinds(case) == ["proposed_time_review"]
    # The offer itself goes stale too if nobody acts on it.
    check_case(session, case, now=datetime(2026, 10, 2, 18, 0, tzinfo=UTC))
    assert open_kinds(case) == ["proposed_time_review", "pickup_expired"]
    assert case.open_exceptions[1].detail["slot"] == "2026-10-02 13:00"


def test_a_scheduled_or_canceled_case_has_its_timers_cleared(session):
    case = _case(session, status=CaseStatus.PENDING.value)
    _request(case, sent_at=NOW - timedelta(days=3))
    check_case(session, case, now=NOW)
    assert open_kinds(case) == ["pickup_expired"]
    case.status = CaseStatus.SCHEDULED.value  # booked some other way, timers still open
    result = sweep(session, now=NOW)
    assert [(k, why) for _, k, why in result.resolved] == [("pickup_expired", "case is scheduled")]


# ------------------------------------------------------------------ confirmations outside the ask


def test_a_confirmation_on_another_day_or_far_from_the_time_is_raised(session):
    case = _case(session, requested="2026-10-01 09:00")
    assert outside_request(case, "2026-10-01", "10:30") is None
    assert outside_request(case, "2026-10-01", None) is None
    assert outside_request(case, "2026-10-01", "14:00", date_only=True) is None
    assert outside_request(case, "2026-10-01", "06:00") == (
        "vendor confirmed Thu 10/01 06:00 ET; we asked for Thu 10/01 09:00 ET (3 h earlier)"
    )
    assert outside_request(case, "2026-10-02", "09:00") == (
        "vendor confirmed Fri 10/02 09:00 ET; we asked for Thu 10/01 09:00 ET"
    )


def test_approving_settles_the_review_the_window_and_the_timers(settings, sessions):
    case_id = _sent_case(settings, sessions)
    classifier = FakeReplyClassifier(
        lambda _ctx: ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date="2026-10-02",
            pickup_time="07:00",
            quotes=["10/2 @ 0700"],
        )
    )
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        ingest(session, [reply("We can do 10/2 @ 0700")], classifier, internal_domains=INTERNAL)
        assert open_kinds(case) == ["confirmation_review", "confirmed_outside_window"]
        confirmed = session.scalars(
            select(BookingEvent).where(
                BookingEvent.case_id == case.id, BookingEvent.action == "vendor_confirmed"
            )
        ).one()
        assert confirmed.detail["outside_window"].startswith("vendor confirmed Fri 10/02")
        flag(session, case, ExceptionType.PICKUP_EXPIRED, "pickup passed")
        approve(session, case, by="megan")
        assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
        assert {e.resolution for e in case.exceptions} == {"approved"}


# ------------------------------------------------------------------ the daily summary


def _morning_store(session, now: datetime) -> dict[str, BookingCase]:  # type: ignore[no-untyped-def]
    """Thursday 10/01 morning: something in every section of the summary."""
    cases = {
        "expired": _case(session, vendor="Polar Corporation", requested="2026-09-30 06:00"),
        "silent": _case(
            session,
            status=CaseStatus.PENDING.value,
            vendor="Koch Foods",
            requested="2026-10-02 09:00",
        ),
        "no_desk": _case(session, vendor="Lidl PYE Store", desk=None, requested="2026-10-06 08:00"),
        "draft": _case(session, vendor="Morgan Foods", requested="2026-10-05 09:00"),
        "booked": _case(
            session,
            status=CaseStatus.SCHEDULED.value,
            vendor="Seneca Foods",
            requested="2026-10-01 10:00",
            confirmed="2026-10-01 10:00",
        ),
        "gone": _case(
            session,
            status=CaseStatus.CANCELED.value,
            vendor="Gone Co",
            requested="2026-10-01 11:00",
        ),
    }
    _request(cases["expired"], sent_at=None)
    _request(cases["silent"], sent_at=datetime(2026, 9, 28, 14, 0, tzinfo=UTC))
    _request(cases["draft"], sent_at=None)
    flag(
        session,
        cases["no_desk"],
        ExceptionType.MISSING_METHOD,
        "no verified email booking desk",
        at=now - timedelta(hours=2),
    )
    cases["booked"].pickup_number = "20463798"
    cases["booked"].events.append(
        BookingEvent(action="marked_booked", actor="megan", created_at=now - timedelta(hours=1))
    )
    session.flush()
    return cases


def test_the_daily_summary_puts_the_most_urgent_first(session):
    now = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)  # 07:00 in Fort Wayne
    cases = _morning_store(session, now)
    sweep(session, now=now)
    data = today_summary(list_cases(session), now=now, timezone="America/Indiana/Indianapolis")
    assert [g["kind"] for g in data["needs_you"]] == [
        "pickup_expired",
        "unanswered_48h",
        "missing_method",
    ]
    assert data["counts"] == {
        "cases_need_you": 3,
        "todos": 3,
        "pickups_soon": 2,
        "pickups_soon_unbooked": 1,
        "drafts_waiting": 1,
        "waiting_on_vendor": 0,
        "raised_24h": 3,
        "resolved_24h": 0,
        "booked_24h": 1,
    }
    assert [r["id"] for r in data["pickups"]] == [cases["booked"].id, cases["silent"].id]
    assert [r["id"] for r in data["drafts"]] == [cases["draft"].id]

    ids = {name: case.id for name, case in cases.items()}
    assert render_today(data) == "\n".join(
        [
            "Pickup appointments: what needs you today",
            "Thursday 10/01/2026, 07:00 ET · all customers",
            "",
            "3 pickups need you (3 to-dos) · 2 pickups today or next business day, 1 not booked "
            "· 1 draft waiting to be sent · 0 waiting on the vendor",
            "Last 24 hours: 3 to-dos raised, 0 resolved, 1 pickup booked.",
            "",
            "NEEDS YOU (3)",
            "",
            "Pickup time passed (1)",
            "  What to do: If the truck picked up, mark it booked; if it still has to move, agree "
            "a new day with the vendor; if it is no longer needed, cancel it.",
            f"  - Polar Corporation, PO 118830092663, Wed 09/30 06:00 ET (case #{ids['expired']}): "
            "Pickup Wed 09/30 06:00 ET passed; the request was drafted but never sent. Just raised.",
            "",
            "No reply in 48 h (1)",
            "  What to do: Call the vendor's desk; if they book by phone, mark it booked with the "
            "slot.",
            f"  - Koch Foods, PO 118830092663, Fri 10/02 09:00 ET (case #{ids['silent']}): No reply "
            "from csr@polar.example to the request sent Mon 09/28 10:00 ET (48 weekday hours). "
            "Just raised.",
            "",
            "No booking desk (1)",
            "  What to do: Find the vendor's appointment desk, or book by phone and mark it booked.",
            f"  - Lidl PYE Store, PO 118830092663, Tue 10/06 08:00 ET (case #{ids['no_desk']}): "
            "No verified email booking desk. Open 2 h.",
            "",
            "PICKUPS THU 10/01 AND FRI 10/02 (2)",
            "",
            "Thu 10/01",
            f"  - 10:00 ET Seneca Foods, PO 118830092663: Booked, pickup# 20463798 "
            f"(case #{ids['booked']})",
            "",
            "Fri 10/02",
            f"  - 09:00 ET Koch Foods, PO 118830092663: Waiting on the vendor: no reply in 48 h "
            f"(case #{ids['silent']})",
            "",
            "DRAFTS WAITING TO BE SENT (1)",
            f"  - Morgan Foods, PO 118830092663, Mon 10/05 09:00 ET (case #{ids['draft']})",
            "",
        ]
    )

    other = today_summary(
        list_cases(session), now=now, timezone="America/Indiana/Indianapolis", customer="Nobody"
    )
    quiet = render_today(other)
    assert "Nothing needs a person right now." in quiet and "No pickups on those days." in quiet


def test_a_case_is_listed_once_under_its_most_urgent_to_do(session):
    now = datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
    case = _case(session, desk=None, requested="2026-09-30 08:00")
    flag(session, case, ExceptionType.MISSING_METHOD, "no verified email booking desk", at=now)
    sweep(session, now=now)
    data = today_summary([case], now=now + timedelta(days=3), timezone="America/New_York")
    assert [g["kind"] for g in data["needs_you"]] == ["pickup_expired"]
    item = data["needs_you"][0]["items"][0]
    assert item["also"] == ["No booking desk"] and item["age"] == "open 3 d"
    assert "Open 3 d. Also: No booking desk." in render_today(data)


# ------------------------------------------------------------------ CLI, API and the server loop


def test_booking_timers_and_today_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    engine = make_engine(url)
    init_db(engine)
    with session_scope(session_factory(engine)) as session:
        case = _case(session, requested="2020-01-06 09:00")  # long gone, by any clock
        _request(case, sent_at=None)
    engine.dispose()
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        result = runner.invoke(app, ["booking", "timers"])
        assert result.exit_code == 0, result.output
        assert "#1    raised   pickup_expired   pickup Mon 01/06 09:00 ET passed;" in result.output
        assert "1 case(s) checked: 1 raised, 0 resolved" in result.output
        assert "0 raised" in runner.invoke(app, ["booking", "timers"]).output

        out = tmp_path / "exports" / "today.txt"
        result = runner.invoke(app, ["booking", "today", "--out", str(out), "--no-timers"])
        assert result.exit_code == 0, result.output
        assert "\nPickup time passed (1)\n" in result.output
        assert out.read_text(encoding="utf-8").startswith("Pickup appointments")
        listed = runner.invoke(app, ["booking", "list", "--exception", "pickup_expired"])
        assert "!pickup_expired" in listed.output
    finally:
        get_settings.cache_clear()


def test_the_board_serves_the_summary_and_the_server_runs_the_timers(settings, tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    with session_scope(sessions) as session:
        old = _case(session, requested="2020-01-06 09:00")
        _request(old, sent_at=None)
        offered = _case(session, status=CaseStatus.PENDING.value, requested="2020-01-06 09:00")
        flag(
            session,
            offered,
            ExceptionType.PROPOSED_TIME_REVIEW,
            "vendor offered 2099-01-05 09:00",
            date="2099-01-05",
            time="09:00",
        )
        old_id, offered_id = old.id, offered.id
    settings = settings.model_copy(update={"database_url": url})

    application = create_app(settings, timers_every=60)
    with TestClient(application) as client:
        deadline = wall.monotonic() + 10
        kinds: list[str] = []
        while wall.monotonic() < deadline:
            kinds = [
                e["kind"]
                for e in client.get(f"/api/booking/cases/{old_id}").json()["open_exceptions"]
            ]
            if kinds:
                break
            wall.sleep(0.05)
        assert kinds == ["pickup_expired"]  # the first pass runs as the server starts
        rows = {r["id"]: r for r in client.get("/api/booking/cases").json()}
        # A live offer for a later day is not past due, though the slot asked for has passed.
        assert rows[old_id]["past_due"] and not rows[offered_id]["past_due"]
        today = client.get("/api/booking/today").json()
        assert today["timezone"] == "America/New_York"
        assert today["counts"]["cases_need_you"] == 2
        assert "\nPickup time passed (1)\n" in today["text"]
        kinds_listed = {k["kind"]: k["label"] for k in client.get("/api/booking/kinds").json()}
        assert kinds_listed["unanswered_48h"] == "No reply in 48 h"

    run_timers(sessions)  # a second pass from outside the server finds nothing new
    with session_scope(sessions) as session:
        case = session.get(BookingCase, old_id)
        assert case is not None and open_kinds(case) == ["pickup_expired"]
    engine.dispose()
