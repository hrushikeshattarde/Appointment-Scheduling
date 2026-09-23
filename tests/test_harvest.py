import json

from facility_profiles.pipeline.harvest import date_windows, stop_observed_at, structured_snapshot
from facility_profiles.tpro.models import Load, Waypoint
from tests.conftest import BREWERY_STOP, make_load, stop


def test_structured_snapshot_keeps_appointment_window_status_and_location_id():
    wp = Waypoint.model_validate(
        stop(
            "CN",
            notes="ignored here",
            status="Requested",
            open_="2026-09-10T13:00:00Z",
            close="2026-09-10T15:00:00Z",
            contact={"name": "Pat", "phone": "555-0100", "email": None, "fax": None},
            **BREWERY_STOP,  # type: ignore[arg-type]
        )
    )
    data = json.loads(structured_snapshot(wp))
    assert data["appointmentTime"] == {
        "open": "2026-09-10T13:00:00Z",
        "close": "2026-09-10T15:00:00Z",
        "appointmentStatus": "Requested",
    }
    assert data["locationId"] == BREWERY_STOP["location_id"]
    assert data["type"] == "CN"
    assert data["contact"]["name"] == "Pat"
    assert {r["type"] for r in data["reference"]} == {"SERVICE_LEVEL", "WEIGHT"}
    assert "notes" not in data  # notes are stored as their own source document
    assert "location" not in data  # identity lives on the facility row, not the snapshot


def test_structured_snapshot_round_trips_into_a_waypoint():
    wp = Waypoint.model_validate(
        stop("SH", status="Confirmed", close=False, **BREWERY_STOP)  # type: ignore[arg-type]
    )
    again = Waypoint.model_validate(json.loads(structured_snapshot(wp)))
    assert again.appointment_time is not None
    assert again.appointment_time.appointment_status == "Confirmed"
    assert again.appointment_time.close is None
    assert again.service_level == "Firm Appointment"
    assert again.resolved_location_id == BREWERY_STOP["location_id"]


def test_date_windows_and_observed_at():
    from datetime import date

    windows = list(date_windows(date(2026, 9, 1), date(2026, 9, 20), days=7))
    assert windows == [
        (date(2026, 9, 1), date(2026, 9, 7)),
        (date(2026, 9, 8), date(2026, 9, 14)),
        (date(2026, 9, 15), date(2026, 9, 20)),
    ]
    load = Load.model_validate(
        make_load(
            1,
            stop("SH", open_=None, close=None, **BREWERY_STOP),  # type: ignore[arg-type]
            stop("CN", open_="2026-09-11T12:00:00Z", **BREWERY_STOP),  # type: ignore[arg-type]
            created="2026-09-01T10:00:00Z",
        )
    )
    assert stop_observed_at(load, load.waypoints[0]) == load.created_at
    assert stop_observed_at(load, load.waypoints[1]).isoformat() == "2026-09-11T12:00:00+00:00"
