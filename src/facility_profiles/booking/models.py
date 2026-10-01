"""Booking case tables (share the staging store's metadata so ``init-db`` creates them).

A case's ``status`` says where the pickup appointment stands and nothing else. Anything a person
has to do or decide before the case can move on is an exception on the case
(:class:`CaseException`): raised, then resolved by the agent when the situation clears or by a
person with a note. A pending case whose confirmation waits for approval is still pending; the
review is the exception.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from facility_profiles.storage.models import Base, utcnow


class CaseStatus(StrEnum):
    """Where one pickup appointment stands."""

    UNSCHEDULED = "unscheduled"  # needs an appointment; no request has gone out (a draft may wait)
    PENDING = "pending"  # a request went out: waiting for the vendor, or for review of its answer
    SCHEDULED = "scheduled"  # booked: approved by a person, or booked outside the agent
    DECLINED = "declined"  # the vendor cannot book it as asked
    CANCELED = "canceled"  # no longer needed


class ExceptionType(StrEnum):
    """Why a case needs a person. A case can carry several; each kind is open at most once."""

    MISSING_METHOD = "missing_method"  # no trusted email booking desk on the profile
    METHOD_NOT_SUPPORTED = "method_not_supported"  # the vendor books by portal, phone or other
    SLOT_UNWORKABLE = "slot_unworkable"  # the slot passed, is too soon, or misses the delivery
    CONFIRMATION_REVIEW = "confirmation_review"  # the vendor confirmed; a person approves it
    PROPOSED_TIME_REVIEW = "proposed_time_review"  # the vendor offered a different time
    FACILITY_QUESTION = "facility_question"  # the vendor asked something the agent did not answer
    FACILITY_DECLINED = "facility_declined"  # the vendor cannot book as asked
    STALE_CONFIRMATION = "stale_confirmation"  # "confirmed" a slot already past when written
    DELIVERY_MOVED = "delivery_moved"  # the customer moved the delivery; re-request the pickup
    HANDOFF = "handoff"  # the agent stopped and no more specific exception was open


class BookingCase(Base):
    """One pickup stop on one load that needs an appointment."""

    __tablename__ = "booking_cases"
    __table_args__ = (
        UniqueConstraint("load_id", "waypoint_index", name="uq_booking_case_stop"),
        Index("ix_booking_cases_status", "status"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    load_id: Mapped[int] = mapped_column(Integer, index=True)
    waypoint_index: Mapped[int] = mapped_column(Integer, default=0)
    customer_id: Mapped[int | None] = mapped_column(Integer)
    customer_name: Mapped[str | None] = mapped_column(String(255))
    facility_key: Mapped[str | None] = mapped_column(String(64), index=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255))
    vendor_city: Mapped[str | None] = mapped_column(String(128))
    vendor_timezone: Mapped[str | None] = mapped_column(String(64))
    po_numbers: Mapped[list[Any]] = mapped_column(JSON, default=list)
    booking_method: Mapped[str | None] = mapped_column(String(32))
    contact_email: Mapped[str | None] = mapped_column(String(255))
    contact_name: Mapped[str | None] = mapped_column(String(255))
    delivery_site: Mapped[str | None] = mapped_column(String(255))
    delivery_ref: Mapped[str | None] = mapped_column(String(64))
    delivery_at_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    tendered_pickup_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    requested_local: Mapped[str | None] = mapped_column(String(32))  # "YYYY-MM-DD HH:MM"
    confirmed_local: Mapped[str | None] = mapped_column(String(32))
    confirmed_start_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confirmed_end_utc: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    pickup_number: Mapped[str | None] = mapped_column(String(64))
    thread_id: Mapped[str | None] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(24), default=CaseStatus.UNSCHEDULED.value)
    # Why the case has its status (a deferral, a decline, a closure, an earlier booking). What a
    # person has to do is never written here: that is an exception.
    reason: Mapped[str | None] = mapped_column(String(255))
    reschedule_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    miles: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list[BookingMessage]] = relationship(
        back_populates="case", cascade="all", order_by="BookingMessage.id"
    )
    events: Mapped[list[BookingEvent]] = relationship(
        back_populates="case", cascade="all", order_by="BookingEvent.id"
    )
    exceptions: Mapped[list[CaseException]] = relationship(
        back_populates="case", cascade="all", order_by="CaseException.id"
    )

    @property
    def open_exceptions(self) -> list[CaseException]:
        """Exceptions not resolved yet, oldest first."""
        return [e for e in self.exceptions if e.resolved_at is None]


class BookingMessage(Base):
    """An email the agent composed or read for a case."""

    __tablename__ = "booking_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    direction: Mapped[str] = mapped_column(String(8))  # out | in
    kind: Mapped[str] = mapped_column(String(24))  # request | reply | follow_up
    to_addr: Mapped[str | None] = mapped_column(String(512))
    cc_addr: Mapped[str | None] = mapped_column(String(512))
    from_addr: Mapped[str | None] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str | None] = mapped_column(Text)
    message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    thread_id: Mapped[str | None] = mapped_column(String(128))
    # RFC 5322 threading: the id this message carries, the id it answers, the chain it quotes.
    # Gmail ids differ per mailbox; these do not, so replies are tied to requests through them.
    rfc_message_id: Mapped[str | None] = mapped_column(String(255), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references_header: Mapped[str | None] = mapped_column(Text)
    draft_ref: Mapped[str | None] = mapped_column(String(512))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    classification: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[BookingCase] = relationship(back_populates="messages")


class BookingEvent(Base):
    """Append-only trail of what happened to a case and who did it."""

    __tablename__ = "booking_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    action: Mapped[str] = mapped_column(String(48))
    actor: Mapped[str] = mapped_column(String(128), default="agent")
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    case: Mapped[BookingCase] = relationship(back_populates="events")


class CaseException(Base):
    """Something on a case that needs a person: an operational exception, not a Python one.

    Raised by the agent (or a migration) with a one-line description for the worklist and
    structured detail; resolved by the agent when the situation clears, or by a person.
    """

    __tablename__ = "booking_exceptions"
    __table_args__ = (Index("ix_booking_exceptions_open", "kind", "resolved_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))  # ExceptionType value
    description: Mapped[str] = mapped_column(String(255))
    detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    raised_by: Mapped[str] = mapped_column(String(128), default="agent")
    raised_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    resolved_by: Mapped[str | None] = mapped_column(String(128))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolution: Mapped[str | None] = mapped_column(String(255))

    case: Mapped[BookingCase] = relationship(back_populates="exceptions")
