"""Read a vendor reply with a structured-output model call, then check it against the text."""

from __future__ import annotations

import json
import re
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

PROMPT_VERSION = "reply-v3"

SYSTEM_PROMPT = """You read one email reply from a shipping facility to a freight broker's pickup \
appointment request and return a JSON object describing it.

Rules:
- status: "confirmed" when the vendor books the pickup: the requested slot ("SET!", "confirmed", \
a pickup number alone), a restated slot, or a time only (then the date is the requested date); \
"counter_offer" when they offer a different date or time instead (a reply like "these are good \
for the adjusted time below" with edited times in the quoted text is a counter_offer at those \
edited times); "deferred" when they ask you to check back later because the order is not \
released or ready yet (put the day to check back in pickup_date); "question" when they need \
something before booking (order number, PO, carrier name, driver info); "rejected" when they \
cannot book (order not ready, not in their system, closed that day); "unrelated" otherwise.
- A reply written after the requested time has passed that gives a latest arrival time, offers \
to work the driver in, or asks for the driver's ETA is a "question" (put their ask in question), \
not a confirmation.
- pickup_date is YYYY-MM-DD and pickup_time is HH:MM in 24-hour local time. Resolve relative \
dates ("tomorrow", "Monday") from the reply date given to you. Use null when not stated.
- pickup_number is the vendor's pickup, confirmation or appointment number, if given.
- quotes are short verbatim snippets copied from the reply's own words (the text above any \
quoted earlier messages) that contain each date, time and number you report. Never paraphrase \
inside quotes. Only for a counter_offer where the vendor edited dates or times inside the \
quoted text may a quote come from that quoted text.
- conditions are rules the vendor states (arrive early, bring load bars, register at gate).
- When the reply answers several PO lines separately (one line per PO or PO pair, each with \
its own date, time, pickup number or verdict), fill items with one entry per line: that line's \
PO numbers as written, its status, date, time and pickup number, and quotes from that line. \
Set the top-level fields to the line about the POs marked as ours, or to the overall reading. \
Leave items empty when the reply has one reading for everything.
- Only use the reply text. Do not invent values that are not written there."""

# Outlook wraps numbers and addresses in link cruft ("115802102660<tel:(580)%20210-2660>",
# "name@x.com<mailto:name@x.com>"); the model quotes the clean line, so both sides are cleaned.
_LINK_CRUFT_RE = re.compile(r"<(?:tel|mailto|sms|callto|https?):[^>\s]*>", re.I)
_CID_RE = re.compile(r"\[cid:[^\]]*\]", re.I)
_RELATIVE_DAY_RE = re.compile(
    r"\b(?:today|tonight|tomorrow|mon(?:day)?|tue(?:s|sday)?|wed(?:nesday)?|thu(?:r|rs|rsday)?|"
    r"fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
    re.I,
)


@dataclass(frozen=True)
class ReplyContext:
    """What the classifier is told about the request the reply answers."""

    vendor_name: str
    po_numbers: list[str]
    requested_local: str | None
    reply_sent_at: datetime
    subject: str
    body: str
    quoted: str = ""
    # Every request in the thread when a batched email covered several cases: (POs, slot).
    requests: list[tuple[list[str], str | None]] = field(default_factory=list)


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
    if len(context.requests) > 1:
        lines = "\n".join(
            f"- PO {' & '.join(pos) or 'unknown'}, requested {slot or 'not stated'}"
            for pos, slot in context.requests
        )
        ours = ", ".join(context.po_numbers) or "unknown"
        head = (
            f"Our requests in this thread, pickups at {context.vendor_name}:\n{lines}\n"
            f"Ours for the top-level fields: PO {ours}, requested {requested}.\n"
        )
    else:
        head = (
            f"Our request: pickup at {context.vendor_name} for PO "
            f"{', '.join(context.po_numbers) or 'unknown'}, requested {requested}.\n"
        )
    return (
        head + f"Reply date: {context.reply_sent_at:%Y-%m-%d %A %H:%M}\n"
        f"Subject: {context.subject}\n\n"
        f'Reply text:\n"""\n{context.body.strip()}\n"""'
        + (
            "\n\nQuoted text under the reply (the vendor may have edited dates or times in it):"
            f'\n"""\n{context.quoted.strip()[:1500]}\n"""'
            if context.quoted.strip()
            else ""
        )
    )


def clean_mail_text(text: str | None) -> str:
    """Drop the link cruft mail clients wrap around numbers and addresses, and inline image tags."""
    return _CID_RE.sub("", _LINK_CRUFT_RE.sub("", text or ""))


def _number_in_text(number: str, text: str) -> bool:
    """A pickup number counts only when it appears verbatim or as its full digit run."""
    if number.lower() in text.lower():
        return True
    run = "".join(ch for ch in number if ch.isdigit())
    return len(run) >= 4 and run in text


