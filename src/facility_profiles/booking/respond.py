"""The conversational half of the agent: what to say back to a vendor, and when to stop.

The policy is deliberately narrow. The agent only ever does five things in a thread:

1. accept a counter-offer that still makes the customer's delivery slot,
2. ask for alternatives inside a stated window when it does not,
3. answer a factual question from data already on the case,
4. nudge once when a request goes unanswered,
5. draft a note to the customer's inbound desk when the vendor cannot ship as planned.

Everything else, and anything that mentions money, is handed to a person. Every message is a
draft in draft mode.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.orm import Session

from facility_profiles import __version__
from facility_profiles.booking.mail import Mailer, OutboundDraft
from facility_profiles.booking.models import BookingCase, BookingEvent, BookingMessage, CaseStatus
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.config import Settings
from facility_profiles.extraction.llm import ExtractionError
from facility_profiles.extraction.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)
from facility_profiles.storage.repository import as_utc

FORBIDDEN_TOPICS = re.compile(
    r"\b(rate|rates|detention|accessorial|tonu|invoice|charge|charges|fee|fees|payment|pay|"
    r"claim|damage|lumper)\b|\$\s?\d",
    re.I,
)
ANSWER_FACT_KEYS = (
    "po_numbers",
    "carrier",
    "equipment",
    "customer",
    "delivery_site",
    "delivery_date",
    "delivery_ref",
    "requested_local",
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


# ------------------------------------------------------------------ facts and feasibility


def case_facts(case: BookingCase, settings: Settings) -> dict[str, Any]:
    """The only facts an answer may contain."""
    tz = ZoneInfo(case.vendor_timezone or "America/New_York")
    delivery = as_utc(case.delivery_at_utc)
    return {
        "po_numbers": [str(p) for p in case.po_numbers],
        "carrier": settings.booking_carrier_name,
        "equipment": None,
        "customer": customer_label(case.customer_name),
        "delivery_site": case.delivery_site,
        "delivery_date": delivery.astimezone(tz).strftime("%m/%d") if delivery else None,
        "delivery_ref": case.delivery_ref,
        "requested_local": case.requested_local,
        "load_id": case.load_id,
        "pickup_number": case.pickup_number,
    }


def customer_label(name: str | None) -> str:
    """Strip the inbound/outbound suffix: "Lidl - Inbound" becomes "Lidl"."""
    return re.sub(r"\s*-\s*(inbound|outbound)\s*$", "", name or "Lidl", flags=re.I).strip()


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
        return False, (
            f"would arrive {arrival:%m/%d %H:%M}Z, after the delivery slot {delivery:%m/%d %H:%M}Z"
        )
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


def answer_from_rules(question: str, facts: dict[str, Any]) -> AnswerDraft | None:
    """Deterministic answers for the questions vendors actually ask."""
    q = question.lower()
    pos = facts.get("po_numbers") or []
    if re.search(r"\b(both|which|what)\b.*\b(order|orders|po|pos|po#|po number)", q) or re.search(
        r"\b(order|po)\s*(number|#)", q
    ):
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
    if re.search(r"\b(which|what|who)\b.*\bcarrier\b|\bcarrier\b.*\?", q):
        carrier = str(facts["carrier"])
        return AnswerDraft(
            answerable=True,
            message=f"The carrier is {carrier}" + ("" if carrier.endswith(".") else "."),
            facts_used=["carrier"],
        )
    if re.search(r"\b(where|which)\b.*\b(deliver\w*|going|destination)\b", q) and facts.get(
        "delivery_site"
    ):
        ref = f" ({facts['delivery_ref']})" if facts.get("delivery_ref") else ""
        when = f" on {facts['delivery_date']}" if facts.get("delivery_date") else ""
        return AnswerDraft(
            answerable=True,
            message=f"This is delivering to {facts['delivery_site']}{when}{ref}.",
            facts_used=["delivery_site", "delivery_date", "delivery_ref"],
        )
    if re.search(r"\b(customer|who is this for|for whom|consignee)\b", q):
        return AnswerDraft(
            answerable=True,
            message=f"This is a {facts['customer']} order.",
            facts_used=["customer"],
        )
    if re.search(r"\b(load|reference|ref)\s*(number|#)", q):
        return AnswerDraft(
            answerable=True,
            message=f"Our load number is {facts['load_id']}.",
            facts_used=["load_id"],
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
    """Decides the next message for a case after a classified reply, and drafts it."""

    settings: Settings
    mailer: Mailer
    composer: AnswerComposer | None = None
    now: datetime | None = None

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
            when = _fmt(result.pickup_date, clock)
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
        tz = ZoneInfo(case.vendor_timezone or "America/New_York")
        deliver_on = delivery.astimezone(tz).strftime("%m/%d") if delivery else "our delivery"
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
        desk = self.settings.booking_customer_desk
        if not desk:
            return ResponsePlan(ResponseIntent.HANDOFF, "vendor cannot ship; no customer desk set")
        pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
        quote = (result.question or text).strip().replace("\n", " ")[:300]
        body = (
            "Hello,\n\n"
            f"Please see the note below from {case.vendor_name or 'the shipper'} on PO# {pos}:\n\n"
            f'"{quote}"\n\n'
            "Can you please assist with a new delivery appointment? Thank you!\n\n"
            f"{self.settings.booking_signature}"
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
        """Draft the planned message, record it and move the case."""
        if plan.intent == ResponseIntent.HANDOFF or not plan.body or not plan.to_addr:
            case.status = CaseStatus.NEEDS_HUMAN.value
            case.reason = (f"{case.reason} | agent: {plan.reason}" if case.reason else plan.reason)[
                :255
            ]
            self._event(session, case, "handoff", reason=plan.reason)
            return None
        subject = self._subject(case, reply, plan)
        draft = OutboundDraft(
            to_addr=plan.to_addr,
            cc_addr=", ".join(self.settings.booking_cc),
            subject=subject,
            body=plan.body
            if plan.intent == ResponseIntent.ESCALATE_TO_CUSTOMER
            else (f"{plan.body}\n\n{self.settings.booking_signature}"),
            thread_id=(
                None if plan.intent == ResponseIntent.ESCALATE_TO_CUSTOMER else case.thread_id
            ),
            in_reply_to=reply.message_id if reply else None,
        )
        ref = self.mailer.create_draft(draft)
        message = BookingMessage(
            case_id=case.id,
            direction="out",
            kind=plan.intent.value,
            to_addr=draft.to_addr,
            cc_addr=draft.cc_addr,
            subject=subject,
            body=draft.body,
            thread_id=draft.thread_id,
            draft_ref=ref,
        )
        case.messages.append(message)
        if plan.intent == ResponseIntent.ACCEPT_OFFER and plan.proposed_local:
            day, _, clock = plan.proposed_local.partition(" ")
            start = local_dt(day, clock or None, case.vendor_timezone).astimezone(UTC)
            case.confirmed_local = plan.proposed_local
            case.confirmed_start_utc = start
            case.confirmed_end_utc = start
            case.status = CaseStatus.PROPOSED.value
            case.reason = None
        elif plan.intent in (ResponseIntent.ASK_ALTERNATIVE, ResponseIntent.ANSWER_QUESTION):
            case.status = CaseStatus.SENT.value
            case.reason = None
        elif plan.intent == ResponseIntent.FOLLOW_UP:
            case.status = CaseStatus.SENT.value
        elif plan.intent == ResponseIntent.ACKNOWLEDGE:
            pass  # the case already moved on the confirmation itself
        else:  # escalation: a person sends it and decides what happens to the pickup
            case.status = CaseStatus.NEEDS_HUMAN.value
            case.reason = plan.reason[:255]
        session.flush()
        self._event(
            session, case, plan.intent.value, reason=plan.reason, to=draft.to_addr, draft_ref=ref
        )
        return message

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
        """Nudge once when a sent request has had no reply for the configured time."""
        if case.status != CaseStatus.SENT.value:
            return None
        outbound = [m for m in case.messages if m.direction == "out"]
        if not outbound or any(m.kind == ResponseIntent.FOLLOW_UP.value for m in outbound):
            return None
        last_out = as_utc(outbound[-1].sent_at) or as_utc(outbound[-1].created_at)
        if last_out is None or self._now() - last_out < timedelta(
            hours=self.settings.booking_follow_up_hours
        ):
            return None
        replied_since = any(
            m.direction == "in" and (as_utc(m.sent_at) or last_out) > last_out
            for m in case.messages
        )
        if replied_since:
            return None
        plan = ResponsePlan(
            ResponseIntent.FOLLOW_UP,
            f"no reply for {self.settings.booking_follow_up_hours} hours",
            body="Hello,\n\nFollowing up on this.",
            to_addr=case.contact_email,
        )
        return self.act(session, case, None, plan)

    def acknowledge(
        self, session: Session, case: BookingCase, reply: BookingMessage
    ) -> BookingMessage | None:
        """The pod always answers a confirmation with "Thank you!"; so does the agent."""
        plan = ResponsePlan(
            ResponseIntent.ACKNOWLEDGE,
            "vendor confirmed",
            body="Thank you!",
            to_addr=reply.from_addr or case.contact_email,
        )
        return self.act(session, case, reply, plan)

    # -- helpers

    def _subject(self, case: BookingCase, reply: BookingMessage | None, plan: ResponsePlan) -> str:
        if plan.intent == ResponseIntent.ESCALATE_TO_CUSTOMER:
            pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
            return f"{pos} - pickup pushed by {case.vendor_name or 'shipper'}"
        base = (reply.subject if reply and reply.subject else None) or next(
            (m.subject for m in case.messages if m.direction == "out" and m.subject), ""
        )
        base = base or f"Pick Up Appointment: {' & '.join(str(p) for p in case.po_numbers)}"
        return base if base.lower().startswith("re:") else f"Re: {base}"

    @staticmethod
    def _event(session: Session, case: BookingCase, action: str, **detail: Any) -> None:
        session.add(BookingEvent(case_id=case.id, action=action, actor="agent", detail=detail))
