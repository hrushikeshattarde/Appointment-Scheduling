"""Write the next email in a thread so it fits what the facility actually wrote.

The agent's code decides the move: thank them, accept their offer, ask for other days, answer
their questions. This module only writes the words, with a model, from the fact sheet
(``booking/facts.py``), and then checks them before anything goes out:

- every date, time, number, phone number and email address in the draft is in the facts or in
  the decision (a draft that says 10/02 when the facts say 10/01 is caught here);
- nothing touches money;
- the decision's own dates and times are there, as written;
- every answer cites facts that exist and are known.

A draft that fails is never sent: the responder falls back to the pod's fixed wording, or hands
the reply to a person. Each of the facility's questions is answered on its own; one the facts
cannot answer is left out, the reply says it will be followed up, and the responder raises it in
Needs you. When none can be answered, the reply only says so ("no carrier is assigned yet; I will
get back to you") and every question waits for a person.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, Field, ValidationError

from facility_profiles import __version__
from facility_profiles.booking.facts import FACT_KEYS, FORBIDDEN_TOPICS
from facility_profiles.claude import Client, Effort, structured_call
from facility_profiles.extraction.llm import ExtractionError
from facility_profiles.extraction.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)

MAX_BODY = 900
# Said when a question is left for a person, if the draft does not say it in its own words.
FOLLOW_UP_LINE = "I will get back to you on the rest."
HOLD_LINE = "I will get back to you on this."
_FOLLOW_UP_RE = re.compile(
    r"\b(?:get back to you|follow up|follow-up|check on|find out|circle back|update you)\b", re.I
)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_URL_RE = re.compile(r"https?://\S+", re.I)
_PHONE_RE = re.compile(r"(?<![\w/])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w/])")
_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?![\d/])")
_ISO_DATE_RE = re.compile(r"\b\d{4}-(\d{2})-(\d{2})\b")
_AMPM_RE = re.compile(r"(?<![\d:])(\d{1,2})(?::(\d{2}))?\s*([ap])\.?\s?m\b\.?", re.I)
_CLOCK_RE = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?![\d:])")
_NUMBER_RE = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+")


@dataclass(frozen=True)
class ReplySituation:
    """Everything the writer is told: what was decided, what they wrote, what we know."""

    intent: str  # thank | accept_offer | ask_alternative | answer_question
    decision: str  # what we tell them, in plain words, with its dates and times as we write them
    must_include: tuple[str, ...] = ()  # those dates and times, which the reply must carry
    their_words: str = ""
    questions: tuple[str, ...] = ()
    conditions: tuple[str, ...] = ()
    facts: dict[str, Any] = field(default_factory=dict)
    history: tuple[str, ...] = ()


class Answer(BaseModel):
    """One of the facility's questions and whether the reply answers it."""

    question: str
    answered: bool
    facts_used: list[str] = Field(default_factory=list, description="Fact keys the answer uses")
    why_not: str | None = Field(None, description="Why it could not be answered, or null")


class WrittenReply(BaseModel):
    """The model's draft: the email text and an account of each question."""

    body: str = Field(description="The email text, without greeting name or signature")
    answers: list[Answer] = Field(
        default_factory=list, description="One entry per question, in the order asked"
    )


class ReplyWriter(Protocol):
    """Anything that writes a reply for a situation."""

    def write(self, situation: ReplySituation) -> WrittenReply:
        """Write the reply; never decides anything."""
        ...


# ------------------------------------------------------------------ checking a draft


@dataclass(frozen=True)
class CheckedReply:
    """A draft after its check: what may go out, and which questions are left for a person."""

    ok: bool
    body: str
    problems: list[str]
    answered: list[str]
    unanswered: list[str]


def _mmdd(month: str, day: str) -> str:
    return f"{int(month):02d}/{int(day):02d}"


def _hhmm(hour: int, minute: int) -> str:
    return f"{hour:02d}{minute:02d}"


def _from_ampm(match: re.Match[str]) -> str | None:
    hour, minute = int(match.group(1)), int(match.group(2) or 0)
    if not 1 <= hour <= 12 or minute > 59:
        return None
    if match.group(3).lower() == "p" and hour != 12:
        hour += 12
    if match.group(3).lower() == "a" and hour == 12:
        hour = 0
    return _hhmm(hour, minute)


