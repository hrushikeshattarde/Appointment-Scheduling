"""The first ten gaps of the email edge-case review, closed one by one.

1 holidays, 2 a tendered weekend, 3 a day the facility is closed, 4 a second request, 5 the
customer's rule for manual commands, 6 the daily cap counts emails, 7 one desk's refusal keeps
the others, 8 a desk the profile no longer trusts, 9 Circle addresses, 10 no pickup date.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from typer.testing import CliRunner

from facility_profiles.booking.mail import OutboundDraft, RecordingMailer, RecordingSender
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.outbox import SendRefusedError, check_send_gate
from facility_profiles.booking.recommend import recommend_time
from facility_profiles.booking.respond import alternative_days, offer_is_feasible
from facility_profiles.booking.rules import business_day_before
from facility_profiles.booking.service import (
    draft_batch,
    draft_case,
    list_cases,
    prepare_drafts,
    rule_stops,
)
from facility_profiles.booking.timers import weekday_hours
from facility_profiles.business_days import (
    holiday,
    is_business_day,
    next_business_day,
    previous_business_day,
)
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository
from tests.test_booking import NOW, koch_key
from tests.test_booking_recommend import WEEKDAYS, a_case, hours
from tests.test_booking_send import _scanned, _send_settings

ET = ZoneInfo("America/New_York")


def _lidl(settings: Settings) -> Settings:
    return settings.model_copy(update={"pilot_terminal_ids": [1089]})


# ------------------------------------------------------------------ 1. holidays


def test_the_freight_holidays_are_not_business_days() -> None:
    assert holiday(date(2026, 11, 26)) == "Thanksgiving"
    assert holiday(date(2026, 7, 3)) == "Independence Day"  # the 4th is a Saturday
    assert holiday(date(2027, 12, 31)) == "New Year's Day"  # 1/1/2028 is a Saturday
    assert holiday(date(2026, 10, 12)) is None  # Columbus Day: docks are open
    assert not is_business_day(date(2026, 11, 26)) and is_business_day(date(2026, 11, 27))
    assert previous_business_day(date(2026, 11, 27)) == date(2026, 11, 25)
    assert next_business_day(date(2026, 12, 24)) == date(2026, 12, 28)


def test_holidays_count_like_weekends_everywhere_a_day_is_counted(settings: Settings) -> None:
    # The no-reply clock: Thanksgiving adds nothing, the Friday after counts.
    start = datetime(2026, 11, 25, 17, 0, tzinfo=UTC)  # Wed noon ET
    end = datetime(2026, 11, 27, 17, 0, tzinfo=UTC)  # Fri noon ET
    assert weekday_hours(start, end, ET) == pytest.approx(24)
    # A desk's cut-off on the business day before: the Friday after Thanksgiving's is Wednesday.
    friday = datetime(2026, 11, 27, 9, 0, tzinfo=ET)
    assert business_day_before(friday).date() == date(2026, 11, 25)
    # A pickup worked back from a Friday delivery skips Thanksgiving.
    case = a_case(
        tendered_pickup_utc=None,
        delivery_at_utc=datetime(2026, 11, 27, 13, 30, tzinfo=UTC),
        miles=300,
    )
    rec = recommend_time(case, settings, None, now=datetime(2026, 11, 20, 12, tzinfo=UTC))
    assert rec.local == "2026-11-25 09:00"
    # Offers on the holiday are not workable, and are never asked for.
    on_holiday = datetime(2026, 11, 26, 9, 0, tzinfo=ET)
    ok, why = offer_is_feasible(case, on_holiday, settings, now=datetime(2026, 11, 20, tzinfo=UTC))
    assert not ok and why == "falls on a holiday (Thanksgiving)"
    days = alternative_days(case, settings, now=datetime(2026, 11, 20, 12, tzinfo=UTC))
    assert days == ["11/27", "11/25", "11/24"]  # Thanksgiving is never offered


# ------------------------------------------------------------------ 2, 3. closed days


def test_a_tendered_saturday_is_asked_for_on_the_friday_before(settings: Settings) -> None:
    saturday = a_case(
        tendered_pickup_utc=datetime(2026, 10, 3, 13, 0, tzinfo=UTC),  # Sat 09:00 ET
        delivery_at_utc=datetime(2026, 10, 6, 13, 30, tzinfo=UTC),
    )
    rec = recommend_time(saturday, settings, None, now=NOW)
    assert rec.local == "2026-10-02 09:00" and rec.feasible
    assert rec.moved[0].rule == "closed"
    assert rec.moved[0].note == ("Sat 10/03 09:00 ET is a Saturday; asking for Fri 10/02 09:00 ET")
    # A facility whose hours say it ships on Saturdays keeps the Saturday.
    open_saturdays = hours("07:00", "15:00", days=[*WEEKDAYS, "sat"])
    assert recommend_time(saturday, settings, open_saturdays, now=NOW).local == "2026-10-03 09:00"


def test_a_day_the_facilitys_hours_have_it_closed_moves_to_the_day_before(
    settings: Settings,
) -> None:
    mon_to_wed = hours("07:00", "15:00", days=["mon", "tue", "wed"])
    rec = recommend_time(a_case(), settings, mon_to_wed, now=NOW)  # tendered Thursday
    assert rec.local == "2026-09-30 09:00"
    assert rec.moved[0].note == (
        "Thu 10/01 09:00 ET is a Thursday, which the facility's hours list closed; asking for "
        "Wed 09/30 09:00 ET"
    )


# ------------------------------------------------------------------ 4, 8, 9, 10. draft checks


def test_a_second_request_for_the_same_pickup_needs_again(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, settings, now=NOW)
        with pytest.raises(ValueError, match="already has a request"):
            draft_case(session, case, mailer, settings, now=NOW)
        draft_case(session, case, mailer, settings, now=NOW, again=True)
        assert len(mailer.drafts) == 2


def test_a_desk_the_profile_no_longer_trusts_is_not_written_to(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        Repository(session).set_field_human(
            koch_key(),
            Role.SHIPPER,
            "contact_email",
            "new.desk@udfinc.example",
            state=FieldState.HUMAN_SET,
        )
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        with pytest.raises(
            ValueError, match=re.escape("profile now has new.desk@udfinc.example, not cci")
        ):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)


def test_a_circle_address_is_never_a_facilitys_desk(
    settings, sessions, tmp_path, monkeypatch
) -> None:  # type: ignore[no-untyped-def]
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        case.contact_email = "desk.person@circledelivers.com"
        with pytest.raises(ValueError, match="is a Circle address"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)
        draft = OutboundDraft(to_addr=case.contact_email, cc_addr=None, subject="s", body="b")
        with pytest.raises(SendRefusedError, match="is a Circle address"):
            check_send_gate(
                session,
                case,
                draft,
                _send_settings(settings),
                trusted_desk=case.contact_email,
            )
    # Nor can a person file one on a profile.
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'p.db').as_posix()}")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(
            app,
            [
                "profile",
                "set",
                "any",
                "shipper",
                "contact_email",
                "--value",
                "lidl@circledelivers.com",
                "--by",
                "tester",
            ],
        )
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 2 and "is a Circle address" in result.output


def test_a_pickup_with_no_date_is_not_asked_for(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        case.requested_local = None
        with pytest.raises(ValueError, match="has no pickup date"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)


# ------------------------------------------------------------------ 5. the customer's rule


def test_manual_commands_follow_the_customers_rule(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660", "226321092661")
    with session_scope(sessions) as session:
        waiting_for_slot, ready = sorted(list_cases(session), key=lambda c: c.id)
        waiting_for_slot.delivery_ref = None  # Lidl asks once the DCT appointment is known
        assert "waits for the delivery's appointment" in (
            rule_stops(waiting_for_slot, settings) or ""
        )
        assert rule_stops(ready, settings) is None
        # Lidl's rule drafts only: a person's `booking send` holds back too.
        assert 'only drafts (do = "draft")' in (rule_stops(ready, settings, sending=True) or "")
        to_draft, waiting = prepare_drafts(session, settings, now=NOW)
        assert [c.id for c in to_draft] == [ready.id]
        assert [(c.id, "delivery" in why) for c, why in waiting] == [(waiting_for_slot.id, True)]
        _, waiting = prepare_drafts(session, settings, now=NOW, sending=True)
        assert {c.id for c, _ in waiting} == {waiting_for_slot.id, ready.id}


# ------------------------------------------------------------------ 6, 7. sending in batches


def test_the_daily_cap_counts_emails_not_po_lines(settings, sessions) -> None:
    send = _send_settings(settings).model_copy(update={"booking_send_daily_cap": 2})
    _scanned(send, sessions, "226321092660", "226321092661", "226321092662", "226321092663")
    sender = RecordingSender()
    with session_scope(sessions) as session:
        cases = sorted(list_cases(session), key=lambda c: c.id)
        draft_batch(session, cases[:3], sender, send, now=NOW)  # one email, three POs
        assert len(sender.drafts) == 1
        draft_case(session, cases[3], sender, send, now=NOW)  # the second email of the day
        assert len(sender.drafts) == 2


def test_one_desks_refusal_keeps_what_was_sent_before(settings, sessions) -> None:
    send = _send_settings(settings)
    _scanned(send, sessions, "226321092660", "226321092661")
    sender = RecordingSender()
    with session_scope(sessions) as session:
        good, bad = sorted(list_cases(session), key=lambda c: c.id)
        bad.contact_email = "desk@another-vendor.example"  # its own desk, and not trusted
        refused: list[tuple[list[BookingCase], str]] = []
        messages = draft_batch(session, [good, bad], sender, send, now=NOW, refused=refused)
        assert [m.case_id for m in messages] == [good.id]
        assert [[c.id for c in group] for group, _ in refused] == [[bad.id]]
        assert "profile now has cci@udfinc.com" in refused[0][1]
    with session_scope(sessions) as session:  # the send that went out is still recorded
        good_again = session.get(BookingCase, good.id)
        assert good_again is not None and good_again.status == CaseStatus.PENDING.value
        assert good_again.messages[-1].sent_at is not None


def test_business_hours_skip_a_holiday_monday() -> None:
    # Labor Day: Friday 17:00 to Tuesday 09:00 is only Tuesday's first nine hours.
    start = datetime(2026, 9, 4, 17, 0, tzinfo=ET)
    end = datetime(2026, 9, 8, 9, 0, tzinfo=ET)
    assert weekday_hours(start, end, ET) == pytest.approx(7 + 9)
    assert weekday_hours(start, end, ET) - weekday_hours(start, end - timedelta(hours=9), ET) == 9
