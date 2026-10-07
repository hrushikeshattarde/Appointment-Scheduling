"""Pydantic models for the Transport Pro records this project reads.

Attribute names are snake_case; the API's camelCase keys are accepted through aliases. The
models are deliberately tolerant (``extra="allow"``, optional fields) because the API omits or
nulls fields freely, uses ``false`` where ``null`` is meant, and the same shape differs
slightly between endpoints (for example ``ianaTimezone`` on a waypoint but ``iana_timezone``
on a facility).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel

T = TypeVar("T")


def parse_iso(value: str | None) -> datetime | None:
    """Parse the API's ISO-8601 strings, including the malformed ``2026-09-02T:18:22:45Z`` form."""
    if not value or not isinstance(value, str):
        return None
    text = value.strip().replace("T:", "T")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _false_to_none(value: Any) -> Any:
    return None if value is False else value


def _blank_to_none(value: Any) -> Any:
    if isinstance(value, str) and not value.strip():
        return None
    return value


def _clean_value(cls: object, value: Any) -> Any:  # noqa: ARG001 - pydantic binds cls
    """Before-validator: the API uses ``false`` and ``""`` where it means ``null``."""
    return _blank_to_none(_false_to_none(value))


class TProModel(BaseModel):
    """Base model: camelCase aliases, snake_case attributes, unknown keys kept."""

    model_config = ConfigDict(
        alias_generator=to_camel,
        validate_by_name=True,
        validate_by_alias=True,
        extra="allow",
        frozen=False,
    )


class Pagination(TProModel):
    """Paging block returned by list endpoints."""

    total_records: int = 0
    per_page: int = 0
    current_page: int = 0
    total_pages: int = 0


class Page(TProModel, Generic[T]):
    """A page of results plus its paging block."""

    pagination: Pagination = Field(default_factory=Pagination)
    results: list[T] = Field(default_factory=list)


class Location(TProModel):
    """A postal location as it appears on waypoints, facilities and tracking notes."""

    location_id: int | None = None
    company_name: str | None = None
    address: str | None = None
    address2: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    country_code: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    timezone: int | None = None
    iana_timezone: str | None = None

    _nullify = field_validator(
        "address",
        "city",
        "state",
        "postal_code",
        "country_code",
        "iana_timezone",
        "company_name",
        mode="before",
    )(_clean_value)


class AppointmentTime(TProModel):
    """Appointment window and status on a stop."""

    open: str | None = None
    close: str | None = None
    appointment_status: str | None = None

    _nullify = field_validator("open", "close", "appointment_status", mode="before")(_clean_value)

    @property
    def open_at(self) -> datetime | None:
        """Window start as an aware datetime."""
        return parse_iso(self.open)

    @property
    def close_at(self) -> datetime | None:
        """Window end as an aware datetime."""
        return parse_iso(self.close)

    @property
    def is_exact_time(self) -> bool | None:
        """True when open and close are identical, None when either is missing."""
        if self.open is None or self.close is None:
            return None
        return self.open == self.close


class StopContact(TProModel):
    """Contact block on a stop."""

    name: str | None = None
    phone: str | None = None
    email: str | None = None
    fax: str | None = None

    _nullify = field_validator("name", "phone", "email", "fax", mode="before")(_clean_value)


class Reference(TProModel):
    """Typed reference value on a stop (SERVICE_LEVEL, WEIGHT, PIECE_COUNT and others)."""

    type: str | None = None
    value: Any = None