@dataclass
class _Allowed:
    """Every value the facts and the decision hold, in the shapes a reply may write them."""

    dates: set[str] = field(default_factory=set)
    times: set[str] = field(default_factory=set)
    numbers: set[str] = field(default_factory=set)
    small: set[int] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)
    text: str = ""

    @classmethod
    def of(cls, situation: ReplySituation) -> _Allowed:
        text = " ".join(
            [json.dumps(situation.facts, default=str), situation.decision, *situation.must_include]
        )
        allowed = cls(text=text.lower())
        for m in _DATE_RE.finditer(text):
            allowed.dates.add(_mmdd(m.group(1), m.group(2)))
        for m in _ISO_DATE_RE.finditer(text):
            allowed.dates.add(_mmdd(m.group(1), m.group(2)))
        for m in _CLOCK_RE.finditer(text):
            allowed.times.add(_hhmm(int(m.group(1)), int(m.group(2))))
        for m in _AMPM_RE.finditer(text):
            if (hhmm := _from_ampm(m)) is not None:
                allowed.times.add(hhmm)
        for m in _PHONE_RE.finditer(text):
            allowed.phones.add(_phone_digits(m.group(0)))
        for raw in re.findall(r"\d[\d,]*", text):
            digits = raw.replace(",", "")
            allowed.numbers.add(digits)
            allowed.numbers.add(digits.lstrip("0") or "0")
            if len(digits) <= 2:
                allowed.small.add(int(digits))
        allowed.times |= {n for n in allowed.numbers if len(n) == 4}
        return allowed


def _phone_digits(text: str) -> str:
    digits = re.sub(r"\D", "", text)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def _known_number(m: re.Match[str], allowed: _Allowed) -> bool:
    digits = m.group(0).replace(",", "")
    if len(digits) <= 2:
        return int(digits) in allowed.small
    return digits in allowed.numbers or (digits.lstrip("0") or "0") in allowed.numbers


def _unknown_values(body: str, allowed: _Allowed) -> list[str]:
    """Each value in ``body`` that the facts and the decision do not hold.

    Each kind is taken out of the text once checked, so the digits of a date or a phone number
    are not checked again as plain numbers.
    """
    checks: list[tuple[re.Pattern[str], Callable[[re.Match[str]], bool]]] = [
        (_EMAIL_RE, lambda m: m.group(0).lower().rstrip(".,;)") in allowed.text),
        (_URL_RE, lambda m: m.group(0).lower().rstrip(".,;)") in allowed.text),
        (
            _PHONE_RE,
            lambda m: _phone_digits(m.group(0)) in allowed.phones | allowed.numbers,
        ),
        (_DATE_RE, lambda m: _mmdd(m.group(1), m.group(2)) in allowed.dates),
        (_AMPM_RE, lambda m: _from_ampm(m) in allowed.times),
        (_CLOCK_RE, lambda m: _hhmm(int(m.group(1)), int(m.group(2))) in allowed.times),
        (_NUMBER_RE, lambda m: _known_number(m, allowed)),
    ]
    found: list[str] = []
    rest = body
    for regex, known in checks:
        found += [m.group(0).strip() for m in regex.finditer(rest) if not known(m)]
        rest = regex.sub(" ", rest)
    return found


def _same(a: str, b: str) -> bool:
    return " ".join(a.lower().split()) == " ".join(b.lower().split())


def _account(written: WrittenReply, questions: tuple[str, ...]) -> list[Answer | None]:
    """The writer's answer for each question asked, in order; None when it gave none."""
    if len(written.answers) == len(questions):
        return list(written.answers)
    return [next((a for a in written.answers if _same(a.question, q)), None) for q in questions]


def check_reply(written: WrittenReply, situation: ReplySituation) -> CheckedReply:
    """Whether the draft may go out as written, and which questions it leaves for a person."""
    body = written.body.strip()
    problems: list[str] = []
    if not body:
        problems.append("the draft is empty")
    if len(body) > MAX_BODY:
        problems.append(f"the draft runs {len(body)} characters")
    if FORBIDDEN_TOPICS.search(body):
        problems.append("the draft touches money or a claim")
    unknown = _unknown_values(body, _Allowed.of(situation))
    if unknown:
        problems.append("values not in the facts: " + ", ".join(dict.fromkeys(unknown)))
    flat = " ".join(body.split()).lower()
    missing = [t for t in situation.must_include if " ".join(t.split()).lower() not in flat]
    if missing:
        problems.append("the decision's " + ", ".join(missing) + " is not in the draft")
    answered: list[str] = []
    unanswered: list[str] = []
    for question, answer in zip(
        situation.questions, _account(written, situation.questions), strict=True
    ):
        if answer is None or not answer.answered:
            unanswered.append(question)
            continue
        cited = [k for k in answer.facts_used if k in FACT_KEYS]
        bad = [k for k in answer.facts_used if k not in FACT_KEYS]
        empty = [k for k in cited if situation.facts.get(k) in (None, "", [])]
        if bad or empty or not cited:
            why = (
                f"cites facts that do not exist: {', '.join(bad)}"
                if bad
                else f"cites facts that are not known: {', '.join(empty)}"
                if empty
                else "cites no fact"
            )
            problems.append(f"the answer to {question!r} {why}")
            continue
        answered.append(question)
    if unanswered and not problems and not _FOLLOW_UP_RE.search(body):
        body = f"{body}\n\n{FOLLOW_UP_LINE if answered else HOLD_LINE}"
    return CheckedReply(
        ok=not problems,
        body=body,
        problems=problems,
        answered=answered,
        unanswered=unanswered,
    )


