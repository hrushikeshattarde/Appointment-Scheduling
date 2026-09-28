"""Apply the write policy to a scored profile and persist the outcome (FR-9, FR-10, FR-14, FR-15).

In ``recommend`` mode a field that qualifies for writing is stored as a recommendation and
audited as such; in ``write`` mode it is marked written. Transport Pro has no facility write
endpoint today, so "written" means authoritative in the staging store, exported for the
vendor bulk import; a :class:`FacilityWriteAdapter` is the seam for a real endpoint later.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from facility_profiles.config import RunMode
from facility_profiles.domain.normalize import normalize_email, normalize_phone, normalize_url
from facility_profiles.domain.rules import Decision, Thresholds, decide, to_tpro_write
from facility_profiles.domain.schema import (
    PROFILE_FIELDS,
    FacilityProfile,
    FieldState,
    ProfileField,
    ValueOrigin,
)
from facility_profiles.extraction.bundle import ExistingValues
from facility_profiles.extraction.derive import booking_method_from_tpro
from facility_profiles.logging import get_logger
from facility_profiles.storage.models import ProfileFieldRecord
from facility_profiles.storage.repository import Repository, as_utc, unwrap

log = get_logger(__name__)

HUMAN_ORIGINS = {ValueOrigin.HUMAN.value, ValueOrigin.VERIFIED.value}
LIVE_STATES = {FieldState.WRITTEN.value, FieldState.EXTRACTED.value}


class FacilityWriteAdapter(Protocol):
    """Pushes a profile's Transport Pro fields to the facility record."""

    def write(self, facility_id: int, fields: dict[str, str | None]) -> None:
        """Write the given facility fields."""
        ...


class NullWriteAdapter:
    """No vendor endpoint exists yet; log what would have been written."""

    def write(self, facility_id: int, fields: dict[str, str | None]) -> None:
        """Log only."""
        log.info("tpro.facility_write.skipped", facility_id=facility_id, fields=fields)


def existing_profile_values(existing: ExistingValues | None) -> dict[str, Any]:
    """Map the facility record's appointment fields onto profile field names."""
    if existing is None:
        return {}
    values: dict[str, Any] = {}
    method = booking_method_from_tpro(existing.method)
    if method:
        values["booking_method"] = method.value
    if existing.contact:
        values["contact_name"] = existing.contact.strip()
    if phone := normalize_phone(existing.phone):
        values["contact_phone"] = phone
    if email := normalize_email(existing.email):
        values["contact_email"] = email
    if url := normalize_url(existing.portal_url):
        values["portal_url"] = url
    return values


@dataclass
class ApplyStats:
    """Actions taken for one profile."""

    actions: dict[str, int] = field(default_factory=dict)

    def bump(self, action: str) -> None:
        """Count an action."""
        self.actions[action] = self.actions.get(action, 0) + 1

    def merge(self, other: ApplyStats) -> None:
        """Add another profile's counts."""
        for action, count in other.actions.items():
            self.actions[action] = self.actions.get(action, 0) + count


