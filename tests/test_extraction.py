from datetime import UTC, datetime

from facility_profiles.domain.schema import (
    Candidate,
    ExtractionResult,
    FacilityIdentity,
    FieldCandidates,
    HoursCandidate,
    HoursSpan,
    Quote,
    Role,
    SourceType,
    Weekday,
)
from facility_profiles.extraction.bundle import BundleBuilder, ExistingValues
from facility_profiles.extraction.derive import (
    booking_method_from_tpro,
    mentions_from_facility,
    mentions_from_stop,
)
from facility_profiles.extraction.llm import FakeExtractor, empty_result
from facility_profiles.extraction.prompts import SYSTEM_PROMPT, render_user_message
from facility_profiles.extraction.validate import coerce, validate_and_convert
from facility_profiles.tpro.models import Facility, Waypoint
from tests.conftest import BREWERY_NOTE, sample_facility, stop

NOW = datetime(2026, 9, 22, tzinfo=UTC)
IDENTITY = FacilityIdentity(
    facility_id=900001, company_name="Northline Brewing", city="Springfield", state="IL"
)


def build_bundle():
    builder = BundleBuilder(
        IDENTITY, Role.SHIPPER, existing=ExistingValues(method="Email Appointment")
    )
    builder.add(SourceType.STOP_NOTE, BREWERY_NOTE, load_id=1001, observed_at=NOW)
    builder.add(
        SourceType.STOP_NOTE, BREWERY_NOTE.replace("<br/>", "<br>"), load_id=1002, observed_at=NOW
    )  # duplicate text
    builder.add(SourceType.STOP_NOTE, BREWERY_NOTE, load_id=1003, observed_at=NOW)  # duplicate text
    builder.add(
        SourceType.LOAD_NOTE, "MACROPOINT UPDATE: LOADED", load_id=1001, gate=True
    )  # filtered
    builder.add(SourceType.LOAD_NOTE, "Appt requested via email", load_id=1001, gate=True)
    builder.add(SourceType.FACILITY_BUSINESS_HOURS, "0600-2000 M-F")
    return builder.build()


def test_bundle_dedupes_gates_and_tags_sources():
    bundle = build_bundle()
    types = [s.source_type for s in bundle.sources]
    assert types[0] is SourceType.FACILITY_BUSINESS_HOURS  # facility sources first
    assert [s.source_id for s in bundle.sources] == ["S1", "S2", "S3"]
    note = next(s for s in bundle.sources if s.source_type is SourceType.STOP_NOTE)
    assert note.repeat_load_ids == [1002, 1003]
    assert note.credited_load_ids == [1001, 1002, 1003]  # repeats capped at three loads
    assert bundle.load_ids == [1001, 1002, 1003]
    message = render_user_message(bundle)
    assert "[S2] stop_note | load 1001" in message and "same text on 2 more load(s)" in message
    assert "method: Email Appointment" in message
    assert "Role on these loads: shipper" in message
    assert "verbatim" in SYSTEM_PROMPT


def test_validate_keeps_backed_quotes_and_drops_invented_contacts():
    bundle = build_bundle()
    note_id = next(s.source_id for s in bundle.sources if s.source_type is SourceType.STOP_NOTE)
    result = empty_result().model_copy(
        update={
            "booking_method": FieldCandidates(
                candidates=[
                    Candidate(
                        value="email",
                        quotes=[Quote(source_id=note_id, text="Email appointment request")],
                        confidence=0.9,
                    ),
                    Candidate(
                        value="phone",
                        quotes=[Quote(source_id=note_id, text="text that is not there")],
                        confidence=0.9,
                    ),
                    Candidate(
                        value="carrier pigeon",
                        quotes=[Quote(source_id=note_id, text="72 HOUR NOTICE")],
                        confidence=0.9,
                    ),
                ]
            ),
            "contact_phone": FieldCandidates(
                candidates=[
                    Candidate(
                        value="217-555-0142",
                        quotes=[Quote(source_id=note_id, text="Dana Rivers 217-555-0142")],
                        confidence=1.0,
                    ),
                    Candidate(
                        value="800-555-9999",
                        quotes=[Quote(source_id=note_id, text="Dana Rivers 217-555-0142")],
                        confidence=1.0,
                    ),
                ]
            ),
            "notice_period_hours": FieldCandidates(
                candidates=[
                    Candidate(
                        value="72",
                        quotes=[Quote(source_id=note_id, text="72 HOUR NOTICE")],
                        confidence=0.95,
                    )
                ]
            ),
            "receiving_hours": [
                HoursCandidate(
                    spans=[
                        HoursSpan(
                            days=[Weekday.MON, Weekday.FRI],
                            open="06:00",
                            close="20:00",
                            by_appointment=False,
                        )
                    ],
                    quotes=[Quote(source_id="S1", text="0600-2000 M-F")],
                    confidence=0.9,
                ),
                HoursCandidate(
                    spans=[
                        HoursSpan(
                            days=[Weekday.MON], open="25:00", close="20:00", by_appointment=False
                        )
                    ],
                    quotes=[Quote(source_id="S1", text="0600-2000 M-F")],
                    confidence=0.9,
                ),
            ],
            "scheduling_summary": "Email the shipping desk 72 hours ahead.",
        }
    )
    mentions, issues = validate_and_convert(result, bundle)
    assert {m.value for m in mentions["booking_method"]} == {"email"}
    assert len(mentions["booking_method"]) == 3  # credited to three loads
    assert {m.value for m in mentions["contact_phone"]} == {"217-555-0142"}
    assert mentions["notice_period_hours"][0].value == 72
    assert mentions["receiving_hours"][0].value[0]["days"] == ["mon", "fri"]
    reasons = {(i.field, i.reason) for i in issues}
    assert ("booking_method", "quote not found in source") in reasons
    assert ("booking_method", "value not valid for field") in reasons
    assert ("contact_phone", "value not present verbatim in source") in reasons
    assert ("receiving_hours", "invalid hours span") in reasons


