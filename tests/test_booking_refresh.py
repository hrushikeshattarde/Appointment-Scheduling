"""A rescan keeps each case in step with its load in Transport Pro, and skips canceled loads.

The loads are the invented Lidl inbound load from tests/test_booking.py (Koch Foods to the PYE
RDC), changed the way Transport Pro changes: the pod enters a pickup number or confirms the
stop, books the DCT slot (its reference lands in the delivery stop's notes), moves the tender,
or cancels the load ("loadStatus": "Canceled", as live Transport Pro writes it).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from facility_profiles.booking.automation import run_once
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.references import ReferenceSource, active, record_reference
from facility_profiles.booking.service import (
    draft_case,
    has_request,
    list_cases,
    mark_sent,
    po_numbers,
    scan,
)
from facility_profiles.booking.worklist import open_kinds, resolve
from facility_profiles.config import Settings
from facility_profiles.domain.schema import ReferenceType
from facility_profiles.storage.db import session_scope
from facility_profiles.tpro.models import Load
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "115802102660"


@pytest.fixture
def pod(settings: Settings, sessions):  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    return settings.model_copy(update={"pilot_terminal_ids": [1089]})


def _scan(settings: Settings, sessions, *loads: dict[str, Any], now: datetime = NOW):  # type: ignore[no-untyped-def]
    return scan(FakeTPro(list(loads), {}), sessions, settings, days_ahead=7, now=now)  # type: ignore[arg-type]


def _load(**changes: Any) -> dict[str, Any]:
    """The test load, with Transport Pro's later changes applied."""
    load = copy.deepcopy(lidl_load(7001, po=changes.pop("po", PO)))
    pickup, delivery = load["waypoints"]
    if "status" in changes:
        load["status"]["loadStatus"] = changes.pop("status")
    if "pickup_number" in changes:
        load["reference"]["pickupNumber"] = changes.pop("pickup_number")
    if "stop_status" in changes:
        pickup["appointmentTime"]["appointmentStatus"] = changes.pop("stop_status")
    if "tender" in changes:
        pickup["appointmentTime"]["open"] = pickup["appointmentTime"]["close"] = changes.pop(
            "tender"
        )
    if "delivery_notes" in changes:
        delivery["notes"] = changes.pop("delivery_notes")
    if "delivery_at" in changes:
        delivery["appointmentTime"]["open"] = delivery["appointmentTime"]["close"] = changes.pop(
            "delivery_at"
        )
    assert not changes, changes
    return load


def _case(sessions):  # type: ignore[no-untyped-def]
    """The one case, with what the tests read loaded before its session closes."""
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        for related in (case.events, case.exceptions, case.jobs, case.references, case.messages):
            list(related)
        return case


def _events(case: BookingCase) -> list[str]:
    return [e.action for e in case.events]


# ------------------------------------------------------------------ POs and canceled loads


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("115806102630 & 115806102631", ["115806102630", "115806102631"]),
        ("115806102630,115806102631", ["115806102630", "115806102631"]),
        (PO, [PO]),
        ("PO 115806102630 see notes", []),
        ("", []),
    ],
)
def test_a_po_field_can_list_several(field: str, expected: list[str]) -> None:
    assert po_numbers(Load.model_validate(_load(po=field))) == expected


def test_a_load_with_two_pos_asks_for_both(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load(po="115806102630 & 115806102631"))
    assert _case(sessions).po_numbers == ["115806102630", "115806102631"]