# ------------------------------------------------------------------ the model writer

WRITER_SYSTEM_PROMPT = """You write the next email from a freight broker's appointment desk to a \
shipping facility, in a thread about booking a truck's pickup appointment. What to say has \
already been decided; you only write the words.

How to write:
- Short and plain, the way a busy appointment desk writes: as few sentences as the answers \
need, no greeting line, no filler, no name. Write "Thank you!" once, at the very end. The \
signature is added after you.
- Say the decision first, as given, and stop there: add nothing about what happens next (no \
"we'll book it", no "see you then"). Copy its dates and times exactly as written (for example \
"10/01 @ 1100"; keep "ET" where it is written).
- Then answer each of the facility's questions, in the order asked, from the facts only. A fact \
that is null is not known: do not guess and do not say anything about it; mark that question \
not answered and say in one short sentence that you will get back to them on it.
- Write no number, date, time, phone number, email address or name that is not in the facts or \
the decision. Do not repeat numbers from the facility's email unless the facts hold them.
- Load notes are instructions on the load, not promises: write "Our load notes call for load \
bars and straps", never "the driver will bring load bars".
- When the facility states a condition (check in at the gate, arrive early), you may acknowledge \
it in a word or two ("Noted."). Promise nothing for the driver or the carrier.
- The carrier is the trucking company in the facts (carrier, carrier_mc, carrier_dot); the \
broker is us. When carrier_assigned is "not yet", the carrier and the driver are not known yet.
- When they ask for the driver's ETA, give the latest update in the facts as it stands, with its \
time ("Our last update from the driver, 10/08 @ 0745 ET: 30 minutes out", or the driver's \
position from last_location). Never work out or promise an arrival time yourself. With neither \
last_check_call nor last_location, the ETA is not answered.
- Never mention rates, pay, money, charges, fees, detention, lumper, claims, damage or insurance.
- Ask the facility for nothing the decision does not ask for.
- answers: one entry per question, in the order asked: the question, answered true or false, \
the fact keys you used, and why_not when it is not answered."""


def render_situation(situation: ReplySituation) -> str:
    """The user message: the decision, their email, their questions, the facts, the thread."""
    lines = [f"Decision: {situation.decision}"]
    if situation.must_include:
        lines.append("Must appear exactly: " + "; ".join(situation.must_include))
    lines += ["", "The facility wrote (their own words):", situation.their_words.strip() or "-"]
    if situation.questions:
        lines += ["", "Their questions:"]
        lines += [f"{i}. {q}" for i, q in enumerate(situation.questions, 1)]
    if situation.conditions:
        lines += ["", "Conditions they stated: " + "; ".join(situation.conditions)]
    lines += [
        "",
        "Facts (times are Eastern; null means not known):",
        json.dumps(situation.facts, indent=2, default=str),
    ]
    if situation.history:
        lines += ["", "Thread so far, oldest first:", "\n---\n".join(situation.history)]
    return "\n".join(lines)


class OpenRouterReplyWriter:
    """Replies written by the model through OpenRouter, in one strict-schema call."""

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
        self._schema = strict_json_schema(WrittenReply)
        self._http = httpx.Client(
            timeout=60.0,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "X-Title": f"facility-profiles-booking/{__version__}",
            },
        )

    def write(self, situation: ReplySituation) -> WrittenReply:
        """One call; a failure raises ExtractionError and the responder falls back."""
        body = {
            "model": self.model,
            "max_tokens": 800,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": WRITER_SYSTEM_PROMPT},
                {"role": "user", "content": render_situation(situation)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "written_reply", "strict": True, "schema": self._schema},
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
            return WrittenReply.model_validate(json.loads(content))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise ExtractionError(f"written reply invalid: {exc}") from exc


class ClaudeReplyWriter:
    """Replies written through Anthropic's SDK (Bedrock or Anthropic's API) in one schema call."""

    def __init__(self, client: Client, *, model: str, effort: Effort | None = None) -> None:
        self.model = model
        self._client = client
        self._effort = effort
        self._schema = strict_json_schema(WrittenReply)

    def write(self, situation: ReplySituation) -> WrittenReply:
        """One call; a failure raises ExtractionError and the responder falls back."""
        reply = structured_call(
            self._client,
            model=self.model,
            system=WRITER_SYSTEM_PROMPT,
            user=render_situation(situation),
            schema=self._schema,
            effort=self._effort,
        )
        try:
            return WrittenReply.model_validate(reply.data)
        except ValidationError as exc:
            raise ExtractionError(f"written reply invalid: {exc}") from exc


@dataclass
class FakeReplyWriter:
    """Scripted writer for tests: a function of the situation, and a log of what it was asked."""

    script: Callable[[ReplySituation], WrittenReply]
    calls: list[ReplySituation] = field(default_factory=list)

    def write(self, situation: ReplySituation) -> WrittenReply:
        """Return the scripted reply."""
        self.calls.append(situation)
        return self.script(situation)
