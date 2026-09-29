"""Harvest facilities from load waypoints and resolve stops (FR-1, FR-2).

Loads are pulled by pickup-date windows for the configured terminals. Every stop is resolved
to a Transport Pro facility or a candidate key, linked to the load, and its note and
structured fields are stored as source documents for the collect stage.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from facility_profiles.domain.resolution import (
    FacilityResolver,
    KnownFacility,
    Resolution,
    ResolutionMethod,
    StopIdentity,
)
from facility_profiles.domain.schema import FacilityIdentity, Role, SourceType
from facility_profiles.logging import get_logger
from facility_profiles.storage.repository import Repository
from facility_profiles.tpro.client import TransportProClient
from facility_profiles.tpro.models import Load, Waypoint

log = get_logger(__name__)

WINDOW_DAYS = 7
STRUCTURED_FIELDS = {"type", "appointment_time", "contact", "reference", "location_id"}


@dataclass
class HarvestStats:
    """Counters for one harvest pass."""

    loads: int = 0
    stops: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    new_links: int = 0
    new_sources: int = 0

    def bump(self, method: ResolutionMethod) -> None:
        """Count a resolution outcome."""
        self.by_method[method.value] = self.by_method.get(method.value, 0) + 1

    def as_dict(self) -> dict[str, object]:
        """Plain dictionary for run statistics."""
        return {
            "loads": self.loads,
            "stops": self.stops,
            "by_method": dict(self.by_method),
            "new_links": self.new_links,
            "new_sources": self.new_sources,
        }


def date_windows(start: date, end: date, days: int = WINDOW_DAYS) -> Iterator[tuple[date, date]]:
    """Split ``[start, end]`` into inclusive windows of at most ``days`` days."""
    cursor = start
    while cursor <= end:
        window_end = min(cursor + timedelta(days=days - 1), end)
        yield cursor, window_end
        cursor = window_end + timedelta(days=1)


def stop_identity(waypoint: Waypoint) -> StopIdentity:
    """Identity fields of a stop."""
    loc = waypoint.location
    return StopIdentity(
        location_id=waypoint.resolved_location_id,
        company_name=loc.company_name if loc else None,
        address=loc.address if loc else None,
        city=loc.city if loc else None,
        state=loc.state if loc else None,
        postal_code=loc.postal_code if loc else None,
        latitude=loc.latitude if loc else None,
        longitude=loc.longitude if loc else None,
    )


def facility_identity(resolution: Resolution, waypoint: Waypoint) -> FacilityIdentity:
    """Identity of the facility a stop resolved to, seeded from the stop's own fields."""
    loc = waypoint.location
    return FacilityIdentity(
        facility_id=resolution.facility_id,
        candidate_key=resolution.candidate_key,
        company_name=loc.company_name if loc else None,
        address=loc.address if loc else None,
        city=loc.city if loc else None,
        state=loc.state if loc else None,
        postal_code=loc.postal_code if loc else None,
        iana_timezone=loc.iana_timezone if loc else None,
    )


def stop_observed_at(load: Load, waypoint: Waypoint) -> datetime | None:
    """When the stop happened (appointment open), falling back to load creation."""
    if waypoint.appointment_time and waypoint.appointment_time.open_at:
        return waypoint.appointment_time.open_at
    return load.created_at


def structured_snapshot(waypoint: Waypoint) -> str:
    """Compact JSON of the stop's structured fields for rule-based signals."""
    data = waypoint.model_dump(by_alias=True, include=STRUCTURED_FIELDS, exclude_none=True)
    return json.dumps(data, sort_keys=True, default=str)


def resolver_from_repo(
    repo: Repository, *, geo_match_meters: float, name_threshold: int
) -> FacilityResolver:
    """Seed a resolver with every linked facility already in the store."""
    known: list[KnownFacility] = []
    for record in repo.list_facilities(linked_only=True):
        if record.facility_id is None:
            continue
        aliases = {a.company_name for a in repo.aliases(record.key) if a.company_name}
        known.append(
            KnownFacility(
                facility_id=record.facility_id,
                company_name=record.company_name,
                address=record.address,
                city=record.city,
                state=record.state,
                postal_code=record.postal_code,
                latitude=record.latitude,
                longitude=record.longitude,
                aliases=aliases,
            )
        )
    return FacilityResolver(known, geo_match_meters=geo_match_meters, name_threshold=name_threshold)


