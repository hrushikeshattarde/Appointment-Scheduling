"""Rules learnt from the live threads: desk slot wordings, the PO-date floor, stale slots."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.service import (
    draft_case,
    list_cases,
    parse_delivery_slot,
    po_embedded_date,
    scan,
)
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor
from tests.test_booking_real_thread import morgan_load, seed_morgan

EARLY = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # The inbound desk after a missed delivery: date and time on one line, reference on the next.
        (
            "What caused the driver to miss delivery? New info below:\n9/30 at 1100\nPYE_300926723\n",
            (datetime(2026, 9, 30, 15, 0, tzinfo=UTC), "PYE_300926723"),
        ),
        # The desk moving a delivery, with a three-digit clock and a dash.
        (
            "Please shift this to pickup on Monday 10/5 and deliver 10/6, appointment: 10/6 730AM - PYE_061026919.",
            (datetime(2026, 10, 6, 11, 30, tzinfo=UTC), "PYE_061026919"),
        ),
        (
            "Here is an updated appointment! 8/20 7AM - GRM_200826926.",
            (datetime(2026, 8, 20, 11, 0, tzinfo=UTC), "GRM_200826926"),
        ),
        (
            "New Appointment: FRG_200526615 05/20 @ 1100",
            (datetime(2026, 5, 20, 15, 0, tzinfo=UTC), "FRG_200526615"),
        ),
        ("Failed to Deliver. Update?", None),
    ],
)
def test_delivery_slot_wordings_from_the_inbound_desk(text: str, expected) -> None:  # type: ignore[no-untyped-def]
    assert parse_delivery_slot(text, year=2026, timezone="America/New_York") == expected


def test_po_embedded_date_reads_lidl_pos_and_nothing_else() -> None:
    near = date(2026, 10, 2)
    assert po_embedded_date("115802102660", near=near) == date(2026, 10, 2)
    assert po_embedded_date("118830092663", near=near) == date(2026, 9, 30)
    assert po_embedded_date("20463798", near=near) is None  # a pickup number, eight digits
    assert po_embedded_date("115899092660", near=near) is None  # day 99
    assert po_embedded_date("115802103060", near=near) is None  # 2030: too far from near


def test_morgan_foods_request_is_never_earlier_than_the_po_date(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "pilot_customer_ids": [7211]}
    )
    seed_morgan(sessions)
    # Tendered 10/01 09:00 ET, PO dated 02/10/26: the desk will not load before 10/02.
    load = morgan_load(
        7101, pos=("115802102660", "115802102661"), pickup_open="2026-10-01T13:00:00Z"
    )
    stats = scan(FakeTPro([load], {}), sessions, settings, days_ahead=14, now=EARLY)  # type: ignore[arg-type]
    assert stats.created == 1
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.NEW.value
        assert case.requested_local == "2026-10-02 09:00"
        floor = next(e for e in case.events if e.action == "po_date_floor")
        assert "PO date 10/02" in floor.detail["reason"] and floor.detail["feasible"] is True
        # The request the pod would have sent on 9/23 now asks for the day the desk will accept.
        draft = draft_case(session, case, RecordingMailer(), settings, now=EARLY)
        assert "PO# 115802102660 & 115802102661 (ALL IN ONE TRUCK) on 10/02 @ 0900" in (
            draft.body or ""
        )


def test_po_date_after_the_delivery_hands_the_case_to_a_person(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "pilot_customer_ids": [7211]}
    )
    seed_morgan(sessions)
    # PO dated 07/10/26, delivery on 10/06: no pickup day can make the delivery.
    load = morgan_load(
        7102, pos=("115807102660", "115807102661"), pickup_open="2026-10-01T13:00:00Z"
    )
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=14, now=EARLY)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.NEEDS_HUMAN.value
        assert case.requested_local == "2026-10-07 09:00"
        assert (
            case.reason is not None
            and "PO date 10/07" in case.reason
            and "delivery slot" in case.reason
        )


def test_other_desks_keep_the_tendered_day(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)  # Koch / CCI desk: not on the floor list
    scan(
        FakeTPro([lidl_load(7103, po="226302102660")], {}),
        sessions,
        settings,
        days_ahead=14,
        now=EARLY,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.requested_local == "2026-10-01 09:00" and case.status == CaseStatus.NEW.value
        assert not any(e.action == "po_date_floor" for e in case.events)


def test_a_slot_inside_the_notice_window_or_already_past_goes_to_a_person(settings, sessions):
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_min_notice_hours": 4}
    )
    seed_vendor(sessions)
    loads = [
        lidl_load(7104, po="226321092660"),  # tendered 2026-10-01 09:00 ET (13:00Z)
        lidl_load(7105, po="226321092661"),
    ]
    loads[1]["waypoints"][0]["appointmentTime"]["open"] = "2026-10-01T06:00:00Z"
    loads[1]["waypoints"][0]["appointmentTime"]["close"] = "2026-10-01T08:00:00Z"
    # Scanned at 10:00Z on 10/01: the first pickup is three hours away, the second was at 02:00 ET.
    scan(
        FakeTPro(loads, {}),
        sessions,
        settings,
        days_ahead=14,
        now=datetime(2026, 10, 1, 10, 0, tzinfo=UTC),
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        soon, past = sorted(list_cases(session), key=lambda c: c.load_id)
        assert soon.status == CaseStatus.NEEDS_HUMAN.value and "notice window" in (
            soon.reason or ""
        )
        assert past.status == CaseStatus.NEEDS_HUMAN.value and "already passed" in (
            past.reason or ""
        )
        assert [e.action for e in soon.events] == ["scanned", "stale_slot"]


def test_a_case_that_went_stale_since_the_scan_is_refused_at_draft_time(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    scan(
        FakeTPro([lidl_load(7106, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=14,
        now=NOW,
    )  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == CaseStatus.NEW.value
        mailer = RecordingMailer()
        with pytest.raises(ValueError, match="notice window"):
            draft_case(
                session, case, mailer, settings, now=datetime(2026, 10, 1, 11, 0, tzinfo=UTC)
            )
        assert not mailer.drafts and case.status == CaseStatus.NEW.value
        # Drafted in time, it goes out as before.
        draft_case(session, case, mailer, settings, now=NOW)
        assert len(mailer.drafts) == 1 and case.status == CaseStatus.DRAFTED.value
        assert isinstance(session.get(BookingCase, case.id), BookingCase)
