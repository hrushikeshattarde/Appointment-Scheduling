"""The conversational half of the agent: what to say back to a vendor, and when to stop.

The policy is deliberately narrow. The agent only ever does five things in a thread:

1. accept a counter-offer that still makes the customer's delivery slot,
2. ask for alternatives inside a stated window when it does not,
3. answer the facility's questions from the facts: the case's, and the load's and its dispatch's
   read from Transport Pro (weight, pallets, equipment, load notes, trucking company, driver),
4. nudge a quiet desk after a day of weekday hours, once per silence, and check back when the
   desk said to (on its day, or after a day when it named none),
5. draft a note to the customer's inbound desk when the vendor cannot ship on the day asked
   (the order is not ready, no slots, closed): moving the delivery fixes that, and nothing else.

The code decides the move; a writer (``booking/writer.py``) writes the words for what the
facility actually wrote, answering every question it asked in the same reply, and its draft goes
out only when every value in it is in the facts. Without a writer, or when a draft fails that
check, the pod's fixed wording goes. A question nothing answered is raised in Needs you.

A facility asking for the driver's ETA gets the driver's latest check call or position from
Transport Pro when there is one; a late arrival it will still take, or a pickup it put on hold,
goes to a person with a to-do of its own.

A question about the carrier or the driver asked before either is on the load waits in Needs
you, and is answered in the thread once Transport Pro shows them (:meth:`Responder.answer_later`,
each pass of the agent on its own): tried once for each carrier and driver seen, never while a
person is writing to the facility about it.

Everything else, and anything that mentions money, is handed to a person: the exception the
reply raised stays open with the agent's reason added to it. So is any change to a pickup that
was already booked, and an offer from a facility off Eastern time that named no time zone. A
move that settles the reply resolves its exception (an answered question, an offer accepted or
declined). Every time the agent writes is Eastern.

Whether an answer is sent or drafted is the customer's rule (``replies``, ``customer_notes``)
with FP_BOOKING_MODE=send and a sender given; every send passes the send gate first (the desk on
the profile, or the person at that company who wrote, or the customer's own desk for a note to
it; the daily cap). An answer the gate refuses is drafted instead and handed to a person. Every
message is a draft in draft mode.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from facility_profiles.booking.automated import AUTOMATED_KINDS
from facility_profiles.booking.coverage import PICKED_UP
from facility_profiles.booking.facts import (
    FACT_KEYS,
    FORBIDDEN_TOPICS,
    FactsSource,
    case_facts,
    read_load_facts,
    with_load,
)
from facility_profiles.booking.mail import Mailer, OutboundDraft, Sender
from facility_profiles.booking.models import (
    PERSON_MAIL,
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseException,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.outbox import (
    SendRefusedError,
    address,
    check_send_gate,
    dispatch,
    is_sender,
)
from facility_profiles.booking.rules import vendor_profile
from facility_profiles.booking.schema import (
    TIMING_REASONS,
    RejectReason,
    ReplyClassification,
    ReplyStatus,
    ReplyTopic,
    questions_of,
)
from facility_profiles.booking.templates import (
    TemplateKind,
    case_values,
    pick,
    render,
    vendor_when,
)
from facility_profiles.booking.timers import is_vendor_answer, waiting_since, weekday_hours
from facility_profiles.booking.worklist import (
    TOPIC_KINDS,
    UNANSWERED,
    annotate,
    flag,
    open_exceptions,
    resolve,
)
from facility_profiles.booking.writer import ReplySituation, ReplyWriter, check_reply
from facility_profiles.business_days import is_business_day, why_closed
from facility_profiles.clock import slot_text, stamp, to_eastern
from facility_profiles.config import Settings
from facility_profiles.customers import customer_of
from facility_profiles.extraction.llm import ExtractionError
from facility_profiles.logging import get_logger
from facility_profiles.storage.repository import Repository, as_utc

log = get_logger(__name__)
# A facility that cannot book for a reason other than the day: moving the delivery would not help,
# so nothing goes to the customer's desk and a person reads the reply.
NOT_THE_DAY: dict[RejectReason, str] = {
    RejectReason.PO_NOT_FOUND: (
        "the facility does not have this PO in its system; check the PO with the customer"
    ),
    RejectReason.ORDER_CANCELED: (
        "the facility says the order was canceled; check with the customer"
    ),
    RejectReason.OTHER: "the facility cannot book, and not because of the day; read their reply",
}


class ResponseIntent(StrEnum):
    """The only moves the agent is allowed to make."""

    ACCEPT_OFFER = "accept_offer"
    ASK_ALTERNATIVE = "ask_alternative"
    ANSWER_QUESTION = "answer_question"
    FOLLOW_UP = "follow_up"
    ACKNOWLEDGE = "acknowledge"
    ESCALATE_TO_CUSTOMER = "escalate_to_customer"
    HANDOFF = "handoff"


@dataclass(frozen=True)
class ResponsePlan:
    """What the responder decided and why."""

    intent: ResponseIntent
    reason: str
    body: str | None = None
    to_addr: str | None = None
    proposed_local: str | None = None
    signed: bool = False  # the body already ends with the signature (it came from a template)
    # What the reply tells the facility, for the writer, with the dates and times it must carry.
    decision: str = ""
    must_include: tuple[str, ...] = ()
    # The facility's questions this reply leaves for a person; they are raised in Needs you.
    unanswered: tuple[str, ...] = ()
    written: bool = False  # the body was written for the situation (and passed its check)


# ------------------------------------------------------------------ facts and feasibility


def local_dt(day: str, clock: str | None, timezone: str | None) -> datetime:
    """A vendor-local wall time as an aware datetime."""
    tz = ZoneInfo(timezone or "America/New_York")
    return datetime.strptime(f"{day} {clock or '09:00'}", "%Y-%m-%d %H:%M").replace(tzinfo=tz)


def transit_hours(case: BookingCase, settings: Settings) -> float:
    """Drive time plus loading, from the load's miles."""
    miles = case.miles or 0
    return miles / settings.booking_avg_mph + settings.booking_load_hours