def test_coerce_types():
    assert coerce("appointment_required", "True") is True
    assert coerce("appointment_required", "maybe") is None
    assert coerce("booking_method", "unknown") is None
    assert coerce("notice_period_hours", "48 hours") == 48
    assert coerce("contact_email", "Bob <BOB@X.COM>") == "bob@x.com"
    assert coerce("portal_url", "see https://Portal.Example/a/") == "https://portal.example/a"
    assert coerce("contact_name", "  Dana Rivers ") == "Dana Rivers"
    assert coerce("time_granularity", "WINDOW") == "window"


def test_fake_extractor_records_calls():
    extractor = FakeExtractor(empty_result(), model="fake")
    out = extractor.extract(build_bundle())
    assert isinstance(out.result, ExtractionResult)
    assert out.model_version == "fake@" + out.prompt_version
    assert len(extractor.calls) == 1


def test_derived_mentions_from_stop_and_facility():
    wp = Waypoint.model_validate(
        stop(
            "SH",
            location_id=1,
            name="X",
            address="1 A St",
            city="C",
            state="IL",
            postal="60000",
            lat=0,
            lon=0,
            status="Confirmed",
            open_="2026-09-10T13:00:00Z",
            close="2026-09-10T13:00:00Z",
            service_level="Firm Appointment",
            contact={"name": "Pat", "phone": None, "email": "801.565.6175", "fax": None},
        )
    )
    mentions = mentions_from_stop(42, NOW, wp)
    by_field = {}
    for m in mentions:
        by_field.setdefault(m.field_name, []).append(m.value)
    assert by_field["appointment_required"] == [True, True]  # status + service level
    assert by_field["time_granularity"] == ["exact"]
    assert by_field["contact_phone"] == ["801-565-6175"]  # phone stored in the email field
    assert by_field["contact_name"] == ["Pat"]

    fcfs = Waypoint.model_validate(
        stop(
            "CN",
            location_id=None,
            name="Y",
            address="2 B St",
            city="C",
            state="IL",
            postal="60000",
            lat=0,
            lon=0,
            status="Not Required",
            close=False,
            service_level="Flexible / FCFS",
        )
    )
    values = {(m.field_name, m.value) for m in mentions_from_stop(43, NOW, fcfs)}
    assert ("booking_method", "fcfs") in values and ("appointment_required", False) in values

    facility = Facility.model_validate(sample_facility())
    fac = {(m.field_name, m.value) for m in mentions_from_facility(facility, NOW)}
    assert ("booking_method", "email") in fac
    assert ("contact_email", "shipping@northline.example") in fac
    assert ("appointment_required", True) in fac

    misplaced = Facility.model_validate(
        {"id": 2, "appointments": {"method": "Web Portal", "portalURL": "appts@dc.example"}}
    )
    fac2 = {(m.field_name, m.value) for m in mentions_from_facility(misplaced, NOW)}
    assert ("contact_email", "appts@dc.example") in fac2
    assert booking_method_from_tpro("Phone Appointment").value == "phone"
    assert booking_method_from_tpro(None) is None