class ProfileWriter:
    """Decide and persist every field of a profile."""

    def __init__(
        self,
        repo: Repository,
        *,
        mode: RunMode,
        thresholds: Thresholds,
        stale_after_days: int,
        run_id: str,
        model_version: str | None,
        now: datetime,
        adapter: FacilityWriteAdapter | None = None,
    ) -> None:
        self._repo = repo
        self._mode = mode
        self._thresholds = thresholds
        self._stale_after = timedelta(days=stale_after_days)
        self._run_id = run_id
        self._model_version = model_version
        self._now = now
        self._adapter = adapter or NullWriteAdapter()

    def apply(self, profile: FacilityProfile, existing: ExistingValues | None) -> ApplyStats:
        """Apply the policy to each field and store the profile summary."""
        stats = ApplyStats()
        key = profile.identity.key
        stored = self._repo.fields(key, profile.role)
        record_values = existing_profile_values(existing)
        written_any = False

        for name in PROFILE_FIELDS:
            scored = profile.fields.get(name) or ProfileField(name=name)
            previous = stored.get(name)
            action = self._apply_field(profile, scored, previous, record_values.get(name))
            stats.bump(action)
            written_any = written_any or action == "write"

        self._repo.upsert_profile(
            key,
            profile.role,
            summary=profile.scheduling_summary,
            run_id=self._run_id,
            model_version=self._model_version,
            source_load_ids=profile.source_load_ids,
            source_count=len(profile.source_load_ids),
        )
        if written_any and profile.identity.facility_id is not None:
            self._adapter.write(profile.identity.facility_id, to_tpro_write(profile).as_dict())
        return stats

    def _apply_field(
        self,
        profile: FacilityProfile,
        scored: ProfileField,
        previous: ProfileFieldRecord | None,
        record_value: Any,
    ) -> str:
        key = profile.identity.key
        role = profile.role
        human_previous = previous is not None and previous.origin in HUMAN_ORIGINS
        existing_value = (
            unwrap(previous.value) if previous is not None and human_previous else record_value
        )
        existing_is_human = human_previous or record_value is not None
        record_backed = bool(scored.evidence) and all(
            e.source_type.value.startswith("facility_") for e in scored.evidence
        )
        ruling = decide(
            scored,
            existing_value=existing_value,
            existing_is_human=existing_is_human,
            thresholds=self._thresholds,
            record_backed=record_backed,
        )
        load_ids = [e.load_id for e in scored.evidence if e.load_id is not None]
        before = unwrap(previous.value) if previous is not None else None

        def audit(action: str, after: Any, reason: str) -> None:
            self._repo.audit(
                run_id=self._run_id,
                key=key,
                role=role,
                field_name=scored.name,
                action=action,
                before=before,
                after=after,
                confidence=scored.confidence,
                source_load_ids=load_ids,
                model_version=self._model_version,
                reason=reason,
            )

        if ruling.decision is Decision.SKIP:
            return self._handle_skip(profile, scored, previous, before, audit)

        if ruling.decision is Decision.VERIFY:
            self._repo.upsert_field(
                key,
                role,
                scored,
                state=FieldState.VERIFIED,
                origin=ValueOrigin.VERIFIED,
                run_id=self._run_id,
                value=existing_value,
            )
            audit("verify", existing_value, ruling.reason)
            return "verify"

        if ruling.decision is Decision.QUEUE:
            self._repo.upsert_field(
                key,
                role,
                scored,
                state=FieldState.QUEUED,
                origin=ValueOrigin.HUMAN if human_previous else ValueOrigin.EXTRACTED,
                run_id=self._run_id,
                keep_value=human_previous,
            )
            self._repo.queue(
                key,
                role,
                scored,
                existing_value=existing_value,
                reason=ruling.reason,
                run_id=self._run_id,
            )
            audit("queue", scored.value, ruling.reason)
            return "queue"

        if ruling.decision is Decision.DISCARD:
            self._repo.upsert_field(
                key,
                role,
                scored,
                state=FieldState.DISCARDED,
                origin=ValueOrigin.EXTRACTED,
                run_id=self._run_id,
                keep_value=human_previous,
            )
            audit("discard", scored.value, ruling.reason)
            return "discard"

        # Decision.WRITE
        if self._mode is RunMode.WRITE:
            self._repo.upsert_field(
                key,
                role,
                scored,
                state=FieldState.WRITTEN,
                origin=ValueOrigin.EXTRACTED,
                run_id=self._run_id,
            )
            audit("write", scored.value, ruling.reason)
            return "write"
        self._repo.upsert_field(
            key,
            role,
            scored,
            state=FieldState.EXTRACTED,
            origin=ValueOrigin.EXTRACTED,
            run_id=self._run_id,
        )
        audit("recommend", scored.value, f"recommend-only mode; {ruling.reason}")
        return "recommend"

    def _handle_skip(
        self,
        profile: FacilityProfile,
        scored: ProfileField,
        previous: ProfileFieldRecord | None,
        before: Any,
        audit: Any,
    ) -> str:
        """No mentions this run: keep human and verified values, withdraw or age the rest."""
        if previous is None or previous.origin in HUMAN_ORIGINS:
            return "skip"
        key, role = profile.identity.key, profile.role
        if previous.state in {FieldState.QUEUED.value, FieldState.EXTRACTED.value}:
            # The value came from sources that no longer support it (for example an internal
            # contact now filtered out). It was never trusted, so it is withdrawn outright.
            withdrawn = ProfileField(name=scored.name, value=None, confidence=0.0)
            self._repo.upsert_field(
                key,
                role,
                withdrawn,
                state=FieldState.DISCARDED,
                origin=ValueOrigin.EXTRACTED,
                run_id=self._run_id,
            )
            self._repo.close_review_items(key, role, scored.name, status="withdrawn")
            audit("withdraw", None, "no source supports the earlier value any more")
            return "withdraw"
        if previous.state != FieldState.WRITTEN.value:
            return "skip"
        last_seen = as_utc(previous.last_seen)
        if last_seen is not None and self._now - last_seen > self._stale_after:
            stale = ProfileField(
                name=scored.name,
                value=before,
                confidence=max(previous.confidence * 0.5, 0.0),
                support_count=previous.support_count,
                mention_count=previous.mention_count,
                distinct_loads=previous.distinct_loads,
                last_seen=last_seen,
            )
            self._repo.upsert_field(
                key,
                role,
                stale,
                state=FieldState.STALE,
                origin=ValueOrigin(previous.origin),
                run_id=self._run_id,
                keep_value=True,
            )
            self._repo.queue(
                key,
                role,
                stale,
                existing_value=before,
                reason=f"no supporting mention for {self._stale_after.days} days",
                run_id=self._run_id,
            )
            audit("stale", before, "value not seen in any source within the stale window")
            return "stale"
        return "skip"
