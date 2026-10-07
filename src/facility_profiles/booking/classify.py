"""Read a vendor reply with a structured-output model call, then check it against the text."""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from pydantic import ValidationError

from facility_profiles import __version__
from facility_profiles.booking.mail import tidy_text
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.clock import (
    EASTERN_ZONE,
    LOCAL_WORDS,
    between,
    is_eastern,
    local_to_eastern,
    to_eastern,
    zone,
    zone_name,
    zone_named,
)
from facility_profiles.domain.normalize import quote_in_source
from facility_profiles.extraction.llm import ExtractionError, LLMUsage
from facility_profiles.extraction.openrouter import (
    DEFAULT_BASE_URL,
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)

PROMPT_VERSION = "reply-v8"

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
- A reply about a pickup that was already booked uses the same statuses: moving it to another \
time is a "counter_offer" (or "confirmed" when they state the new time as set), and no longer \
being able to ship it is "rejected".
- A reply written after the requested time has passed that gives a latest arrival time, offers \
to work the driver in, or asks for the driver's ETA is a "question" (put their ask in question), \
not a confirmation.
- topic: "eta" when they ask when the driver or the truck will arrive, or for news of the \
driver; "work_in" when, after a missed or late arrival, they will still take the truck later \
(status "question"; put the latest time they will take it in pickup_time, and its day in \
pickup_date when they give one); "hold" when they put the pickup, the appointment or the order \
on hold with no day to check back (status "deferred" with no pickup_date: a hold is not a \
rejection); "none" otherwise.
- pickup_date is YYYY-MM-DD and pickup_time is HH:MM in 24-hour time, exactly as the reply \
states them: never convert between time zones. Resolve relative dates ("tomorrow", "Monday") \
from the date the reply was written at the facility, given to you. Use null when not stated.
- time_zone is the time zone the reply names for its times, as written ("ET", "EST", "CT", \
"Central", "PT" ...), or "local" when it says the time is the facility's own; null when it \
names none.
- pickup_number is the vendor's pickup, confirmation or appointment number, if given. A phone, \
fax or extension number, or anything in the sender's signature, is never a pickup number.
- quotes are short verbatim snippets copied from the reply's own words (the text above any \
quoted earlier messages) that contain each date, time and number you report. Never paraphrase \
inside quotes. Only for a counter_offer where the vendor edited dates or times inside the \
quoted text may a quote come from that quoted text.
- reject_reason, only when the status is "rejected": "not_ready" (the order or product is not \
ready or released for that day), "no_capacity" (no appointments left that day), "closed" (the \
facility is closed that day), "po_not_found" (the PO or order is not in their system, or is \
wrong), "order_canceled", or "other". Null for every other status.
- conditions are rules the vendor states (arrive early, bring load bars, register at gate).
- questions lists every question or request for information the facility puts to us, one per \
entry, in their own words from the reply's own text, whatever the status: "Both orders?", "What \
is the weight?", "Please send the driver name and cell". A confirmation or an offer can carry \
questions too; question is the main one. Questions inside quoted earlier messages are not \
listed.
- When the reply answers several PO lines separately (one line per PO or PO pair, each with \
its own date, time, pickup number or verdict), fill items with one entry per line: that line's \
PO numbers as written, its status, date, time, pickup number and reject_reason, and quotes from \
that line. \
Set the top-level fields to the line about the POs marked as ours, or to the overall reading. \
Leave items empty when the reply has one reading for everything.
- Text from files attached to the reply follows its own words, each file under a line \
"--- Attached file: <name> ---". It counts as the reply's own words (a facility may send its \
confirmation only as a PDF): read it, and quote from it as from the reply.
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
# Dates written out: 10/01, 10/01/26, 10-01-2026, 2026-10-01, Oct 1st, 1 October.
_MONTH_NAMES = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_SLASH_DATE_RE = re.compile(r"(?<![\d/])(\d{1,2})/(\d{1,2})(?:/(?:\d{4}|\d{2}))?(?![\d/])")
_DASH_DATE_RE = re.compile(r"(?<![\d-])(\d{1,2})-(\d{1,2})-(?:\d{4}|\d{2})(?![\d-])")
_ISO_DATE_RE = re.compile(r"(?<!\d)\d{4}-(\d{2})-(\d{2})(?!\d)")
_NAMED_DATE_RE = re.compile(rf"\b{_MONTH_NAMES}\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.I)
_DAY_MONTH_RE = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_NAMES}", re.I)
_MONTHS = {
    m: i
    for i, m in enumerate(
        ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1
    )
}
# A sign-off line: where a signature starts ("Thanks,", "Regards", "-- ", "Sent from my iPhone").
_SIGN_OFF_RE = re.compile(
    r"^\s*(?:--|thanks?(?: you)?(?: so much)?|thx|many thanks|regards|best regards|kind regards|"
    r"warm regards|best|sincerely|respectfully|cheers|have a (?:great|good|nice) "
    r"(?:day|one|weekend)|sent from my \w+.*)\s*[,.!]*\s*$",
    re.I,
)
SIGNATURE_LINES = 10
# Words that make a line booking content rather than a signature.
_BOOKING_WORDS_RE = re.compile(
    r"\b(?:pu|pick\s*-?\s*up|appointment|appt|confirm\w*|po|load|dock|door)\b|\d{1,2}/\d{1,2}",
    re.I,
)
# Phone numbers: formatted (260-208-4500, (260) 208-4500, +1 260.208.4500 x12), or after a
# label (Phone:, Cell, Fax, Office, Direct; P:, C:, F:).
_PHONE_RE = re.compile(
    r"(?<!\d)(?:\+?1[\s.-]?)?(?:\(\d{3}\)\s*|\d{3}[\s.-])\d{3}[\s.-]\d{4}(?!\d)"
    r"(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?",
    re.I,
)
_LABELLED_PHONE_RE = re.compile(
    r"(?:\b(?:phone|ph|tel|telephone|cell|mobile|mob|fax|office|direct|main)\b\s*[:.#]?|"
    r"\b[PCFOMT]\s*:)\s*(\+?[\d(][\d\s().-]{5,}\d)",
    re.I,
)


