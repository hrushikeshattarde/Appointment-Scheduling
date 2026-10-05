"""Shared fixtures: settings, in-memory store, synthetic Transport Pro payloads, fake client."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.config import RunMode, Settings
from facility_profiles.storage.db import init_db, make_engine, session_factory
from facility_profiles.storage.repository import Repository
from facility_profiles.tpro.models import (
    Dispatch,
    Facility,
    Load,
    LoadNote,
    Terminal,
    TrackingNote,
)

NOW = datetime(2026, 9, 22, 12, 0, tzinfo=UTC)


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        TPRO_BASE_URL="https://tpro.test",
        TPRO_USERNAME="api-user",
        TPRO_PASSWORD="secret",
        database_url="sqlite://",
        pilot_terminal_ids=[1160],
        mode=RunMode.WRITE,
        lookback_days=90,
    )


@pytest.fixture
def sessions() -> sessionmaker[Session]:
    engine = make_engine("sqlite://")
    init_db(engine)
    return session_factory(engine)


@pytest.fixture
def session(sessions: sessionmaker[Session]) -> Iterator[Session]:
    s = sessions()
    try:
        yield s
        s.commit()
    finally:
        s.close()


@pytest.fixture
def repo(session: Session) -> Repository:
    return Repository(session)


# --------------------------------------------------------------------------- payload builders


def stop(
    kind: str,
    *,
    location_id: int | None,
    name: str,
    address: str,
    city: str,
    state: str,
    postal: str,
    lat: float,
    lon: float,
    status: str | None = "Confirmed",
    open_: str | None = "2026-09-10T13:00:00Z",
    close: Any = "2026-09-10T15:00:00Z",
    notes: str | None = None,
    service_level: str | None = "Firm Appointment",
    contact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    refs: list[dict[str, Any]] = [{"type": "WEIGHT", "value": 40000}]
    if service_level:
        refs.insert(0, {"type": "SERVICE_LEVEL", "value": service_level})
    return {
        "id": None,
        "type": kind,
        "stopoff": False,
        "locationId": location_id,
        "location": {
            "locationId": location_id,
            "companyName": name,
            "address": address,
            "address2": None,
            "city": city,
            "state": state,
            "postalCode": postal,
            "countryCode": "USA",
            "latitude": lat,
            "longitude": lon,
            "timezone": -5,
            "ianaTimezone": "America/Chicago",
        },
        "appointmentTime": {"open": open_, "close": close, "appointmentStatus": status},
        "contact": contact or {"name": None, "phone": None, "email": None, "fax": None},
        "notes": notes,
        "reference": refs,
    }


def make_load(
    load_id: int,
    shipper: dict[str, Any],
    consignee: dict[str, Any],
    *,
    terminal: int = 1160,
    created: str = "2026-09-01T10:00:00Z",
) -> dict[str, Any]:
    return {
        "id": load_id,
        "dateCreated": created,
        "lastUpdated": "2026-09-11T20:46:53Z",
        "internalContacts": [
            {"type": "ORDERTAKER", "id": 11},
            {"type": "CARRIERSALESREP", "id": 22},
        ],
        "assignedTerminal": terminal,
        "waypoints": [shipper, consignee],
        "status": {
            "loadStatus": "Delivered",
            "documentStatus": "Documents Received",
            "billingStatus": "Billed",
        },
        "postingInfo": {"isPosted": False},
        "reference": {"equipmentType": "Van", "miles": 500},
        "billingInfo": {"customerId": 5, "customer": {"id": 5, "companyName": "Acme Beverages"}},
    }


BREWERY_STOP = dict(
    location_id=900001,
    name="Northline Brewing - Springfield",
    address="1001 Technology Drive",
    city="Springfield",
    state="IL",
    postal="62701",
    lat=39.7817,
    lon=-89.6501,
)
BREWERY_NOTE = (
    "LOAD REQUIRES: STRAPS<br/>Email appointment request to shipping@northline.example 72 HOUR NOTICE<br/>"
    "Shipping hours 0600-2000 M-F<br/>Contact Dana Rivers 217-555-0142"
)

DC_STOP = dict(
    location_id=None,
    name="Bluewater Distribution Center",
    address="500 Commerce Pkwy",
    city="Dayton",
    state="OH",
    postal="45402",
    lat=39.7589,
    lon=-84.1916,
)
DC_NOTE = "Receiver is FCFS 0700-1430 MON-FRI. Driver must call ahead 937-555-0199."


def sample_loads() -> list[dict[str, Any]]:
    """Three loads sharing the same shipper (with a location ID) and receiver (without)."""
    loads = []
    for i, (load_id, created) in enumerate(
        [
            (1001, "2026-08-20T10:00:00Z"),
            (1002, "2026-09-01T10:00:00Z"),
            (1003, "2026-09-10T10:00:00Z"),
        ]
    ):
        sh = stop(
            "SH",
            notes=BREWERY_NOTE,
            open_=f"2026-09-1{i}T13:00:00Z",
            close=f"2026-09-1{i}T13:00:00Z",
            **BREWERY_STOP,
        )  # type: ignore[arg-type]
        cn_name = "Bluewater Distribution Center" if i < 2 else "BLUEWATER DIST CTR"
        cn = stop(
            "CN",
            notes=DC_NOTE,
            status="Not Required",
            service_level="Flexible / FCFS",
            open_=f"2026-09-1{i + 1}T12:00:00Z",
            close=False,
            **{**DC_STOP, "name": cn_name},  # type: ignore[arg-type]
        )
        loads.append(make_load(load_id, sh, cn, created=created))
    return loads


def sample_facility() -> dict[str, Any]:
    return {
        "id": 900001,
        "companyName": "Northline Brewing - Springfield",
        "locationCode": None,
        "location": {
            "address": "1001 Technology Drive",
            "city": "Springfield",
            "state": "IL",
            "postalCode": "62701",
            "countryCode": "USA",
            "latitude": 39.7817,
            "longitude": -89.6501,
            "iana_timezone": "America/Chicago",
        },
        "appointments": {
            "method": "Email Appointment",
            "contact": None,
            "email": "shipping@northline.example",
            "phone": None,
            "portalURL": None,
            "notes": None,
        },
        "businessHours": "0600-2000 M-F",
        "internalComments": "<b>P# USED FOR CHECK IN</b>",
        "dispatchNotes": "SHIPPER SPEED LIMIT 15 MPH",
    }


class FakeTPro:
    """In-memory stand-in for TransportProClient used by pipeline tests."""

    def __init__(self, loads: list[dict[str, Any]], facilities: dict[int, dict[str, Any]]) -> None:
        self._loads = [Load.model_validate(item) for item in loads]
        self._facilities = {k: Facility.model_validate(v) for k, v in facilities.items()}
        self.calls: list[str] = []

    def iter_loads(self, **filters: Any) -> Iterator[Load]:
        """Loads in the pickup window. Like Transport Pro, a canceled load only when asked for
        with ``load_status``."""
        self.calls.append(f"iter_loads {sorted(filters)}")
        start = filters.get("pickup_date_start")
        end = filters.get("pickup_date_end")
        wanted = str(filters.get("load_status") or "").lower()
        for load in self._loads:
            status = ((load.status.load_status if load.status else None) or "").lower()
            if (wanted and status != wanted) or (not wanted and status == "canceled"):
                continue
            first = load.waypoints[0].appointment_time if load.waypoints else None
            pickup = (first.open or "")[:10] if first else ""
            if (start and pickup < start) or (end and pickup > end):
                continue
            yield load

    def get_facility(self, location_id: int) -> Facility:
        self.calls.append(f"get_facility {location_id}")
        return self._facilities[location_id]

    def get_load_notes(self, load_id: int) -> list[LoadNote]:
        self.calls.append(f"get_load_notes {load_id}")
        return [
            LoadNote.model_validate(
                {
                    "id": 1,
                    "controlId": load_id,
                    "recordType": "dispatch",
                    "dateCreated": "2026-09-02T18:22:45Z",
                    "createdBy": 4275,
                    "content": "Carrier Rep Assignment Change: Someone (Bot Change)",
                }
            ),
            LoadNote.model_validate(
                {
                    "id": 2,
                    "controlId": load_id,
                    "recordType": "dispatch",
                    "dateCreated": "2026-09-03T:13:02:44Z",
                    "createdBy": 4275,
                    "content": "Appt requested via email for 09/12 with Dana at shipper",
                }
            ),
        ]

    def get_tracking_load_notes(self, load_id: int) -> list[TrackingNote]:
        self.calls.append(f"get_tracking_load_notes {load_id}")
        return [
            TrackingNote.model_validate(
                {
                    "id": 3,
                    "eventDate": "2026-09-05T22:03:36Z",
                    "dataSource": "Macropoint",
                    "loadId": load_id,
                    "comments": None,
                    "location": {"state": False, "countryCode": False},
                }
            ),
            TrackingNote.model_validate(
                {
                    "id": 4,
                    "eventDate": "2026-09-05T22:04:12Z",
                    "dataSource": "User Entered",
                    "loadId": load_id,
                    "comments": "Happyrobot Added Note: Email Received: Carrier confirms loaded.",
                }
            ),
        ]

    def search_dispatches(self, load_id: int) -> list[Dispatch]:
        self.calls.append(f"search_dispatches {load_id}")
        return [
            Dispatch.model_validate(
                {"id": 5000 + load_id, "loadId": load_id, "status": "Delivered"}
            )
        ]

    def get_dispatch_notes(self, dispatch_id: int) -> list[TrackingNote]:
        self.calls.append(f"get_dispatch_notes {dispatch_id}")
        return []

    def list_terminals(self) -> list[Terminal]:
        self.calls.append("list_terminals")
        return [
            Terminal.model_validate(
                {
                    "id": 1160,
                    "title": "POD (X)",
                    "phoneNumbers": [{"type": "MAIN", "value": "555-010-0100"}],
                }
            )
        ]

    def close(self) -> None:
        return None
