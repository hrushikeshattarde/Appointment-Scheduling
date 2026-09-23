"""SQLAlchemy ORM models for the staging store, review queue and audit log.

The staging store is the system of record until Transport Pro accepts facility writes
(FR-10). Every table carries the run that last touched it so any value can be traced to its
sources in one query (FR-12).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Timezone-aware UTC now."""
    return datetime.now(tz=UTC)


class Base(DeclarativeBase):
    """Declarative base with a JSON type that works on SQLite and Postgres."""

    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Run(Base):
    """One execution of the routine."""

    __tablename__ = "runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    mode: Mapped[str] = mapped_column(String(16))
    status: Mapped[str] = mapped_column(String(16), default="running")
    terminal_ids: Mapped[str] = mapped_column(String(255), default="")
    model_version: Mapped[str | None] = mapped_column(String(128))
    stats: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)


class FacilityRecord(Base):
    """A facility (Transport Pro location or unlinked candidate) and its record snapshot."""

    __tablename__ = "facilities"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    facility_id: Mapped[int | None] = mapped_column(Integer, index=True)
    candidate_key: Mapped[str | None] = mapped_column(String(32), index=True)
    company_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(16))
    postal_code: Mapped[str | None] = mapped_column(String(32))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    iana_timezone: Mapped[str | None] = mapped_column(String(64))
    existing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    existing_fetched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stop_count: Mapped[int] = mapped_column(Integer, default=0)
    last_seen_load_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    aliases: Mapped[list[FacilityAlias]] = relationship(back_populates="facility", cascade="all")
    fields: Mapped[list[ProfileFieldRecord]] = relationship(
        back_populates="facility", cascade="all"
    )


class FacilityAlias(Base):
    """A name and address under which the facility appeared on stops."""

    __tablename__ = "facility_aliases"
    __table_args__ = (
        UniqueConstraint("facility_key", "company_name", "address", "postal_code", name="uq_alias"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    company_name: Mapped[str | None] = mapped_column(String(255))
    address: Mapped[str | None] = mapped_column(String(255))
    city: Mapped[str | None] = mapped_column(String(128))
    state: Mapped[str | None] = mapped_column(String(16))
    postal_code: Mapped[str | None] = mapped_column(String(32))
    seen_count: Mapped[int] = mapped_column(Integer, default=1)

    facility: Mapped[FacilityRecord] = relationship(back_populates="aliases")


class FacilityLoadLink(Base):
    """A stop on a load resolved to a facility (FR-1, FR-2)."""

    __tablename__ = "facility_loads"
    __table_args__ = (
        UniqueConstraint("facility_key", "load_id", "role", "stop_type", name="uq_facility_load"),
        Index("ix_facility_loads_load", "load_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    load_id: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    stop_type: Mapped[str | None] = mapped_column(String(8))
    terminal_id: Mapped[int | None] = mapped_column(Integer, index=True)
    resolution_method: Mapped[str] = mapped_column(String(32))
    resolution_score: Mapped[int] = mapped_column(Integer, default=0)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SourceDocument(Base):
    """Source text collected for a facility (FR-3)."""

    __tablename__ = "source_documents"
    __table_args__ = (
        UniqueConstraint(
            "facility_key", "role", "source_type", "load_id", "text_hash", name="uq_source"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    load_id: Mapped[int | None] = mapped_column(Integer, index=True)
    source_type: Mapped[str] = mapped_column(String(48))
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    text: Mapped[str] = mapped_column(Text)
    text_hash: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ProfileRecord(Base):
    """Per facility and role: summary and provenance of the last extraction."""

    __tablename__ = "profiles"
    __table_args__ = (UniqueConstraint("facility_key", "role", name="uq_profile"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    scheduling_summary: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str | None] = mapped_column(String(36))
    model_version: Mapped[str | None] = mapped_column(String(128))
    source_load_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    source_count: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProfileFieldRecord(Base):
    """One scored field of a facility profile with its lifecycle state."""

    __tablename__ = "profile_fields"
    __table_args__ = (UniqueConstraint("facility_key", "role", "field_name", name="uq_field"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    field_name: Mapped[str] = mapped_column(String(48))
    value: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"v": <value>}
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    support_count: Mapped[int] = mapped_column(Integer, default=0)
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    distinct_loads: Mapped[int] = mapped_column(Integer, default=0)
    conflict: Mapped[bool] = mapped_column(Boolean, default=False)
    state: Mapped[str] = mapped_column(String(16), default="extracted")
    origin: Mapped[str] = mapped_column(String(16), default="extracted")
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    evidence: Mapped[list[Any]] = mapped_column(JSON, default=list)
    run_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    facility: Mapped[FacilityRecord] = relationship(back_populates="fields")


class ReviewItem(Base):
    """A queued disagreement or low-confidence value for a person to settle (FR-11)."""

    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(ForeignKey("facilities.key"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    field_name: Mapped[str] = mapped_column(String(48))
    proposed: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"v": value}
    existing: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    candidates: Mapped[list[Any]] = mapped_column(JSON, default=list)
    reason: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="open", index=True)
    decision: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {"v": value}
    decided_by: Mapped[str | None] = mapped_column(String(128))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    run_id: Mapped[str | None] = mapped_column(String(36))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditEntry(Base):
    """Append-only record of every decision the routine or a reviewer made (FR-12)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    facility_key: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    field_name: Mapped[str | None] = mapped_column(String(48))
    action: Mapped[str] = mapped_column(String(24), index=True)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    confidence: Mapped[float | None] = mapped_column(Float)
    source_load_ids: Mapped[list[Any]] = mapped_column(JSON, default=list)
    model_version: Mapped[str | None] = mapped_column(String(128))
    reason: Mapped[str | None] = mapped_column(String(255))
    actor: Mapped[str] = mapped_column(String(128), default="facility-profiles")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Checkpoint(Base):
    """Per-run, per-facility progress marker so a failed run resumes where it stopped."""

    __tablename__ = "checkpoints"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    facility_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), primary_key=True)
    stage: Mapped[str] = mapped_column(String(16))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
