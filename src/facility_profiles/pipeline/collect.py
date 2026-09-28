"""Collect sources for one facility and role (FR-3) and build the extractor's bundle.

Stop notes and structured stop fields were stored at harvest time. This stage adds what needs
extra API calls: the facility record itself (existing appointment fields, dispatch notes,
internal comments, business hours) and, for the most recent loads, load notes, tracking notes
and dispatch-level stop notes.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from facility_profiles.domain.contacts import InternalContacts
from facility_profiles.domain.normalize import html_to_text
from facility_profiles.domain.schema import FacilityIdentity, Role, SourceType
from facility_profiles.domain.scoring import Mention
from facility_profiles.extraction.bundle import BundleBuilder, ExistingValues, SourceBundle
from facility_profiles.extraction.derive import mentions_from_facility, mentions_from_stop
from facility_profiles.logging import get_logger
from facility_profiles.storage.models import FacilityRecord
from facility_profiles.storage.repository import Repository, as_utc
from facility_profiles.tpro.client import TransportProClient
from facility_profiles.tpro.errors import TransportProApiError
from facility_profiles.tpro.models import Facility, Waypoint

log = get_logger(__name__)

EXTRACTION_SOURCE_TYPES = {
    SourceType.STOP_NOTE,
    SourceType.FACILITY_APPOINTMENTS,
    SourceType.FACILITY_DISPATCH_NOTES,
    SourceType.FACILITY_INTERNAL_COMMENTS,
    SourceType.FACILITY_BUSINESS_HOURS,
    SourceType.LOAD_NOTE,
    SourceType.TRACKING_NOTE,
}


@dataclass
class CollectStats:
    """Counters for one collect pass."""

    facility_fetched: bool = False
    deep_loads: int = 0
    new_sources: int = 0
    api_errors: int = 0
    errors: list[str] = field(default_factory=list)


_APPOINTMENT_LABELS = {
    "method": "method",
    "contact": "contact",
    "email": "email",
    "phone": "phone",
    "portalURL": "portal URL",
    "notes": "notes",
}


def render_appointments(raw: str) -> str:
    """Render the stored appointments JSON as quotable ``label: value`` lines."""
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if not isinstance(data, dict):
        return raw
    lines = [f"{label}: {data[key]}" for key, label in _APPOINTMENT_LABELS.items() if data.get(key)]
    return "\n".join(lines) or raw


def existing_values_from_facility(facility: Facility) -> ExistingValues:
    """Snapshot of the appointment fields on the facility record."""
    appt = facility.appointments
    return ExistingValues(
        method=appt.method if appt else None,
        contact=appt.contact if appt else None,
        email=appt.email if appt else None,
        phone=appt.phone if appt else None,
        portal_url=appt.portal_url if appt else None,
        notes=appt.notes if appt else None,
        business_hours=facility.business_hours,
    )


def existing_values_from_record(record: FacilityRecord) -> ExistingValues | None:
    """Rebuild the snapshot stored on the facility row."""
    data = record.existing or {}
    if not data:
        return None
    return ExistingValues(**{k: data.get(k) for k in ExistingValues.__dataclass_fields__})


class Collector:
    """Fetch per-facility and per-load sources from Transport Pro and store them."""

    def __init__(
        self,
        client: TransportProClient | None,
        repo: Repository,
        *,
        deep_loads_per_facility: int = 15,
        fetch_dispatch_notes: bool = True,
    ) -> None:
        self._client = client
        self._repo = repo
        self._deep = deep_loads_per_facility
        self._dispatch_notes = fetch_dispatch_notes
        self._load_cache: dict[int, dict[str, Any]] = {}

    def collect(self, record: FacilityRecord, role: Role) -> CollectStats:
        """Fetch and store the sources that need API calls for one facility and role."""
        stats = CollectStats()
        if self._client is None:
            return stats
        if record.facility_id is not None:
            self._collect_facility_record(record, role, stats)
        for link in self._repo.load_links(record.key, role=role, limit=self._deep):
            self._collect_load(record.key, role, link.load_id, stats)
        return stats

    def _collect_facility_record(
        self, record: FacilityRecord, role: Role, stats: CollectStats
    ) -> None:
        assert self._client is not None
        assert record.facility_id is not None
        try:
            facility = self._client.get_facility(record.facility_id)
        except TransportProApiError as exc:
            stats.api_errors += 1
            stats.errors.append(f"facility {record.facility_id}: {exc}")
            log.warning("collect.facility_failed", facility_id=record.facility_id, error=str(exc))
            return
        stats.facility_fetched = True
        existing = existing_values_from_facility(facility)
        self._repo.set_existing(record.key, existing.__dict__)
        fetched_at = as_utc(record.existing_fetched_at)
        for source_type, text in (
            (SourceType.FACILITY_DISPATCH_NOTES, facility.dispatch_notes),
            (SourceType.FACILITY_INTERNAL_COMMENTS, facility.internal_comments),
            (SourceType.FACILITY_BUSINESS_HOURS, facility.business_hours),
        ):
            clean = html_to_text(text)
            if clean and self._repo.upsert_source(
                record.key,
                role=role,
                source_type=source_type,
                text=clean,
                load_id=None,
                observed_at=fetched_at,
            ):
                stats.new_sources += 1
        appt = facility.appointments
        if appt is not None and not appt.is_empty:
            snapshot = json.dumps(appt.model_dump(by_alias=True, exclude_none=True), sort_keys=True)
            if self._repo.upsert_source(
                record.key,
                role=role,
                source_type=SourceType.FACILITY_APPOINTMENTS,
                text=snapshot,
                load_id=None,
                observed_at=fetched_at,
            ):
                stats.new_sources += 1

    def _collect_load(self, key: str, role: Role, load_id: int, stats: CollectStats) -> None:
        assert self._client is not None
        stats.deep_loads += 1
        cached = self._load_cache.get(load_id)
        if cached is None:
            cached = self._fetch_load_sources(load_id, stats)
            self._load_cache[load_id] = cached
        for source_type, items in cached.items():
            for observed_at, text in items:
                if self._repo.upsert_source(
                    key,
                    role=role,
                    source_type=SourceType(source_type),
                    text=text,
                    load_id=load_id,
                    observed_at=observed_at,
                ):
                    stats.new_sources += 1

    def _fetch_load_sources(
        self, load_id: int, stats: CollectStats
    ) -> dict[str, list[tuple[datetime | None, str]]]:
        assert self._client is not None
        out: dict[str, list[tuple[datetime | None, str]]] = {
            SourceType.LOAD_NOTE.value: [],
            SourceType.TRACKING_NOTE.value: [],
        }
        try:
            for note in self._client.get_load_notes(load_id):
                if note.content:
                    out[SourceType.LOAD_NOTE.value].append((note.created_at, note.content))
        except TransportProApiError as exc:
            stats.api_errors += 1
            stats.errors.append(f"load notes {load_id}: {exc}")
        try:
            for tnote in self._client.get_tracking_load_notes(load_id):
                if tnote.comments and tnote.is_user_entered:
                    out[SourceType.TRACKING_NOTE.value].append((tnote.event_at, tnote.comments))
        except TransportProApiError as exc:
            stats.api_errors += 1
            stats.errors.append(f"tracking notes {load_id}: {exc}")
        if self._dispatch_notes:
            try:
                for dispatch in self._client.search_dispatches(load_id):
                    for dnote in self._client.get_dispatch_notes(dispatch.id):
                        if dnote.comments and dnote.is_user_entered:
                            out[SourceType.TRACKING_NOTE.value].append(
                                (dnote.event_at, dnote.comments)
                            )
            except TransportProApiError as exc:
                stats.api_errors += 1
                stats.errors.append(f"dispatch notes {load_id}: {exc}")
        return out


def identity_from_record(record: FacilityRecord, aliases: list[str]) -> FacilityIdentity:
    """Profile identity from the stored facility row."""
    return FacilityIdentity(
        facility_id=record.facility_id,
        candidate_key=record.candidate_key,
        company_name=record.company_name,
        address=record.address,
        city=record.city,
        state=record.state,
        postal_code=record.postal_code,
        iana_timezone=record.iana_timezone,
        aliases=aliases,
    )


def build_bundle(
    repo: Repository,
    record: FacilityRecord,
    role: Role,
    *,
    max_sources: int,
    max_loads: int,
) -> SourceBundle:
    """Assemble the extractor's bundle from stored sources, gating chatty note types."""
    alias_names = sorted(
        {
            a.company_name
            for a in repo.aliases(record.key)
            if a.company_name and a.company_name != record.company_name
        }
    )
    identity = identity_from_record(record, alias_names)
    builder = BundleBuilder(
        identity, role, existing=existing_values_from_record(record), max_sources=max_sources
    )
    recent_loads = {
        link.load_id for link in repo.load_links(record.key, role=role, limit=max_loads)
    }
    for doc in repo.sources(record.key, role):
        source_type = SourceType(doc.source_type)
        if source_type not in EXTRACTION_SOURCE_TYPES:
            continue
        if doc.load_id is not None and doc.load_id not in recent_loads:
            continue
        gate = source_type in {SourceType.LOAD_NOTE, SourceType.TRACKING_NOTE}
        builder.add(
            source_type,
            render_appointments(doc.text)
            if source_type is SourceType.FACILITY_APPOINTMENTS
            else doc.text,
            load_id=doc.load_id,
            observed_at=as_utc(doc.observed_at),
            gate=gate,
        )
    return builder.build()


