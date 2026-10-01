"""Desk memory: when a booking works, the vendor profile remembers the desk and the method.

Every pickup that becomes scheduled is counted against the way it was booked: approved after
the vendor confirmed by email (the desk the request went to), or marked booked by a person
(``via`` phone, portal or email, with the desk they used when they say). The count lives in
``booking_desk_memory``, one row per facility, method and desk.

The profile learns from it where it knows nothing trustworthy yet. A booking method, email desk,
phone number or portal address that is missing or only extracted is filed as a person's value,
because a person booked with it; one already trusted is never overwritten (the memory still
shows what else worked). When the profile now has an email desk, the facility's other cases
that were waiting for one take it, and the agent can request them: the "No booking desk" to-do
is resolved with where the desk came from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    CaseStatus,
    DeskMemory,
    ExceptionType,
)
from facility_profiles.booking.rules import TRUSTED_STATES, VendorProfile, vendor_profile
from facility_profiles.booking.worklist import open_exceptions, resolve
from facility_profiles.domain.normalize import (
    normalize_email,
    normalize_phone,
    normalize_url,
    portal_vendor_from_url,
)
from facility_profiles.domain.schema import BookingMethod, FacilityIdentity, FieldState, Role
from facility_profiles.storage.models import FacilityRecord
from facility_profiles.storage.repository import Repository, unwrap

# How a person says they booked it, and the profile's booking method for that.
VIA_METHODS: dict[str, str] = {
    "email": BookingMethod.EMAIL.value,
    "phone": BookingMethod.PHONE.value,
    "portal": BookingMethod.WEB_PORTAL.value,
    "web_portal": BookingMethod.WEB_PORTAL.value,
}
# The profile field that holds a method's desk.
DESK_FIELDS: dict[str, str] = {
    BookingMethod.EMAIL.value: "contact_email",
    BookingMethod.PHONE.value: "contact_phone",
    BookingMethod.WEB_PORTAL.value: "portal_url",
}
METHOD_EXCEPTIONS = [ExceptionType.MISSING_METHOD, ExceptionType.METHOD_NOT_SUPPORTED]


@dataclass
class Learned:
    """What one booking taught: the memory row, the profile fields filed, the cases unblocked."""

    method: str | None = None
    desk: str = ""
    worked_count: int = 0
    filled: list[str] = field(default_factory=list)
    unblocked: list[int] = field(default_factory=list)


def normalize_desk(method: str, desk: str | None) -> str | None:
    """The desk in the form the profile keeps it, or None when it is not one for that method."""
    if not desk:
        return None
    if method == BookingMethod.EMAIL.value:
        return normalize_email(desk)
    if method == BookingMethod.PHONE.value:
        return normalize_phone(desk)
    if method == BookingMethod.WEB_PORTAL.value:
        return normalize_url(desk) or normalize_url(f"https://{desk.strip()}")
    return None


def _facility(repo: Repository, case: BookingCase) -> FacilityRecord | None:
    """The case's facility record, created from the case when the harvest never stored it."""
    key = case.facility_key or ""
    record = repo.get_facility(key)
    if record is not None:
        return record
    prefix, _, rest = key.partition(":")
    city, _, state = (case.vendor_city or "").partition(", ")
    if prefix == "candidate" and rest:
        identity = FacilityIdentity(candidate_key=rest)
    elif prefix == "tpro" and rest.isdigit():
        identity = FacilityIdentity(facility_id=int(rest))
    else:
        return None
    identity = identity.model_copy(
        update={
            "company_name": case.vendor_name,
            "city": city or None,
            "state": state or None,
            "iana_timezone": case.vendor_timezone,
        }
    )
    return repo.upsert_facility(identity)


def _counted(session: Session, case: BookingCase, method: str, desk: str) -> bool:
    """True when this case was counted for this method and desk already (a booking updated)."""
    events = session.scalars(
        select(BookingEvent).where(
            BookingEvent.case_id == case.id, BookingEvent.action == "desk_remembered"
        )
    )
    return any(
        (e.detail or {}).get("method") == method and (e.detail or {}).get("desk") == desk
        for e in events
    )