@dataclass(frozen=True)
class ReplyContext:
    """What the classifier is told about the request the reply answers.

    ``requested_local`` and the slots in ``requests`` are on the facility's clock, in
    ``timezone``; the model is told them on the Eastern clock, as our emails give them.
    """

    vendor_name: str
    po_numbers: list[str]
    requested_local: str | None
    reply_sent_at: datetime
    subject: str
    body: str
    quoted: str = ""
    # Every request in the thread when a batched email covered several cases: (POs, slot).
    requests: list[tuple[list[str], str | None]] = field(default_factory=list)
    timezone: str | None = None  # the facility's time zone


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


def _asked(local: str | None, timezone: str | None) -> str:
    """A requested slot as our email gave it: on the Eastern clock."""
    eastern = local_to_eastern(local, timezone)
    if not eastern:
        return "not stated"
    return f"{eastern} ET" if " " in eastern else eastern


def _written(context: ReplyContext) -> str:
    """When the reply was written, on the facility's clock (what "tomorrow" is counted from)."""
    sent = context.reply_sent_at
    sent = sent if sent.tzinfo else sent.replace(tzinfo=UTC)
    eastern = to_eastern(sent)
    if is_eastern(context.timezone):
        return f"{eastern:%Y-%m-%d %A %H:%M} ET (the facility's local time)"
    _, label = zone_name(context.timezone)
    local = sent.astimezone(zone(context.timezone))
    return f"{local:%Y-%m-%d %A %H:%M} {label} at the facility ({eastern:%Y-%m-%d %A %H:%M} ET)"


