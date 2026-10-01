"""Profile schema shared by extraction, scoring, storage and the review queue.

Two layers live here:

* **LLM output** (:class:`ExtractionResult`): a fixed, flat schema the model must return. Every
  field is a list of candidate values, each backed by verbatim quotes that point at a numbered
  source in the bundle. Values are strings here and are coerced to typed values afterwards.
* **Profile** (:class:`FacilityProfile`, :class:`ProfileField`): the scored, stateful result
  per facility and role, with evidence and lifecycle state.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Role(StrEnum):
    """Which side of the load the facility plays; rules can differ per role."""

    SHIPPER = "shipper"
    RECEIVER = "receiver"


class BookingMethod(StrEnum):
    """How a facility takes an appointment."""

    PHONE = "phone"
    EMAIL = "email"
    WEB_PORTAL = "web_portal"
    FCFS = "fcfs"
    PRESET_BY_CUSTOMER = "preset_by_customer"
    UNKNOWN = "unknown"


class PortalVendor(StrEnum):
    """Scheduling portal vendor when the method is a web portal."""

    OPENDOCK = "opendock"
    C3 = "c3"
    DATADOCKS = "datadocks"
    ONE_NETWORK = "one_network"
    E2OPEN = "e2open"
    BLUE_YONDER = "blue_yonder"
    RETALIX = "retalix"  # also NCR Power Traffic, its successor
    # Retailers' own schedulers, which pod 1160 had filed as "other".
    COSTCO = "costco"
    UNFI = "unfi"
    AHOLD = "ahold"
    PUBLIX = "publix"
    BOZZUTOS = "bozzutos"
    OTHER = "other"
    UNKNOWN = "unknown"


class ReferenceType(StrEnum):
    """A number a booking desk can ask for before it gives a slot."""

    PO_NUMBER = "po_number"
    LOAD_NUMBER = "load_number"  # Circle's load number
    DELIVERY_NUMBER = "delivery_number"  # the customer's delivery booking (Lidl's DCT reference)
    SHIPMENT_NUMBER = "shipment_number"  # the customer's shipment number (Lidl's TI number)
    SALES_ORDER_NUMBER = "sales_order_number"  # the customer's sales order (Lidl's SO number)
    BOL_NUMBER = "bol_number"


class TimeGranularity(StrEnum):
    """Whether the site gives exact appointment times or windows."""

    EXACT = "exact"
    WINDOW = "window"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class Weekday(StrEnum):
    """ISO weekday names."""

    MON = "mon"
    TUE = "tue"
    WED = "wed"
    THU = "thu"
    FRI = "fri"
    SAT = "sat"
    SUN = "sun"


class FieldState(StrEnum):
    """Lifecycle of one profile field (see the PRD state machine)."""

    EXTRACTED = "extracted"
    WRITTEN = "written"
    QUEUED = "queued"
    DISCARDED = "discarded"
    HUMAN_SET = "human_set"
    REJECTED = "rejected"
    VERIFIED = "verified"
    STALE = "stale"


class ValueOrigin(StrEnum):
    """Where the current value of a field came from."""

    EXTRACTED = "extracted"
    EXISTING = "existing"  # already on the Transport Pro facility record
    HUMAN = "human"
    VERIFIED = "verified"


class SourceType(StrEnum):
    """Kinds of source text the extractor reads."""

    STOP_NOTE = "stop_note"
    STOP_CONTACT = "stop_contact"
    SERVICE_LEVEL = "service_level"
    APPOINTMENT_TIMES = "appointment_times"
    FACILITY_DISPATCH_NOTES = "facility_dispatch_notes"
    FACILITY_INTERNAL_COMMENTS = "facility_internal_comments"
    FACILITY_BUSINESS_HOURS = "facility_business_hours"
    FACILITY_APPOINTMENTS = "facility_appointments"
    LOAD_NOTE = "load_note"
    TRACKING_NOTE = "tracking_note"
    STOP_STRUCTURED = "stop_structured"  # JSON snapshot of a stop, for rule-based signals only


# Names of the scored profile fields, in display order.
PROFILE_FIELDS: tuple[str, ...] = (
    "appointment_required",
    "booking_method",
    "contact_name",
    "contact_phone",
    "contact_email",
    "portal_url",
    "portal_vendor",
    "notice_period_hours",
    "cutoff_time",
    "max_days_ahead",
    "required_refs",
    "time_granularity",
    "receiving_hours",
)

# The booking desk's rules. A person files them (``profile set``, the review workbook) from what
# a desk says in mail; the extractor does not look for them yet. The booking agent obeys them:
# - cutoff_time: "HH:MM" local; a request must reach the desk by then on the business day
#   before the pickup ("appointments for tomorrow by 2 PM");
# - max_days_ahead: whole days; the desk takes no request earlier than that before the pickup;
# - required_refs: the ReferenceType values the request must carry besides the PO.
RULE_FIELDS: frozenset[str] = frozenset({"cutoff_time", "max_days_ahead", "required_refs"})

# Fields whose values must appear verbatim in a source (FR-6).
VERBATIM_FIELDS: frozenset[str] = frozenset({"contact_phone", "contact_email", "portal_url"})


# --------------------------------------------------------------------------- LLM output


class LLMBase(BaseModel):
    """Strict base for the model's structured output."""

    model_config = ConfigDict(extra="forbid")


