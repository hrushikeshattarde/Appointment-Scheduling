"""Structured shape of a classified vendor reply."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class ReplyStatus(StrEnum):
    """What the vendor's reply means for the request."""

    CONFIRMED = "confirmed"  # the requested date and time (or a stated one) is booked
    COUNTER_OFFER = "counter_offer"  # a different date or time is offered
    QUESTION = "question"  # the vendor needs something before booking
    REJECTED = "rejected"  # cannot book: order not ready, not in system, closed
    DEFERRED = "deferred"  # check back later (pickup_date holds the day to check back)
    UNRELATED = "unrelated"  # not about this pickup appointment


class RejectReason(StrEnum):
    """Why a facility cannot book, which decides who can fix it.

    The first three are about timing: the customer can move the delivery to a day the facility
    can ship. The others are not, and asking the customer for a new delivery would not help.
    """

    NOT_READY = "not_ready"  # the order or product is not ready or released for that day
    NO_CAPACITY = "no_capacity"  # no appointments left that day
    CLOSED = "closed"  # the facility is closed that day
    PO_NOT_FOUND = "po_not_found"  # the PO or order is not in their system, or is wrong
    ORDER_CANCELED = "order_canceled"  # the order was canceled
    OTHER = "other"


# Reasons the customer can fix by moving the delivery.
TIMING_REASONS = frozenset({RejectReason.NOT_READY, RejectReason.NO_CAPACITY, RejectReason.CLOSED})
# Each reason in a few words, for to-dos and notes.
REJECT_WORDS: dict[RejectReason, str] = {
    RejectReason.NOT_READY: "the order is not ready that day",
    RejectReason.NO_CAPACITY: "no appointments left that day",
    RejectReason.CLOSED: "the facility is closed that day",
    RejectReason.PO_NOT_FOUND: "the PO is not in their system",
    RejectReason.ORDER_CANCELED: "the order was canceled",
    RejectReason.OTHER: "see their reply",
}


class ReplyItem(BaseModel):
    """One PO line of a reply that answers several POs separately.

    Morgan Foods answers a batched request line by line: "A & B-9/28 @ 9am pickup# 20463264"
    then "C & D-10/2 @ 9am pickup# 20463798 (we cannot schedule early pickups)". Each line is
    its own verdict for its own POs.
    """

    po_numbers: list[str] = Field(
        default_factory=list,
        description="The PO numbers this line is about, as written; empty when it applies to all",
    )
    status: ReplyStatus
    pickup_date: str | None = Field(None, description="YYYY-MM-DD, resolved from this line")
    pickup_time: str | None = Field(None, description="HH:MM 24-hour local time, start")
    pickup_time_end: str | None = Field(None, description="HH:MM if a window was given")
    pickup_number: str | None = Field(None, description="Pickup number given for this line")
    reject_reason: RejectReason | None = Field(
        None, description="Why this line cannot be booked, when its status is rejected"
    )
    conditions: list[str] = Field(default_factory=list, description="Rules stated on this line")
    quotes: list[str] = Field(
        default_factory=list, description="Verbatim snippets from this line backing its values"
    )


class ReplyClassification(BaseModel):
    """The model's reading of one reply. Every value must be backed by a verbatim quote."""

    status: ReplyStatus
    pickup_date: str | None = Field(None, description="YYYY-MM-DD, resolved from the reply")
    pickup_time: str | None = Field(None, description="HH:MM 24-hour local time, start")
    pickup_time_end: str | None = Field(None, description="HH:MM if a window was given")
    pickup_number: str | None = Field(None, description="Vendor pickup/confirmation number")
    time_zone: str | None = Field(
        None,
        description=(
            "The time zone the reply names for its times (ET, EST, CT, Central, PT ...), or "
            "'local' when it says the time is the facility's own; null when it names none"
        ),
    )
    reject_reason: RejectReason | None = Field(
        None, description="Why the facility cannot book, when the status is rejected"
    )
    conditions: list[str] = Field(default_factory=list, description="Rules the vendor stated")
    question: str | None = Field(None, description="What the vendor asked, if anything")
    questions: list[str] = Field(
        default_factory=list,
        description=(
            "Every question or request for information the facility puts to us, one per entry, "
            "in their own words (driver name and cell, carrier MC, weight, both orders?); also "
            "inside a confirmation or an offer"
        ),
    )
    quotes: list[str] = Field(
        default_factory=list, description="Verbatim snippets from the reply that back the values"
    )
    items: list[ReplyItem] = Field(
        default_factory=list,
        description=(
            "One entry per PO line when the reply answers several POs separately; empty when the "
            "whole reply has one reading"
        ),
    )
    confidence: float = Field(0.0, description="0 to 1")


def questions_of(result: ReplyClassification) -> list[str]:
    """Everything the facility asked us, in order: the listed questions, else the one question."""
    asked = [q.strip() for q in result.questions if q and q.strip()]
    if not asked and result.question and result.question.strip():
        asked = [result.question.strip()]
    return list(dict.fromkeys(asked))
