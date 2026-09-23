from datetime import UTC, datetime, timedelta

from facility_profiles.domain.rules import Decision, Thresholds, decide, format_hours, to_tpro_write
from facility_profiles.domain.schema import (
    FacilityIdentity,
    FacilityProfile,
    ProfileField,
    Role,
    SourceType,
)
from facility_profiles.domain.scoring import Mention, breadth, recency_weight, score_field

NOW = datetime(2026, 9, 22, tzinfo=UTC)


def m(value, load_id, days_ago=1, conf=1.0, field="booking_method"):
    return Mention(
        field_name=field,
        value=value,
        load_id=load_id,
        source_type=SourceType.STOP_NOTE,
        quote=f"quote {value}",
        observed_at=NOW - timedelta(days=days_ago),
        source_confidence=conf,
        normalized=value,
    )


def test_breadth_and_recency():
    assert breadth(0) == 0
    assert breadth(1) == 0.5
    assert breadth(2) == 0.75
    assert breadth(3) == 1.0
    assert breadth(9) == 1.0
    assert recency_weight(NOW, NOW, 90) == 1.0
    assert abs(recency_weight(NOW - timedelta(days=90), NOW, 90) - 0.5) < 1e-9
    assert recency_weight(None, NOW, 90) == 0.7


def test_three_agreeing_loads_reach_write_confidence():
    scored = score_field("booking_method", [m("email", 1), m("email", 2), m("email", 3)], now=NOW)
    assert scored.value == "email"
    assert scored.confidence >= 0.99
    assert scored.distinct_loads == 3
    assert not scored.conflict
    assert decide(scored).decision is Decision.WRITE


def test_single_load_is_queued_not_written():
    scored = score_field("booking_method", [m("email", 1)], now=NOW)
    assert scored.confidence == 0.5
    assert decide(scored).decision is Decision.QUEUE


def test_conflict_detected_when_second_value_has_material_support():
    mentions = [m("email", 1), m("email", 2), m("email", 3), m("phone", 4), m("phone", 5)]
    scored = score_field("booking_method", mentions, now=NOW, conflict_support=0.3)
    assert scored.value == "email"
    assert scored.conflict
    assert [c.value for c in scored.candidates] == ["email", "phone"]
    assert decide(scored).decision is Decision.QUEUE


def test_stale_minority_does_not_trigger_conflict():
    mentions = [m("email", 1), m("email", 2), m("email", 3), m("phone", 4, days_ago=400)]
    scored = score_field("booking_method", mentions, now=NOW)
    assert not scored.conflict


def test_low_confidence_is_discarded_and_no_mentions_skip():
    scored = score_field("booking_method", [m("email", 1, conf=0.5)], now=NOW)
    assert decide(scored).decision is Decision.DISCARD
    assert decide(ProfileField(name="booking_method")).decision is Decision.SKIP


def test_existing_human_value_is_verified_or_queued_never_overwritten():
    scored = score_field(
        "contact_phone", [m("217-555-0142", i, field="contact_phone") for i in (1, 2, 3)], now=NOW
    )
    verify = decide(scored, existing_value="(217) 555-0142", existing_is_human=True)
    assert verify.decision is Decision.VERIFY
    queue = decide(scored, existing_value="217-555-0000", existing_is_human=True)
    assert queue.decision is Decision.QUEUE


def test_thresholds_are_configurable():
    scored = score_field("booking_method", [m("email", 1), m("email", 2)], now=NOW)
    assert scored.confidence == 0.75
    assert decide(scored, thresholds=Thresholds(write=0.7, queue=0.5)).decision is Decision.WRITE


def test_to_tpro_write_maps_fields_and_formats_hours():
    profile = FacilityProfile(
        identity=FacilityIdentity(facility_id=1, company_name="X"),
        role=Role.SHIPPER,
        fields={
            "booking_method": ProfileField(name="booking_method", value="email"),
            "contact_email": ProfileField(name="contact_email", value="a@b.com"),
            "notice_period_hours": ProfileField(name="notice_period_hours", value=72),
            "time_granularity": ProfileField(name="time_granularity", value="exact"),
            "receiving_hours": ProfileField(
                name="receiving_hours",
                value=[
                    {
                        "days": ["mon", "tue", "wed", "thu", "fri"],
                        "open": "07:00",
                        "close": "14:30",
                        "by_appointment": False,
                    }
                ],
            ),
        },
        scheduling_summary="Email the shipping desk 72 hours ahead.",
    )
    write = to_tpro_write(profile)
    assert write.method == "Email Appointment"
    assert write.email == "a@b.com"
    assert write.business_hours == "0700-1430 MON-FRI"
    assert (
        write.notes
        == "Email the shipping desk 72 hours ahead. Notice period: 72 hours. Gives exact appointment times."
    )
    assert format_hours(None) is None
    assert (
        format_hours([{"days": ["sat"], "open": "08:00", "close": "12:00", "by_appointment": True}])
        == "0800-1200 SAT by appt"
    )