def offer_is_feasible(
    case: BookingCase, pickup_local: datetime, settings: Settings, *, now: datetime
) -> tuple[bool, str]:
    """Can a pickup at ``pickup_local`` still make the customer's delivery slot?"""
    closed = why_closed(pickup_local.date())
    if closed is not None:  # "falls on a weekend", "falls on a holiday (Thanksgiving)"
        return False, f"falls on {closed}" if "holiday" in closed else "falls on a weekend"
    if pickup_local <= now + timedelta(hours=settings.booking_min_notice_hours):
        return False, "too soon to dispatch a driver"
    delivery = as_utc(case.delivery_at_utc)
    if delivery is None:
        return True, "no delivery slot on the load to check against"
    arrival = pickup_local.astimezone(UTC) + timedelta(hours=transit_hours(case, settings))
    if arrival > delivery:
        return False, (f"would arrive {stamp(arrival)}, after the delivery slot {stamp(delivery)}")
    return True, "makes the delivery slot"


def alternative_days(case: BookingCase, settings: Settings, *, now: datetime) -> list[str]:
    """Up to three business days that still make the delivery, latest first, as MM/DD."""
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    delivery = as_utc(case.delivery_at_utc)
    if delivery is None:
        return []
    latest = (delivery - timedelta(hours=transit_hours(case, settings))).astimezone(tz).date()
    earliest = (now + timedelta(hours=settings.booking_min_notice_hours)).astimezone(tz).date()
    days: list[str] = []
    day = latest
    while day >= earliest and len(days) < 3:
        if is_business_day(day):
            days.append(day.strftime("%m/%d"))
        day -= timedelta(days=1)
    return days


def rounds_so_far(case: BookingCase) -> int:
    """The agent's outbound messages in the thread after the first request (not a person's)."""
    return max(
        0, sum(1 for m in case.messages if m.direction == "out" and m.kind != PERSON_MAIL) - 1
    )


# ------------------------------------------------------------------ answers to questions


class AnswerDraft(BaseModel):
    """An answer from the fixed rules: only allowed facts, or a refusal."""

    answerable: bool
    message: str | None = Field(None, description="Short reply text, or null")
    facts_used: list[str] = Field(default_factory=list, description="Keys of the facts used")
    reason: str | None = None


# The questions the case itself answers. Each is narrow on purpose: a question that only looks
# like one ("What is the delivery number for this PO?" names a PO but does not ask which PO) must
# not get its answer. Without a writer, anything else goes to a person.
_DELIVERY_NUMBER = re.compile(
    r"\b(?:delivery|dct|receiving)\s*(?:appointment\s*|appt\.?\s*)?"
    r"(?:number|#|no\.?|ref\w*|confirmation)",
    re.I,
)
_LOAD_NUMBER = re.compile(r"\bload\s*(?:number|#|no\.?)", re.I)
_WHICH_CARRIER = re.compile(
    r"\b(?:which|what|who)(?:'s|\s+is|\s+are)?\s+(?:the\s+|your\s+)?"
    r"(?:carrier|trucking\s+company)\b|\bcarrier\s*(?:name)?\s*\?",
    re.I,
)
_WHICH_CUSTOMER = re.compile(
    r"\bwho(?:'s|\s+is)\s+(?:the\s+)?(?:customer|consignee|receiver)\b|"
    r"\bwho\s+is\s+this\s+(?:for|going\s+to)\b|\bfor\s+whom\b|\b(?:what|which)\s+customer\b",
    re.I,
)
_WHERE_DELIVERING = re.compile(
    r"\b(?:where|which)\b[^?]*\b(?:deliver\w*|going|destination)\b", re.I
)
_WHICH_PO = re.compile(
    r"\bboth\b[^?]*\b(?:orders?|pos?)\b|"
    r"\b(?:which|what)\s+(?:orders?|pos?|po\s*numbers?|po#|purchase\s+orders?)\b|"
    r"\b(?:po|order)\s*(?:numbers?|#)",
    re.I,
)
# A question about anything else is not one of these, whatever else it mentions.
_ABOUT_TIME = re.compile(r"\b(?:time|when|eta|arriv\w*|late|early|hours?|today|tomorrow)\b", re.I)
_ABOUT_OTHER_NUMBER = re.compile(
    r"\b(?:deliver\w*|appointment|appt|pick\s*-?\s*up|pu|load|reference|ref|confirmation|"
    r"trailer|seal|bol|mc|driver|phone|cell|weight|pallets?|cases|temp\w*|live|drop|dock|door)\b",
    re.I,
)
_ABOUT_CARRIER_DETAIL = re.compile(
    r"\b(?:mc|dot|scac|number|phone|insurance|driver|truck|trailer)\b|#", re.I
)
_ABOUT_PAPERWORK = re.compile(
    r"\b(?:driver|paperwork|bol|park|dock|door|check\s*-?\s*in|gate)\b", re.I
)


