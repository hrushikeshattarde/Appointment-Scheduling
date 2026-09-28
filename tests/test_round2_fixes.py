"""Round-2 fixes: contact hygiene, verify-before-threshold, mixed granularity, record source,
link reassignment, extraction persistence."""

from __future__ import annotations

from datetime import UTC, datetime

from facility_profiles.domain.contacts import InternalContacts, looks_like_person_name
from facility_profiles.domain.rules import Decision, decide
from facility_profiles.domain.schema import (
    CandidateValue,
    Evidence,
    FacilityIdentity,
    ProfileField,
    Role,
    SourceType,
)
from facility_profiles.extraction.derive import mentions_from_stop
from facility_profiles.pipeline.collect import render_appointments
from facility_profiles.pipeline.profile import resolve_time_granularity
from facility_profiles.tpro.models import Waypoint
from tests.conftest import BREWERY_STOP, stop

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def test_internal_contacts_detection():
    internal = InternalContacts.build(["Example.org"], ["(312) 300-7447"]).with_phones(
        ["615-823-1937 ext 12", None]
    )
    assert internal.is_internal_email("RateCon@CircleDelivers.com")
    assert internal.is_internal_email("a@mail.example.org")
    assert not internal.is_internal_email("dana@northline.example")
    assert internal.is_internal_phone("260.208.4500 x2001")
    assert internal.is_internal_phone("312-300-7447 ext 8220")
    assert internal.is_internal_phone("6158231937")
    assert not internal.is_internal_phone("217-555-0142")
    assert not internal.is_internal_phone(None)


def test_person_name_heuristic():
    names = ["City Brewing Latrobe-Tarrs", "Tarrs"]
    assert looks_like_person_name("Carmen Bene", names)
    assert looks_like_person_name("Jesús Chávez", names)
    assert looks_like_person_name("Ramon", names)
    for bad in (
        "CBG Warehouse",
        "7335 Scheduling",
        "DATA DOCKS",
        "City Brewing",
        "RJW",
        "PBC Clackamas",
        "ratecon@circledelivers.com",
        "",
        None,
        "Shipping",
        "N/A",
    ):
        assert not looks_like_person_name(
            bad, [*names, "RJW Lockport IL - Prologis", "Clackamas"]
        ), bad


def test_stop_contacts_skip_internal_and_desk_labels():
    wp = Waypoint.model_validate(
        stop(
            "SH",
            contact={
                "name": "CBG Warehouse",
                "phone": "260-208-4500 x2001",
                "email": "ratecon@circledelivers.com",
                "fax": None,
            },
            **BREWERY_STOP,  # type: ignore[arg-type]
        )
    )
    fields = {
        m.field_name for m in mentions_from_stop(1, NOW, wp, facility_names=["Northline Brewing"])
    }
    assert "contact_phone" not in fields and "contact_email" not in fields
    assert "contact_name" not in fields
    wp2 = Waypoint.model_validate(
        stop(
            "SH",
            contact={"name": "Dana Rivers", "phone": "217-555-0142", "email": None, "fax": None},
            **BREWERY_STOP,
        )  # type: ignore[arg-type]
    )
    by = {m.field_name: m for m in mentions_from_stop(1, NOW, wp2)}
    assert by["contact_phone"].value == "217-555-0142"
    assert by["contact_name"].value == "Dana Rivers" and by["contact_name"].source_confidence == 0.5
    assert by["time_granularity"].source_confidence == 0.8


def _scored(name, value, conf, conflict=False, mentions=3):
    return ProfileField(
        name=name, value=value, confidence=conf, mention_count=mentions, conflict=conflict
    )


def test_agreement_with_record_is_verified_before_thresholds():
    low = _scored("portal_url", "https://booking.datadocks.com/x", 0.35)
    assert (
        decide(
            low, existing_value="https://Booking.DataDocks.com/x/", existing_is_human=True
        ).decision
        is Decision.VERIFY
    )
    weak_disagree = _scored("contact_email", "a@b.com", 0.35)
    assert (
        decide(weak_disagree, existing_value="c@d.com", existing_is_human=True).decision
        is Decision.DISCARD
    )
    strong_disagree = _scored("contact_email", "a@b.com", 0.6)
    assert (
        decide(strong_disagree, existing_value="c@d.com", existing_is_human=True).decision
        is Decision.QUEUE
    )


