"""Assemble a scored profile from LLM and rule-based mentions, and rebuild one from storage."""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from facility_profiles.domain.schema import (
    PROFILE_FIELDS,
    CandidateValue,
    Evidence,
    FacilityIdentity,
    FacilityProfile,
    FieldState,
    ProfileField,
    Role,
    ValueOrigin,
)
from facility_profiles.domain.scoring import Mention, breadth, score_field
from facility_profiles.storage.models import ProfileFieldRecord, ProfileRecord
from facility_profiles.storage.repository import as_utc, unwrap

MIXED_CONFIDENCE = 0.8
_GRANULARITY_VALUES = {"exact", "window"}


def resolve_time_granularity(field: ProfileField) -> ProfileField:
    """A site that gives both exact times and windows is ``mixed``, not a conflict for a person."""
    if field.name != "time_granularity" or not field.conflict:
        return field
    values = {str(c.value) for c in field.candidates}
    if not values <= _GRANULARITY_VALUES:
        return field
    total_loads = sum(c.distinct_loads for c in field.candidates)
    evidence: list[Evidence] = []
    for candidate in field.candidates:
        evidence.extend(candidate.evidence[:3])
    return field.model_copy(
        update={
            "value": "mixed",
            "conflict": False,
            "confidence": round(breadth(total_loads) * MIXED_CONFIDENCE, 4),
            "distinct_loads": total_loads,
            "evidence": evidence,
        }
    )


def assemble_profile(
    identity: FacilityIdentity,
    role: Role,
    mentions: list[Mention],
    *,
    summary: str | None,
    run_id: str,
    model_version: str | None,
    source_load_ids: list[int],
    now: datetime,
    half_life_days: float,
    conflict_support: float,
) -> FacilityProfile:
    """Score every profile field from the pooled mentions."""
    by_field: dict[str, list[Mention]] = defaultdict(list)
    for mention in mentions:
        by_field[mention.field_name].append(mention)
    fields = {
        name: resolve_time_granularity(
            score_field(
                name,
                by_field.get(name, []),
                now=now,
                half_life_days=half_life_days,
                conflict_support=conflict_support,
            )
        )
        for name in PROFILE_FIELDS
    }
    return FacilityProfile(
        identity=identity,
        role=role,
        fields=fields,
        scheduling_summary=summary,
        run_id=run_id,
        model_version=model_version,
        updated_at=now,
        source_load_ids=source_load_ids,
    )


def profile_from_records(
    identity: FacilityIdentity,
    role: Role,
    records: dict[str, ProfileFieldRecord],
    summary: ProfileRecord | None,
) -> FacilityProfile:
    """Rebuild a profile from stored field rows (for lookup, export and digest)."""
    fields: dict[str, ProfileField] = {}
    for name, row in records.items():
        fields[name] = ProfileField(
            name=name,
            value=unwrap(row.value),
            confidence=row.confidence,
            support_count=row.support_count,
            mention_count=row.mention_count,
            distinct_loads=row.distinct_loads,
            conflict=row.conflict,
            evidence=[Evidence.model_validate(e) for e in row.evidence or []],
            candidates=[CandidateValue.model_validate(c) for c in row.candidates or []],
            origin=ValueOrigin(row.origin),
            state=FieldState(row.state),
            last_seen=as_utc(row.last_seen),
        )
    return FacilityProfile(
        identity=identity,
        role=role,
        fields=fields,
        scheduling_summary=summary.scheduling_summary if summary else None,
        run_id=summary.run_id if summary else None,
        model_version=summary.model_version if summary else None,
        updated_at=as_utc(summary.updated_at) if summary else None,
        source_load_ids=[int(i) for i in (summary.source_load_ids if summary else [])],
    )


def displayable_fields(profile: FacilityProfile) -> dict[str, ProfileField]:
    """Fields a reader should trust: written, verified or human-set."""
    return {
        name: fld
        for name, fld in profile.fields.items()
        if fld.state in {FieldState.WRITTEN, FieldState.VERIFIED, FieldState.HUMAN_SET}
        and fld.value is not None
    }
