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
    MISSING_REFERENCE = "missing_reference"  # the desk needs a number the case does not have
    SLOT_UNWORKABLE = "slot_unworkable"  # the slot passed, is too soon, or misses the delivery
    CONFIRMATION_REVIEW = "confirmation_review"  # the vendor confirmed; a person approves it
    PROPOSED_TIME_REVIEW = "proposed_time_review"  # the vendor offered a different time
    FACILITY_QUESTION = "facility_question"  # the vendor asked something the agent did not answer
    FACILITY_DECLINED = "facility_declined"  # the vendor cannot book as asked
    STALE_CONFIRMATION = "stale_confirmation"  # "confirmed" a slot already past when written
    DELIVERY_MOVED = "delivery_moved"  # the customer moved the delivery; re-request the pickup
    HANDOFF = "handoff"  # the agent stopped and no more specific exception was open
    # Raised as time passes (booking/timers.py), not by a reply.
    UNANSWERED_24H = "unanswered_24h"  # no reply for 24 weekday hours after we wrote
    UNANSWERED_48H = "unanswered_48h"  # still no reply after 48 weekday hours
    PICKUP_EXPIRED = "pickup_expired"  # the pickup time passed and the case is not booked
    # No pickup the desk would take that day gets the load to the delivery in time.
    LOAD_INFEASIBLE = "load_infeasible"
    # The agent tried to write a request on its own and failed three times (booking/automation.py).
    AUTOMATION_FAILED = "automation_failed"
    # Transport Pro already has a different confirmed time for the stop (booking/writeback.py).
    TPRO_MISMATCH = "tpro_mismatch"
    # The vendor confirmed a different day, or a time more than two hours from the one asked for.
    CONFIRMED_OUTSIDE_WINDOW = "confirmed_outside_window"


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
    # Numbers a desk needs that the load does not carry, added by a person: the customer's
    # shipment number, its sales order number. Keyed by ReferenceType value.
    reference_numbers: Mapped[dict[str, Any]] = mapped_column(
        JSON, default=dict, server_default="{}"
    )
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
    references: Mapped[list[BookingReference]] = relationship(
        back_populates="case", cascade="all", order_by="BookingReference.id"
    )
    offers: Mapped[list[SlotOffer]] = relationship(
        back_populates="case", cascade="all", order_by="SlotOffer.id"
    )
    jobs: Mapped[list[AutomationJob]] = relationship(
        back_populates="case", cascade="all", order_by="AutomationJob.id"
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

    Raised by the agent, its timers (or a migration) with a one-line description for the
    worklist and structured detail; resolved by the agent when the situation clears, or by a
    person.
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


class BookingReference(Base):
    """One number on a pickup, where it came from and when; the one place they are kept.

    ``kind`` is a ReferenceType value (pickup_number, delivery_number, po_number,
    shipment_number, confirmation_number, portal_appointment_id ...). A newer value of the same
    kind replaces the older one (``replaced_at``), which stays as history; a case can carry
    several POs at once. The case's ``pickup_number``, ``delivery_ref`` and
    ``reference_numbers`` columns mirror the current values.
    """

    __tablename__ = "booking_references"
    __table_args__ = (Index("ix_booking_references_value", "value"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32))
    value: Mapped[str] = mapped_column(String(128))
    # load | vendor | customer_desk | person | migration
    source: Mapped[str] = mapped_column(String(24))
    actor: Mapped[str] = mapped_column(String(128), default="agent")
    message_id: Mapped[int | None] = mapped_column(Integer)  # the email it came from
    note: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    replaced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[BookingCase] = relationship(back_populates="references")


class SlotOffer(Base):
    """The pickup times one request offered as one-click links (``booking/links.py``).

    ``slots`` are vendor-local, "YYYY-MM-DD HH:MM" (or "YYYY-MM-DD" for a desk given dates
    only). An offer is answered once (a time chosen, or ``proposed`` with the vendor's own in
    ``answer_detail``), superseded when a later request offers new times, and dead after
    ``expires_at``.
    """

    __tablename__ = "booking_slot_offers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(Integer)  # the request it went out in
    slots: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    answer: Mapped[str | None] = mapped_column(String(32))
    answer_detail: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)

    case: Mapped[BookingCase] = relationship(back_populates="offers")


class JobStatus(StrEnum):
    """Where one thing the agent does on its own stands."""

    WAITING = "waiting"  # for something to happen first (the delivery slot)
    PLANNED = "planned"  # will run at due_at
    BLOCKED = "blocked"  # something on the case needs a person first
    HELD = "held"  # a rule says a person books this
    DONE = "done"
    FAILED = "failed"  # tried and failed; retried until it gives up
    CANCELED = "canceled"  # no longer needed (booked or canceled some other way)


class AutomationJob(Base):
    """One thing the agent does on its own for a case: a request, a follow-up.

    The rule that planned it (from the customer file), when it is due, why it waits, how many
    times it was tried and what it produced.
    """

    __tablename__ = "booking_jobs"
    __table_args__ = (Index("ix_booking_jobs_due", "status", "due_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    case_id: Mapped[int] = mapped_column(ForeignKey("booking_cases.id"), index=True)
    kind: Mapped[str] = mapped_column(String(24))  # request | follow_up
    rule: Mapped[str] = mapped_column(String(64))
    action: Mapped[str] = mapped_column(String(8))  # draft | send | hold | skip
    status: Mapped[str] = mapped_column(String(16), default=JobStatus.PLANNED.value)
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(String(255))
    attempts: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    case: Mapped[BookingCase] = relationship(back_populates="jobs")


class BookingTemplate(Base):
    """A person's wording for one kind of email, for one desk, one customer or the whole pod.

    ``scope`` is desk, customer or default; ``match`` is the desk's address (lower case) or the
    customer's name, "" for the default. The body and subject carry fields such as ``{po}``.
    """

    __tablename__ = "booking_templates"
    __table_args__ = (UniqueConstraint("kind", "scope", "match", name="uq_booking_template"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(24))  # TemplateKind value
    scope: Mapped[str] = mapped_column(String(16))
    match: Mapped[str] = mapped_column(String(255), default="")
    subject: Mapped[str | None] = mapped_column(String(512))
    body: Mapped[str] = mapped_column(Text)
    updated_by: Mapped[str | None] = mapped_column(String(128))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class DeskMemory(Base):
    """A way of booking a facility that worked: the method, the desk, how often and when last.

    One row per facility, method and desk. ``desk`` is the email address, phone number or
    portal URL the booking was made with, or "" when nobody said.
    """

    __tablename__ = "booking_desk_memory"
    __table_args__ = (
        UniqueConstraint("facility_key", "method", "desk", name="uq_booking_desk_memory"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    facility_key: Mapped[str] = mapped_column(String(64), index=True)
    method: Mapped[str] = mapped_column(String(32))  # BookingMethod value
    desk: Mapped[str] = mapped_column(String(512), default="")
    worked_count: Mapped[int] = mapped_column(Integer, default=0)
    first_worked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_worked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_case_id: Mapped[int | None] = mapped_column(Integer)
    last_by: Mapped[str | None] = mapped_column(String(128))
