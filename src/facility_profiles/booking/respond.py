"""The conversational half of the agent: what to say back to a vendor, and when to stop.

The policy is deliberately narrow. The agent only ever does five things in a thread:

1. accept a counter-offer that still makes the customer's delivery slot,
2. ask for alternatives inside a stated window when it does not,
3. answer a factual question from data already on the case,
4. nudge once when a request goes unanswered,
5. draft a note to the customer's inbound desk when the vendor cannot ship on the day asked
   (the order is not ready, no slots, closed): moving the delivery fixes that, and nothing else.

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
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from facility_profiles import __version__
from facility_profiles.booking.mail import Mailer, OutboundDraft, Sender
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
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
)
from facility_profiles.booking.templates import (
    TemplateKind,
    case_values,
    pick,
    render,
    vendor_when,
)
from facility_profiles.booking.worklist import (
    UNANSWERED,
    annotate,
    flag,
    open_exceptions,
    resolve,
)
from facility_profiles.clock import local_to_eastern, slot_text, stamp, to_eastern
from facility_profiles.config import Settings
from facility_profiles.customers import customer_of
from facility_profiles.extraction.llm import ExtractionError
from facility_profiles.extraction.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)
from facility_profiles.storage.repository import Repository, as_utc

FORBIDDEN_TOPICS = re.compile(
    r"\b(rate|rates|detention|accessorial|tonu|invoice|charge|charges|fee|fees|payment|pay|"
    r"claim|damage|lumper)\b|\$\s?\d",
    re.I,
)
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
ANSWER_FACT_KEYS = (
    "po_numbers",
    "carrier",
    "equipment",
    "customer",
    "delivery_site",
    "delivery_date",
    "delivery_ref",
    "requested_pickup",
    "load_id",
    "pickup_number",
)


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


# ------------------------------------------------------------------ facts and feasibility


def case_facts(case: BookingCase, settings: Settings) -> dict[str, Any]:
    """The only facts an answer may contain. Every date and time is Eastern."""
    delivery = as_utc(case.delivery_at_utc)
    requested = local_to_eastern(case.requested_local, case.vendor_timezone)
    return {
        "po_numbers": [str(p) for p in case.po_numbers],
        "carrier": settings.booking_carrier_name,
        "equipment": None,
        "customer": customer_of(case, settings).label(case.customer_name),
        "delivery_site": case.delivery_site,
        "delivery_date": to_eastern(delivery).strftime("%m/%d") if delivery else None,
        "delivery_ref": case.delivery_ref,
        "requested_pickup": f"{requested} ET" if requested and " " in requested else requested,
        "load_id": case.load_id,
        "pickup_number": case.pickup_number,
    }


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
    if pickup_local.weekday() >= 5:
        return False, "falls on a weekend"
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
    """Up to three weekdays that still make the delivery, latest first, as MM/DD."""
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    delivery = as_utc(case.delivery_at_utc)
    if delivery is None:
        return []
    latest = (delivery - timedelta(hours=transit_hours(case, settings))).astimezone(tz).date()
    earliest = (now + timedelta(hours=settings.booking_min_notice_hours)).astimezone(tz).date()
    days: list[str] = []
    day = latest
    while day >= earliest and len(days) < 3:
        if day.weekday() < 5:
            days.append(day.strftime("%m/%d"))
        day -= timedelta(days=1)
    return days


def rounds_so_far(case: BookingCase) -> int:
    """Outbound messages in the thread after the first request."""
    return max(0, sum(1 for m in case.messages if m.direction == "out") - 1)


# ------------------------------------------------------------------ answers to questions


class AnswerDraft(BaseModel):
    """Structured answer from the model: only allowed facts, or a refusal."""

    answerable: bool
    message: str | None = Field(None, description="Short reply text, or null")
    facts_used: list[str] = Field(default_factory=list, description="Keys of the facts used")
    reason: str | None = None


class AnswerComposer(Protocol):
    """Anything that turns a vendor question plus facts into a reply."""

    def compose(self, question: str, facts: dict[str, Any], history: list[str]) -> AnswerDraft:
        """Compose the answer or say it cannot be answered."""
        ...


# The questions the case itself answers. Each is narrow on purpose: a question that only looks
# like one ("What is the delivery number for this PO?" names a PO but does not ask which PO) must
# not get its answer. Anything else goes to the model, which may use only the facts, or a person.
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
    unknown = [k for k in draft.facts_used if k not in ANSWER_FACT_KEYS]
    if unknown:
        return False, f"answer used facts that do not exist: {', '.join(unknown)}"
    return True, "ok"


ANSWER_SYSTEM_PROMPT = """You draft a one- or two-sentence email reply from a freight broker to a \
shipping facility that asked a question about a pickup appointment request.

