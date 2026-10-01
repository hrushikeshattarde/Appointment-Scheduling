"""Assemble a scored profile from LLM and rule-based mentions, and rebuild one from storage."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select

from facility_profiles.domain.normalize import portal_vendor_from_url, url_host
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
from facility_profiles.storage.repository import Repository, as_utc, unwrap, wrap

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


def resolve_portal_vendor(fields: dict[str, ProfileField]) -> dict[str, ProfileField]:
    """A portal URL names its scheduling system, so it settles ``portal_vendor``.

    The notes may call a Costco or UNFI portal "other" (or the wrong vendor); the URL's host is
    certain. The vendor takes the URL's support and evidence; what the notes said stays among
    the candidates. Two different URLs are left for a person.
    """
    url = fields.get("portal_url")
    if url is None or url.value is None or url.conflict:
        return fields
    vendor = portal_vendor_from_url(str(url.value))
    current = fields.get("portal_vendor") or ProfileField(name="portal_vendor")
    if vendor is None or (current.value == vendor and not current.conflict):
        return fields
    evidence = url.evidence[:3]
    resolved = current.model_copy(
        update={
            "value": vendor,
            "conflict": False,
            "confidence": url.confidence,
            "support_count": url.support_count,
            "mention_count": url.mention_count,
            "distinct_loads": url.distinct_loads,
            "last_seen": url.last_seen,
            "evidence": evidence,
            "candidates": [
                CandidateValue(
                    value=vendor,
                    support=url.confidence,
                    distinct_loads=url.distinct_loads,
                    evidence=evidence,
                ),
                *(c for c in current.candidates if c.value != vendor),
            ],
        }
    )
    return {**fields, "portal_vendor": resolved}


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
    fields = resolve_portal_vendor(fields)
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


# ------------------------------------------------------------------ stores filed before the URL


@dataclass(frozen=True)
class PortalFix:
    """A stored portal vendor that its portal URL contradicts."""

    key: str
    role: Role
    url: str
    before: str | None
    after: str
    held: bool  # a person set the vendor: reported, never changed


_UNTRUSTED_URL_STATES = {FieldState.DISCARDED.value, FieldState.REJECTED.value}


def portal_vendor_fixes(repo: Repository) -> list[PortalFix]:
    """Profiles whose portal vendor is missing, "other" or wrong for the portal URL on file."""
    rows = repo.session.scalars(
        select(ProfileFieldRecord)
        .where(ProfileFieldRecord.field_name == "portal_url")
        .order_by(ProfileFieldRecord.facility_key, ProfileFieldRecord.role)
    )
    fixes: list[PortalFix] = []
    for row in rows:
        url = unwrap(row.value)
        vendor = portal_vendor_from_url(str(url)) if url else None
        if vendor is None or row.state in _UNTRUSTED_URL_STATES:
            continue
        role = Role(row.role)
        current = repo.fields(row.facility_key, role).get("portal_vendor")
        before = unwrap(current.value) if current is not None else None
        if before == vendor:
            continue
        held = current is not None and current.origin == ValueOrigin.HUMAN.value
        fixes.append(PortalFix(row.facility_key, role, str(url), before, vendor, held))
    return fixes


def apply_portal_fixes(repo: Repository, fixes: list[PortalFix], *, by: str) -> int:
    """File the URL's vendor where no person set one; return how many profiles changed.

    The vendor keeps the state its field had (a verified "other" becomes a verified "costco"),
    or takes the URL's state when it had none. An open review of the old value is superseded.
    """
    changed = 0
    for fix in fixes:
        if fix.held:
            continue
        stored = repo.fields(fix.key, fix.role)
        row = stored.get("portal_vendor")
        url_row = stored["portal_url"]
        if row is None:
            row = ProfileFieldRecord(
                facility_key=fix.key,
                role=fix.role.value,
                field_name="portal_vendor",
                state=url_row.state,
                origin=url_row.origin,
                confidence=url_row.confidence,
                evidence=list(url_row.evidence or []),
            )
            repo.session.add(row)
        row.value = wrap(fix.after)
        row.conflict = False
        repo.close_review_items(fix.key, fix.role, "portal_vendor", status="superseded")
        repo.audit(
            run_id=None,
            key=fix.key,
            role=fix.role,
            field_name="portal_vendor",
            action="refine",
            before=fix.before,
            after=fix.after,
            reason=f"portal URL host {url_host(fix.url)} is {fix.after}",
            actor=by,
        )
        changed += 1
    repo.session.flush()
    return changed
