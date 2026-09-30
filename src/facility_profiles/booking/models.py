"""Booking case tables (share the staging store's metadata so ``init-db`` creates them)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from facility_profiles.storage.models import Base, utcnow


class CaseStatus(StrEnum):
    """Lifecycle of one pickup booking."""

    NEW = "new"  # load found, profile has an email desk, nothing sent yet
    NEEDS_PROFILE = "needs_profile"  # no verified booking email for the vendor
    ALREADY_BOOKED = "already_booked"  # the load already carries a vendor pickup number
    DRAFTED = "drafted"  # request composed (draft mode) and waiting for a person to send
    SENT = "sent"  # request sent, waiting for the vendor
    PROPOSED = "proposed"  # vendor confirmed; appointment proposed, waiting for approval
    NEEDS_HUMAN = "needs_human"  # question, rejection, counter-offer or unclear reply
    APPROVED = "approved"  # a person approved the proposed appointment
    CLOSED = "closed"  # rejected by a person or no longer needed


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
    status: Mapped[str] = mapped_column(String(24), default=CaseStatus.NEW.value)
    reason: Mapped[str | None] = mapped_column(String(255))
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
