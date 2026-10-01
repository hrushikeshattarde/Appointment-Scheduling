"""A booking desk's rules: read from the vendor profile, checked before a request goes out.

The profile says how a desk books (``booking_method``, ``contact_email``) and the rules a person
filed for it from what the desk wrote:

- ``notice_period_hours``: how long before the pickup the desk wants the request;
- ``cutoff_time``: the time, on the business day before the pickup, by which it wants it;
- ``max_days_ahead``: how far ahead it books at all;
- ``required_refs``: the numbers it needs besides the PO. Morgan Foods wants Lidl's TI shipment
  number; RLS wants Lidl's SO number. The agent cannot produce them, so a person adds them to the
  case (``booking ref``).

Before a request is drafted the agent checks them. Too early is a wait, with nothing for a person
to do. Past the cut-off or the notice is raised as ``slot_unworkable``, a missing number as
``missing_reference``. The numbers a desk needs go into the request line next to the PO.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy.orm import Session

from facility_profiles.booking.models import BookingCase, BookingEvent, CaseStatus, ExceptionType
from facility_profiles.booking.worklist import flag, resolve
from facility_profiles.config import Settings
from facility_profiles.domain.schema import ReferenceType, Role
from facility_profiles.storage.repository import Repository, unwrap

TRUSTED_STATES = frozenset({"human_set", "verified", "written"})
# How each number is written in a request line, the way desks write them back.
REFERENCE_LABELS: dict[str, str] = {
    ReferenceType.PO_NUMBER.value: "PO#",
    ReferenceType.LOAD_NUMBER.value: "Load#",
    ReferenceType.DELIVERY_NUMBER.value: "Delivery#",
    ReferenceType.SHIPMENT_NUMBER.value: "Shipment#",
    ReferenceType.SALES_ORDER_NUMBER.value: "SO#",
    ReferenceType.BOL_NUMBER.value: "BOL#",
}
# And in plain words, for the person asked to find one.
REFERENCE_NAMES: dict[str, str] = {
    ReferenceType.PO_NUMBER.value: "PO number",
    ReferenceType.LOAD_NUMBER.value: "load number",
    ReferenceType.DELIVERY_NUMBER.value: "delivery number",
    ReferenceType.SHIPMENT_NUMBER.value: "customer's shipment number",
    ReferenceType.SALES_ORDER_NUMBER.value: "customer's sales order (SO) number",
    ReferenceType.BOL_NUMBER.value: "BOL number",
}
# Numbers the case carries from the load itself; the rest a person adds.
FROM_THE_LOAD = frozenset(
    {
        ReferenceType.PO_NUMBER.value,
        ReferenceType.LOAD_NUMBER.value,
        ReferenceType.DELIVERY_NUMBER.value,
    }
)


@dataclass(frozen=True)
class VendorProfile:
    """The trusted booking facts and rules for a pickup facility."""

    key: str
    booking_method: str | None
    contact_email: str | None
    contact_name: str | None
    appointment_required: bool | None
    summary: str | None
    time_granularity: str | None = None
    portal_vendor: str | None = None
    portal_url: str | None = None
    notice_period_hours: int | None = None
    cutoff_time: str | None = None
    max_days_ahead: int | None = None
    required_refs: list[str] = field(default_factory=list)

    @property
    def date_only(self) -> bool:
        """First-come-first-served shippers get a date, not a time (the pod does the same)."""
        return self.appointment_required is False or self.time_granularity == "window"

    @property
    def can_email(self) -> bool:
        """True when the agent has a verified email desk to write to."""
        return self.booking_method == "email" and bool(self.contact_email)


def vendor_profile(repo: Repository, key: str) -> VendorProfile:
    """Read the trusted shipper-side fields for a facility key."""
    fields = repo.fields(key, Role.SHIPPER)

    def trusted(name: str) -> Any:
        fld = fields.get(name)
        if fld is None or fld.state not in TRUSTED_STATES:
            return None
        return unwrap(fld.value)

    profile = repo.profile(key, Role.SHIPPER)
    return VendorProfile(
        key=key,
        booking_method=trusted("booking_method"),
        contact_email=trusted("contact_email"),
        contact_name=trusted("contact_name"),
        appointment_required=trusted("appointment_required"),
        summary=profile.scheduling_summary if profile else None,
        time_granularity=trusted("time_granularity"),
        portal_vendor=trusted("portal_vendor"),
        portal_url=trusted("portal_url"),
        notice_period_hours=trusted("notice_period_hours"),
        cutoff_time=trusted("cutoff_time"),
        max_days_ahead=trusted("max_days_ahead"),
        required_refs=list(trusted("required_refs") or []),
    )


# ------------------------------------------------------------------ references


def reference_values(case: BookingCase) -> dict[str, str]:
    """Every number the case can give a desk: from the load, and what a person added."""
    values = {k: str(v) for k, v in (case.reference_numbers or {}).items() if v}
    pos = " & ".join(str(p) for p in case.po_numbers)
    if pos:
        values[ReferenceType.PO_NUMBER.value] = pos
    values[ReferenceType.LOAD_NUMBER.value] = str(case.load_id)
    if case.delivery_ref:
        values[ReferenceType.DELIVERY_NUMBER.value] = case.delivery_ref
    return values


def missing_references(case: BookingCase, profile: VendorProfile | None) -> list[str]:
    """The numbers the desk requires that the case does not have yet."""
    if profile is None:
        return []
    have = reference_values(case)
    return [ref for ref in profile.required_refs if not have.get(ref)]


def extra_references(case: BookingCase, profile: VendorProfile | None) -> list[str]:
    """What the request line carries besides the PO: "Shipment# 7781234", in the desk's order."""
    if profile is None:
        return []
    have = reference_values(case)
    return [
        f"{REFERENCE_LABELS.get(ref, ref)} {have[ref]}"
        for ref in profile.required_refs
        if ref != ReferenceType.PO_NUMBER.value and have.get(ref)
    ]