def remember_booking(
    session: Session,
    case: BookingCase,
    *,
    method: str | None,
    desk: str | None,
    by: str,
    at: datetime | None = None,
) -> Learned:
    """Count the way this case was booked and teach the vendor profile what it lacked."""
    learned = Learned()
    if not case.facility_key or not method:
        return learned
    when = at or datetime.now(tz=UTC)
    desk_value = normalize_desk(method, desk) or ""
    learned.method, learned.desk = method, desk_value
    row = session.scalar(
        select(DeskMemory).where(
            DeskMemory.facility_key == case.facility_key,
            DeskMemory.method == method,
            DeskMemory.desk == desk_value,
        )
    )
    if row is None:
        row = DeskMemory(
            facility_key=case.facility_key,
            method=method,
            desk=desk_value,
            worked_count=0,
            first_worked_at=when,
        )
        session.add(row)
    if not _counted(session, case, method, desk_value):
        row.worked_count = (row.worked_count or 0) + 1
    row.last_worked_at = when
    row.last_case_id = case.id
    row.last_by = by
    session.flush()
    learned.worked_count = row.worked_count

    repo = Repository(session)
    if _facility(repo, case) is not None:
        learned.filled = _teach_profile(repo, case, method, desk_value, by=by)
    if learned.filled:
        learned.unblocked = apply_desk_to_waiting_cases(
            session, case.facility_key, by=by, source=case
        )
    how = f"{method.replace('_', ' ')}" + (f" with {desk_value}" if desk_value else "")
    times = "once" if row.worked_count == 1 else f"{row.worked_count} times"
    reason = f"booked by {how}; this has worked {times} for this vendor"
    if learned.filled:
        reason += (
            f"; the profile learned its {', '.join(f.replace('_', ' ') for f in learned.filled)}"
        )
    session.add(
        BookingEvent(
            case_id=case.id,
            action="desk_remembered",
            actor=by,
            detail={
                "method": method,
                "desk": desk_value,
                "worked_count": row.worked_count,
                "filled": learned.filled,
                "unblocked": learned.unblocked,
                "reason": reason,
            },
        )
    )
    return learned


def _teach_profile(
    repo: Repository, case: BookingCase, method: str, desk: str, *, by: str
) -> list[str]:
    """File the method and desk on the shipper profile where it has nothing trusted yet."""
    key = case.facility_key or ""
    stored = repo.fields(key, Role.SHIPPER)

    def untrusted(name: str) -> bool:
        current = stored.get(name)
        return current is None or current.state not in TRUSTED_STATES

    wanted: list[tuple[str, object]] = [("booking_method", method)]
    if desk and DESK_FIELDS.get(method):
        wanted.append((DESK_FIELDS[method], desk))
        vendor = portal_vendor_from_url(desk) if method == BookingMethod.WEB_PORTAL.value else None
        if vendor:
            wanted.append(("portal_vendor", vendor))
    filled: list[str] = []
    for name, value in wanted:
        if not untrusted(name):
            continue
        previous = stored.get(name)
        repo.close_review_items(key, Role.SHIPPER, name, status="superseded")
        repo.set_field_human(key, Role.SHIPPER, name, value, state=FieldState.HUMAN_SET)
        repo.audit(
            run_id=None,
            key=key,
            role=Role.SHIPPER,
            field_name=name,
            action="learned",
            before=unwrap(previous.value) if previous is not None else None,
            after=value,
            confidence=1.0,
            reason=f"booked case #{case.id} by {method.replace('_', ' ')}"
            + (f" with {desk}" if desk else ""),
            actor=by,
        )
        filled.append(name)
    return filled


def apply_desk_to_waiting_cases(
    session: Session, facility_key: str | None, *, by: str, source: BookingCase | None = None
) -> list[int]:
    """Give the facility's cases still waiting for a desk the one the profile now has.

    Only an email desk unblocks a case: the agent books by email. Returns the case ids moved.
    """
    if not facility_key:
        return []
    profile = vendor_profile(Repository(session), facility_key)
    if not profile.can_email:
        return []
    waiting = session.scalars(
        select(BookingCase)
        .where(
            BookingCase.facility_key == facility_key,
            BookingCase.status == CaseStatus.UNSCHEDULED.value,
        )
        .order_by(BookingCase.id)
    )
    origin = f" (learned from case #{source.id})" if source is not None else ""
    moved = [
        case.id
        for case in waiting
        if case is not source and take_desk(session, case, profile, by=by, origin=origin)
    ]
    session.flush()
    return moved


def take_desk(
    session: Session, case: BookingCase, profile: VendorProfile, *, by: str, origin: str = ""
) -> bool:
    """An unscheduled case waiting for a desk takes the profile's email desk; True if it did."""
    if (
        case.status != CaseStatus.UNSCHEDULED.value
        or not profile.can_email
        or not open_exceptions(case, *METHOD_EXCEPTIONS)
    ):
        return False
    case.booking_method = profile.booking_method
    case.contact_email = profile.contact_email
    case.contact_name = profile.contact_name or case.contact_name
    resolve(
        session,
        case,
        METHOD_EXCEPTIONS,
        resolution=f"desk on file now: {profile.contact_email}{origin}",
        by=by,
    )
    session.add(
        BookingEvent(
            case_id=case.id,
            action="desk_learned",
            actor=by,
            detail={"to": profile.contact_email, "reason": f"desk on file now{origin}"},
        )
    )
    return True


def desk_history(session: Session, facility_key: str | None) -> list[DeskMemory]:
    """The ways a facility was booked, most used first."""
    if not facility_key:
        return []
    return list(
        session.scalars(
            select(DeskMemory)
            .where(DeskMemory.facility_key == facility_key)
            .order_by(DeskMemory.worked_count.desc(), DeskMemory.last_worked_at.desc())
        )
    )
