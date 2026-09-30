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
    conditions: list[str] = Field(default_factory=list, description="Rules the vendor stated")
    question: str | None = Field(None, description="What the vendor asked, if anything")
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