class Harvester:
    """Turn loads into facilities, links and source documents."""

    def __init__(self, repo: Repository, resolver: FacilityResolver) -> None:
        self._repo = repo
        self._resolver = resolver
        self.stats = HarvestStats()

    def ingest_loads(self, loads: Iterable[Load]) -> HarvestStats:
        """Ingest an iterable of loads."""
        for load in loads:
            self.ingest_load(load)
        return self.stats

    def ingest_load(self, load: Load) -> None:
        """Resolve every stop on a load and store what it says about its facility."""
        self.stats.loads += 1
        for waypoint in load.waypoints:
            self._ingest_stop(load, waypoint)

    def _ingest_stop(self, load: Load, waypoint: Waypoint) -> None:
        self.stats.stops += 1
        identity = stop_identity(waypoint)
        if identity.location_id is None and not identity.company_name and not identity.address:
            return  # nothing to identify the stop by
        resolution = self._resolver.resolve(identity)
        self.stats.bump(resolution.method)
        fac_identity = facility_identity(resolution, waypoint)
        loc = waypoint.location
        self._repo.upsert_facility(
            fac_identity,
            latitude=loc.latitude if loc else None,
            longitude=loc.longitude if loc else None,
        )
        key = fac_identity.key
        self._repo.add_alias(
            key,
            company_name=fac_identity.company_name,
            address=fac_identity.address,
            city=fac_identity.city,
            state=fac_identity.state,
            postal_code=fac_identity.postal_code,
        )
        if resolution.facility_id is not None and loc is not None:
            self._resolver.add(
                KnownFacility(
                    facility_id=resolution.facility_id,
                    company_name=loc.company_name,
                    address=loc.address,
                    city=loc.city,
                    state=loc.state,
                    postal_code=loc.postal_code,
                    latitude=loc.latitude,
                    longitude=loc.longitude,
                )
            )

        role = Role(waypoint.role)
        observed_at = stop_observed_at(load, waypoint)
        if self._repo.link_load(
            key,
            load_id=load.id,
            role=role,
            stop_type=waypoint.type,
            terminal_id=load.assigned_terminal,
            method=resolution.method.value,
            score=resolution.score,
            observed_at=observed_at,
        ):
            self.stats.new_links += 1

        if waypoint.notes and self._repo.upsert_source(
            key,
            role=role,
            source_type=SourceType.STOP_NOTE,
            text=waypoint.notes,
            load_id=load.id,
            observed_at=observed_at,
        ):
            self.stats.new_sources += 1
        if self._repo.upsert_source(
            key,
            role=role,
            source_type=SourceType.STOP_STRUCTURED,
            text=structured_snapshot(waypoint),
            load_id=load.id,
            observed_at=observed_at,
        ):
            self.stats.new_sources += 1


def iter_terminal_loads(
    client: TransportProClient,
    *,
    terminal_ids: Iterable[int | None],
    start: date,
    end: date,
    customer_ids: Iterable[int | None] = (None,),
    extra_filters: dict[str, object] | None = None,
) -> Iterator[Load]:
    """Yield loads for each terminal, customer and date window.

    ``None`` in ``terminal_ids`` means every terminal; ``None`` in ``customer_ids`` means every
    customer. A pod that serves many customers can be scoped to one shipper this way (for
    example the Lidl inbound and outbound customer records on the Megan Goodwin pod).
    """
    customers = list(customer_ids) or [None]
    for terminal_id in terminal_ids:
        for customer_id in customers:
            for window_start, window_end in date_windows(start, end):
                filters: dict[str, object] = {
                    "pickup_date_start": window_start.isoformat(),
                    "pickup_date_end": window_end.isoformat(),
                    **(extra_filters or {}),
                }
                if terminal_id is not None:
                    filters["terminal_id"] = terminal_id
                if customer_id is not None:
                    filters["customer_id"] = customer_id
                log.info(
                    "harvest.window",
                    terminal=terminal_id,
                    customer=customer_id,
                    start=str(window_start),
                    end=str(window_end),
                )
                yield from client.iter_loads(**filters)