class Waypoint(TProModel):
    """A stop on a load or dispatch."""

    id: int | None = None
    type: str | None = None
    stopoff: bool | None = None
    location_id: int | None = None
    location: Location | None = None
    appointment_time: AppointmentTime | None = None
    contact: StopContact | None = None
    notes: str | None = None
    reference: list[Reference] = Field(default_factory=list)

    _nullify = field_validator("notes", mode="before")(_clean_value)

    @field_validator("reference", mode="before")
    @classmethod
    def _reference_list(cls, value: Any) -> Any:
        return value if isinstance(value, list) else []

    @property
    def resolved_location_id(self) -> int | None:
        """Location ID from the stop or its nested location block."""
        if self.location_id is not None:
            return self.location_id
        return self.location.location_id if self.location else None

    @property
    def service_level(self) -> str | None:
        """SERVICE_LEVEL reference, for example ``"Firm Appointment"``."""
        for ref in self.reference:
            if ref.type == "SERVICE_LEVEL" and isinstance(ref.value, str) and ref.value:
                return ref.value
        return None

    @property
    def role(self) -> str:
        """``shipper`` for pickups (SH, PU) and ``receiver`` for drops (CN, SO)."""
        return "shipper" if (self.type or "").upper() in {"SH", "PU"} else "receiver"


class InternalContact(TProModel):
    """Circle-side contact on a load or dispatch (ORDERTAKER, CARRIERSALESREP, DISPATCHER)."""

    type: str | None = None
    id: int | None = None


class LoadStatus(TProModel):
    """Status block on a load."""

    load_status: str | None = None
    document_status: str | None = None
    billing_status: str | None = None


class CustomerRef(TProModel):
    """Customer summary on a load."""

    id: int | None = None
    company_name: str | None = None


class BillingInfo(TProModel):
    """Billing block on a load."""

    customer_id: int | None = None
    customer: CustomerRef | None = None


class Load(TProModel):
    """A Transport Pro load."""

    id: int
    date_created: str | None = None
    last_updated: str | None = None
    internal_contacts: list[InternalContact] = Field(default_factory=list)
    assigned_terminal: int | None = None
    waypoints: list[Waypoint] = Field(default_factory=list)
    status: LoadStatus | None = None
    billing_info: BillingInfo | None = None
    reference: dict[str, Any] | None = None

    @property
    def created_at(self) -> datetime | None:
        """Creation time as an aware datetime."""
        return parse_iso(self.date_created)

    @property
    def updated_at(self) -> datetime | None:
        """Last update as an aware datetime."""
        return parse_iso(self.last_updated)

    @property
    def customer_name(self) -> str | None:
        """Billing customer name when present."""
        if self.billing_info and self.billing_info.customer:
            return self.billing_info.customer.company_name
        return None


class FacilityAppointments(TProModel):
    """Appointment block on a facility record. ``method`` is ``false`` when empty."""

    method: str | None = None
    contact: str | None = None
    email: str | None = None
    phone: str | None = None
    portal_url: str | None = Field(default=None, alias="portalURL")
    notes: str | None = None

    _nullify = field_validator(
        "method", "contact", "email", "phone", "portal_url", "notes", mode="before"
    )(_clean_value)

    @property
    def is_empty(self) -> bool:
        """True when every appointment field is blank."""
        return not any(
            (self.method, self.contact, self.email, self.phone, self.portal_url, self.notes)
        )


class Facility(TProModel):
    """A Transport Pro facility (location) record."""

    id: int
    company_name: str | None = None
    location_code: str | None = None
    location: Location | None = None
    appointments: FacilityAppointments | None = None
    business_hours: str | None = None
    internal_comments: str | None = None
    dispatch_notes: str | None = None

    _nullify = field_validator(
        "company_name",
        "location_code",
        "business_hours",
        "internal_comments",
        "dispatch_notes",
        mode="before",
    )(_clean_value)


class LoadNote(TProModel):
    """A note on a load (``GET /load/{id}/notes``)."""

    id: int | None = None
    control_id: int | None = None
    record_type: str | None = None
    priority: bool | None = None
    date_created: str | None = None
    created_by: int | None = None
    content: str | None = None

    @property
    def created_at(self) -> datetime | None:
        """Creation time as an aware datetime."""
        return parse_iso(self.date_created)