def test_informational_field_never_queues():
    assert decide(_scored("time_granularity", "exact", 0.64)).decision is Decision.WRITE
    assert decide(_scored("time_granularity", "exact", 0.4)).decision is Decision.DISCARD
    assert (
        decide(_scored("time_granularity", "exact", 0.6, conflict=True)).decision
        is Decision.DISCARD
    )
    assert decide(_scored("booking_method", "email", 0.64)).decision is Decision.QUEUE


def test_mixed_granularity_replaces_conflict():
    ev = Evidence(load_id=1, source_type=SourceType.APPOINTMENT_TIMES, quote="q")
    field = ProfileField(
        name="time_granularity",
        value="window",
        confidence=0.57,
        mention_count=36,
        conflict=True,
        candidates=[
            CandidateValue(value="window", support=0.63, distinct_loads=21, evidence=[ev]),
            CandidateValue(value="exact", support=0.37, distinct_loads=14, evidence=[ev]),
        ],
    )
    resolved = resolve_time_granularity(field)
    assert resolved.value == "mixed" and not resolved.conflict
    assert resolved.confidence == 0.8 and resolved.distinct_loads == 35
    untouched = resolve_time_granularity(field.model_copy(update={"name": "booking_method"}))
    assert untouched.conflict


def test_render_appointments_lines():
    text = render_appointments(
        '{"method": "Web Portal", "portalURL": "https://p.example", "email": null, "contact": ""}'
    )
    assert text == "method: Web Portal\nportal URL: https://p.example"
    assert render_appointments("not json") == "not json"


def test_link_moves_to_new_facility_and_repair_recounts(repo):
    a = FacilityIdentity(candidate_key="abc", company_name="A")
    b = FacilityIdentity(facility_id=77, company_name="A Real")
    repo.upsert_facility(a)
    repo.upsert_facility(b)
    assert repo.link_load(
        a.key,
        load_id=1,
        role=Role.RECEIVER,
        stop_type="CN",
        terminal_id=1,
        method="candidate",
        score=0,
        observed_at=NOW,
    )
    repo.upsert_source(
        a.key,
        role=Role.RECEIVER,
        source_type=SourceType.STOP_NOTE,
        text="FCFS",
        load_id=1,
        observed_at=NOW,
    )
    # the resolver later learns the real facility: the stop moves, it is not duplicated
    assert repo.link_load(
        b.key,
        load_id=1,
        role=Role.RECEIVER,
        stop_type="CN",
        terminal_id=1,
        method="address",
        score=100,
        observed_at=NOW,
    )
    assert repo.get_facility(a.key).stop_count == 0
    assert repo.get_facility(b.key).stop_count == 1
    assert [d.facility_key for d in repo.sources(b.key, Role.RECEIVER)] == [b.key]
    assert repo.sources(a.key, Role.RECEIVER) == []
    # same stop again for the same facility is a no-op
    assert not repo.link_load(
        b.key,
        load_id=1,
        role=Role.RECEIVER,
        stop_type="CN",
        terminal_id=1,
        method="address",
        score=100,
        observed_at=NOW,
    )
    # repair handles duplicates created before this rule existed
    repo.session.add(
        __import__(
            "facility_profiles.storage.models", fromlist=["FacilityLoadLink"]
        ).FacilityLoadLink(
            facility_key=a.key,
            load_id=1,
            role="receiver",
            stop_type="CN",
            resolution_method="candidate",
        )
    )
    repo.session.flush()
    removed, recounted = repo.repair_links()
    assert removed == 1 and recounted == 2
    assert repo.get_facility(b.key).stop_count == 1 and repo.get_facility(a.key).stop_count == 0


def test_extraction_persistence(repo):
    identity = FacilityIdentity(facility_id=5, company_name="X")
    repo.upsert_facility(identity)
    repo.save_extraction(
        run_id="r1",
        key=identity.key,
        role=Role.SHIPPER,
        model="m",
        prompt_version="p",
        request_id="req",
        input_tokens=10,
        output_tokens=2,
        cache_read_tokens=1,
        source_count=3,
        raw={"appointment_required": {"candidates": []}},
        issues=[{"field": "x", "reason": "y"}],
    )
    rows = repo.extractions_for(identity.key)
    assert len(rows) == 1 and rows[0].raw["appointment_required"] == {"candidates": []}
    assert rows[0].issues[0]["reason"] == "y"
