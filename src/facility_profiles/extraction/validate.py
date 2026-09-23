"""Evidence validation and typed conversion of the model's output (FR-5, FR-6).

Every quote must be found in the source it references; every phone, email and URL value must
appear verbatim in the quoted source. Candidates that fail lose their quotes (or are dropped)
and each failure is recorded as a :class:`ValidationIssue` for the run report.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from facility_profiles.domain.normalize import (
    extract_emails,
    extract_phones,
    extract_urls,
    normalize_email,
    normalize_phone,
    normalize_url,
    quote_in_source,
)
from facility_profiles.domain.schema import (
    VERBATIM_FIELDS,
    BookingMethod,
    Candidate,
    ExtractionResult,
    FieldCandidates,
    HoursCandidate,
    PortalVendor,
    Quote,
    TimeGranularity,
)
from facility_profiles.domain.scoring import FACILITY_SOURCE_LOAD_ID, Mention
from facility_profiles.extraction.bundle import SourceBundle, SourceDoc

_TRUE = {"true", "yes", "1"}
_FALSE = {"false", "no", "0"}


@dataclass(frozen=True)
class ValidationIssue:
    """A dropped quote or candidate and why."""

    field: str
    value: str
    reason: str
    source_id: str | None = None


def coerce(field_name: str, raw: str) -> Any | None:
    """Convert the model's string value to the field's Python type, or None when invalid."""
    text = raw.strip()
    if not text:
        return None
    if field_name == "appointment_required":
        low = text.lower()
        return True if low in _TRUE else False if low in _FALSE else None
    if field_name == "booking_method":
        low = text.lower()
        return low if low in BookingMethod.__members__.values() and low != "unknown" else None
    if field_name == "portal_vendor":
        low = text.lower()
        return low if low in PortalVendor.__members__.values() and low != "unknown" else None
    if field_name == "time_granularity":
        low = text.lower()
        return low if low in TimeGranularity.__members__.values() and low != "unknown" else None
    if field_name == "notice_period_hours":
        digits = "".join(ch for ch in text if ch.isdigit())
        return int(digits) if digits else None
    if field_name == "contact_phone":
        return normalize_phone(text)
    if field_name == "contact_email":
        return normalize_email(text)
    if field_name == "portal_url":
        return normalize_url(text)
    return text


def grouping_key(field_name: str, value: Any) -> Any:
    """Key used to group agreeing mentions."""
    if isinstance(value, str) and field_name in {"contact_name"}:
        return value.strip().lower()
    if isinstance(value, list | dict):
        return json.dumps(value, sort_keys=True)
    return value


def _verbatim_present(field_name: str, value: Any, source_text: str) -> bool:
    if field_name == "contact_phone":
        return value in extract_phones(source_text)
    if field_name == "contact_email":
        return value in extract_emails(source_text)
    if field_name == "portal_url":
        return value in extract_urls(source_text)
    return True


def _valid_quotes(
    field_name: str, value: Any, candidate_value: str, quotes: list[Quote], bundle: SourceBundle
) -> tuple[list[tuple[Quote, SourceDoc]], list[ValidationIssue]]:
    kept: list[tuple[Quote, SourceDoc]] = []
    issues: list[ValidationIssue] = []
    for quote in quotes:
        source = bundle.source(quote.source_id)
        if source is None:
            issues.append(
                ValidationIssue(field_name, candidate_value, "unknown source tag", quote.source_id)
            )
            continue
        if not quote_in_source(quote.text, source.text):
            issues.append(
                ValidationIssue(
                    field_name, candidate_value, "quote not found in source", quote.source_id
                )
            )
            continue
        if field_name in VERBATIM_FIELDS and not _verbatim_present(field_name, value, source.text):
            issues.append(
                ValidationIssue(
                    field_name,
                    candidate_value,
                    "value not present verbatim in source",
                    quote.source_id,
                )
            )
            continue
        kept.append((quote, source))
    return kept, issues


def _mentions_for(
    field_name: str,
    value: Any,
    confidence: float,
    kept: list[tuple[Quote, SourceDoc]],
) -> list[Mention]:
    mentions: list[Mention] = []
    for quote, source in kept:
        for load_id in source.credited_load_ids:
            mentions.append(
                Mention(
                    field_name=field_name,
                    value=value,
                    load_id=FACILITY_SOURCE_LOAD_ID if load_id is None else load_id,
                    source_type=source.source_type,
                    quote=quote.text,
                    observed_at=source.observed_at,
                    source_confidence=confidence,
                    normalized=grouping_key(field_name, value),
                )
            )
    return mentions


def _convert_field(
    field_name: str, block: FieldCandidates, bundle: SourceBundle
) -> tuple[list[Mention], list[ValidationIssue]]:
    mentions: list[Mention] = []
    issues: list[ValidationIssue] = []
    candidate: Candidate
    for candidate in block.candidates:
        value = coerce(field_name, candidate.value)
        if value is None:
            issues.append(ValidationIssue(field_name, candidate.value, "value not valid for field"))
            continue
        kept, quote_issues = _valid_quotes(
            field_name, value, candidate.value, candidate.quotes, bundle
        )
        issues.extend(quote_issues)
        if not kept:
            issues.append(ValidationIssue(field_name, candidate.value, "no valid quotes remain"))
            continue
        mentions.extend(_mentions_for(field_name, value, candidate.confidence, kept))
    return mentions, issues


def _hours_value(candidate: HoursCandidate) -> list[dict[str, Any]] | None:
    spans: list[dict[str, Any]] = []
    for span in candidate.spans:
        if not span.days or not _valid_hhmm(span.open) or not _valid_hhmm(span.close):
            return None
        spans.append(
            {
                "days": sorted({d.value for d in span.days}, key=_weekday_order),
                "open": span.open,
                "close": span.close,
                "by_appointment": span.by_appointment,
            }
        )
    return sorted(spans, key=lambda s: (s["days"], s["open"])) if spans else None


_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")


def _weekday_order(day: str) -> int:
    return _WEEKDAYS.index(day) if day in _WEEKDAYS else 99


def _valid_hhmm(value: str) -> bool:
    parts = value.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    hours, minutes = int(parts[0]), int(parts[1])
    return 0 <= hours <= 23 and 0 <= minutes <= 59


def validate_and_convert(
    result: ExtractionResult, bundle: SourceBundle
) -> tuple[dict[str, list[Mention]], list[ValidationIssue]]:
    """Turn a model result into per-field mentions, dropping anything the sources do not back."""
    mentions: dict[str, list[Mention]] = {}
    issues: list[ValidationIssue] = []

    simple_fields: dict[str, FieldCandidates] = {
        "appointment_required": result.appointment_required,
        "booking_method": result.booking_method,
        "contact_name": result.contact_name,
        "contact_phone": result.contact_phone,
        "contact_email": result.contact_email,
        "portal_url": result.portal_url,
        "portal_vendor": result.portal_vendor,
        "notice_period_hours": result.notice_period_hours,
        "time_granularity": result.time_granularity,
    }
    for name, block in simple_fields.items():
        field_mentions, field_issues = _convert_field(name, block, bundle)
        if field_mentions:
            mentions[name] = field_mentions
        issues.extend(field_issues)

    hours_mentions: list[Mention] = []
    for candidate in result.receiving_hours:
        value = _hours_value(candidate)
        label = json.dumps([s.model_dump() for s in candidate.spans])
        if value is None:
            issues.append(ValidationIssue("receiving_hours", label, "invalid hours span"))
            continue
        kept, quote_issues = _valid_quotes(
            "receiving_hours", value, label, candidate.quotes, bundle
        )
        issues.extend(quote_issues)
        if not kept:
            issues.append(ValidationIssue("receiving_hours", label, "no valid quotes remain"))
            continue
        hours_mentions.extend(_mentions_for("receiving_hours", value, candidate.confidence, kept))
    if hours_mentions:
        mentions["receiving_hours"] = hours_mentions

    return mentions, issues
