"""For now the agent's job ends at the confirmed appointment: no after-booking to-dos by default."""

from __future__ import annotations

from datetime import timedelta

from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.service import list_cases, mark_booked, scan
from facility_profiles.booking.steps import SWITCHED_OFF
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.worklist import flag, open_kinds
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from tests.test_booking import NOW, lidl_load, seed_vendor
from tests.test_booking_gaps_37_38 import BOOKED, CANCELED, LOAD, Dispatched
from tests.test_booking_gaps_39_40 import _steps


def test_the_after_booking_checks_are_off_unless_switched_on(settings: Settings) -> None:
    assert Settings.model_fields["booking_watch_booked"].default is False


def test_a_booked_pickup_stays_booked_with_nothing_watched_or_raised(
    settings: Settings, sessions
) -> None:
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    _steps(sessions, seed_vendor(sessions))  # the facility sets the carrier a step
    tpro = Dispatched(lidl_load(LOAD, po="226321092660"))
    scan(tpro, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        mark_booked(s, case, by="megan", via="phone", local=BOOKED)
        case_id = case.id
        # Raised while the checks were on: closed once they are off.
        flag(s, case, ExceptionType.CARRIER_MISSING, "no carrier yet")
    tpro.dispatches = [CANCELED]  # the carrier dropped: nobody is told, the pickup stays booked
    stats = scan(tpro, sessions, settings, days_ahead=7, now=NOW + timedelta(hours=1))  # type: ignore[arg-type]
    assert stats.carriers_watched == 0 and stats.carrier_alerts == 0
    assert not [r for r in tpro.read if r.startswith("dispatches")]
    with session_scope(sessions) as s:
        swept = sweep(s, now=NOW + timedelta(hours=1), settings=settings)
        assert swept.counts()["raised"] == {}
        case = s.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        assert open_kinds(case) == []
        closed = next(e for e in case.exceptions if e.kind == "carrier_missing")
        assert closed.resolution == SWITCHED_OFF
