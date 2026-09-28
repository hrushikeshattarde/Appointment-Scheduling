"""Repository: the only place that touches the ORM.

Values are stored wrapped as ``{"v": value}`` so JSON columns can hold ``null``, booleans and
lists uniformly on SQLite and Postgres.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from facility_profiles.domain.schema import (
    FacilityIdentity,
    FieldState,
    ProfileField,
    Role,
    SourceType,
    ValueOrigin,
)
from facility_profiles.storage.models import (
    AuditEntry,
    Checkpoint,
    ExtractionRecord,
    FacilityAlias,
    FacilityLoadLink,
    FacilityRecord,
    ProfileFieldRecord,
    ProfileRecord,
    ReviewItem,
    Run,
    SourceDocument,
    utcnow,
)


def wrap(value: Any) -> dict[str, Any]:
    """Wrap a value for a JSON column."""
    return {"v": value}


def unwrap(blob: dict[str, Any] | None) -> Any:
    """Unwrap a value from a JSON column."""
    return None if not blob else blob.get("v")


def as_utc(value: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive timestamps as UTC."""
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def text_hash(text: str) -> str:
    """Stable hash for deduplicating source text."""
    return hashlib.sha1(text.strip().lower().encode()).hexdigest()  # noqa: S324 - not security


class Repository:
    """Data access for one session."""

    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ runs

    def create_run(self, mode: str, terminal_ids: Sequence[int], model_version: str | None) -> Run:
        """Insert a new run row."""
        run = Run(
            id=str(uuid.uuid4()),
            mode=mode,
            terminal_ids=",".join(str(t) for t in terminal_ids),
            model_version=model_version,
            stats={},
        )
        self.session.add(run)
        self.session.flush()
        return run

    def finish_run(
        self, run_id: str, *, status: str, stats: dict[str, Any], error: str | None = None
    ) -> None:
        """Close a run with its final status and statistics."""
        run = self.session.get(Run, run_id)
        if run is None:
            return
        run.status = status
        run.stats = stats
        run.error = error
        run.finished_at = utcnow()

    def get_run(self, run_id: str) -> Run | None:
        """Fetch a run."""
        return self.session.get(Run, run_id)

    def latest_run(self) -> Run | None:
        """Most recently started run."""
        return self.session.scalars(select(Run).order_by(Run.started_at.desc()).limit(1)).first()

    # ------------------------------------------------------------------ facilities

    def upsert_facility(
        self,
        identity: FacilityIdentity,
        *,
        latitude: float | None = None,
        longitude: float | None = None,
    ) -> FacilityRecord:
        """Insert or refresh a facility row from its identity."""
        record = self.session.get(FacilityRecord, identity.key)
        if record is None:
            record = FacilityRecord(
                key=identity.key,
                facility_id=identity.facility_id,
                candidate_key=identity.candidate_key,
                existing={},
            )
            self.session.add(record)
        for attr in ("company_name", "address", "city", "state", "postal_code", "iana_timezone"):
            value = getattr(identity, attr)
            if value and not getattr(record, attr):
                setattr(record, attr, value)
        if latitude is not None and record.latitude is None:
            record.latitude = latitude
        if longitude is not None and record.longitude is None:
            record.longitude = longitude
        self.session.flush()
        return record

    def set_existing(
        self, key: str, existing: dict[str, Any], fetched_at: datetime | None = None
    ) -> None:
        """Store the snapshot of the Transport Pro facility record's appointment fields."""
        record = self.session.get(FacilityRecord, key)
        if record is None:
            return
        record.existing = existing
        record.existing_fetched_at = fetched_at or utcnow()

    def get_facility(self, key: str) -> FacilityRecord | None:
        """Fetch a facility by key."""
        return self.session.get(FacilityRecord, key)

    def find_facility(
        self, *, facility_id: int | None = None, name: str | None = None
    ) -> list[FacilityRecord]:
        """Look up facilities by Transport Pro ID or a name fragment."""
        stmt = select(FacilityRecord)
        if facility_id is not None:
            stmt = stmt.where(FacilityRecord.facility_id == facility_id)
        if name:
            stmt = stmt.where(func.lower(FacilityRecord.company_name).like(f"%{name.lower()}%"))
        return list(self.session.scalars(stmt.order_by(FacilityRecord.stop_count.desc()).limit(50)))

    def list_facilities(
        self, *, limit: int | None = None, linked_only: bool = False
    ) -> list[FacilityRecord]:
        """Facilities by descending stop count."""
        stmt = select(FacilityRecord).order_by(FacilityRecord.stop_count.desc())
        if linked_only:
            stmt = stmt.where(FacilityRecord.facility_id.is_not(None))
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def add_alias(
        self,
        key: str,
        *,
        company_name: str | None,
        address: str | None,
        city: str | None,
        state: str | None,
        postal_code: str | None,
    ) -> None:
        """Record a name and address the facility appeared under."""
        stmt = select(FacilityAlias).where(
            FacilityAlias.facility_key == key,
            FacilityAlias.company_name == company_name,
            FacilityAlias.address == address,
            FacilityAlias.postal_code == postal_code,
        )
        alias = self.session.scalars(stmt).first()
        if alias:
            alias.seen_count += 1
            return
        self.session.add(
            FacilityAlias(
                facility_key=key,
                company_name=company_name,
                address=address,
                city=city,
                state=state,
                postal_code=postal_code,
            )
        )

    def aliases(self, key: str) -> list[FacilityAlias]:
        """Aliases recorded for a facility."""
        return list(
            self.session.scalars(
                select(FacilityAlias)
                .where(FacilityAlias.facility_key == key)
                .order_by(FacilityAlias.seen_count.desc())
            )
        )

    def link_load(
        self,
        key: str,
        *,
        load_id: int,
        role: Role,
        stop_type: str | None,
        terminal_id: int | None,
        method: str,
        score: int,
        observed_at: datetime | None,
    ) -> bool:
        """Record that a load's stop resolved to this facility; returns True when new or moved.

        A stop is identified by (load, role, stop type). If it was previously linked to another
        facility (for example a candidate key before the resolver learned the real facility), the
        link and the stop's source documents move to the new facility instead of duplicating.
        """
        stmt = select(FacilityLoadLink).where(
            FacilityLoadLink.load_id == load_id,
            FacilityLoadLink.role == role.value,
            FacilityLoadLink.stop_type == stop_type,
        )
        existing = self.session.scalars(stmt).first()
        if existing is not None and existing.facility_key == key:
            return False
        if existing is not None:
            self._move_link(existing, key, method=method, score=score)
        else:
            self.session.add(
                FacilityLoadLink(
                    facility_key=key,
                    load_id=load_id,
                    role=role.value,
                    stop_type=stop_type,
                    terminal_id=terminal_id,
                    resolution_method=method,
                    resolution_score=score,
                    observed_at=observed_at,
                )
            )
        record = self.session.get(FacilityRecord, key)
        if record is not None:
            record.stop_count += 1
            last_seen = as_utc(record.last_seen_load_at)
            if observed_at and (last_seen is None or observed_at > last_seen):
                record.last_seen_load_at = observed_at
        return True

    def _move_link(self, link: FacilityLoadLink, key: str, *, method: str, score: int) -> None:
        old_key = link.facility_key
        old_record = self.session.get(FacilityRecord, old_key)
        if old_record is not None and old_record.stop_count > 0:
            old_record.stop_count -= 1
        link.facility_key = key
        link.resolution_method = method
        link.resolution_score = score
        docs = self.session.scalars(
            select(SourceDocument).where(
                SourceDocument.facility_key == old_key,
                SourceDocument.load_id == link.load_id,
                SourceDocument.role == link.role,
            )
        ).all()
        for doc in docs:
            duplicate = self.session.scalars(
                select(SourceDocument).where(
                    SourceDocument.facility_key == key,
                    SourceDocument.role == doc.role,
                    SourceDocument.source_type == doc.source_type,
                    SourceDocument.load_id == doc.load_id,
                    SourceDocument.text_hash == doc.text_hash,
                )
            ).first()
            if duplicate is None:
                doc.facility_key = key
            else:
                self.session.delete(doc)
        self.session.flush()

    def repair_links(self) -> tuple[int, int]:
        """Remove duplicate stop links left by earlier re-resolution and recount stops.

        Returns (duplicate links removed, facilities recounted).
        """
        rows = self.session.execute(
            select(FacilityLoadLink.load_id, FacilityLoadLink.role, FacilityLoadLink.stop_type)
            .group_by(FacilityLoadLink.load_id, FacilityLoadLink.role, FacilityLoadLink.stop_type)
            .having(func.count(FacilityLoadLink.id) > 1)
        ).all()
        removed = 0
        for load_id, role, stop_type in rows:
            links = self.session.scalars(
                select(FacilityLoadLink)
                .where(
                    FacilityLoadLink.load_id == load_id,
                    FacilityLoadLink.role == role,
                    FacilityLoadLink.stop_type == stop_type,
                )
                .order_by(FacilityLoadLink.id.desc())
            ).all()
            linked = [
                link
                for link in links
                if (rec := self.session.get(FacilityRecord, link.facility_key))
                and rec.facility_id is not None
            ]
            keep = (linked or links)[0]
            for extra in links:
                if extra.id != keep.id:
                    self.session.delete(extra)
                    removed += 1
        self.session.flush()
        counts: dict[str, int] = {
            str(fkey): int(n)
            for fkey, n in self.session.execute(
                select(FacilityLoadLink.facility_key, func.count(FacilityLoadLink.id)).group_by(
                    FacilityLoadLink.facility_key
                )
            ).all()
        }
        recounted = 0
        for record in self.session.scalars(select(FacilityRecord)):
            record.stop_count = int(counts.get(record.key, 0))
            recounted += 1
        self.session.flush()
        return removed, recounted

    def load_links(
        self, key: str, *, role: Role | None = None, limit: int | None = None
    ) -> list[FacilityLoadLink]:
        """Loads linked to a facility, newest first."""
        stmt = select(FacilityLoadLink).where(FacilityLoadLink.facility_key == key)
        if role:
            stmt = stmt.where(FacilityLoadLink.role == role.value)
        stmt = stmt.order_by(FacilityLoadLink.observed_at.desc().nullslast())
        if limit:
            stmt = stmt.limit(limit)
        return list(self.session.scalars(stmt))

    def roles_for(self, key: str) -> list[Role]:
        """Roles in which the facility has appeared."""
        rows = self.session.scalars(
            select(FacilityLoadLink.role).where(FacilityLoadLink.facility_key == key).distinct()
        )
        return [Role(r) for r in rows]

    # ------------------------------------------------------------------ sources

    def upsert_source(
        self,
        key: str,
        *,
        role: Role,
        source_type: SourceType,
        text: str,
        load_id: int | None,
        observed_at: datetime | None,
    ) -> bool:
        """Store a source document once; returns True when new."""
        digest = text_hash(text)
        stmt = select(SourceDocument).where(
            SourceDocument.facility_key == key,
            SourceDocument.role == role.value,
            SourceDocument.source_type == source_type.value,
            SourceDocument.load_id == load_id,
            SourceDocument.text_hash == digest,
        )
        if self.session.scalars(stmt).first() is not None:
            return False
        self.session.add(
            SourceDocument(
                facility_key=key,
                role=role.value,
                load_id=load_id,
                source_type=source_type.value,
                observed_at=observed_at,
                text=text,
                text_hash=digest,
            )
        )
        return True

    def sources(self, key: str, role: Role) -> list[SourceDocument]:
        """Source documents for a facility and role, newest first."""
        return list(
            self.session.scalars(
                select(SourceDocument)
                .where(SourceDocument.facility_key == key, SourceDocument.role == role.value)
                .order_by(SourceDocument.observed_at.desc().nullslast())
            )
        )

    def has_new_sources_since(self, key: str, role: Role, since: datetime | None) -> bool:
        """True when a source was added after ``since`` (or when ``since`` is None)."""
        if since is None:
            return True
        stmt = select(func.count(SourceDocument.id)).where(
            SourceDocument.facility_key == key,
            SourceDocument.role == role.value,
            SourceDocument.created_at > since,
        )
        return bool(self.session.scalar(stmt))

    # ------------------------------------------------------------------ profile fields

    def profile(self, key: str, role: Role) -> ProfileRecord | None:
        """Profile summary row."""
        return self.session.scalars(
            select(ProfileRecord).where(
                ProfileRecord.facility_key == key, ProfileRecord.role == role.value
            )
        ).first()

    def upsert_profile(
        self,
        key: str,
        role: Role,
        *,
        summary: str | None,
        run_id: str,
        model_version: str | None,
        source_load_ids: Iterable[int],
        source_count: int,
    ) -> ProfileRecord:
        """Insert or update the profile summary row."""
        record = self.profile(key, role)
        if record is None:
            record = ProfileRecord(facility_key=key, role=role.value)
            self.session.add(record)
        record.scheduling_summary = summary
        record.run_id = run_id
        record.model_version = model_version
        record.source_load_ids = sorted(set(source_load_ids))
        record.source_count = source_count
        record.updated_at = utcnow()
        self.session.flush()
        return record

    def fields(self, key: str, role: Role) -> dict[str, ProfileFieldRecord]:
        """Stored fields for a facility and role, by field name."""
        rows = self.session.scalars(
            select(ProfileFieldRecord).where(
                ProfileFieldRecord.facility_key == key, ProfileFieldRecord.role == role.value
            )
        )
        return {row.field_name: row for row in rows}

    def upsert_field(
        self,
        key: str,
        role: Role,
        scored: ProfileField,
        *,
        state: FieldState,
        origin: ValueOrigin,
        run_id: str,
        value: Any = None,
        keep_value: bool = False,
    ) -> ProfileFieldRecord:
        """Insert or update a field. ``keep_value`` leaves the stored value alone (human-set)."""
        existing = self.fields(key, role).get(scored.name)
        if existing is None:
            existing = ProfileFieldRecord(facility_key=key, role=role.value, field_name=scored.name)
            self.session.add(existing)
        if not keep_value:
            existing.value = wrap(scored.value if value is None else value)
        existing.confidence = scored.confidence
        existing.support_count = scored.support_count
        existing.mention_count = scored.mention_count
        existing.distinct_loads = scored.distinct_loads
        existing.conflict = scored.conflict
        existing.state = state.value
        existing.origin = origin.value
        existing.last_seen = scored.last_seen
        existing.candidates = [c.model_dump(mode="json") for c in scored.candidates]
        existing.evidence = [e.model_dump(mode="json") for e in scored.evidence]
        existing.run_id = run_id
        existing.updated_at = utcnow()
        self.session.flush()
        return existing

    def set_field_human(
        self, key: str, role: Role, field_name: str, value: Any, *, state: FieldState
    ) -> ProfileFieldRecord:
        """Apply a reviewer's decision to a field."""
        existing = self.fields(key, role).get(field_name)
        if existing is None:
            existing = ProfileFieldRecord(facility_key=key, role=role.value, field_name=field_name)
            self.session.add(existing)
        existing.value = wrap(value)
        existing.state = state.value
        existing.origin = ValueOrigin.HUMAN.value
        existing.confidence = 1.0
        existing.updated_at = utcnow()
        self.session.flush()
        return existing

    # ------------------------------------------------------------------ review queue

    def open_review_item(self, key: str, role: Role, field_name: str) -> ReviewItem | None:
        """The open review item for a field, if any."""
        return self.session.scalars(
            select(ReviewItem).where(
                ReviewItem.facility_key == key,
                ReviewItem.role == role.value,
                ReviewItem.field_name == field_name,
                ReviewItem.status == "open",
            )
        ).first()

    def queue(
        self,
        key: str,
        role: Role,
        scored: ProfileField,
        *,
        existing_value: Any,
        reason: str,
        run_id: str,
    ) -> ReviewItem:
        """Create or refresh the open review item for a field."""
        item = self.open_review_item(key, role, scored.name)
        if item is None:
            item = ReviewItem(facility_key=key, role=role.value, field_name=scored.name)
            self.session.add(item)
        item.proposed = wrap(scored.value)
        item.existing = wrap(existing_value)
        item.candidates = [c.model_dump(mode="json") for c in scored.candidates]
        item.reason = reason[:255]
        item.run_id = run_id
        self.session.flush()
        return item

    def list_review_items(self, *, status: str = "open", limit: int = 200) -> list[ReviewItem]:
        """Review items by status, oldest first."""
        return list(
            self.session.scalars(
                select(ReviewItem)
                .where(ReviewItem.status == status)
                .order_by(ReviewItem.created_at.asc())
                .limit(limit)
            )
        )

    def get_review_item(self, item_id: int) -> ReviewItem | None:
        """Fetch a review item."""
        return self.session.get(ReviewItem, item_id)

    def decide_review_item(
        self, item: ReviewItem, *, status: str, value: Any, decided_by: str
    ) -> None:
        """Record the reviewer's decision on an item."""
        item.status = status
        item.decision = wrap(value)
        item.decided_by = decided_by
        item.decided_at = utcnow()

    # ------------------------------------------------------------------ audit

    def audit(
        self,
        *,
        run_id: str | None,
        key: str,
        role: Role,
        field_name: str | None,
        action: str,
        before: Any = None,
        after: Any = None,
        confidence: float | None = None,
        source_load_ids: Iterable[int] = (),
        model_version: str | None = None,
        reason: str | None = None,
        actor: str = "facility-profiles",
    ) -> AuditEntry:
        """Append an audit entry."""
        entry = AuditEntry(
            run_id=run_id,
            facility_key=key,
            role=role.value,
            field_name=field_name,
            action=action,
            before=wrap(before),
            after=wrap(after),
            confidence=confidence,
            source_load_ids=sorted({i for i in source_load_ids if i is not None}),
            model_version=model_version,
            reason=(reason or "")[:255] or None,
            actor=actor,
        )
        self.session.add(entry)
        return entry

    def audit_for(
        self, key: str, *, field_name: str | None = None, limit: int = 100
    ) -> list[AuditEntry]:
        """Audit entries for a facility, newest first."""
        stmt = select(AuditEntry).where(AuditEntry.facility_key == key)
        if field_name:
            stmt = stmt.where(AuditEntry.field_name == field_name)
        return list(self.session.scalars(stmt.order_by(AuditEntry.created_at.desc()).limit(limit)))

    def action_counts(self, run_id: str) -> dict[str, int]:
        """Number of audit entries per action for a run."""
        rows = self.session.execute(
            select(AuditEntry.action, func.count(AuditEntry.id))
            .where(AuditEntry.run_id == run_id)
            .group_by(AuditEntry.action)
        )
        return {action: int(count) for action, count in rows}

    # ------------------------------------------------------------------ checkpoints

    def checkpoint(self, run_id: str, key: str, role: Role, stage: str) -> None:
        """Mark progress for a facility in a run."""
        row = self.session.get(Checkpoint, (run_id, key, role.value))
        if row is None:
            self.session.add(
                Checkpoint(run_id=run_id, facility_key=key, role=role.value, stage=stage)
            )
        else:
            row.stage = stage

    def reached(self, run_id: str, key: str, role: Role, stage: str) -> bool:
        """True when the facility already reached ``stage`` in this run."""
        row = self.session.get(Checkpoint, (run_id, key, role.value))
        return row is not None and row.stage == stage

    # ------------------------------------------------------------------ extractions

    def save_extraction(
        self,
        *,
        run_id: str,
        key: str,
        role: Role,
        model: str | None,
        prompt_version: str | None,
        request_id: str | None,
        input_tokens: int,
        output_tokens: int,
        cache_read_tokens: int,
        source_count: int,
        raw: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> ExtractionRecord:
        """Persist a raw model result and its validation issues for later inspection."""
        row = ExtractionRecord(
            run_id=run_id,
            facility_key=key,
            role=role.value,
            model=model,
            prompt_version=prompt_version,
            request_id=request_id,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            source_count=source_count,
            raw=raw,
            issues=issues,
        )
        self.session.add(row)
        return row

    def extractions_for(self, key: str, *, limit: int = 10) -> list[ExtractionRecord]:
        """Stored extractions for a facility, newest first."""
        return list(
            self.session.scalars(
                select(ExtractionRecord)
                .where(ExtractionRecord.facility_key == key)
                .order_by(ExtractionRecord.created_at.desc())
                .limit(limit)
            )
        )

    # ------------------------------------------------------------------ digest queries

    def count_facilities(self, *, linked_only: bool = False) -> int:
        """Number of facilities harvested."""
        stmt = select(func.count(FacilityRecord.key))
        if linked_only:
            stmt = stmt.where(FacilityRecord.facility_id.is_not(None))
        return int(self.session.scalar(stmt) or 0)

    def count_fields_by_state(self) -> dict[str, int]:
        """Number of profile fields per lifecycle state."""
        rows = self.session.execute(
            select(ProfileFieldRecord.state, func.count(ProfileFieldRecord.id)).group_by(
                ProfileFieldRecord.state
            )
        )
        return {state: int(count) for state, count in rows}

    def count_open_reviews(self) -> int:
        """Open review items."""
        return int(
            self.session.scalar(
                select(func.count(ReviewItem.id)).where(ReviewItem.status == "open")
            )
            or 0
        )