def answer_from_rules(question: str, facts: dict[str, Any]) -> AnswerDraft | None:
    """Deterministic answers for the questions vendors actually ask; None when none fits."""
    q = question.strip()
    pos = facts.get("po_numbers") or []
    if _DELIVERY_NUMBER.search(q):
        if not facts.get("delivery_ref"):
            return None
        return AnswerDraft(
            answerable=True,
            message=f"The delivery number is {facts['delivery_ref']}.",
            facts_used=["delivery_ref"],
        )
    if _LOAD_NUMBER.search(q) and not _ABOUT_TIME.search(q):
        return AnswerDraft(
            answerable=True,
            message=f"Our load number is {facts['load_id']}.",
            facts_used=["load_id"],
        )
    if (
        _WHICH_CARRIER.search(q)
        and not _ABOUT_TIME.search(q)
        and not _ABOUT_CARRIER_DETAIL.search(q)
        and facts.get("carrier")  # the trucking company on the dispatch; none assigned, no answer
    ):
        carrier = str(facts["carrier"])
        return AnswerDraft(
            answerable=True,
            message=f"The carrier is {carrier}" + ("" if carrier.endswith(".") else "."),
            facts_used=["carrier"],
        )
    if _WHICH_CUSTOMER.search(q):
        return AnswerDraft(
            answerable=True,
            message=f"This is a {facts['customer']} order.",
            facts_used=["customer"],
        )
    if (
        _WHERE_DELIVERING.search(q)
        and facts.get("delivery_site")
        and not _ABOUT_PAPERWORK.search(q)
        and not _ABOUT_TIME.search(q)
    ):
        ref = f" ({facts['delivery_ref']})" if facts.get("delivery_ref") else ""
        when = f" on {facts['delivery_date']}" if facts.get("delivery_date") else ""
        return AnswerDraft(
            answerable=True,
            message=f"This is delivering to {facts['delivery_site']}{when}{ref}.",
            facts_used=["delivery_site", "delivery_date", "delivery_ref"],
        )
    if _WHICH_PO.search(q) and not _ABOUT_OTHER_NUMBER.search(q) and not _ABOUT_TIME.search(q):
        if not pos:
            return None
        if len(pos) > 1:
            return AnswerDraft(
                answerable=True,
                message="Yes, both orders: " + " & ".join(f"PO# {p}" for p in pos) + ".",
                facts_used=["po_numbers"],
            )
        return AnswerDraft(
            answerable=True, message=f"Just PO# {pos[0]}.", facts_used=["po_numbers"]
        )
    return None


def answer_is_safe(draft: AnswerDraft, facts: dict[str, Any]) -> tuple[bool, str]:
    """Every number in the answer must come from the facts; no money talk."""
    if not draft.answerable or not draft.message:
        return False, draft.reason or "not answerable from the facts on the case"
    if FORBIDDEN_TOPICS.search(draft.message):
        return False, "answer touches a forbidden topic"
    allowed = set(re.findall(r"\d{4,}", json.dumps(facts)))
    for number in re.findall(r"\d{4,}", draft.message):
        if number not in allowed:
            return False, f"answer contains a number not on the case: {number}"
    unknown = [k for k in draft.facts_used if k not in FACT_KEYS]
    if unknown:
        return False, f"answer used facts that do not exist: {', '.join(unknown)}"
    return True, "ok"


# ------------------------------------------------------------------ waiting for a carrier

# Who hauls the load: the carrier (its name, MC, DOT), the driver (name, cell) or the truck's
# numbers. Asked before a carrier is on the load, such a question waits for one (answer_later).
_WHO_HAULS = re.compile(
    r"\b(?:carrier|trucking|mc|dot|scac)\b"
    r"|\bdriver'?s?\s+(?:name|phone|cell|number|#|info\w*|contact|details)"
    r"|\bwho\b[^?.]{0,40}\bdriver\b"
    r"|\b(?:truck|tractor|trailer)\s*(?:#|number|no\b)",
    re.I,
)
_ASKS_DRIVER = re.compile(r"\bdriver", re.I)
_ASKS_TRUCK = re.compile(r"\b(?:truck|tractor)\s*(?:#|number|no\b)", re.I)
_ASKS_TRAILER = re.compile(r"\btrailer\s*(?:#|number|no\b)", re.I)
# What answering such a question depends on: who Transport Pro shows on the load.
HAULER_FACTS = (
    "carrier",
    "carrier_mc",
    "carrier_dot",
    "carrier_phone",
    "driver_name",
    "driver_phone",
    "truck_number",
    "trailer_number",
)


def about_hauler(question: str) -> bool:
    """True when the question asks who hauls the load: the carrier, the driver, the truck."""
    return bool(_WHO_HAULS.search(question))


def hauler_seen(facts: dict[str, Any]) -> str:
    """The carrier, driver and truck in the facts, as one value to tell a change by."""
    return json.dumps([facts.get(k) for k in HAULER_FACTS], default=str)


def hauler_answer(questions: str, facts: dict[str, Any]) -> str | None:
    """The fixed answer to who hauls the load; None unless the facts hold all that was asked."""
    carrier = facts.get("carrier")
    if not carrier:
        return None
    ids = ", ".join(
        f"{label} {facts[key]}"
        for label, key in (("MC", "carrier_mc"), ("DOT", "carrier_dot"))
        if facts.get(key)
    )
    parts = [f"The carrier is {str(carrier).rstrip('.')}" + (f" ({ids})" if ids else "") + "."]
    if _ASKS_DRIVER.search(questions):
        name, phone = facts.get("driver_name"), facts.get("driver_phone")
        if not name or not phone:
            return None
        parts.append(f"The driver is {name}, {phone}.")
    for asked, key, label in (
        (_ASKS_TRUCK, "truck_number", "truck"),
        (_ASKS_TRAILER, "trailer_number", "trailer"),
    ):
        if asked.search(questions):
            if not facts.get(key):
                return None
            parts.append(f"The {label} number is {facts[key]}.")
    return " ".join(parts)


# Follow-ups and check-backs one pickup may get; then the no-reply to-dos and a person take over.
MAX_NUDGES = 3


def _at(message: BookingMessage) -> datetime | None:
    """When a message was sent, or written when it was never sent (a draft)."""
    return as_utc(message.sent_at) or as_utc(message.created_at)


