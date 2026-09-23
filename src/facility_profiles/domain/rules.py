"""Write policy (FR-9) and the mapping from profile fields to Transport Pro facility fields.

The rules never overwrite a value a person entered:

* conflict                                -> queue
* confidence below the queue threshold    -> discard
* human value present and equal           -> verify
* human value present and different       -> queue
* field empty and confidence >= write     -> write
* otherwise                               -> queue
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from facility_profiles.domain.normalize import normalize_email, normalize_phone, normalize_url
from facility_profiles.domain.schema import BookingMethod, FacilityProfile, ProfileField


class Decision(StrEnum):
    """What the writer should do with a scored field."""

    WRITE = "write"
    QUEUE = "queue"
    DISCARD = "discard"
    VERIFY = "verify"
    SKIP = "skip"


@dataclass(frozen=True)
class Thresholds:
    """Configurable decision thresholds."""

    write: float = 0.8
    queue: float = 0.5


@dataclass(frozen=True)
class Ruling:
    """Decision plus the reason, for the audit log."""

    decision: Decision
    reason: str


def values_equal(field_name: str, left: Any, right: Any) -> bool:
    """Compare two values with field-aware normalisation."""
    if left is None or right is None:
        return left is None and right is None
    if field_name == "contact_phone":
        return normalize_phone(str(left)) == normalize_phone(str(right))
    if field_name == "contact_email":
        return normalize_email(str(left)) == normalize_email(str(right))
    if field_name == "portal_url":
        return normalize_url(str(left)) == normalize_url(str(right))
    return str(left).strip().lower() == str(right).strip().lower()


def decide(
    scored: ProfileField,
    *,
    existing_value: Any = None,
    existing_is_human: bool = False,
    thresholds: Thresholds | None = None,
) -> Ruling:
    """Apply the write policy to one scored field."""
    thresholds = thresholds or Thresholds()
    if scored.value is None or scored.mention_count == 0:
        return Ruling(Decision.SKIP, "no mentions")
    if scored.conflict:
        return Ruling(Decision.QUEUE, "conflicting values with material support")
    if scored.confidence < thresholds.queue:
        return Ruling(Decision.DISCARD, f"confidence {scored.confidence:.2f} below queue threshold")
    if existing_is_human and existing_value is not None:
        if values_equal(scored.name, scored.value, existing_value):
            return Ruling(Decision.VERIFY, "matches the value a person entered")
        return Ruling(Decision.QUEUE, "differs from the value a person entered")
    if existing_value is not None and not values_equal(scored.name, scored.value, existing_value):
        return Ruling(Decision.QUEUE, "differs from the value already on the record")
    if scored.confidence >= thresholds.write:
        return Ruling(
            Decision.WRITE, f"confidence {scored.confidence:.2f} at or above write threshold"
        )
    return Ruling(Decision.QUEUE, f"confidence {scored.confidence:.2f} between thresholds")


# Transport Pro appointment methods seen on live records. Other enum members have no confirmed
# vendor value yet (vendor ask 3 in the PRD); they are described in appointments.notes instead.
TPRO_METHOD_VALUES: dict[BookingMethod, str] = {
    BookingMethod.EMAIL: "Email Appointment",
    BookingMethod.WEB_PORTAL: "Web Portal",
}


@dataclass(frozen=True)
class TProFacilityWrite:
    """The subset of a facility record the routine may fill."""

    method: str | None = None
    contact: str | None = None
    email: str | None = None
    phone: str | None = None
    portal_url: str | None = None
    notes: str | None = None
    business_hours: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        """Dictionary in Transport Pro field names."""
        return {
            "appointments.method": self.method,
            "appointments.contact": self.contact,
            "appointments.email": self.email,
            "appointments.phone": self.phone,
            "appointments.portalURL": self.portal_url,
            "appointments.notes": self.notes,
            "businessHours": self.business_hours,
        }


def _value(profile: FacilityProfile, name: str) -> Any:
    fld = profile.field(name)
    return fld.value if fld else None


def format_hours(spans: Any) -> str | None:
    """Render receiving-hours spans as ``0700-1430 MON-FRI`` style text."""
    if not spans or not isinstance(spans, list):
        return None
    parts: list[str] = []
    for span in spans:
        if not isinstance(span, dict):
            continue
        days = [str(d).upper() for d in span.get("days", [])]
        day_text = f"{days[0]}-{days[-1]}" if len(days) > 2 else "/".join(days)
        open_ = str(span.get("open", "")).replace(":", "")
        close = str(span.get("close", "")).replace(":", "")
        suffix = " by appt" if span.get("by_appointment") else ""
        parts.append(f"{open_}-{close} {day_text}{suffix}".strip())
    return "; ".join(parts) or None


def to_tpro_write(profile: FacilityProfile) -> TProFacilityWrite:
    """Map a profile to the Transport Pro facility fields it would fill."""
    method_value = _value(profile, "booking_method")
    method_enum = (
        BookingMethod(method_value) if method_value in BookingMethod.__members__.values() else None
    )
    tpro_method = TPRO_METHOD_VALUES.get(method_enum) if method_enum else None

    notes_parts: list[str] = []
    if profile.scheduling_summary:
        notes_parts.append(profile.scheduling_summary)
    if method_enum and tpro_method is None and method_enum is not BookingMethod.UNKNOWN:
        notes_parts.append(f"Booking method: {method_enum.value.replace('_', ' ')}")
    notice = _value(profile, "notice_period_hours")
    if notice:
        notes_parts.append(f"Notice period: {notice} hours")
    granularity = _value(profile, "time_granularity")
    if granularity and granularity != "unknown":
        notes_parts.append(f"Gives {granularity} appointment times")
    required = _value(profile, "appointment_required")
    if required is False:
        notes_parts.append("No appointment required")

    return TProFacilityWrite(
        method=tpro_method,
        contact=_value(profile, "contact_name"),
        email=_value(profile, "contact_email"),
        phone=_value(profile, "contact_phone"),
        portal_url=_value(profile, "portal_url"),
        notes=". ".join(p.rstrip(".") for p in notes_parts) + "." if notes_parts else None,
        business_hours=format_hours(_value(profile, "receiving_hours")),
    )
