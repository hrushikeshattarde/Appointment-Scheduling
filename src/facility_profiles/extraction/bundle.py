"""Source bundles: everything Circle knows about one facility, ready for extraction (FR-3).

The builder deduplicates identical text (facility dispatch notes are copied onto every stop),
keeps only scheduling-related load and tracking notes, caps size, and tags each source with a
stable ``S<n>`` ID that the model's quotes must reference.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from facility_profiles.domain.normalize import html_to_text, looks_scheduling_related, squash
from facility_profiles.domain.schema import FacilityIdentity, Role, SourceType

MAX_SOURCE_CHARS = 1_500
DEFAULT_MAX_SOURCES = 120
# Identical text copied onto many loads earns corroboration credit for at most this many
# distinct loads. Three matches the write threshold; staleness is handled by recency decay.
REPEAT_LOAD_CREDIT = 3


@dataclass
class SourceDoc:
    """One piece of source text with its provenance."""

    source_id: str
    source_type: SourceType
    text: str
    load_id: int | None = None
    observed_at: datetime | None = None
    role: Role | None = None
    repeat_load_ids: list[int] = field(default_factory=list)

    @property
    def credited_load_ids(self) -> list[int | None]:
        """Load IDs that earn corroboration credit for this text (capped for repeats)."""
        ids: list[int] = []
        if self.load_id is not None:
            ids.append(self.load_id)
        for extra in self.repeat_load_ids:
            if extra not in ids:
                ids.append(extra)
        if not ids:
            return [None]
        return list(ids[:REPEAT_LOAD_CREDIT])


@dataclass(frozen=True)
class ExistingValues:
    """Appointment fields already on the Transport Pro facility record."""

    method: str | None = None
    contact: str | None = None
    email: str | None = None
    phone: str | None = None
    portal_url: str | None = None
    notes: str | None = None
    business_hours: str | None = None

    def is_empty(self) -> bool:
        """True when no appointment field is filled."""
        return not any(
            (
                self.method,
                self.contact,
                self.email,
                self.phone,
                self.portal_url,
                self.notes,
                self.business_hours,
            )
        )


@dataclass
class SourceBundle:
    """The extractor's input for one facility and role."""

    identity: FacilityIdentity
    role: Role
    sources: list[SourceDoc]
    existing: ExistingValues | None = None

    def source(self, source_id: str) -> SourceDoc | None:
        """Look up a source by its tag."""
        wanted = source_id.strip().upper()
        return next((s for s in self.sources if s.source_id == wanted), None)

    @property
    def load_ids(self) -> list[int]:
        """Every load that contributed text, most recent first."""
        seen: dict[int, None] = {}
        for doc in self.sources:
            for load_id in [doc.load_id, *doc.repeat_load_ids]:
                if load_id is not None:
                    seen.setdefault(load_id, None)
        return list(seen)

    def all_text(self) -> str:
        """Concatenated source text, used for whole-bundle checks."""
        return "\n".join(doc.text for doc in self.sources)


class BundleBuilder:
    """Accumulate sources for one facility and role, then build a capped, deduplicated bundle."""

    def __init__(
        self,
        identity: FacilityIdentity,
        role: Role,
        *,
        existing: ExistingValues | None = None,
        max_sources: int = DEFAULT_MAX_SOURCES,
        max_source_chars: int = MAX_SOURCE_CHARS,
    ) -> None:
        self._identity = identity
        self._role = role
        self._existing = existing
        self._max_sources = max_sources
        self._max_chars = max_source_chars
        self._docs: list[SourceDoc] = []
        self._by_text: dict[tuple[SourceType, str], SourceDoc] = {}

    def add(
        self,
        source_type: SourceType,
        text: str | None,
        *,
        load_id: int | None = None,
        observed_at: datetime | None = None,
        gate: bool = False,
    ) -> bool:
        """Add a source; False when empty, filtered out, or merged into a duplicate."""
        clean = html_to_text(text)
        if not clean:
            return False
        if gate and not looks_scheduling_related(clean):
            return False
        if len(clean) > self._max_chars:
            clean = clean[: self._max_chars].rstrip() + " […]"
        key = (source_type, squash(clean))
        existing = self._by_text.get(key)
        if existing is not None:
            if load_id is not None and load_id != existing.load_id:
                existing.repeat_load_ids.append(load_id)
            if observed_at and (existing.observed_at is None or observed_at > existing.observed_at):
                existing.observed_at = observed_at
            return False
        doc = SourceDoc(
            source_id="",
            source_type=source_type,
            text=clean,
            load_id=load_id,
            observed_at=observed_at,
            role=self._role,
        )
        self._docs.append(doc)
        self._by_text[key] = doc
        return True

    def add_many(
        self,
        source_type: SourceType,
        items: Iterable[tuple[int | None, datetime | None, str | None]],
        *,
        gate: bool = False,
    ) -> int:
        """Add several ``(load_id, observed_at, text)`` triples; returns how many were kept."""
        return sum(
            1
            for load_id, observed_at, text in items
            if self.add(source_type, text, load_id=load_id, observed_at=observed_at, gate=gate)
        )

    def build(self) -> SourceBundle:
        """Order sources (facility record first, then newest loads), cap, and tag them."""

        def sort_key(doc: SourceDoc) -> tuple[int, float]:
            is_facility = doc.source_type.value.startswith("facility_")
            when = doc.observed_at.timestamp() if doc.observed_at else 0.0
            return (0 if is_facility else 1, -when)

        ordered = sorted(self._docs, key=sort_key)[: self._max_sources]
        for index, doc in enumerate(ordered, start=1):
            doc.source_id = f"S{index}"
        return SourceBundle(
            identity=self._identity, role=self._role, sources=ordered, existing=self._existing
        )