def structured_mentions(
    repo: Repository,
    record: FacilityRecord,
    role: Role,
    *,
    max_loads: int,
    internal: InternalContacts | None = None,
    facility_names: list[str | None] | None = None,
) -> list[Mention]:
    """Rule-based mentions from stored stop snapshots and the facility appointment block."""
    mentions: list[Mention] = []
    recent_loads = {
        link.load_id for link in repo.load_links(record.key, role=role, limit=max_loads)
    }
    for doc in repo.sources(record.key, role):
        source_type = SourceType(doc.source_type)
        if source_type is SourceType.STOP_STRUCTURED and doc.load_id in recent_loads:
            try:
                waypoint = Waypoint.model_validate(json.loads(doc.text))
            except (ValueError, TypeError):
                continue
            mentions.extend(
                mentions_from_stop(
                    doc.load_id or 0,
                    as_utc(doc.observed_at),
                    waypoint,
                    internal=internal,
                    facility_names=facility_names or [record.company_name, record.city],
                )
            )
        elif source_type is SourceType.FACILITY_APPOINTMENTS and record.facility_id is not None:
            try:
                facility = Facility.model_validate(
                    {"id": record.facility_id, "appointments": json.loads(doc.text)}
                )
            except (ValueError, TypeError):
                continue
            mentions.extend(
                mentions_from_facility(facility, as_utc(doc.observed_at), internal=internal)
            )
    return mentions
