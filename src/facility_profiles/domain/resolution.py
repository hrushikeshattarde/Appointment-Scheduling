"""Stop-to-facility resolution (FR-2).

Most stops in Transport Pro carry no location ID (62% in the September 2026 sample). This
module matches such stops to a known facility on normalised company name, street address,
postal code and coordinates, and otherwise assigns a stable candidate key so the same
unlinked site is grouped across loads.
"""

from __future__ import annotations

import hashlib
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum

from rapidfuzz import fuzz

from facility_profiles.domain.normalize import (
    haversine_m,
    normalize_address,
    normalize_company_name,
    normalize_postal,
)


class ResolutionMethod(StrEnum):
    """How a stop was tied to a facility."""

    LOCATION_ID = "location_id"
    ADDRESS = "address"
    GEO_NAME = "geo_name"
    NAME_CITY = "name_city"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class StopIdentity:
    """The identifying fields of a stop, as read from a waypoint."""

    location_id: int | None
    company_name: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None

    @property
    def name_norm(self) -> str:
        """Normalised company name."""
        return normalize_company_name(self.company_name)

    @property
    def address_norm(self) -> str:
        """Normalised street address."""
        return normalize_address(self.address)

    @property
    def postal5(self) -> str:
        """Normalised postal code."""
        return normalize_postal(self.postal_code)

    @property
    def city_norm(self) -> str:
        """Lower-cased city."""
        return (self.city or "").strip().lower()

    def candidate_key(self) -> str:
        """Stable key for an unlinked site.

        Street address plus postal code when both are present (so spelling variants of the
        name at one address group together), otherwise name, city, state and postal code.
        """
        if self.address_norm and self.postal5:
            raw = "|".join(("addr", self.address_norm, self.postal5))
        else:
            raw = "|".join(
                ("name", self.name_norm, self.city_norm, (self.state or "").upper(), self.postal5)
            )
        return hashlib.sha1(raw.encode()).hexdigest()[:16]  # noqa: S324 - not security


@dataclass
class KnownFacility:
    """A Transport Pro facility the resolver can match against."""

    facility_id: int
    company_name: str | None
    address: str | None
    city: str | None
    state: str | None
    postal_code: str | None
    latitude: float | None
    longitude: float | None
    aliases: set[str] = field(default_factory=set)

    @property
    def name_norm(self) -> str:
        """Normalised company name."""
        return normalize_company_name(self.company_name)

    @property
    def address_norm(self) -> str:
        """Normalised street address."""
        return normalize_address(self.address)

    @property
    def postal5(self) -> str:
        """Normalised postal code."""
        return normalize_postal(self.postal_code)

    @property
    def city_norm(self) -> str:
        """Lower-cased city."""
        return (self.city or "").strip().lower()

    def name_scores(self, name_norm: str) -> int:
        """Best fuzzy score of ``name_norm`` against the facility name and its aliases."""
        names = {self.name_norm, *(normalize_company_name(a) for a in self.aliases)}
        names.discard("")
        if not names or not name_norm:
            return 0
        return max(int(fuzz.token_set_ratio(name_norm, n)) for n in names)


@dataclass(frozen=True)
class Resolution:
    """Outcome of resolving one stop."""

    facility_id: int | None
    candidate_key: str | None
    method: ResolutionMethod
    score: int

    @property
    def key(self) -> str:
        """Profile grouping key."""
        if self.facility_id is not None:
            return f"tpro:{self.facility_id}"
        return f"candidate:{self.candidate_key}"


class FacilityResolver:
    """Match stops to known facilities; hand out candidate keys for the rest."""

    def __init__(
        self,
        known: Iterable[KnownFacility] = (),
        *,
        geo_match_meters: float = 200.0,
        name_threshold: int = 92,
        geo_name_threshold: int = 70,
    ) -> None:
        self._by_id: dict[int, KnownFacility] = {}
        self._by_postal: dict[str, list[KnownFacility]] = defaultdict(list)
        self._by_city: dict[tuple[str, str], list[KnownFacility]] = defaultdict(list)
        self._geo_m = geo_match_meters
        self._name_threshold = name_threshold
        self._geo_name_threshold = geo_name_threshold
        for facility in known:
            self.add(facility)

    def add(self, facility: KnownFacility) -> None:
        """Register (or replace) a known facility."""
        self._by_id[facility.facility_id] = facility
        if facility.postal5:
            self._by_postal[facility.postal5].append(facility)
        if facility.city_norm:
            self._by_city[(facility.city_norm, (facility.state or "").upper())].append(facility)

    def __len__(self) -> int:
        return len(self._by_id)

    def resolve(self, stop: StopIdentity) -> Resolution:
        """Resolve a stop to a facility ID or a candidate key."""
        if stop.location_id is not None:
            return Resolution(stop.location_id, None, ResolutionMethod.LOCATION_ID, 100)

        pool = list(self._by_postal.get(stop.postal5, [])) if stop.postal5 else []
        if stop.city_norm:
            for candidate in self._by_city.get((stop.city_norm, (stop.state or "").upper()), []):
                if candidate not in pool:
                    pool.append(candidate)

        best: tuple[int, ResolutionMethod, KnownFacility] | None = None
        for facility in pool:
            outcome = self._match(stop, facility)
            if outcome and (best is None or outcome[0] > best[0]):
                best = (outcome[0], outcome[1], facility)

        if best is not None:
            score, method, facility = best
            return Resolution(facility.facility_id, None, method, score)
        return Resolution(None, stop.candidate_key(), ResolutionMethod.CANDIDATE, 0)

    def _match(
        self, stop: StopIdentity, facility: KnownFacility
    ) -> tuple[int, ResolutionMethod] | None:
        same_postal = bool(stop.postal5) and stop.postal5 == facility.postal5
        name_score = facility.name_scores(stop.name_norm)

        if same_postal and stop.address_norm and stop.address_norm == facility.address_norm:
            return (100, ResolutionMethod.ADDRESS)

        if (
            stop.latitude is not None
            and stop.longitude is not None
            and facility.latitude is not None
            and facility.longitude is not None
        ):
            distance = haversine_m(
                stop.latitude, stop.longitude, facility.latitude, facility.longitude
            )
            if distance <= self._geo_m and name_score >= self._geo_name_threshold:
                return (max(name_score, 90), ResolutionMethod.GEO_NAME)

        same_city = bool(stop.city_norm) and stop.city_norm == facility.city_norm
        if (same_postal or same_city) and name_score >= self._name_threshold:
            return (name_score, ResolutionMethod.NAME_CITY)
        return None
