"""Rule-based mentions from structured Transport Pro fields.

These need no model call: stop appointment status, service level, confirmed windows, stop
contacts and the facility record's own appointment fields. Their confidences are deliberately
modest because the September 2026 sample showed these flags contradicting each other.
"""

from __future__ import annotations

from datetime import datetime

from facility_profiles.domain.normalize import normalize_email, normalize_phone, normalize_url
from facility_profiles.domain.schema import BookingMethod, SourceType
from facility_profiles.domain.scoring import FACILITY_SOURCE_LOAD_ID, Mention
from facility_profiles.tpro.models import Facility, Waypoint

STATUS_REQUIRES = {"confirmed", "requested", "appointment required"}
STATUS_NOT_REQUIRED = {"not required"}


def booking_method_from_tpro(method: str | None) -> BookingMethod | None:
    """Map a Transport Pro ``appointments.method`` string to the profile enum."""
    if not method:
        return None
    low = method.lower()
    if "email" in low:
        return BookingMethod.EMAIL
    if "portal" in low or "web" in low or "online" in low:
        return BookingMethod.WEB_PORTAL
    if "phone" in low or "call" in low:
        return BookingMethod.PHONE
    if "fcfs" in low or "first come" in low:
        return BookingMethod.FCFS
    if "preset" in low or "customer" in low:
        return BookingMethod.PRESET_BY_CUSTOMER
    return None


def mentions_from_stop(
    load_id: int, observed_at: datetime | None, waypoint: Waypoint
) -> list[Mention]:
    """Structured signals from one stop on one load."""
    out: list[Mention] = []

    def add(field: str, value: object, source: SourceType, quote: str, conf: float) -> None:
        out.append(
            Mention(
                field_name=field,
                value=value,
                load_id=load_id,
                source_type=source,
                quote=quote,
                observed_at=observed_at,
                source_confidence=conf,
                normalized=value if not isinstance(value, str) else value.lower(),
            )
        )

    appt = waypoint.appointment_time
    status = (appt.appointment_status or "").strip().lower() if appt else ""
    if status in STATUS_REQUIRES:
        add("appointment_required", True, SourceType.APPOINTMENT_TIMES, f"status: {status}", 0.6)
    elif status in STATUS_NOT_REQUIRED:
        add("appointment_required", False, SourceType.APPOINTMENT_TIMES, f"status: {status}", 0.4)

    if appt and status == "confirmed" and appt.is_exact_time is not None:
        granularity = "exact" if appt.is_exact_time else "window"
        add(
            "time_granularity",
            granularity,
            SourceType.APPOINTMENT_TIMES,
            f"confirmed {appt.open} to {appt.close}",
            0.5,
        )

    level = (waypoint.service_level or "").strip()
    low = level.lower()
    if "firm appointment" in low:
        add("appointment_required", True, SourceType.SERVICE_LEVEL, level, 0.6)
    elif "fcfs" in low or "first come" in low:
        add("booking_method", BookingMethod.FCFS.value, SourceType.SERVICE_LEVEL, level, 0.5)
        add("appointment_required", False, SourceType.SERVICE_LEVEL, level, 0.4)

    contact = waypoint.contact
    if contact:
        for raw in (contact.phone, contact.email):
            phone = normalize_phone(raw)
            email = normalize_email(raw)
            if phone:
                add("contact_phone", phone, SourceType.STOP_CONTACT, raw or "", 0.6)
            elif email:
                add("contact_email", email, SourceType.STOP_CONTACT, raw or "", 0.6)
        if contact.name:
            add("contact_name", contact.name.strip(), SourceType.STOP_CONTACT, contact.name, 0.5)
    return out


def mentions_from_facility(
    facility: Facility, observed_at: datetime | None = None
) -> list[Mention]:
    """Existing appointment fields on the facility record, as modest-confidence mentions."""
    out: list[Mention] = []
    appt = facility.appointments
    if appt is None:
        return out

    def add(field: str, value: object, quote: str, conf: float = 0.7) -> None:
        out.append(
            Mention(
                field_name=field,
                value=value,
                load_id=FACILITY_SOURCE_LOAD_ID,
                source_type=SourceType.FACILITY_APPOINTMENTS,
                quote=quote,
                observed_at=observed_at,
                source_confidence=conf,
                normalized=value if not isinstance(value, str) else value.lower(),
            )
        )

    method = booking_method_from_tpro(appt.method)
    if method:
        add("booking_method", method.value, f"method: {appt.method}")
        if method is BookingMethod.FCFS:
            add("appointment_required", False, f"method: {appt.method}", 0.6)
        elif method is not BookingMethod.PRESET_BY_CUSTOMER:
            add("appointment_required", True, f"method: {appt.method}", 0.5)
    if appt.contact:
        add("contact_name", appt.contact.strip(), f"contact: {appt.contact}")
    phone = normalize_phone(appt.phone)
    if phone:
        add("contact_phone", phone, f"phone: {appt.phone}")
    email = normalize_email(appt.email)
    if email:
        add("contact_email", email, f"email: {appt.email}")
    # Some records store an email address in the portal URL field; route it to the right field.
    if appt.portal_url:
        url = normalize_url(appt.portal_url)
        misplaced_email = normalize_email(appt.portal_url)
        if url:
            add("portal_url", url, f"portalURL: {appt.portal_url}")
        elif misplaced_email:
            add("contact_email", misplaced_email, f"portalURL: {appt.portal_url}", 0.5)
    return out
