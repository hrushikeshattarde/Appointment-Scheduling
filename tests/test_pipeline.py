from __future__ import annotations

import csv
from datetime import UTC, datetime
from pathlib import Path

from facility_profiles.config import RunMode
from facility_profiles.domain.schema import Candidate, FieldCandidates, Quote, Role, SourceType
from facility_profiles.extraction.bundle import SourceBundle
from facility_profiles.extraction.llm import ExtractionError, FakeExtractor, empty_result
from facility_profiles.pipeline.digest import render_digest
from facility_profiles.pipeline.export import export_profiles_csv
from facility_profiles.pipeline.run import Pipeline
from facility_profiles.review.queue import ReviewError, ReviewService
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, unwrap
from tests.conftest import NOW, FakeTPro, sample_facility, sample_loads


def scripted_extractor() -> FakeExtractor:
    """Return candidates backed by real quotes from whichever bundle arrives."""

    def script(bundle: SourceBundle):
        result = empty_result()
        notes = [s for s in bundle.sources if s.source_type is SourceType.STOP_NOTE]
        if not notes:
            return result
        note = notes[0]
        if bundle.role is Role.SHIPPER:
            return result.model_copy(
                update={
                    "booking_method": FieldCandidates(
                        candidates=[
                            Candidate(
                                value="email",
                                quotes=[
                                    Quote(
                                        source_id=note.source_id, text="Email appointment request"
                                    )
                                ],
                                confidence=0.95,
                            )
                        ]
                    ),
                    "contact_email": FieldCandidates(
                        candidates=[
                            Candidate(
                                value="shipping@northline.example",
                                quotes=[
                                    Quote(
                                        source_id=note.source_id, text="shipping@northline.example"
                                    )
                                ],
                                confidence=1.0,
                            )
                        ]
                    ),
                    "contact_phone": FieldCandidates(
                        candidates=[
                            Candidate(
                                value="217-555-0142",
                                quotes=[
                                    Quote(source_id=note.source_id, text="Dana Rivers 217-555-0142")
                                ],
                                confidence=1.0,
                            )
                        ]
                    ),
                    "notice_period_hours": FieldCandidates(
                        candidates=[
                            Candidate(
                                value="72",
                                quotes=[Quote(source_id=note.source_id, text="72 HOUR NOTICE")],
                                confidence=0.9,
                            )
                        ]
                    ),
                    "scheduling_summary": "Email the shipping desk 72 hours ahead.",
                }
            )
        return result.model_copy(
            update={
                "booking_method": FieldCandidates(
                    candidates=[
                        Candidate(
                            value="fcfs",
                            quotes=[Quote(source_id=note.source_id, text="Receiver is FCFS")],
                            confidence=0.95,
                        ),
                        Candidate(
                            value="phone",
                            quotes=[Quote(source_id=note.source_id, text="Driver must call ahead")],
                            confidence=0.9,
                        ),
                    ]
                ),
                "appointment_required": FieldCandidates(
                    candidates=[
                        Candidate(
                            value="false",
                            quotes=[Quote(source_id=note.source_id, text="Receiver is FCFS")],
                            confidence=0.9,
                        )
                    ]
                ),
            }
        )

    return FakeExtractor(script, model="scripted")


def test_end_to_end_run_writes_queues_and_audits(settings, sessions, tmp_path: Path):
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    pipeline = Pipeline(settings, sessions, client=client, extractor=scripted_extractor(), now=NOW)  # type: ignore[arg-type]

    report = pipeline.run()

    assert report.status == "completed"
    assert report.harvest["loads"] == 3 and report.harvest["stops"] == 6
    assert report.harvest["by_method"]["location_id"] == 3
    assert report.harvest["by_method"]["candidate"] >= 1
    assert report.profiles == 2
    assert report.extraction_errors == 0

    with session_scope(sessions) as session:
        repo = Repository(session)
        shipper = repo.find_facility(facility_id=900001)[0]
        assert shipper.stop_count == 3
        assert shipper.existing["method"] == "Email Appointment"
        fields = repo.fields(shipper.key, Role.SHIPPER)
        # Three loads agree -> written; the record already says Email Appointment -> verified instead.
        assert fields["booking_method"].state == "verified"
        assert unwrap(fields["booking_method"].value) == "email"
        assert fields["contact_email"].state == "verified"  # matches the record's email
        assert fields["contact_phone"].state == "written"
        assert unwrap(fields["contact_phone"].value) == "217-555-0142"
        assert fields["notice_period_hours"].state == "written"
        # Only structured flags (0.6) back this one -> queued for a person, not written.
        assert fields["appointment_required"].state == "queued"
        assert repo.profile(shipper.key, Role.SHIPPER).scheduling_summary.startswith(
            "Email the shipping desk"
        )

        # Receiver had no location ID: resolved to one candidate facility across all three loads,
        # including the differently spelled third stop.
        candidates = [f for f in repo.list_facilities() if f.facility_id is None]
        assert len(candidates) == 1
        receiver = candidates[0]
        assert receiver.stop_count == 3
        aliases = {a.company_name for a in repo.aliases(receiver.key)}
        assert "BLUEWATER DIST CTR" in aliases
        rfields = repo.fields(receiver.key, Role.RECEIVER)
        assert unwrap(rfields["booking_method"].value) == "fcfs"
        assert rfields["booking_method"].conflict  # phone had material support -> queued
        assert rfields["booking_method"].state == "queued"
        assert unwrap(rfields["appointment_required"].value) is False

        open_items = repo.list_review_items()
        assert any(
            i.field_name == "booking_method" and i.facility_key == receiver.key for i in open_items
        )
        actions = repo.action_counts(report.run_id)
        assert (
            actions.get("write", 0) >= 2
            and actions.get("verify", 0) >= 1
            and actions.get("queue", 0) >= 1
        )
        trail = repo.audit_for(shipper.key, field_name="contact_phone")
        assert trail[0].action == "write" and 1001 in trail[0].source_load_ids

        # Digest and export read the same store.
        digest = render_digest(repo, repo.latest_run(), now=NOW)
        assert "Needs a decision" in digest and "booking_method" in digest
        out = tmp_path / "export.csv"
        rows = export_profiles_csv(repo, out)
        assert rows == 1
        with out.open(encoding="utf-8") as fh:
            exported = list(csv.DictReader(fh))
        assert exported[0]["facility_id"] == "900001"
        assert exported[0]["appointments.phone"] == "217-555-0142"
        assert exported[0]["appointments.method"] == "Email Appointment"

    # A second run is idempotent: checkpoints are per run, but decisions do not duplicate rows.
    second = pipeline.run(do_harvest=False)
    assert second.profiles == 2
    with session_scope(sessions) as session:
        repo = Repository(session)
        assert len(repo.list_review_items()) == len(open_items)
        assert repo.get_facility(shipper.key).stop_count == 3