def _day(value: object) -> date | None:
    try:
        return date.fromisoformat(str(value)) if value else None
    except ValueError:
        return None


def raise_questions(
    session: Session, case: BookingCase, questions: Iterable[str], *, replied: bool = False
) -> None:
    """Put what the facility asked and the agent did not answer in Needs you.

    ``replied`` says the agent's reply went out with the rest answered and told them someone
    will get back to them on these.
    """
    asked = "; ".join(q.strip() for q in questions if q.strip())
    if not asked:
        return
    told = "; the reply said someone will get back to them" if replied else ""
    topical = open_exceptions(case, *TOPIC_KINDS)
    if topical:  # the ETA, late-arrival or hold to-do already asks a person to answer them
        exc = topical[-1]
        exc.description = f"{exc.description} | also asked: {asked}{told}"[:255]
        exc.detail = {**exc.detail, "also_asked": asked}
        session.flush()
        return
    flag(
        session,
        case,
        ExceptionType.FACILITY_QUESTION,
        f"vendor asked: {asked}"[: 255 - len(told)] + told,
        question=asked,
    )


# ------------------------------------------------------------------ the responder


def _person_wrote_since(case: BookingCase, since: datetime | None) -> bool:
    """True when someone at Circle wrote in the thread after ``since``: a person is on it."""
    return any(
        m.kind == PERSON_MAIL
        and (since is None or (as_utc(m.sent_at) or as_utc(m.created_at) or since) > since)
        for m in case.messages
    )


def _fmt(day: str, clock: str | None) -> str:
    parsed = datetime.strptime(day, "%Y-%m-%d")
    return f"{parsed:%m/%d}" + (f" @ {clock.replace(':', '')}" if clock else "")