class TrackingNote(TProModel):
    """A dispatch or tracking note (``/dispatch/{id}/notes``, ``/tracking/note/...``)."""

    id: int | None = None
    event_date: str | None = None
    data_source: str | None = None
    entered_by: int | None = None
    load_id: int | None = None
    dispatch_id: int | None = None
    carrier_id: int | None = None
    comments: str | None = None
    location: Location | None = None

    _nullify = field_validator("comments", "data_source", mode="before")(_clean_value)

    @property
    def event_at(self) -> datetime | None:
        """Event time as an aware datetime."""
        return parse_iso(self.event_date)

    @property
    def is_user_entered(self) -> bool:
        """True for human or bot text, False for Macropoint pings."""
        return (self.data_source or "").lower() != "macropoint"


class Dispatch(TProModel):
    """A dispatch record attached to a load."""

    id: int
    load_id: int | None = None
    status: str | None = None
    dispatch_date: str | None = None
    date_created: str | None = None
    waypoints: list[Waypoint] | None = None
    internal_contacts: list[InternalContact] = Field(default_factory=list)
    # Who hauls it: {"type": "brokerCarrier", "carrier": {companyName, mcNumber, usDOT, ...},
    # "contacts": [{"type": "DRIVER", "name", "phoneNumber"}, ...], "tractorNumber", ...}
    assigned_to: dict[str, Any] | None = None


class Terminal(TProModel):
    """A terminal (office or pod)."""

    id: int
    status: str | None = None
    title: str | None = None
    terminal_code: str | None = None
    parent_terminal_id: int | str | None = None
    phone_numbers: list[dict[str, Any]] = Field(default_factory=list)

    @property
    def is_pod(self) -> bool:
        """True when the terminal title marks it as a pod."""
        return "pod" in (self.title or "").lower()


class VoiceAiDate(TProModel):
    """Start and end pair on the Voice AI load detail."""

    start: str | None = None
    end: str | None = None
    iana_timezone: str | None = None

    @property
    def start_at(self) -> datetime | None:
        """Start as an aware datetime."""
        return parse_iso(self.start)

    @property
    def end_at(self) -> datetime | None:
        """End as an aware datetime."""
        return parse_iso(self.end)


class VoiceAiWaypoint(TProModel):
    """Stop on the Voice AI load detail, which carries actual arrival and departure times."""

    type: str | None = None
    status: str | None = None
    location_id: int | None = None
    company_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    appointment_date: VoiceAiDate | None = None
    actual_date: VoiceAiDate | None = None
    service_level: str | None = None
    notes: str | None = None

    _nullify = field_validator("service_level", "notes", mode="before")(_clean_value)

    @property
    def role(self) -> str:
        """``shipper`` for pickups and ``receiver`` for deliveries."""
        return "shipper" if "pickup" in (self.type or "").lower() else "receiver"


class VoiceAiLoad(TProModel):
    """The AI-agent-friendly load summary (``GET /voiceai/load/{id}``)."""

    load_id: int | None = None
    load_status: str | None = None
    dispatch_id: int | None = None
    dispatch_status: str | None = None
    dispatch_information: dict[str, Any] | None = None

    @property
    def dispatch_waypoints(self) -> list[VoiceAiWaypoint]:
        """Stops from the dispatch block, with actual arrival and departure times."""
        if not self.dispatch_information:
            return []
        raw = self.dispatch_information.get("waypoints") or []
        return [VoiceAiWaypoint.model_validate(item) for item in raw if isinstance(item, dict)]


class User(TProModel):
    """A Transport Pro user, used to name note authors in the audit log."""

    id: int | None = None
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None
    username: str | None = None

    @property
    def display_name(self) -> str:
        """Best available human-readable name."""
        parts = [p for p in (self.first_name, self.last_name) if p]
        return " ".join(parts) or self.username or self.email or f"user {self.id}"
