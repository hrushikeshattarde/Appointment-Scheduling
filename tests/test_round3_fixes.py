"""Round-3 fixes: record-backed verification, weak conflicts, withdrawal of unsupported values,
review-item closing, and the replay extractor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from facility_profiles.config import RunMode
from facility_profiles.domain.contacts import looks_like_person_name
from facility_profiles.domain.rules import Decision, Thresholds, decide
from facility_profiles.domain.schema import (
    Evidence,
    FacilityIdentity,
    FacilityProfile,
    ProfileField,
    Role,
    SourceType,
)
from facility_profiles.extraction.bundle import BundleBuilder
from facility_profiles.extraction.llm import ExtractionError, empty_result
from facility_profiles.extraction.replay import ReplayExtractor
from facility_profiles.pipeline.writer import ProfileWriter
from facility_profiles.storage.repository import unwrap

NOW = datetime(2026, 9, 28, tzinfo=UTC)


def _field(name, value, conf, *, conflict=False, source=SourceType.STOP_NOTE, mentions=2):
    return ProfileField(
        name=name,
        value=value,
        confidence=conf,
        mention_count=mentions,
        conflict=conflict,
        evidence=[Evidence(load_id=None, source_type=source, quote="q")],
    )


def test_record_backed_candidates_are_verified_without_a_mapped_existing_value():
    hours = _field(
        "receiving_hours",
        [{"days": ["mon"], "open": "07:00", "close": "15:00"}],
        0.45,
        source=SourceType.FACILITY_BUSINESS_HOURS,
    )
    assert decide(hours, record_backed=True).decision is Decision.VERIFY
    # a record-backed candidate that conflicts with other loads still goes to a person
    assert (
        decide(hours.model_copy(update={"conflict": True}), record_backed=True).decision
        is Decision.QUEUE
    )
    # not record-backed: the usual thresholds apply
    assert decide(hours).decision is Decision.DISCARD


def test_weak_conflict_without_a_record_value_is_discarded():
    weak = _field("contact_name", "Jami", 0.19, conflict=True)
    assert decide(weak).decision is Decision.DISCARD
    strong = _field("contact_name", "Jami", 0.55, conflict=True)
    assert decide(strong).decision is Decision.QUEUE
    # with a record value present, any conflict is worth a look
    assert decide(weak, existing_value="Pat Lee", existing_is_human=True).decision is Decision.QUEUE


def test_department_names_are_not_people():
    assert not looks_like_person_name("Track and Trace")
    assert not looks_like_person_name("Tracking Team")
    assert looks_like_person_name("Adrian Vinchery")


def test_unsupported_extracted_values_are_withdrawn_and_review_items_closed(repo):
    identity = FacilityIdentity(facility_id=9, company_name="Nine")
    repo.upsert_facility(identity)
    common = dict(
        mode=RunMode.RECOMMEND,
        thresholds=Thresholds(),
        stale_after_days=180,
        model_version="m",
        now=NOW,
    )
    first = ProfileWriter(repo, run_id="run-1", **common)
    queued = ProfileField(
        name="contact_email",
        value="x@y.com",
        confidence=0.6,
        mention_count=4,
        evidence=[Evidence(load_id=1, source_type=SourceType.STOP_CONTACT, quote="x@y.com")],
    )
    first.apply(
        FacilityProfile(identity=identity, role=Role.SHIPPER, fields={"contact_email": queued}),
        None,
    )
    assert repo.fields(identity.key, Role.SHIPPER)["contact_email"].state == "queued"
    assert repo.open_review_item(identity.key, Role.SHIPPER, "contact_email") is not None

    # next run: the source that produced the value is now filtered out -> no mentions
    second = ProfileWriter(repo, run_id="run-2", **common)
    stats = second.apply(FacilityProfile(identity=identity, role=Role.SHIPPER, fields={}), None)
    row = repo.fields(identity.key, Role.SHIPPER)["contact_email"]
    assert row.state == "discarded" and unwrap(row.value) is None
    assert stats.actions.get("withdraw") == 1
    assert repo.open_review_item(identity.key, Role.SHIPPER, "contact_email") is None
    closed = [
        i for i in repo.list_review_items(status="withdrawn") if i.facility_key == identity.key
    ]
    assert len(closed) == 1
    assert repo.audit_for(identity.key, field_name="contact_email")[0].action == "withdraw"

    # a verified value is left alone when a later run has nothing to say
    verified = ProfileField(
        name="booking_method",
        value="email",
        confidence=0.4,
        mention_count=1,
        evidence=[Evidence(load_id=None, source_type=SourceType.FACILITY_APPOINTMENTS, quote="m")],
    )
    ProfileWriter(repo, run_id="run-3", **common).apply(
        FacilityProfile(identity=identity, role=Role.SHIPPER, fields={"booking_method": verified}),
        None,
    )
    assert repo.fields(identity.key, Role.SHIPPER)["booking_method"].state == "verified"
    ProfileWriter(repo, run_id="run-4", **common).apply(
        FacilityProfile(identity=identity, role=Role.SHIPPER, fields={}), None
    )
    assert repo.fields(identity.key, Role.SHIPPER)["booking_method"].state == "verified"


def test_replay_extractor_returns_stored_output(repo, sessions):
    identity = FacilityIdentity(facility_id=3, company_name="Three")
    repo.upsert_facility(identity)
    raw = (
        empty_result()
        .model_copy(update={"scheduling_summary": "Email the desk."})
        .model_dump(mode="json")
    )
    repo.save_extraction(
        run_id="r",
        key=identity.key,
        role=Role.RECEIVER,
        model="anthropic/x",
        prompt_version="p1",
        request_id="req-1",
        input_tokens=5,
        output_tokens=1,
        cache_read_tokens=0,
        source_count=1,
        raw=raw,
        issues=[],
    )
    repo.session.commit()
    bundle = BundleBuilder(identity, Role.RECEIVER).build()
    out = ReplayExtractor(sessions).extract(bundle)
    assert out.result.scheduling_summary == "Email the desk."
    assert out.model == "anthropic/x" and out.request_id == "replay" and out.usage.input_tokens == 0
    with pytest.raises(ExtractionError, match="no stored extraction"):
        ReplayExtractor(sessions).extract(BundleBuilder(identity, Role.SHIPPER).build())