@dataclass
class Responder:
    """Decides the next message for a case after a classified reply, and drafts or sends it.

    ``mailer`` drafts (or, passed a sender, sends everything through the gate). ``sender``
    sends what the customer's rule says to send, in send mode; the rest is drafted.

    ``writer`` writes each reply for its situation (``booking/writer.py``) from the facts:
    the case's, and the load's read through ``facts`` (Transport Pro, read only). Without a
    writer, or when its draft fails its check, the pod's fixed wording goes out instead.
    """

    settings: Settings
    mailer: Mailer | Sender
    writer: ReplyWriter | None = None
    now: datetime | None = None
    sender: Sender | None = None
    facts: FactsSource | None = None
    _loads_read: dict[int, dict[str, Any]] = field(default_factory=dict, init=False, repr=False)

    def _now(self) -> datetime:
        return self.now or datetime.now(tz=UTC)

    # -- planning

    def plan(
        self, case: BookingCase, reply: BookingMessage, result: ReplyClassification
    ) -> ResponsePlan:
        """Decide what to do about ``reply``; no side effects."""
        text = reply.body or ""
        if FORBIDDEN_TOPICS.search(text):
            return ResponsePlan(ResponseIntent.HANDOFF, "reply mentions money or a claim")
        if rounds_so_far(case) >= self.settings.booking_max_rounds:
            return ResponsePlan(
                ResponseIntent.HANDOFF, f"{self.settings.booking_max_rounds} rounds reached"
            )
        if result.status != ReplyStatus.QUESTION and open_exceptions(
            case, ExceptionType.BOOKED_SLOT_CHANGED
        ):
            return ResponsePlan(
                ResponseIntent.HANDOFF,
                "the pickup was already booked; a person agrees any change with the facility "
                "and the carrier",
            )
        if result.topic in (ReplyTopic.WORK_IN, ReplyTopic.HOLD):
            return ResponsePlan(
                ResponseIntent.HANDOFF,
                "a late arrival needs the driver's ETA"
                if result.topic == ReplyTopic.WORK_IN
                else "the facility put the pickup on hold",
            )
        if result.status == ReplyStatus.COUNTER_OFFER and open_exceptions(
            case, ExceptionType.TIME_ZONE_UNCLEAR
        ):
            return ResponsePlan(
                ResponseIntent.HANDOFF,
                "the facility named no time zone; a person checks which clock they meant",
            )
        if result.status == ReplyStatus.COUNTER_OFFER:
            plan = self._plan_counter_offer(case, result)
        elif result.status == ReplyStatus.QUESTION:
            plan = self._plan_question(case, result, text)
        elif result.status == ReplyStatus.REJECTED:
            return self._plan_rejection(case, result, text)
        else:
            return ResponsePlan(ResponseIntent.HANDOFF, f"no policy for a {result.status} reply")
        return self._write(case, reply, result, plan)

    def _plan_counter_offer(self, case: BookingCase, result: ReplyClassification) -> ResponsePlan:
        if not result.pickup_date:
            return ResponsePlan(ResponseIntent.HANDOFF, "counter-offer without a date")
        clock = result.pickup_time
        offered = local_dt(result.pickup_date, clock, case.vendor_timezone)
        feasible, why = offer_is_feasible(case, offered, self.settings, now=self._now())
        mmdd, hhmm = vendor_when(
            f"{result.pickup_date} {clock or ''}".strip(), case.vendor_timezone
        )
        when = mmdd + (f" @ {hhmm}" if hhmm else "")
        if feasible:
            return ResponsePlan(
                ResponseIntent.ACCEPT_OFFER,
                f"offer {when} {why}",
                body=f"Yes, {when} works. Thank you!",
                to_addr=case.contact_email,
                proposed_local=f"{result.pickup_date} {clock or ''}".strip(),
                decision=f"Accept the pickup time they offered, {when}: tell them it works.",
                must_include=tuple(x for x in (mmdd, hhmm) if x),
            )
        days = alternative_days(case, self.settings, now=self._now())
        if not days:
            return ResponsePlan(ResponseIntent.HANDOFF, f"offer {why}; no workable day left")
        delivery = as_utc(case.delivery_at_utc)
        deliver_on = to_eastern(delivery).strftime("%m/%d") if delivery else "our delivery"
        ref = f" ({case.delivery_ref})" if case.delivery_ref else ""
        body = (
            f"That would not make our delivery appointment on {deliver_on}{ref}. "
            f"What do you have available on {' or '.join(days)}? Thank you!"
        )
        return ResponsePlan(
            ResponseIntent.ASK_ALTERNATIVE,
            f"offer {why}",
            body=body,
            to_addr=case.contact_email,
            decision=(
                f"Their offer of {when} would not make our delivery appointment on "
                f"{deliver_on}{ref}. Ask what they have available on {' or '.join(days)}."
            ),
            must_include=tuple(days) + ((deliver_on,) if delivery else ()),
        )

    def _plan_question(
        self, case: BookingCase, result: ReplyClassification, text: str
    ) -> ResponsePlan:
        """Answer from the fixed rules; the writer, when there is one, answers every question."""
        question = (result.question or text).strip()
        facts = self._facts(case)
        draft = answer_from_rules(question, facts)
        why = f"no rule answers: {question[:120]}"
        if draft is not None:
            ok, why = answer_is_safe(draft, facts)
            draft = draft if ok else None
        if draft is None and self.writer is None:
            return ResponsePlan(ResponseIntent.HANDOFF, why)
        return ResponsePlan(
            ResponseIntent.ANSWER_QUESTION,
            f"answered from {', '.join(draft.facts_used) or 'facts'}" if draft else why,
            body=f"{draft.message} Thank you!" if draft else None,
            to_addr=case.contact_email,
            decision="Answer their questions.",
        )

    # -- writing for the situation

    def _facts(self, case: BookingCase) -> dict[str, Any]:
        """The fact sheet for ``case``: its own facts now, its load's read once per responder."""
        facts = case_facts(case, self.settings)
        if self.facts is None:
            return facts
        if case.load_id not in self._loads_read:
            self._loads_read[case.load_id] = read_load_facts(case, self.facts)
        return with_load(facts, self._loads_read[case.load_id])

    @staticmethod
    def _history(case: BookingCase) -> tuple[str, ...]:
        recent = [m for m in case.messages if (m.body or "").strip()][-4:]
        return tuple(
            f"{'us' if m.direction == 'out' else 'facility'}: {(m.body or '').strip()[:800]}"
            for m in recent
        )

    def _write(
        self,
        case: BookingCase,
        reply: BookingMessage | None,
        result: ReplyClassification | None,
        plan: ResponsePlan,
    ) -> ResponsePlan:
        """The plan with its words written for what the facility wrote, or the fixed wording.

        The writer states the decision and answers every question it can from the facts; the
        ones it cannot are left for a person (``unanswered``). Its draft goes out only when it
        passes its check. Otherwise the fixed wording goes, and every question but the one a
        fixed rule answered is left for a person; an answer with no fixed wording is handed off.
        """
        if plan.intent == ResponseIntent.HANDOFF or result is None:
            return plan
        asked = questions_of(result)
        if plan.intent == ResponseIntent.ANSWER_QUESTION and not asked:
            asked = [((result.question or (reply.body if reply else "")) or "").strip()]
        asked = [q for q in asked if q]
        # A fixed rule answered the main question (the reading's own, else the first listed).
        fixed = plan.intent == ResponseIntent.ANSWER_QUESTION and plan.body is not None
        main = (result.question or "").strip()
        main = main if main in asked else (asked[0] if asked else "")
        fallback = replace(plan, unanswered=tuple(q for q in asked if not (fixed and q == main)))
        if self.writer is None:
            return fallback
        situation = ReplySituation(
            intent=plan.intent.value,
            decision=plan.decision or "Thank them.",
            must_include=plan.must_include,
            their_words=(reply.body or "") if reply else "",
            questions=tuple(asked),
            conditions=tuple(result.conditions),
            facts=self._facts(case),
            history=self._history(case),
        )
        try:
            checked = check_reply(self.writer.write(situation), situation)
            problems = checked.problems
        except ExtractionError as exc:
            checked, problems = None, [f"the writer failed: {exc}"]
        if checked is not None and checked.ok and fixed and not checked.answered:
            return fallback  # a holding reply is worse than the fixed answer the rules have
        if checked is not None and checked.ok:
            return replace(
                plan,
                body=checked.body,
                unanswered=tuple(checked.unanswered),
                written=True,
                reason=f"{plan.reason}; written for the situation",
            )
        note = "; ".join(problems)[:200]
        log.warning("booking.reply_not_written", case=case.id, intent=plan.intent, why=note)
        if plan.body is None:
            return ResponsePlan(ResponseIntent.HANDOFF, f"no answer passed its check: {note}")
        return replace(fallback, reason=f"{plan.reason}; fixed wording ({note})")

    def _plan_rejection(
        self, case: BookingCase, result: ReplyClassification, text: str
    ) -> ResponsePlan:
        reason = result.reject_reason or RejectReason.OTHER
        if reason not in TIMING_REASONS:
            return ResponsePlan(ResponseIntent.HANDOFF, NOT_THE_DAY[reason])
        customer = customer_of(case, self.settings)
        desk = customer.customer_desk
        if not desk:
            who = customer.label(case.customer_name)
            return ResponsePlan(
                ResponseIntent.HANDOFF, f"vendor cannot ship; no customer desk set for {who}"
            )
        pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
        quote = (result.question or text).strip().replace("\n", " ")[:300]
        ready = (
            f" Shipper needs to ship this out on {_fmt(result.pickup_date, None)}."
            if result.pickup_date
            else ""
        )
        body = (
            "Hello,\n\n"
            f"Please see the below from {case.vendor_name or 'the shipper'} on PO# {pos}:\n\n"
            f'"{quote}"\n\n'
            f"{ready.strip()} Can you please assist with a new delivery appointment?\n\n"
            "Thank you!\n\n"
            f"{customer.signature or self.settings.booking_signature}"
        )
        return ResponsePlan(
            ResponseIntent.ESCALATE_TO_CUSTOMER,
            "vendor cannot ship as planned; customer must move the delivery",
            body=body,
            to_addr=desk,
        )

    # -- acting

    def act(
        self, session: Session, case: BookingCase, reply: BookingMessage | None, plan: ResponsePlan
    ) -> BookingMessage | None:
        """Draft the planned message, record it, move the case and settle what the reply raised."""
        if plan.intent == ResponseIntent.HANDOFF or not plan.body or not plan.to_addr:
            if annotate(session, case, plan.reason) is None:
                flag(session, case, ExceptionType.HANDOFF, plan.reason)
            self._event(session, case, "handoff", reason=plan.reason)
            return None
        subject = self._subject(case, reply, plan)
        escalation = plan.intent == ResponseIntent.ESCALATE_TO_CUSTOMER
        customer = customer_of(case, self.settings)
        outbox = self._outbox(case, plan.intent)
        signature = customer.signature or self.settings.booking_signature
        draft = OutboundDraft(
            to_addr=plan.to_addr,
            cc_addr=customer.cc_header,
            from_addr=customer.sender,
            reply_to=customer.group,
            subject=subject,
            body=plan.body if escalation or plan.signed else f"{plan.body}\n\n{signature}",
            thread_id=None if escalation else case.thread_id,
            # A new thread to the customer desk answers nothing; everything else answers the
            # vendor's message by its RFC Message-ID so the next reply threads back to the case.
            in_reply_to=None if escalation or reply is None else reply.rfc_message_id,
            references=None if escalation or reply is None else reply.references_header,
        )
        refused = (
            self._refused(session, case, reply, draft, plan.intent) if is_sender(outbox) else None
        )
        if refused is not None:
            if is_sender(self.mailer):  # nowhere to keep a draft: a person takes it from here
                return self.act(
                    session,
                    case,
                    reply,
                    ResponsePlan(ResponseIntent.HANDOFF, f"not sent: {refused}"),
                )
            outbox = self.mailer
        message = BookingMessage(
            case_id=case.id,
            direction="out",
            kind=plan.intent.value,
            to_addr=draft.to_addr,
            cc_addr=draft.cc_addr,
            subject=subject,
            body=draft.body,
            thread_id=draft.thread_id,
        )
        case.messages.append(message)
        session.flush()
        delivery = dispatch(session, case, message, outbox, draft)
        ref = delivery.ref
        if refused is not None:
            note = f"the agent's answer was not sent ({refused}); it is a draft for a person"
            if annotate(session, case, note) is None:
                flag(session, case, ExceptionType.HANDOFF, note[:255])
            self._event(session, case, "reply_not_sent", reason=refused, draft_ref=ref)
        self._settle(session, case, plan, sent=delivery.sent)
        if plan.unanswered:
            raise_questions(session, case, plan.unanswered, replied=plan.written)
        session.flush()
        self._event(
            session,
            case,
            plan.intent.value,
            reason=plan.reason,
            to=draft.to_addr,
            draft_ref=ref,
            written=plan.written,
            unanswered=list(plan.unanswered),
        )
        return message

    def _settle(
        self, session: Session, case: BookingCase, plan: ResponsePlan, *, sent: bool
    ) -> None:
        """Move the case for the answer that went out (or was drafted) and settle its to-do."""
        if plan.intent == ResponseIntent.ACCEPT_OFFER and plan.proposed_local:
            day, _, clock = plan.proposed_local.partition(" ")
            start = local_dt(day, clock or None, case.vendor_timezone).astimezone(UTC)
            case.confirmed_local = plan.proposed_local
            case.confirmed_start_utc = start
            case.confirmed_end_utc = start
            case.status = CaseStatus.PENDING.value
            case.reason = None
            # The offer is settled; what it books still waits for a person's approval.
            resolve(
                session,
                case,
                [ExceptionType.PROPOSED_TIME_REVIEW],
                resolution=f"agent accepted: {plan.reason}",
            )
            pickup = f", pickup# {case.pickup_number}" if case.pickup_number else ""
            flag(
                session,
                case,
                ExceptionType.CONFIRMATION_REVIEW,
                f"vendor offered {slot_text(plan.proposed_local, case.vendor_timezone)}{pickup}; "
                "the agent accepted it, approve "
                "to accept",
                local=plan.proposed_local,
                pickup_number=case.pickup_number,
                accepted_by_agent=True,
            )
            if sent:  # the vendor has the agent's "yes"; book it when the rule says so
                from facility_profiles.booking.service import auto_confirm  # noqa: PLC0415

                auto_confirm(
                    session,
                    case,
                    self.settings,
                    now=self._now(),
                    reason=f"the agent accepted the vendor's {plan.proposed_local} and said so",
                )
        elif plan.intent == ResponseIntent.ASK_ALTERNATIVE:
            case.status = CaseStatus.PENDING.value
            case.reason = None
            resolve(
                session,
                case,
                [ExceptionType.PROPOSED_TIME_REVIEW],
                resolution=f"agent asked for other days: {plan.reason}",
            )
        elif plan.intent == ResponseIntent.ANSWER_QUESTION:
            # A question never moved the slot, so the status stays; only the question is settled.
            # An ETA asked for is settled too once nothing in the answer was left open.
            settled = [ExceptionType.FACILITY_QUESTION]
            if not plan.unanswered:
                settled.append(ExceptionType.ETA_REQUESTED)
            resolve(session, case, settled, resolution=f"agent {plan.reason}")
        elif plan.intent == ResponseIntent.FOLLOW_UP:
            case.status = CaseStatus.PENDING.value
            if sent:  # a draft has not chased anyone yet; a sent follow-up has
                resolve(session, case, [ExceptionType.UNANSWERED_24H], resolution="follow-up sent")
        elif plan.intent == ResponseIntent.ACKNOWLEDGE:
            pass  # the case already moved on the confirmation itself
        else:  # escalation: the decline stays open until the customer moves the delivery
            done = "sent" if sent else "drafted"
            annotate(
                session,
                case,
                f"note to the customer desk {done}; the customer must move the delivery",
            )

    def respond(
        self,
        session: Session,
        case: BookingCase,
        reply: BookingMessage,
        result: ReplyClassification,
    ) -> tuple[ResponsePlan, BookingMessage | None]:
        """Plan and act in one step."""
        plan = self.plan(case, reply, result)
        return plan, self.act(session, case, reply, plan)

    def follow_up(self, session: Session, case: BookingCase) -> BookingMessage | None:
        """Nudge the desk when it has gone quiet, counting silence the way the to-dos count it.

        - Silence starts at the first email the desk has not answered (``timers.waiting_since``):
          an out-of-office, a delay notice, a note to the customer or a person's email does not
          end it. After FP_BOOKING_FOLLOW_UP_HOURS weekday hours of it (weekends and holidays do
          not count), a follow-up.
        - "Check back on <day>": a check-back on that day. "Check back later" with no day: a
          check-back after the same weekday hours. Either goes even after an earlier follow-up,
          because the desk answered since.
        - One nudge per silence, and at most :data:`MAX_NUDGES` on a pickup; after that the
          no-reply to-dos and a person take over.

        Only a pending case with nothing open but the vendor's silence itself is nudged: a
        confirmation waiting for approval or a question waiting for a person is not the
        vendor's silence.
        """
        silence = {k.value for k in UNANSWERED}
        if case.status != CaseStatus.PENDING.value or any(
            e.kind not in silence for e in case.open_exceptions
        ):
            return None
        nudges = [
            m
            for m in case.messages
            if m.direction == "out" and m.kind == ResponseIntent.FOLLOW_UP.value
        ]
        if len(nudges) >= MAX_NUDGES:
            return None
        answers = [m for m in case.messages if is_vendor_answer(m) and _at(m) is not None]
        last = max(answers, key=lambda m: _at(m) or datetime.min.replace(tzinfo=UTC), default=None)
        last_at = _at(last) if last is not None else None
        if any(last_at is None or (_at(m) or last_at) > last_at for m in nudges):
            return None  # this silence, or this check-back, was nudged already
        tz = ZoneInfo(case.vendor_timezone or "America/New_York")
        now = self._now()
        hours = self.settings.booking_follow_up_hours
        reading = (last.classification or {}) if last is not None else {}
        if last_at is not None and reading.get("status") == ReplyStatus.DEFERRED.value:
            day = _day(reading.get("pickup_date"))
            if day is not None:
                if now.astimezone(tz).date() < day:
                    return None  # the vendor said when to ask again; wait for that day
                reason = f"vendor said to check back on {day:%m/%d}"
            else:
                if weekday_hours(last_at, now, tz) < hours:
                    return None
                reason = f"vendor said to check back later; {hours} weekday hours since"
            kind = TemplateKind.CHECK_BACK
        else:
            waiting = waiting_since(case)
            since = as_utc(waiting.sent_at) if waiting is not None else None
            if since is None or weekday_hours(since, now, tz) < hours:
                return None
            reason = f"no reply for {hours} weekday hours"
            kind = TemplateKind.FOLLOW_UP
        names = customer_of(case, self.settings).template_matches(case.customer_name)
        template = pick(session, kind, desk=case.contact_email, customer=names)
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        _, body = render(template, case_values([case], self.settings, profile))
        plan = ResponsePlan(
            ResponseIntent.FOLLOW_UP,
            f"{reason} ({template.source} wording)" if template.source != "built-in" else reason,
            body=body,
            to_addr=case.contact_email,
            signed=True,
        )
        return self.act(session, case, None, plan)

    def acknowledge(
        self,
        session: Session,
        case: BookingCase,
        reply: BookingMessage,
        result: ReplyClassification | None = None,
    ) -> BookingMessage | None:
        """The pod always answers a confirmation with "Thank you!"; so does the agent, once.

        Given the reading, the thanks is written for the situation: it also answers what the
        confirmation asked ("Confirmed for 9am. What's the trailer number?").
        """
        if any(
            m.direction == "out" and m.kind == ResponseIntent.ACKNOWLEDGE.value
            for m in case.messages
        ):
            return None
        slot = case.confirmed_local or case.requested_local
        mmdd, hhmm = vendor_when(slot, case.vendor_timezone) if slot else ("", "")
        when = f" for {mmdd}" + (f" @ {hhmm}" if hhmm else "") if mmdd else ""
        plan = ResponsePlan(
            ResponseIntent.ACKNOWLEDGE,
            "vendor confirmed",
            body="Thank you!",
            to_addr=reply.from_addr or case.contact_email,
            decision=f"They confirmed the pickup{when}. Thank them.",
        )
        return self.act(session, case, reply, self._write(case, reply, result, plan))

    def answer_later(self, session: Session, case: BookingCase) -> BookingMessage | None:
        """Answer what the facility asked about the carrier or the driver, now there is one.

        The question waits in Needs you (``facility_question``) because no carrier was on the
        load when it came, and the reply said someone would get back to them. Once Transport Pro
        shows a carrier, the answer goes in the thread, written for the situation or in fixed
        words, drafted or sent by the customer's rule like any answer, and the to-do is settled;
        what it still cannot answer stays for a person. It is tried once for each carrier and
        driver seen, and not at all while a person is writing to the facility about it.
        """
        current = open_exceptions(case, ExceptionType.FACILITY_QUESTION)
        if not current or self.facts is None or case.status == CaseStatus.CANCELED.value:
            return None
        exc = current[-1]
        asked = [q.strip() for q in str((exc.detail or {}).get("question") or "").split("; ")]
        asked = [q for q in asked if q]
        if not any(about_hauler(q) for q in asked):
            return None
        if str((case.tpro_seen or {}).get("load_status") or "").lower() in PICKED_UP:
            return None
        facts = self._facts(case)
        if facts.get("carrier_assigned") != "yes":
            return None
        seen = hauler_seen(facts)
        if (exc.detail or {}).get("hauler_seen") == seen:
            return None
        exc.detail = {**(exc.detail or {}), "hauler_seen": seen}
        reply = self._asked_in(case, exc)
        if reply is None or _person_wrote_since(case, as_utc(exc.raised_at)):
            return None
        plan = self._later_plan(case, reply, asked, facts)
        if plan is None:
            return None
        message = self.act(session, case, reply, plan)
        for left in open_exceptions(case, ExceptionType.FACILITY_QUESTION):
            left.detail = {**(left.detail or {}), "hauler_seen": seen}  # wait for the next change
        session.flush()
        return message

    def _later_plan(
        self,
        case: BookingCase,
        reply: BookingMessage,
        asked: list[str],
        facts: dict[str, Any],
    ) -> ResponsePlan | None:
        """The answer to questions that waited for a carrier; None when it still has none."""
        decision = (
            "Follow up on what they asked earlier: the carrier is on the load now. "
            "Answer their questions."
        )
        plan = ResponsePlan(
            ResponseIntent.ANSWER_QUESTION,
            "the carrier is on the load now: answered what they asked before",
            to_addr=reply.from_addr or case.contact_email,
            decision=decision,
        )
        if self.writer is not None:
            situation = ReplySituation(
                intent=plan.intent.value,
                decision=decision,
                their_words=reply.body or "",
                questions=tuple(asked),
                facts=facts,
                history=self._history(case),
            )
            try:
                checked = check_reply(self.writer.write(situation), situation)
            except ExtractionError as exc:
                log.warning("booking.later_not_written", case=case.id, error=str(exc))
                checked = None
            if checked is not None and checked.ok and checked.answered:
                return replace(
                    plan,
                    body=checked.body,
                    unanswered=tuple(checked.unanswered),
                    written=True,
                    reason=f"{plan.reason}; written for the situation",
                )
        hauled = [q for q in asked if about_hauler(q)]
        fixed = hauler_answer(" ".join(hauled), facts)
        if fixed is None:
            return None
        return replace(
            plan,
            body=f"{fixed} Thank you!",
            unanswered=tuple(q for q in asked if q not in hauled),
        )

    @staticmethod
    def _asked_in(case: BookingCase, exc: CaseException) -> BookingMessage | None:
        """The facility's email the question came in: its last before the to-do was raised."""
        raised = as_utc(exc.raised_at)
        theirs = [
            m
            for m in case.messages
            if m.direction == "in" and m.kind not in AUTOMATED_KINDS and m.kind != PERSON_MAIL
        ]
        before = [m for m in theirs if raised is None or (as_utc(m.created_at) or raised) <= raised]
        pool = before or theirs
        return pool[-1] if pool else None

    # -- helpers

    def _outbox(self, case: BookingCase, intent: ResponseIntent) -> Mailer | Sender:
        """The sender when the customer's rule and the mode say send, else the drafts."""
        if self.sender is not None and self.settings.booking_mode == "send":
            rule = customer_of(case, self.settings).rule_for(case)
            wanted = (
                rule.customer_notes
                if intent == ResponseIntent.ESCALATE_TO_CUSTOMER
                else rule.reply_mode
            )
            if wanted == "send":
                return self.sender
        return self.mailer

    def _refused(
        self,
        session: Session,
        case: BookingCase,
        reply: BookingMessage | None,
        draft: OutboundDraft,
        intent: ResponseIntent,
    ) -> str | None:
        """Why the send gate refuses this answer, or None when it may go.

        Besides the desk on the profile, an answer may go to the person at that desk's company
        who wrote the reply, and a note for the customer to the desk in the customer's file.
        """
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        trusted = profile.contact_email if profile and profile.can_email else None
        also: list[str | None] = []
        if intent == ResponseIntent.ESCALATE_TO_CUSTOMER:
            also.append(customer_of(case, self.settings).customer_desk)
        elif reply is not None and trusted:
            wrote = address(reply.from_addr)
            if wrote.rpartition("@")[2] == address(trusted).rpartition("@")[2]:
                also.append(wrote)
        try:
            check_send_gate(session, case, draft, self.settings, trusted_desk=trusted, also=also)
        except SendRefusedError as exc:
            return str(exc)
        return None

    def _subject(self, case: BookingCase, reply: BookingMessage | None, plan: ResponsePlan) -> str:
        if plan.intent == ResponseIntent.ESCALATE_TO_CUSTOMER:
            pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
            return f"RESCHEDULE {pos}"
        base = (reply.subject if reply and reply.subject else None) or next(
            (
                m.subject
                for m in case.messages
                if m.direction == "out" and m.kind != PERSON_MAIL and m.subject
            ),
            "",
        )
        base = base or f"Pick Up Appointment: {' & '.join(str(p) for p in case.po_numbers)}"
        return base if base.lower().startswith("re:") else f"Re: {base}"

    @staticmethod
    def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
        session.add(BookingEvent(case_id=case.id, action=action, actor="agent", detail=detail))