def _digit_runs(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def date_is_backed(value: str, quotes: str) -> bool:
    """The day of the month, or a relative day word, appears in the backing quotes."""
    try:
        day = datetime.strptime(value, "%Y-%m-%d").day
    except ValueError:
        return False
    runs = _digit_runs(quotes)
    if str(day) in runs or f"{day:02d}" in runs:
        return True
    return _RELATIVE_DAY_RE.search(quotes) is not None


def time_is_backed(value: str, quotes: str) -> bool:
    """The hour appears in the backing quotes in one of the ways vendors write it."""
    try:
        clock = datetime.strptime(value, "%H:%M")
    except ValueError:
        return False
    hour12 = clock.hour % 12 or 12
    forms = {
        str(clock.hour),
        f"{clock.hour:02d}",
        str(hour12),
        f"{clock.hour:02d}{clock.minute:02d}",
        f"{clock.hour}{clock.minute:02d}",
        f"{hour12}{clock.minute:02d}",
    }
    if _digit_runs(quotes) & forms:
        return True
    return clock.hour in (0, 12) and re.search(r"\b(?:noon|midnight)\b", quotes, re.I) is not None


def _sort_quotes(
    quotes: list[str], own: str, history: str, *, allow_history: bool
) -> tuple[list[str], list[str], list[ClassificationIssue]]:
    """Split the model's quotes into (found in the reply's own words, kept, issues)."""
    own_quotes: list[str] = []
    kept: list[str] = []
    issues: list[ClassificationIssue] = []
    for q in quotes:
        cleaned = clean_mail_text(q)
        if not cleaned.strip():
            continue
        if quote_in_source(cleaned, own):
            own_quotes.append(q)
            kept.append(q)
        elif history and quote_in_source(cleaned, history):
            if allow_history:
                kept.append(q)
            else:
                issues.append(
                    ClassificationIssue(
                        "quotes", "quote is from the quoted history, not the reply", q
                    )
                )
        else:
            issues.append(ClassificationIssue("quotes", "quote not found in reply", q))
    return own_quotes, kept, issues


def _validate_slot(
    data: dict[str, Any], own: str, history: str, *, label: str = ""
) -> list[ClassificationIssue]:
    """Check one reading (the whole reply, or one PO line) against the text, in place."""
    prefix = f"{label}." if label else ""
    own_quotes, kept_quotes, issues = _sort_quotes(
        list(data.get("quotes") or []),
        own,
        history,
        allow_history=data.get("status") == ReplyStatus.COUNTER_OFFER,
    )
    for issue in issues:
        issue.field_name = prefix + issue.field_name
    backing = " ".join(clean_mail_text(q) for q in kept_quotes)
    data["quotes"] = kept_quotes

    number = data.get("pickup_number")
    if number and not _number_in_text(str(number), own):
        issues.append(
            ClassificationIssue(
                prefix + "pickup_number", "number not in the reply's own words", number
            )
        )
        data["pickup_number"] = None
    day = data.get("pickup_date")
    if day and not date_is_backed(str(day), backing):
        issues.append(ClassificationIssue(prefix + "pickup_date", "no quote backs this value", day))
        data["pickup_date"] = None
    for name in ("pickup_time", "pickup_time_end"):
        value = data.get(name)
        if value and not time_is_backed(str(value), backing):
            issues.append(ClassificationIssue(prefix + name, "no quote backs this value", value))
            data[name] = None
    question = data.get("question")
    if data["status"] == ReplyStatus.CONFIRMED and not own_quotes:
        issues.append(
            ClassificationIssue(
                prefix + "status", "confirmation without a quote from the reply's own words"
            )
        )
        data["status"] = ReplyStatus.QUESTION if question else ReplyStatus.UNRELATED
    if data["status"] == ReplyStatus.COUNTER_OFFER and not (
        data["pickup_date"] or data["pickup_time"]
    ):
        issues.append(
            ClassificationIssue(prefix + "status", "counter-offer without a backed date or time")
        )
        data["status"] = ReplyStatus.QUESTION if question else ReplyStatus.UNRELATED
    return issues


def validate_classification(
    result: ReplyClassification, text: str, quoted: str = ""
) -> tuple[ReplyClassification, list[ClassificationIssue]]:
    """Keep only what the reply's own words back; return what survives and why.

    ``text`` is the reply's own words, ``quoted`` the history under it. A quote found only in
    the history backs a counter-offer (vendors edit times inside the quoted request) and nothing
    else: a chaser that carries the old confirmation underneath must not re-confirm the slot.
    Each PO line in ``items`` is checked the same way, and a line whose PO numbers are nowhere
    in the message is dropped: it cannot be about anything the message says.
    """
    own = clean_mail_text(text)
    history = clean_mail_text(quoted)
    data = result.model_dump()
    issues = _validate_slot(data, own, history)
    everything = f"{own}\n{history}"
    kept_items: list[dict[str, Any]] = []
    for index, item in enumerate(data.get("items") or []):
        label = f"items[{index}]"
        pos = [str(p) for p in item.get("po_numbers") or []]
        missing = [p for p in pos if not _number_in_text(p, everything)]
        if missing:
            issues.append(
                ClassificationIssue(label + ".po_numbers", "PO not in the message", missing)
            )
            continue
        item["question"] = None  # lines carry verdicts and values, the reply carries the question
        issues.extend(_validate_slot(item, own, history, label=label))
        item.pop("question", None)
        kept_items.append(item)
    data["items"] = kept_items
    return ReplyClassification.model_validate(data), issues


def for_case(result: ReplyClassification, po_numbers: list[str]) -> ReplyClassification | None:
    """The reading that applies to one case's POs.

    With no PO lines, the whole reply applies. With lines, the first line naming one of the
    case's POs applies; failing that, a line with no POs (a verdict for everything); failing
    that, the reply is not about this case at all and None is returned.
    """
    if not result.items:
        return result
    wanted = {p.strip() for p in po_numbers}
    chosen = next(
        (item for item in result.items if wanted & {p.strip() for p in item.po_numbers}), None
    ) or next((item for item in result.items if not item.po_numbers), None)
    if chosen is None:
        return None
    return ReplyClassification(
        status=chosen.status,
        pickup_date=chosen.pickup_date,
        pickup_time=chosen.pickup_time,
        pickup_time_end=chosen.pickup_time_end,
        pickup_number=chosen.pickup_number,
        conditions=list(chosen.conditions)
        + [c for c in result.conditions if c not in chosen.conditions],
        question=result.question,
        quotes=list(chosen.quotes),
        items=[],
        confidence=result.confidence,
    )


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
