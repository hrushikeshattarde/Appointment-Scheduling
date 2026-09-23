from datetime import UTC, datetime

import pytest

from facility_profiles.extraction.derive import mentions_from_stop
from facility_profiles.tpro.models import Waypoint
from tests.conftest import BREWERY_STOP, stop

NOW = datetime(2026, 9, 23, tzinfo=UTC)


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("Confirmed", True),
        ("Requested", True),
        ("Appointment Required", True),
        ("Appt Required - Carrier Request", True),  # seen live on terminal 1160, 23 Sep 2026
        ("Not Required", False),
        ("", None),
        (None, None),
    ],
)
def test_stop_status_maps_to_appointment_required(status, expected):
    wp = Waypoint.model_validate(
        stop("CN", status=status, service_level=None, **BREWERY_STOP)  # type: ignore[arg-type]
    )
    values = [
        m.value for m in mentions_from_stop(1, NOW, wp) if m.field_name == "appointment_required"
    ]
    assert (values[0] if values else None) is expected