# ------------------------------------------------------------------ when a request can go out


def _pickup(requested: str | None, timezone: str | None) -> datetime | None:
    if not requested:
        return None
    day, _, clock = requested.partition(" ")
    try:
        naive = datetime.strptime(f"{day} {clock or '09:00'}", "%Y-%m-%d %H:%M")
    except ValueError:
        return None
    return naive.replace(tzinfo=ZoneInfo(timezone or "America/New_York"))


def business_day_before(day: datetime) -> datetime:
    """The weekday before ``day`` (a Monday's is the Friday)."""
    before = day - timedelta(days=1)
    while before.weekday() >= 5:
        before -= timedelta(days=1)
    return before


def cutoff_at(requested: str | None, timezone: str | None, cutoff: str | None) -> datetime | None:
    """When the desk's cut-off for this pickup falls: ``cutoff`` on the business day before."""
    pickup = _pickup(requested, timezone)
    if pickup is None or not cutoff:
        return None
    try:
        clock = time.fromisoformat(cutoff)
    except ValueError:
        return None
    return datetime.combine(business_day_before(pickup).date(), clock, tzinfo=pickup.tzinfo)


def slot_is_stale(
    requested: str | None,
    timezone: str | None,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None = None,
) -> str | None:
    """Why a requested slot can no longer be asked for by email, or None when it still can.

    The slot has passed, is inside the agent's own notice window (a same-day ask needs a
    person), or is past the desk's cut-off or notice period.
    """
    pickup = _pickup(requested, timezone)
    if pickup is None:
        return None
    if pickup <= now:
        return f"requested slot {requested} has already passed"
    if pickup <= now + timedelta(hours=settings.booking_min_notice_hours):
        return (
            f"requested slot {requested} is inside the {settings.booking_min_notice_hours} h "
            "notice window; a same-day ask needs a person"
        )
    if profile is None:
        return None
    cutoff = cutoff_at(requested, timezone, profile.cutoff_time)
    if cutoff is not None and cutoff <= now:
        return (
            f"the desk's cut-off for {requested} was {cutoff:%a %m/%d %H:%M} "
            f"({profile.cutoff_time} the business day before)"
        )
    notice = profile.notice_period_hours
    if notice and pickup - timedelta(hours=notice) <= now:
        latest = pickup - timedelta(hours=notice)
        return (
            f"requested slot {requested} is inside the desk's {notice} h notice; it had to be "
            f"asked for by {latest:%a %m/%d %H:%M}"
        )
    return None


