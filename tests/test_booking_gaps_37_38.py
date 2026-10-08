"""Gaps 37 and 38 of the email edge-case review: the truck for a booked pickup.

37 the carrier on a booked pickup is watched (its dispatch canceled, or none on the load in time),
38 a booked pickup time that passes with no arrival at the shipper is raised.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from facility_profiles.api.booking import case_summary, latest_updates
from facility_profiles.booking.coverage import CARRIER_KEY, carrier_due
from facility_profiles.booking.models import BookingCase, ExceptionType
from facility_profiles.booking.service import list_cases, mark_booked, scan
from facility_profiles.booking.worklist import open_kinds, resolve
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from facility_profiles.tpro.models import Dispatch, VoiceAiLoad
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor
from tests.test_booking_situation_replies import DISPATCH

LOAD = 6200
BOOKED = "2026-10-01 09:00"  # Thursday, 13:00 UTC
AT = datetime(2026, 10, 1, 13, 0, tzinfo=UTC)
CANCELED = {
    **DISPATCH,
    "id": 500,
    "status": "Canceled",
    "dateCreated": "2026-09-29T14:00:00Z",
    "assignedTo": {"carrier": {"companyName": "Blue Line Haulers LLC"}},
}
OTHER = {**DISPATCH, "id": 502, "assignedTo": {"carrier": {"companyName": "Harbor Road Inc"}}}


class Dispatched(FakeTPro):
    """Transport Pro with the load's dispatches and its stops' actual times."""

    def __init__(self, load: dict[str, Any]) -> None:
        super().__init__([load], {})
        self.dispatches: list[dict[str, Any]] = []
        self.arrived: str | None = None
        self.read: list[str] = []

    def search_dispatches(self, load_id: int) -> list[Dispatch]:
        self.read.append(f"dispatches {load_id}")
        return [Dispatch.model_validate(d) for d in self.dispatches]

    def get_voiceai_load(self, load_id: int) -> VoiceAiLoad | None:
        self.read.append(f"summary {load_id}")
        pickup: dict[str, Any] = {"type": "Pickup", "status": "Pending"}
        if self.arrived:
            pickup = {"type": "Pickup", "status": "Complete", "actualDate": {"start": self.arrived}}
        return VoiceAiLoad.model_validate(
            {
                "loadId": load_id,
                "dispatchStatus": "Dispatched",
                "dispatchInformation": {"waypoints": [pickup, {"type": "Delivery"}]},
            }
        )


def _settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"pilot_terminal_ids": [1089], "booking_watch_booked": True})


def _booked(settings: Settings, sessions) -> tuple[int, Dispatched]:  # type: ignore[no-untyped-def]
    """A pickup booked for Thu 10/01 09:00 ET, its load read from a Transport Pro with dispatches."""
    seed_vendor(sessions)
    tpro = Dispatched(lidl_load(LOAD, po="226321092660"))
    scan(tpro, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        mark_booked(session, case, by="megan", via="phone", local=BOOKED)
        return case.id, tpro


def _scan(settings: Settings, sessions, tpro: Dispatched, at: datetime) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    stats = scan(tpro, sessions, settings, days_ahead=7, now=at)  # type: ignore[arg-type]
    return stats.__dict__


def _case(sessions, case_id: int) -> tuple[list[str], list[str], list[tuple[str, Any]], Any]:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        return (
            open_kinds(case),
            [e.description for e in case.open_exceptions],
            [(e.action, (e.detail or {}).get("reason")) for e in case.events],
            (case.tpro_seen or {}).get(CARRIER_KEY),
        )


# ------------------------------------------------------------------ 37. the carrier


def test_a_carrier_dropped_from_a_booked_pickup_is_raised_and_clears_when_covered(
    settings, sessions
) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    tpro.dispatches = [CANCELED]
    stats = _scan(settings, sessions, tpro, NOW + timedelta(hours=1))
    assert stats["carriers_watched"] == 1 and stats["carrier_alerts"] == 1
    kinds, said, events, seen = _case(sessions, case_id)
    assert kinds == ["carrier_dropped"]
    assert said == [
        "Blue Line Haulers LLC's dispatch was canceled; the pickup booked for Thu 10/01 09:00 ET "
        "has no carrier now"
    ]
    assert seen == {"dispatch": None, "name": None}
    # The same canceled dispatch is not raised twice.
    assert _scan(settings, sessions, tpro, NOW + timedelta(hours=2))["carrier_alerts"] == 0
    # Covered again: the to-do clears and the carrier is kept, then a change is noted.
    tpro.dispatches = [CANCELED, DISPATCH]
    _scan(settings, sessions, tpro, NOW + timedelta(hours=3))
    kinds, _, events, seen = _case(sessions, case_id)
    assert kinds == [] and seen == {"dispatch": 501, "name": "Ridgeway Freight LLC"}
    assert ("carrier_assigned", "Ridgeway Freight LLC") in events
    tpro.dispatches = [CANCELED, {**DISPATCH, "status": "Canceled"}, OTHER]
    _scan(settings, sessions, tpro, NOW + timedelta(hours=4))
    _, _, events, seen = _case(sessions, case_id)
    assert ("carrier_changed", "Ridgeway Freight LLC replaced by Harbor Road Inc") in events
    assert seen == {"dispatch": 502, "name": "Harbor Road Inc"}


def test_no_carrier_by_noon_the_business_day_before_is_raised_once(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    assert carrier_due(AT.date(), settings) == datetime(2026, 9, 30, 16, 0, tzinfo=UTC)
    _scan(settings, sessions, tpro, datetime(2026, 9, 30, 15, 59, tzinfo=UTC))
    assert _case(sessions, case_id)[0] == []
    _scan(settings, sessions, tpro, datetime(2026, 9, 30, 16, 5, tzinfo=UTC))
    kinds, said, _, _ = _case(sessions, case_id)
    assert kinds == ["carrier_missing"]
    assert said == [
        "booked for Thu 10/01 09:00 ET and no carrier on the load yet (due by 09/30 12:00 ET)"
    ]
    # A person who handled it is not asked again for the same booked time.
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        resolve(
            session, case, [ExceptionType.CARRIER_MISSING], resolution="covering it", by="megan"
        )
    _scan(settings, sessions, tpro, datetime(2026, 9, 30, 18, 0, tzinfo=UTC))
    assert _case(sessions, case_id)[0] == []


def test_only_booked_pickups_due_soon_are_watched(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.confirmed_local = "2026-10-08 09:00"  # nine days out
    _scan(settings, sessions, tpro, NOW)
    assert tpro.read == []


# ------------------------------------------------------------------ 38. the truck at the pickup


def test_a_booked_pickup_with_no_arrival_after_two_hours_is_raised_and_clears(
    settings, sessions
) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    tpro.dispatches = [DISPATCH]
    _scan(settings, sessions, tpro, AT + timedelta(hours=1, minutes=30))
    assert _case(sessions, case_id)[0] == [] and "summary 6200" not in tpro.read
    stats = _scan(settings, sessions, tpro, AT + timedelta(hours=2, minutes=30))
    kinds, said, _, _ = _case(sessions, case_id)
    assert stats["no_shows"] == 1 and kinds == ["pickup_no_show"]
    assert said == [
        "booked for Thu 10/01 09:00 ET; Transport Pro shows no arrival at the shipper yet"
    ]
    tpro.arrived = "2026-10-01T16:10:00Z"  # late, but there
    _scan(settings, sessions, tpro, AT + timedelta(hours=3, minutes=30))
    assert _case(sessions, case_id)[0] == []
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        done = next(e for e in case.exceptions if e.kind == "pickup_no_show")
        assert done.resolution == "Transport Pro shows the truck arrived 10/01 12:10 ET"


def test_a_no_show_with_no_carrier_says_so(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    _scan(settings, sessions, tpro, AT + timedelta(hours=3))
    kinds, said, _, _ = _case(sessions, case_id)
    assert kinds == ["carrier_missing", "pickup_no_show"]
    assert said[1].endswith("no arrival at the shipper yet; no carrier is on the load")


# ------------------------------------------------------------------ the board


def test_the_board_shows_the_carrier_and_its_changes(settings, sessions) -> None:
    settings = _settings(settings)
    case_id, tpro = _booked(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        row = case_summary(case, now=NOW)
        assert row["carrier_checked"] is False and row["carrier"] is None
    tpro.dispatches = [DISPATCH]
    _scan(settings, sessions, tpro, NOW)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        row = case_summary(case, now=NOW)
        assert row["carrier_checked"] is True and row["carrier"] == "Ridgeway Freight LLC"
        updates = latest_updates([case], now=datetime.now(tz=UTC))
        assert any(
            u["what"] == "Carrier on the load" and u["about"] == "Ridgeway Freight LLC"
            for u in updates
        )