def test_review_decisions_become_human_set_and_are_respected(settings, sessions):
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    pipeline = Pipeline(settings, sessions, client=client, extractor=scripted_extractor(), now=NOW)  # type: ignore[arg-type]
    pipeline.run()

    with session_scope(sessions) as session:
        repo = Repository(session)
        service = ReviewService(repo)
        item = next(i for i in service.list_open() if i.field_name == "booking_method")
        service.edit(item.id, "phone", by="dana")
        fields = repo.fields(item.facility_key, Role(item.role))
        assert fields["booking_method"].state == "human_set"
        assert unwrap(fields["booking_method"].value) == "phone"
        try:
            service.accept(item.id, by="dana")
        except ReviewError as exc:
            assert "already edited" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("second decision should fail")
        try:
            service.edit(item.id, "nonsense", by="dana")
        except ReviewError:
            pass

    # The next run must not overwrite the human value; the extractor still says fcfs -> queue again.
    pipeline.run(do_harvest=False)
    with session_scope(sessions) as session:
        repo = Repository(session)
        key = item.facility_key
        fields = repo.fields(key, Role.RECEIVER)
        assert unwrap(fields["booking_method"].value) == "phone"
        assert fields["booking_method"].origin == "human"
        assert fields["booking_method"].state == "queued"
        assert repo.open_review_item(key, Role.RECEIVER, "booking_method") is not None


def test_recommend_mode_records_recommendations_and_extraction_errors(settings, sessions):
    settings = settings.model_copy(update={"mode": RunMode.RECOMMEND})
    client = FakeTPro(sample_loads(), {900001: sample_facility()})

    class Failing:
        model = "failing"

        def extract(self, bundle):
            if bundle.role is Role.RECEIVER:
                raise ExtractionError("boom", retryable=False)
            return scripted_extractor().extract(bundle)

    pipeline = Pipeline(settings, sessions, client=client, extractor=Failing(), now=NOW)  # type: ignore[arg-type]
    report = pipeline.run()
    assert report.extraction_errors == 1
    assert report.profiles == 1
    with session_scope(sessions) as session:
        repo = Repository(session)
        actions = repo.action_counts(report.run_id)
        assert actions.get("recommend", 0) >= 1 and "write" not in actions
        assert actions.get("extraction_error") == 1
        run = repo.get_run(report.run_id)
        assert run.status == "completed" and run.stats["extraction_errors"] == 1


def test_refresh_only_skips_unchanged_and_resume_skips_applied(settings, sessions):
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    extractor = scripted_extractor()
    pipeline = Pipeline(settings, sessions, client=client, extractor=extractor, now=NOW)  # type: ignore[arg-type]
    first = pipeline.run()
    calls_after_first = len(extractor.calls)

    refreshed = pipeline.run(do_harvest=False, refresh_only=True)
    assert refreshed.skipped_unchanged == 2
    assert len(extractor.calls) == calls_after_first

    resumed = pipeline.run(do_harvest=False, run_id=first.run_id)
    assert resumed.run_id == first.run_id
    assert resumed.profiles == 0  # everything already checkpointed in that run


def test_offline_run_without_client_uses_stored_sources(settings, sessions):
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    Pipeline(settings, sessions, client=client, extractor=scripted_extractor(), now=NOW).harvest()  # type: ignore[arg-type]
    offline = Pipeline(settings, sessions, client=None, extractor=scripted_extractor(), now=NOW)
    report = offline.run(do_harvest=False)
    assert report.profiles == 2
    assert datetime.now(tz=UTC) > NOW