def render_user_message(context: ReplyContext) -> str:
    """The user turn: the request being answered and the reply itself.

    Times are given as our emails give them, on the Eastern clock; when the reply was written is
    given on the facility's own clock, because that is the day its "tomorrow" counts from.
    """
    tz = context.timezone
    requested = _asked(context.requested_local, tz)
    if len(context.requests) > 1:
        lines = "\n".join(
            f"- PO {' & '.join(pos) or 'unknown'}, requested {_asked(slot, tz)}"
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
    if not is_eastern(tz):
        words, label = zone_name(tz)
        head += (
            f"The facility is on {words} time ({label}); our emails give times in Eastern "
            "Time (ET).\n"
        )
    return (
        head + f"Reply written: {_written(context)}\n"
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
    """Drop the link cruft mail clients wrap around numbers and addresses, and inline image tags.

    HTML codes and odd spaces are made plain too (:func:`tidy_text`), on both sides of a check.
    """
    return tidy_text(_CID_RE.sub("", _LINK_CRUFT_RE.sub("", text or "")))


def without_signature(text: str) -> str:
    """The reply's own words without the sign-off and what follows it (a name, a title, phones).

    Only a sign-off followed by a short block that says nothing about the booking counts:
    "Thanks!" over a long reply, or "Thanks" over "PU# 4471 for 10/01", is not a signature.
    """
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if not _SIGN_OFF_RE.match(line):
            continue
        rest = [ln for ln in lines[i + 1 :] if ln.strip()]
        short = len(rest) <= SIGNATURE_LINES and all(len(ln) <= 80 for ln in rest)
        if short and not any(_BOOKING_WORDS_RE.search(ln) for ln in rest):
            return "\n".join(lines[:i]).rstrip()
    return text


def without_phone_numbers(text: str) -> str:
    """The text with every phone, fax or extension number blanked out."""
    return _LABELLED_PHONE_RE.sub(" ", _PHONE_RE.sub(" ", text))


def written_dates(text: str) -> set[tuple[int, int]]:
    """Every (month, day) written out in the text: 10/01, 10-01-26, 2026-10-01, Oct 1st, 1 Oct."""
    found: list[tuple[int, int]] = []
    for pattern in (_SLASH_DATE_RE, _DASH_DATE_RE, _ISO_DATE_RE):
        found += [(int(m.group(1)), int(m.group(2))) for m in pattern.finditer(text)]
    for m in _NAMED_DATE_RE.finditer(text):
        found.append((_MONTHS[m.group(1)[:3].lower()], int(m.group(2))))
    for m in _DAY_MONTH_RE.finditer(text):
        found.append((_MONTHS[m.group(2)[:3].lower()], int(m.group(1))))
    return {(month, day) for month, day in found if 1 <= month <= 12 and 1 <= day <= 31}


def _number_in_text(number: str, text: str) -> bool:
    """A pickup number counts only when it appears verbatim or as its full digit run."""
    if number.lower() in text.lower():
        return True
    run = "".join(ch for ch in number if ch.isdigit())
    return len(run) >= 4 and run in text


def _digit_runs(text: str) -> set[str]:
    return set(re.findall(r"\d+", text))


def date_is_backed(value: str, quotes: str) -> bool:
    """The backing quotes give this date.

    A date written out (10/01, Oct 1st) must match by month and day: "11/01" never backs
    10/01. With no date written out, the day of the month or a relative day word will do
    ("the 1st", "tomorrow").
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    written = written_dates(quotes)
    if written:
        return (parsed.month, parsed.day) in written
    day = parsed.day
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
    data: dict[str, Any],
    own: str,
    history: str,
    *,
    label: str = "",
    numbers: str | None = None,
    whole: str | None = None,
) -> list[ClassificationIssue]:
    """Check one reading (the whole reply, or one PO line) against the text, in place.

    ``own`` is the reply's own words without its signature; ``numbers`` the same without phone
    numbers, where a pickup number must be found; ``whole`` the own words as written.
    """
    prefix = f"{label}." if label else ""
    numbers = own if numbers is None else numbers
    whole = own if whole is None else whole
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
    if number and not _number_in_text(str(number), numbers):
        why = (
            "number is only in a phone number or the signature"
            if _number_in_text(str(number), whole)
            else "number not in the reply's own words"
        )
        issues.append(ClassificationIssue(prefix + "pickup_number", why, number))
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
    question = data.get("question") or (data.get("questions") or [None])[0]
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

    The sender's signature backs nothing (a name, a title, office hours), and a pickup number
    found only inside a phone, fax or extension number is not one.
    """
    whole = clean_mail_text(text)
    words, mark, files = whole.partition("\n--- Attached file")  # read files carry no signature
    own = without_signature(words) + mark + files
    numbers = without_phone_numbers(own)
    history = clean_mail_text(quoted)
    data = result.model_dump()
    issues = _validate_slot(data, own, history, numbers=numbers, whole=whole)
    everything = f"{whole}\n{history}"
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
        item["questions"] = []
        issues.extend(_validate_slot(item, own, history, label=label, numbers=numbers, whole=whole))
        item.pop("question", None)
        item.pop("questions", None)
        kept_items.append(item)
    data["items"] = kept_items
    asked = [str(q) for q in data.get("questions") or []]
    data["questions"] = [q for q in asked if _asked_in(q, own)]
    for dropped in (q for q in asked if q not in data["questions"]):
        issues.append(
            ClassificationIssue("questions", "question not in the reply's own words", dropped)
        )
    named = data.get("time_zone")
    if named and not _zone_in_text(str(named), everything):
        issues.append(ClassificationIssue("time_zone", "zone not in the message", named))
        data["time_zone"] = None
    for reading in (data, *kept_items):  # a reason belongs to a decline, and a decline has one
        if reading["status"] != ReplyStatus.REJECTED:
            reading["reject_reason"] = None
        elif not reading.get("reject_reason"):
            reading["reject_reason"] = "other"
    return ReplyClassification.model_validate(data), issues


_WORD_RE = re.compile(r"[a-z0-9#]{3,}")


def _asked_in(question: str, own: str) -> bool:
    """Whether a listed question comes from the reply's own words, not the quoted history.

    The model may tidy a question, so half its words (3+ letters) in the own words will do.
    """
    words = set(_WORD_RE.findall(question.lower()))
    if not words:
        return False
    seen = set(_WORD_RE.findall(own.lower()))
    return len(words & seen) * 2 >= len(words)


def _zone_in_text(word: str, text: str) -> bool:
    """The zone the model names is written in the message ("CT", "central", "local")."""
    lowered = word.strip().lower().rstrip(".")
    candidates = {lowered} | ({"local", "our time"} if lowered in LOCAL_WORDS else set())
    return any(
        re.search(rf"(?<![a-z]){re.escape(c)}(?![a-z])", text, re.I) for c in candidates if c
    )


def _moved(reading: dict[str, Any], source: str, timezone: str | None, day: str | None) -> None:
    """Move one reading's times from ``source``'s clock onto the facility's, in place."""
    clock = reading.get("pickup_time")
    on = reading.get("pickup_date") or day
    if not clock or not on:
        return
    new_day, _, new_clock = (between(f"{on} {clock}", source, timezone) or "").partition(" ")
    if reading.get("pickup_date"):
        reading["pickup_date"] = new_day
    reading["pickup_time"] = new_clock or clock
    end = reading.get("pickup_time_end")
    if end:
        moved_end = (between(f"{on} {end}", source, timezone) or "").partition(" ")[2]
        reading["pickup_time_end"] = moved_end or end


def to_facility_clock(
    result: ReplyClassification, timezone: str | None, *, day: str | None = None
) -> ReplyClassification:
    """The reading with its times moved onto the facility's own clock, where the store keeps them.

    A time the reply gives with a zone ("10am CT") is that zone's; one without is Eastern, the
    clock our emails give times in. ``day`` dates a time the reply gave without one (the day
    asked for). For a facility on Eastern time nothing moves.
    """
    source = zone_named(result.time_zone, timezone) or EASTERN_ZONE
    data = result.model_dump()
    for reading in (data, *data["items"]):
        _moved(reading, source, timezone, day)
    return ReplyClassification.model_validate(data)


def zone_doubt(
    reading: ReplyClassification, *, timezone: str | None, requested_local: str | None
) -> ClassificationIssue | None:
    """Why a time cannot be trusted as Eastern: a facility off Eastern time named no zone.

    Its desk may have answered on its own clock or on ours. A time that is the one we asked for
    is ours repeated back; anything else is left for a person to check. ``reading`` is on the
    facility's clock already (read as Eastern), like ``requested_local``.
    """
    if reading.time_zone or is_eastern(timezone) or not reading.pickup_time:
        return None
    asked_day, _, asked_clock = (requested_local or "").partition(" ")
    day = reading.pickup_date or asked_day
    if day == asked_day and reading.pickup_time == asked_clock:
        return None
    words, label = zone_name(timezone)
    read_as = local_to_eastern(f"{day} {reading.pickup_time}", timezone) or ""
    written = read_as.partition(" ")[2]
    if_theirs = (local_to_eastern(f"{day} {written}", timezone) or "").partition(" ")[2]
    return ClassificationIssue(
        "time_zone",
        (
            f"the facility is on {words} time and wrote {written} with no zone: {written} ET, "
            f"or {written} {label} ({if_theirs} ET)?"
        ),
        {"written": written, "read_as": read_as, "if_theirs": if_theirs},
    )


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
        time_zone=result.time_zone,
        reject_reason=chosen.reject_reason or result.reject_reason,
        topic=result.topic,
        conditions=list(chosen.conditions)
        + [c for c in result.conditions if c not in chosen.conditions],
        question=result.question,
        questions=list(result.questions),
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