def request_opens(
    requested: str | None, timezone: str | None, profile: VendorProfile | None
) -> datetime | None:
    """The first moment the desk takes a request for this pickup, when it limits how far ahead."""
    pickup = _pickup(requested, timezone)
    if pickup is None or profile is None or not profile.max_days_ahead:
        return None
    first_day = pickup.date() - timedelta(days=profile.max_days_ahead)
    return datetime.combine(first_day, time(0), tzinfo=pickup.tzinfo)


def too_early(
    requested: str | None,
    timezone: str | None,
    profile: VendorProfile | None,
    *,
    now: datetime,
) -> str | None:
    """Why the request has to wait (the desk does not book that far ahead), or None."""
    opens = request_opens(requested, timezone, profile)
    if opens is None or now >= opens:
        return None
    assert profile is not None  # opens is only known from a profile
    days = profile.max_days_ahead
    plural = "" if days == 1 else "s"
    return f"{WAIT_PREFIX} {days} day{plural} ahead; ask from {opens:%a %m/%d}"


WAIT_PREFIX = "the desk books at most"


# ------------------------------------------------------------------ the check


def _raised_for(case: BookingCase, kind: ExceptionType, key: str, value: object) -> bool:
    """True when this kind was raised for the same thing already, open or resolved since."""
    return any(e.kind == kind.value and (e.detail or {}).get(key) == value for e in case.exceptions)


def check_desk_rules(
    session: Session,
    case: BookingCase,
    settings: Settings,
    *,
    now: datetime,
    profile: VendorProfile | None,
    actor: str = "agent",
) -> str | None:
    """Apply the desk's rules to an unscheduled case whose request has not gone out.

    Past the cut-off or the notice raises ``slot_unworkable``; a number the desk needs that the
    case lacks raises ``missing_reference`` (resolved once the case has them all). Each is
    raised once for the same slot or the same missing numbers, so a person's resolution stands.
    Returns why the request has to wait (the desk does not book that far ahead yet), written as
    the case's reason, or None.
    """
    if case.status != CaseStatus.UNSCHEDULED.value or not case.contact_email:
        return None
    missing = missing_references(case, profile)
    if missing:
        if not _raised_for(case, ExceptionType.MISSING_REFERENCE, "missing", missing):
            names = " and ".join(REFERENCE_NAMES.get(m, m) for m in missing)
            flag(
                session,
                case,
                ExceptionType.MISSING_REFERENCE,
                f"the desk needs the {names} before it books",
                actor=actor,
                at=now,
                missing=missing,
            )
    else:
        resolve(
            session,
            case,
            [ExceptionType.MISSING_REFERENCE],
            resolution="the case has every number the desk needs",
            by=actor,
            at=now,
        )
    stale = slot_is_stale(
        case.requested_local, case.vendor_timezone, settings, now=now, profile=profile
    )
    if stale:
        if not _raised_for(case, ExceptionType.SLOT_UNWORKABLE, "requested", case.requested_local):
            flag(
                session,
                case,
                ExceptionType.SLOT_UNWORKABLE,
                stale,
                actor=actor,
                at=now,
                requested=case.requested_local,
            )
            session.add(
                BookingEvent(
                    case_id=case.id, action="stale_slot", actor=actor, detail={"reason": stale}
                )
            )
        return None
    wait = too_early(case.requested_local, case.vendor_timezone, profile, now=now)
    if wait:
        case.reason = wait
    elif case.reason and case.reason.startswith(WAIT_PREFIX):
        case.reason = None
    return wait