def test_a_canceled_load_opens_no_case(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    client = FakeTPro([_load(status="Canceled")], {})
    stats = scan(client, sessions, pod, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert stats.canceled_loads == 1 and stats.created == 0
    assert any("load_status" in call for call in client.calls)  # asked for separately
    with session_scope(sessions) as s:
        assert list_cases(s) == []


def test_a_load_canceled_before_any_request_cancels_its_case(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    stats = _scan(pod, sessions, _load(status="Canceled"))
    # Like Transport Pro, the usual search leaves the canceled load out; the scan asks for it.
    assert stats.loads == 0 and stats.cases_canceled == 1 and stats.refreshed == 1
    case = _case(sessions)
    assert case.status == CaseStatus.CANCELED.value
    assert case.reason == "the load was canceled in Transport Pro"
    closed = next(e for e in case.events if e.action == "closed")
    assert closed.actor == "agent"
    again = _scan(pod, sessions, _load(status="Canceled"))
    assert again.refreshed == 0 and again.cases_canceled == 0


def test_a_load_canceled_after_a_request_is_raised_once(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    with session_scope(sessions) as s:
        draft_case(s, list_cases(s)[0], RecordingMailer(), pod, now=NOW)
    stats = _scan(pod, sessions, _load(status="Canceled"))
    assert stats.load_canceled == 1 and stats.cases_canceled == 0
    case = _case(sessions)
    # Drafted, not sent: the case stays so nobody sends the draft; a person deletes it.
    assert case.status == CaseStatus.UNSCHEDULED.value
    assert open_kinds(case) == ["load_canceled"]
    assert "delete the drafted request" in case.open_exceptions[0].description
    with session_scope(sessions) as s:
        resolve(
            s,
            list_cases(s)[0],
            [ExceptionType.LOAD_CANCELED],
            resolution="deleted the draft",
            by="Test User",
        )
    assert _scan(pod, sessions, _load(status="Canceled")).load_canceled == 0
    assert open_kinds(_case(sessions)) == []


def test_a_load_canceled_after_the_request_went_out_says_tell_the_vendor(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        draft_case(s, case, RecordingMailer(), pod, now=NOW)
        mark_sent(s, case, by="Test User", thread_id="t1", sent_at=NOW)
    _scan(pod, sessions, _load(status="Canceled"))
    case = _case(sessions)
    assert case.status == CaseStatus.PENDING.value and open_kinds(case) == ["load_canceled"]
    assert "tell the vendor" in case.open_exceptions[0].description


# ------------------------------------------------------------------ booked in Transport Pro


def test_a_pickup_number_entered_in_transport_pro_books_the_case(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    assert _case(sessions).status == CaseStatus.UNSCHEDULED.value
    stats = _scan(pod, sessions, _load(pickup_number="20463798"))
    assert stats.booked_in_tpro == 1
    case = _case(sessions)
    assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
    assert case.pickup_number == "20463798"
    assert case.reason == "booked in Transport Pro: vendor pickup number 20463798"
    assert "booked_in_tpro" in _events(case)
    number = active(case, ReferenceType.PICKUP_NUMBER.value)[0]
    assert number.source == ReferenceSource.LOAD.value
    # It is in Transport Pro already: nothing is queued to write back.
    assert not [j for j in case.jobs if j.kind == "tpro_write"]


def test_a_stop_confirmed_in_transport_pro_books_the_case_at_its_time(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    _scan(pod, sessions, _load(stop_status="Confirmed"))
    case = _case(sessions)
    assert case.status == CaseStatus.SCHEDULED.value
    assert case.confirmed_local == "2026-10-01 09:00"  # 13:00 UTC, the vendor's New York time
    assert case.reason == "booked in Transport Pro: the stop's appointment is confirmed there"


# ------------------------------------------------------------------ the delivery slot


def test_the_dct_slot_booked_after_the_scan_lets_the_request_go(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    no_slot = _load(delivery_notes="Please ensure driver has a load bar.")
    _scan(pod, sessions, no_slot)
    with session_scope(sessions) as s:
        run_once(s, pod, now=NOW, mailer=RecordingMailer(), timers=False)
        case = list_cases(s)[0]
        assert case.delivery_ref is None and not has_request(case)
        assert case.jobs[0].status == "waiting"  # Lidl's rule waits for the delivery slot
    stats = _scan(pod, sessions, _load())
    assert stats.refreshed == 1
    case = _case(sessions)
    assert case.delivery_ref == "PYE_021026123"
    assert active(case, ReferenceType.DELIVERY_NUMBER.value)[0].source == "load"
    assert "delivery_from_tpro" in _events(case) and open_kinds(case) == []
    with session_scope(sessions) as s:
        run_once(s, pod, now=NOW, mailer=RecordingMailer(), timers=False)
        assert has_request(list_cases(s)[0])  # the agent drafted it on its next pass


def test_a_delivery_moved_after_the_request_went_out_is_raised(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        draft_case(s, case, RecordingMailer(), pod, now=NOW)
        mark_sent(s, case, by="Test User", thread_id="t1", sent_at=NOW)
    moved = _load(delivery_notes="DELIVERY# PYE_031026555", delivery_at="2026-10-03T13:30:00Z")
    _scan(pod, sessions, moved)
    case = _case(sessions)
    assert case.delivery_ref == "PYE_031026555"
    assert case.delivery_at_utc is not None
    assert case.delivery_at_utc.replace(tzinfo=UTC) == datetime(2026, 10, 3, 13, 30, tzinfo=UTC)
    assert open_kinds(case) == ["delivery_moved"]
    assert (
        "Transport Pro moved the delivery to PYE_031026555" in case.open_exceptions[0].description
    )


def test_a_slot_from_the_customer_desk_is_not_undone_by_a_rescan(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    newer = datetime(2026, 10, 5, 11, 0, tzinfo=UTC)
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        record_reference(
            s,
            case,
            ReferenceType.DELIVERY_NUMBER.value,
            "PYE_051026999",
            source=ReferenceSource.CUSTOMER_DESK,
        )
        case.delivery_at_utc = newer
    _scan(pod, sessions, _load())  # Transport Pro still has the first slot
    case = _case(sessions)
    assert case.delivery_ref == "PYE_051026999"
    assert case.delivery_at_utc is not None and case.delivery_at_utc.replace(tzinfo=UTC) == newer


# ------------------------------------------------------------------ the tender and older cases


def test_a_new_tender_before_the_request_moves_the_ask(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    assert _case(sessions).requested_local == "2026-10-01 09:00"
    _scan(pod, sessions, _load(tender="2026-10-01T15:00:00Z"))
    case = _case(sessions)
    assert case.requested_local == "2026-10-01 11:00"
    assert "tender_changed" in _events(case)


def test_a_new_tender_after_the_request_is_kept_but_the_ask_stands(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    with session_scope(sessions) as s:
        draft_case(s, list_cases(s)[0], RecordingMailer(), pod, now=NOW)
    _scan(pod, sessions, _load(tender="2026-10-01T15:00:00Z"))
    case = _case(sessions)
    assert case.requested_local == "2026-10-01 09:00"  # what the vendor was asked for
    assert case.tendered_pickup_utc is not None
    assert case.tendered_pickup_utc.replace(tzinfo=UTC) == datetime(2026, 10, 1, 15, tzinfo=UTC)


def test_an_older_case_takes_only_what_it_lacks(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    with session_scope(sessions) as s:
        list_cases(s)[0].tpro_seen = None  # as cases were before scans kept what they saw
    changed = _load(
        delivery_notes="DELIVERY# PYE_999999999", pickup_number="20463798", status="Covered"
    )
    _scan(pod, sessions, changed)
    case = _case(sessions)
    assert case.delivery_ref == "PYE_021026123"  # it had one: not overwritten
    assert case.pickup_number == "20463798" and case.status == CaseStatus.SCHEDULED.value
    assert case.tpro_seen is not None and case.tpro_seen["delivery_ref"] == "PYE_999999999"


def test_nothing_changed_in_transport_pro_changes_nothing(pod, sessions) -> None:  # type: ignore[no-untyped-def]
    _scan(pod, sessions, _load())
    before = _events(_case(sessions))
    later = NOW + timedelta(hours=2)
    stats = _scan(pod, sessions, _load(), now=later)
    assert stats.existing == 1 and stats.refreshed == 0
    assert _events(_case(sessions)) == before