class Quote(LLMBase):
    """A verbatim quote from one numbered source in the bundle."""

    source_id: str = Field(description="Source tag exactly as given in the bundle, e.g. 'S3'.")
    text: str = Field(description="Verbatim text copied from that source, at most 200 characters.")


class Candidate(LLMBase):
    """One candidate value for a field with the quotes that support it."""

    value: str = Field(
        description=(
            "The candidate value as a string. Booleans as 'true'/'false', integers as digits, "
            "enumerations exactly as listed in the instructions."
        )
    )
    quotes: list[Quote] = Field(description="Every quote that supports this value.")
    confidence: float = Field(ge=0, le=1, description="Your confidence that the quotes mean this.")


class FieldCandidates(LLMBase):
    """All candidate values found for one field. Empty when nothing in the sources says."""

    candidates: list[Candidate]


class HoursSpan(LLMBase):
    """Opening span on given weekdays, in the facility's local time."""

    days: list[Weekday]
    open: str = Field(description="Opening time as HH:MM, 24-hour.")
    close: str = Field(description="Closing time as HH:MM, 24-hour.")
    by_appointment: bool = Field(
        description="True when the span applies only to booked appointments."
    )


class HoursCandidate(LLMBase):
    """One candidate set of receiving hours with supporting quotes."""

    spans: list[HoursSpan]
    quotes: list[Quote]
    confidence: float = Field(ge=0, le=1)


class ExtractionResult(LLMBase):
    """What the model returns for one facility and role."""

    appointment_required: FieldCandidates
    booking_method: FieldCandidates
    contact_name: FieldCandidates
    contact_phone: FieldCandidates
    contact_email: FieldCandidates
    portal_url: FieldCandidates
    portal_vendor: FieldCandidates
    notice_period_hours: FieldCandidates
    time_granularity: FieldCandidates
    receiving_hours: list[HoursCandidate]
    scheduling_summary: str | None = Field(
        description=(
            "One plain sentence, at most 200 characters, saying how this site books, or null "
            "when the sources say nothing about scheduling."
        )
    )


# --------------------------------------------------------------------------- profile


class Evidence(BaseModel):
    """A quote tied to the load and source it came from."""

    load_id: int | None
    source_type: SourceType
    quote: str
    observed_at: datetime | None = None


class CandidateValue(BaseModel):
    """A scored alternative value for a field."""

    value: Any
    support: float
    distinct_loads: int
    evidence: list[Evidence] = Field(default_factory=list)


class ProfileField(BaseModel):
    """One scored field of a facility profile."""

    name: str
    value: Any = None
    confidence: float = 0.0
    support_count: int = 0
    mention_count: int = 0
    distinct_loads: int = 0
    conflict: bool = False
    evidence: list[Evidence] = Field(default_factory=list)
    candidates: list[CandidateValue] = Field(default_factory=list)
    origin: ValueOrigin = ValueOrigin.EXTRACTED
    state: FieldState = FieldState.EXTRACTED
    last_seen: datetime | None = None


class FacilityIdentity(BaseModel):
    """Who the profile is about."""

    facility_id: int | None = None
    candidate_key: str | None = None
    company_name: str | None = None
    address: str | None = None
    city: str | None = None
    state: str | None = None
    postal_code: str | None = None
    iana_timezone: str | None = None
    aliases: list[str] = Field(default_factory=list)

    @property
    def key(self) -> str:
        """Stable key: the Transport Pro ID when known, else the candidate key."""
        if self.facility_id is not None:
            return f"tpro:{self.facility_id}"
        return f"candidate:{self.candidate_key or 'unknown'}"


class FacilityProfile(BaseModel):
    """Scored scheduling profile for one facility in one role."""

    identity: FacilityIdentity
    role: Role
    fields: dict[str, ProfileField] = Field(default_factory=dict)
    scheduling_summary: str | None = None
    run_id: str | None = None
    model_version: str | None = None
    updated_at: datetime | None = None
    source_load_ids: list[int] = Field(default_factory=list)

    def field(self, name: str) -> ProfileField | None:
        """Return a field by name when present."""
        return self.fields.get(name)