Rules:
- Use only the facts given. If the question needs anything else (driver name, truck number, \
rates, times not listed), set answerable=false and explain in reason.
- Never mention rates, charges, detention, fees or payment.
- Keep the same plain tone as the example: "The carrier is Circle Logistics, Inc."
- List every fact key you used in facts_used."""


class OpenRouterAnswerComposer:
    """Model-drafted answers for questions the rules do not cover."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.model = qualify_model(model)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._schema = strict_json_schema(AnswerDraft)
        self._http = httpx.Client(
            timeout=60.0,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": f"facility-profiles-booking/{__version__}",
            },
        )

    def compose(self, question: str, facts: dict[str, Any], history: list[str]) -> AnswerDraft:
        """One strict-schema call."""
        user = (
            f"Facts:\n{json.dumps(facts, indent=2)}\n\nThread so far:\n"
            + "\n---\n".join(history[-4:])
            + f"\n\nThe vendor asked: {question}"
        )
        body = {
            "model": self.model,
            "max_tokens": 600,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": ANSWER_SYSTEM_PROMPT},
                {"role": "user", "content": user},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer_draft", "strict": True, "schema": self._schema},
            },
            "provider": {"require_parameters": True},
        }
        try:
            response = self._http.post(self._url, json=body)
        except httpx.TransportError as exc:
            raise ExtractionError(f"could not reach OpenRouter: {exc}", retryable=True) from exc
        data = OpenRouterExtractor._parse_envelope(response)
        content = ((data.get("choices") or [{}])[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError("OpenRouter returned empty content")
        try:
            return AnswerDraft.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ExtractionError(f"answer draft invalid: {exc}") from exc


@dataclass
class FakeAnswerComposer:
    """Scripted composer for tests."""

    drafts: dict[str, AnswerDraft] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def compose(self, question: str, facts: dict[str, Any], history: list[str]) -> AnswerDraft:
        """Return the scripted draft for the question, else not answerable."""
        del facts, history
        self.calls.append(question)
        return self.drafts.get(question, AnswerDraft(answerable=False, reason="not scripted"))


# ------------------------------------------------------------------ the responder


def _fmt(day: str, clock: str | None) -> str:
    parsed = datetime.strptime(day, "%Y-%m-%d")
    return f"{parsed:%m/%d}" + (f" @ {clock.replace(':', '')}" if clock else "")


@dataclass
class Responder:
    """Decides the next message for a case after a classified reply, and drafts or sends it.

    ``mailer`` drafts (or, passed a sender, sends everything through the gate). ``sender``
    sends what the customer's rule says to send, in send mode; the rest is drafted.
    """

    settings: Settings
    mailer: Mailer | Sender
    composer: AnswerComposer | None = None
    now: datetime | None = None
    sender: Sender | None = None

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
        if result.status == ReplyStatus.COUNTER_OFFER and open_exceptions(
            case, ExceptionType.TIME_ZONE_UNCLEAR
        ):
            return ResponsePlan(
                ResponseIntent.HANDOFF,
                "the facility named no time zone; a person checks which clock they meant",
            )
        if result.status == ReplyStatus.COUNTER_OFFER:
            return self._plan_counter_offer(case, result)
        if result.status == ReplyStatus.QUESTION:
            return self._plan_question(case, result, text)
        if result.status == ReplyStatus.REJECTED:
            return self._plan_rejection(case, result, text)
        return ResponsePlan(ResponseIntent.HANDOFF, f"no policy for a {result.status} reply")

    def _plan_counter_offer(self, case: BookingCase, result: ReplyClassification) -> ResponsePlan:
        if not result.pickup_date:
            return ResponsePlan(ResponseIntent.HANDOFF, "counter-offer without a date")
        clock = result.pickup_time
        offered = local_dt(result.pickup_date, clock, case.vendor_timezone)
        feasible, why = offer_is_feasible(case, offered, self.settings, now=self._now())
        if feasible:
            mmdd, hhmm = vendor_when(
                f"{result.pickup_date} {clock or ''}".strip(), case.vendor_timezone
            )
            when = mmdd + (f" @ {hhmm}" if hhmm else "")
            return ResponsePlan(
                ResponseIntent.ACCEPT_OFFER,
                f"offer {when} {why}",
                body=f"Yes, {when} works. Thank you!",
                to_addr=case.contact_email,
                proposed_local=f"{result.pickup_date} {clock or ''}".strip(),
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
            ResponseIntent.ASK_ALTERNATIVE, f"offer {why}", body=body, to_addr=case.contact_email
        )

    def _plan_question(
        self, case: BookingCase, result: ReplyClassification, text: str
    ) -> ResponsePlan:
        question = (result.question or text).strip()
        facts = case_facts(case, self.settings)
        draft = answer_from_rules(question, facts)
        if draft is None and self.composer is not None:
            history = [
                f"{'us' if m.direction == 'out' else 'vendor'}: {m.body or ''}"
                for m in case.messages
            ]
            draft = self.composer.compose(question, facts, history)
        if draft is None:
            return ResponsePlan(ResponseIntent.HANDOFF, f"no rule answers: {question[:120]}")
        ok, why = answer_is_safe(draft, facts)
        if not ok:
            return ResponsePlan(ResponseIntent.HANDOFF, why)
        assert draft.message is not None
        return ResponsePlan(
            ResponseIntent.ANSWER_QUESTION,
            f"answered from {', '.join(draft.facts_used) or 'facts'}",
            body=f"{draft.message} Thank you!",
            to_addr=case.contact_email,
        )

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
        session.flush()
        self._event(
            session, case, plan.intent.value, reason=plan.reason, to=draft.to_addr, draft_ref=ref
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
            resolve(
                session, case, [ExceptionType.FACILITY_QUESTION], resolution=f"agent {plan.reason}"
            )
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
        """Nudge once when a sent request has had no reply for the configured time.

        Only a pending case with nothing open but the vendor's silence itself is nudged: a
        confirmation waiting for approval or a question waiting for a person is not the
        vendor's silence.
        """
        silence = {k.value for k in UNANSWERED}
        if case.status != CaseStatus.PENDING.value or any(
            e.kind not in silence for e in case.open_exceptions
        ):
            return None
        outbound = [m for m in case.messages if m.direction == "out"]
        if not outbound or any(m.kind == ResponseIntent.FOLLOW_UP.value for m in outbound):
            return None
        last_out = as_utc(outbound[-1].sent_at) or as_utc(outbound[-1].created_at)
        if last_out is None:
            return None
        check_back = self._check_back_date(session, case, since=last_out)
        tz = ZoneInfo(case.vendor_timezone or "America/New_York")
        if check_back is not None:
            if self._now().astimezone(tz).date() < check_back:
                return None  # the vendor said when to ask again; wait for that day
            reason = f"vendor said to check back on {check_back:%m/%d}"
            kind = TemplateKind.CHECK_BACK
        else:
            if self._now() - last_out < timedelta(hours=self.settings.booking_follow_up_hours):
                return None
            replied_since = any(
                m.direction == "in" and (as_utc(m.sent_at) or last_out) > last_out
                for m in case.messages
            )
            if replied_since:
                return None
            reason = f"no reply for {self.settings.booking_follow_up_hours} hours"
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

    @staticmethod
    def _check_back_date(session: Session, case: BookingCase, *, since: datetime) -> date | None:
        """The day the vendor asked to be contacted again (latest deferral after ``since``)."""
        from sqlalchemy import select  # noqa: PLC0415

        events = session.scalars(
            select(BookingEvent)
            .where(BookingEvent.case_id == case.id, BookingEvent.action == "deferred")
            .order_by(BookingEvent.id.desc())
        )
        for event in events:
            created = as_utc(event.created_at)
            if created is not None and created < since:
                break
            raw = (event.detail or {}).get("check_back")
            if raw:
                try:
                    return date.fromisoformat(str(raw))
                except ValueError:
                    return None
        return None

    def acknowledge(
        self, session: Session, case: BookingCase, reply: BookingMessage
    ) -> BookingMessage | None:
        """The pod always answers a confirmation with "Thank you!"; so does the agent, once."""
        if any(
            m.direction == "out" and m.kind == ResponseIntent.ACKNOWLEDGE.value
            for m in case.messages
        ):
            return None
        plan = ResponsePlan(
            ResponseIntent.ACKNOWLEDGE,
            "vendor confirmed",
            body="Thank you!",
            to_addr=reply.from_addr or case.contact_email,
        )
        return self.act(session, case, reply, plan)

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
            (m.subject for m in case.messages if m.direction == "out" and m.subject), ""
        )
        base = base or f"Pick Up Appointment: {' & '.join(str(p) for p in case.po_numbers)}"
        return base if base.lower().startswith("re:") else f"Re: {base}"

    @staticmethod
    def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
        session.add(BookingEvent(case_id=case.id, action=action, actor="agent", detail=detail))
