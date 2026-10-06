"""A load Transport Pro shows delivered is no longer anyone's to book.

Seen live on 2026-10-06: the September Lidl pickups (Polar, Morgan Foods) sat on the board as
"Pickup time passed" though Transport Pro had them Delivered, because a scan only reads the
coming days. Now a pickup whose load is delivered is closed as picked up, and each scan reads
back, by load number, the pickups still on the board whose day has passed.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import pytest

from facility_profiles.api.booking import case_summary
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.service import RECHECK_DAYS, scan
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, seed_vendor
from tests.test_booking_refresh import _case, _events, _load


@pytest.fixture
def pod(settings: Settings, sessions):  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    return settings.model_copy(update={"pilot_terminal_ids": [1089]})


LATER = NOW + timedelta(days=5)  # the pickup (10/01) has passed and left the scan's window


def _scan(settings: Settings, sessions, *loads: dict[str, Any], now=NOW):  # type: ignore[no-untyped-def]
    fake = FakeTPro(list(loads), {})
    return scan(fake, sessions, settings, days_ahead=7, now=now), fake  # type: ignore[arg-type]


def _missed(sessions) -> None:  # type: ignore[no-untyped-def]
    """The timers mark the pickup missed once its time has gone by."""
    with session_scope(sessions) as session:
        sweep(session, now=LATER)


def test_a_missed_pickup_whose_load_was_delivered_is_closed_as_picked_up(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    _missed(sessions)
    assert open_kinds(_case(sessions)) == ["pickup_expired"]

    stats, fake = _scan(pod, sessions, _load(status="Delivered"), now=LATER)

    assert "get_load 7001" in fake.calls  # read back by its number: no longer in the window
    assert stats.rechecked == 1 and stats.picked_up == 1
    case = _case(sessions)
    assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
    assert case.reason == "picked up: Transport Pro shows the load Delivered"
    assert case.tpro_seen["load_status"] == "Delivered"
    assert _events(case)[-1] == "picked_up_in_tpro"
    with session_scope(sessions) as session:  # the board lists it apart, as picked up
        row = case_summary(session.get(BookingCase, case.id), now=LATER)  # type: ignore[arg-type]
    assert row["picked_up"] and row["load_status"] == "Delivered" and not row["past_due"]
    # Seen once: the next scan neither reads it again nor records it twice.
    again, fake = _scan(pod, sessions, _load(status="Delivered"), now=LATER)
    assert again.rechecked == 0 and again.picked_up == 0
    assert not any(c.startswith("get_load") for c in fake.calls)


def test_a_booked_pickup_stays_booked_and_counts_as_picked_up(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load(pickup_number="20463798"))
    assert _case(sessions).status == CaseStatus.SCHEDULED.value
    stats, _ = _scan(pod, sessions, _load(pickup_number="20463798", status="Delivered"), now=LATER)
    case = _case(sessions)
    assert stats.picked_up == 1 and case.status == CaseStatus.SCHEDULED.value
    assert case.reason == "load already carries vendor pickup number 20463798"  # kept


def test_a_load_still_on_its_way_is_left_alone(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    _missed(sessions)
    # Dispatched can still be before the pickup, so it is not taken as picked up.
    stats, fake = _scan(pod, sessions, _load(status="Dispatched"), now=LATER)
    assert "get_load 7001" in fake.calls and stats.picked_up == 0
    case = _case(sessions)
    assert case.status == CaseStatus.UNSCHEDULED.value and open_kinds(case) == ["pickup_expired"]


@pytest.mark.parametrize("status", ["Delivered", "In Transit", "Completed"])
def test_a_load_delivered_before_the_board_saw_it_opens_no_case(pod, sessions, status: str) -> None:  # type: ignore[no-untyped-def]
    stats, _ = _scan(pod, sessions, _load(status=status))
    assert stats.created == 0 and stats.picked_up == 1
    with session_scope(sessions) as session:
        assert session.query(BookingCase).count() == 0


def test_only_the_last_days_are_read_back_and_a_lost_load_does_not_stop_the_scan(
    pod, sessions
) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    # Transport Pro no longer has the load (no 7001 in the fake): logged, and the scan goes on.
    stats, fake = _scan(pod, sessions, now=LATER)
    assert "get_load 7001" in fake.calls and stats.rechecked == 0
    assert _case(sessions).status == CaseStatus.UNSCHEDULED.value
    # A pickup older than RECHECK_DAYS is not read back at all.
    _, fake = _scan(pod, sessions, _load(status="Delivered"), now=NOW + timedelta(RECHECK_DAYS + 4))
    assert not any(c.startswith("get_load") for c in fake.calls)
