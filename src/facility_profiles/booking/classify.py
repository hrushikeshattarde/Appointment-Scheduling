"""Read a vendor reply with a structured-output model call, then check it against the text."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from facility_profiles import __version__
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.domain.normalize import quote_in_source
from facility_profiles.extraction.llm import ExtractionError, LLMUsage
from facility_profiles.extraction.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)

PROMPT_VERSION = "reply-v1"

SYSTEM_PROMPT = """You read one email reply from a shipping facility to a freight broker's pickup \
appointment request and return a JSON object describing it.

Rules:
- status: "confirmed" when the vendor books the pickup: the requested slot ("SET!", "confirmed", \
a pickup number alone), a restated slot, or a time only (then the date is the requested date); \
"deferred" when they ask you to check back later because the order is not released or ready yet \
(put the day to check back in pickup_date); \
"counter_offer" when they offer a different date or time instead; "question" when they need \
something before booking (order number, PO, carrier name, driver info); "rejected" when they \
cannot book (order not ready, not in their system, closed that day); "unrelated" otherwise.
- pickup_date is YYYY-MM-DD and pickup_time is HH:MM in 24-hour local time. Resolve relative \
dates ("tomorrow", "Monday") from the reply date given to you. Use null when not stated.
- pickup_number is the vendor's pickup, confirmation or appointment number, if given.
- quotes are short verbatim snippets copied from the reply that contain each date, time and \
number you report. Never paraphrase inside quotes.
- conditions are rules the vendor states (arrive early, bring load bars, register at gate).
- Only use the reply text. Do not invent values that are not written there."""


@dataclass(frozen=True)
class ReplyContext:
    """What the classifier is told about the request the reply answers."""

    vendor_name: str
    po_numbers: list[str]
    requested_local: str | None
    reply_sent_at: datetime
    subject: str
    body: str


@dataclass(frozen=True)
class ClassifierOutput:
    """A validated classification plus provenance."""

    result: ReplyClassification
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    request_id: str | None = None


@dataclass
class ClassificationIssue:
    """A value the text does not back."""

    field_name: str
    reason: str
    value: Any = None


class ReplyClassifier(Protocol):
    """Anything that turns a reply into a :class:`ReplyClassification`."""

    def classify(self, context: ReplyContext) -> ClassifierOutput:
        """Classify one reply."""
        ...


def render_user_message(context: ReplyContext) -> str:
    """The user turn: the request being answered and the reply itself."""
    requested = context.requested_local or "not stated"
    return (
        f"Our request: pickup at {context.vendor_name} for PO "
        f"{', '.join(context.po_numbers) or 'unknown'}, requested {requested}.\n"
        f"Reply date: {context.reply_sent_at:%Y-%m-%d %A %H:%M}\n"
        f"Subject: {context.subject}\n\n"
        f'Reply text:\n"""\n{context.body.strip()}\n"""'
    )


def _number_in_text(number: str, text: str) -> bool:
    """A pickup number counts only when it appears verbatim or as its full digit run."""
    if number.lower() in text.lower():
        return True
    run = "".join(ch for ch in number if ch.isdigit())
    return len(run) >= 4 and run in text


def validate_classification(
    result: ReplyClassification, text: str
) -> tuple[ReplyClassification, list[ClassificationIssue]]:
    """Drop values without a verbatim quote in the reply; return what survives and why."""
    issues: list[ClassificationIssue] = []
    kept_quotes = [q for q in result.quotes if q.strip() and quote_in_source(q, text)]
    for q in result.quotes:
        if q not in kept_quotes:
            issues.append(ClassificationIssue("quotes", "quote not found in reply", q))
    backed = " ".join(kept_quotes)
    data = result.model_dump()
    data["quotes"] = kept_quotes

    if result.pickup_number and not _number_in_text(result.pickup_number, text):
        issues.append(
            ClassificationIssue("pickup_number", "number not in reply", result.pickup_number)
        )
        data["pickup_number"] = None
    for name in ("pickup_date", "pickup_time"):
        value = getattr(result, name)
        if value and not backed:
            issues.append(ClassificationIssue(name, "no quote backs this value", value))
            data[name] = None
    if data["status"] == ReplyStatus.CONFIRMED and not kept_quotes:
        issues.append(ClassificationIssue("status", "confirmation without any backed quote"))
        data["status"] = ReplyStatus.QUESTION if data["question"] else ReplyStatus.UNRELATED
    if data["status"] == ReplyStatus.COUNTER_OFFER and not (
        data["pickup_date"] or data["pickup_time"]
    ):
        issues.append(ClassificationIssue("status", "counter-offer without a backed date or time"))
        data["status"] = ReplyStatus.QUESTION if data["question"] else ReplyStatus.UNRELATED
    return ReplyClassification.model_validate(data), issues


class OpenRouterReplyClassifier:
    """Strict JSON-schema classification through OpenRouter (same plumbing as extraction)."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 120.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            msg = "OpenRouter API key is required (OPENROUTER_API_KEY)"
            raise ValueError(msg)
        self.model = qualify_model(model)
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._schema = strict_json_schema(ReplyClassification)
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": f"facility-profiles-booking/{__version__}",
            },
        )

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()

    def classify(self, context: ReplyContext) -> ClassifierOutput:
        """One call, schema-valid by construction, then pydantic-validated."""
        body = {
            "model": self.model,
            "max_tokens": 1500,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": render_user_message(context)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "vendor_reply", "strict": True, "schema": self._schema},
            },
            "provider": {"require_parameters": True},
        }
        try:
            response = self._http.post(self._url, json=body)
        except httpx.TransportError as exc:
            raise ExtractionError(f"could not reach OpenRouter: {exc}", retryable=True) from exc
        data = OpenRouterExtractor._parse_envelope(response)
        choices = data.get("choices") or []
        if not choices:
            raise ExtractionError("OpenRouter returned no choices")
        content = (choices[0].get("message") or {}).get("content")
        if isinstance(content, list):
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError("OpenRouter returned empty content")
        try:
            result = ReplyClassification.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ExtractionError(f"reply classification invalid: {exc}") from exc
        usage = data.get("usage") or {}
        return ClassifierOutput(
            result=result,
            model=str(data.get("model") or self.model),
            usage=LLMUsage(
                input_tokens=int(usage.get("prompt_tokens") or 0),
                output_tokens=int(usage.get("completion_tokens") or 0),
            ),
            request_id=response.headers.get("x-request-id") or data.get("id"),
        )


@dataclass
class FakeReplyClassifier:
    """Scripted classifier for tests and dry runs: a function of the reply body."""

    script: Callable[[ReplyContext], ReplyClassification]
    calls: list[ReplyContext] = field(default_factory=list)
    model: str = "fake-reply-classifier"

    def classify(self, context: ReplyContext) -> ClassifierOutput:
        """Return the scripted classification."""
        self.calls.append(context)
        return ClassifierOutput(result=self.script(context), model=self.model)
