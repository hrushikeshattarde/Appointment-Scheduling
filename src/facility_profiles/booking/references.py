"""Reference numbers: every number on a pickup in one place, with where it came from.

A pickup collects numbers from everywhere: PO numbers and the delivery (DCT) reference from the
load, the vendor's pickup or confirmation number from its reply, a new delivery reference from
the customer's desk, the customer's shipment or sales order number and a portal's appointment id
from a person. Each is kept as a row in ``booking_references`` with its kind, its source (load,
vendor, customer_desk, person, migration), who and when, and the email it came from.

:func:`record_reference` is the one way a number is written. A newer value of a kind replaces
the older one, which stays as history ("PU# 4411, replaced by 4523 on 10/02"); POs are kept side
by side. The case's ``pickup_number``, ``delivery_ref`` and ``reference_numbers`` mirror the
current values, so everything that reads them keeps working. Any number finds its case
(``booking find``, the board's search).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import BookingCase, BookingReference
from facility_profiles.booking.rules import REFERENCE_LABELS, REFERENCE_NAMES
from facility_profiles.domain.schema import ReferenceType
from facility_profiles.storage.repository import as_utc


class ReferenceSource(StrEnum):
    """Where a number came from."""

    LOAD = "load"  # the Transport Pro load (PO, pickup number, DCT reference in the notes)
    VENDOR = "vendor"  # the vendor's reply (pickup or confirmation number)
    CUSTOMER_DESK = "customer_desk"  # the customer's inbound desk (a new delivery slot)
    PERSON = "person"  # someone on the pod (booking ref, booked, delivery-updated)
    MIGRATION = "migration"  # on the case before numbers were kept here


SOURCE_LABELS: dict[str, str] = {
    ReferenceSource.LOAD.value: "from the load",
    ReferenceSource.VENDOR.value: "from the vendor",
    ReferenceSource.CUSTOMER_DESK.value: "from the customer's desk",
    ReferenceSource.PERSON.value: "added by",
    ReferenceSource.MIGRATION.value: "on file",
}
# Kinds a case can carry several of at once; the others keep one current value.
MULTI_VALUED = frozenset({ReferenceType.PO_NUMBER.value})
# Kinds mirrored in a column of their own; the rest go into ``reference_numbers``.
_COLUMN_KINDS = frozenset(
    {
        ReferenceType.PICKUP_NUMBER.value,
        ReferenceType.DELIVERY_NUMBER.value,
        ReferenceType.PO_NUMBER.value,
        ReferenceType.LOAD_NUMBER.value,
    }
)


def _clean(kind: str, value: object) -> str:
    text = " ".join(str(value or "").split())
    return text.upper() if kind == ReferenceType.DELIVERY_NUMBER.value else text


def active(case: BookingCase, kind: str | None = None) -> list[BookingReference]:
    """The case's current numbers (of one kind, when given), oldest first."""
    return [
        r for r in case.references if r.replaced_at is None and (kind is None or r.kind == kind)
    ]


def record_reference(
    session: Session,
    case: BookingCase,
    kind: str,
    value: object,
    *,
    source: ReferenceSource,
    by: str = "agent",
    message_id: int | None = None,
    note: str | None = None,
    at: datetime | None = None,
) -> BookingReference | None:
    """Keep a number on the case; a new value of a single-valued kind replaces the old one.

    The same value again is a no-op (it returns the row already there); an empty value is
    ignored. Returns the current row for that value.
    """
    if kind not in REFERENCE_LABELS:
        msg = f"reference type must be one of {', '.join(REFERENCE_LABELS)}"
        raise ValueError(msg)
    text = _clean(kind, value)
    if not text:
        return None
    current = active(case, kind)
    same = next((r for r in current if r.value == text), None)
    if same is not None:
        return same
    when = at or datetime.now(tz=UTC)
    if kind not in MULTI_VALUED:
        for row in current:
            row.replaced_at = when
    row = BookingReference(
        kind=kind,
        value=text,
        source=source.value,
        actor=by,
        message_id=message_id,
        note=(note or "")[:255] or None,
        created_at=when,
    )
    case.references.append(row)
    _mirror(case, kind, text)
    session.flush()
    return row


def _mirror(case: BookingCase, kind: str, value: str) -> None:
    """Keep the case's columns on the current values."""
    if kind == ReferenceType.PICKUP_NUMBER.value:
        case.pickup_number = value
    elif kind == ReferenceType.DELIVERY_NUMBER.value:
        case.delivery_ref = value
    elif kind == ReferenceType.PO_NUMBER.value:
        if value not in [str(p) for p in case.po_numbers]:
            case.po_numbers = [*case.po_numbers, value]
    elif kind not in _COLUMN_KINDS:
        case.reference_numbers = {**(case.reference_numbers or {}), kind: value}


def record_load_numbers(session: Session, case: BookingCase, *, at: datetime | None = None) -> None:
    """The numbers a scanned load brings: its POs, the DCT reference, a vendor pickup number."""
    for po in case.po_numbers:
        record_reference(
            session, case, ReferenceType.PO_NUMBER.value, po, source=ReferenceSource.LOAD, at=at
        )
    record_reference(
        session,
        case,
        ReferenceType.DELIVERY_NUMBER.value,
        case.delivery_ref,
        source=ReferenceSource.LOAD,
        at=at,
    )
    record_reference(
        session,
        case,
        ReferenceType.PICKUP_NUMBER.value,
        case.pickup_number,
        source=ReferenceSource.LOAD,
        at=at,
    )


# ------------------------------------------------------------------ reading them


@dataclass(frozen=True)
class Number:
    """One number as a person reads it."""

    kind: str
    label: str
    name: str
    value: str
    source: str
    said: str  # "from the vendor", "added by megan"
    at: datetime | None
    current: bool
    replaced_at: datetime | None

    def as_dict(self) -> dict[str, Any]:
        """For the API."""
        return {
            "kind": self.kind,
            "label": self.label,
            "name": self.name,
            "value": self.value,
            "source": self.source,
            "said": self.said,
            "at": self.at.isoformat() if self.at else None,
            "current": self.current,
            "replaced_at": self.replaced_at.isoformat() if self.replaced_at else None,
        }


def case_numbers(case: BookingCase) -> list[Number]:
    """Every number on the case, current ones first (load number first of all), then history."""
    rows = sorted(case.references, key=lambda r: (r.replaced_at is not None, r.id))
    numbers = [
        Number(
            kind=ReferenceType.LOAD_NUMBER.value,
            label=REFERENCE_LABELS[ReferenceType.LOAD_NUMBER.value],
            name=REFERENCE_NAMES[ReferenceType.LOAD_NUMBER.value],
            value=str(case.load_id),
            source=ReferenceSource.LOAD.value,
            said=SOURCE_LABELS[ReferenceSource.LOAD.value],
            at=as_utc(case.created_at),
            current=True,
            replaced_at=None,
        )
    ]
    for row in rows:
        said = SOURCE_LABELS.get(row.source, row.source)
        if row.source == ReferenceSource.PERSON.value:
            said = f"{said} {row.actor}"
        numbers.append(
            Number(
                kind=row.kind,
                label=REFERENCE_LABELS.get(row.kind, row.kind),
                name=REFERENCE_NAMES.get(row.kind, row.kind),
                value=row.value,
                source=row.source,
                said=said,
                at=as_utc(row.created_at),
                current=row.replaced_at is None,
                replaced_at=as_utc(row.replaced_at),
            )
        )
    return numbers


def find_cases(session: Session, number: str) -> list[BookingCase]:
    """Cases carrying ``number`` now or before (any kind), and the case for a load number."""
    text = " ".join(number.split()).lower()
    if not text:
        return []
    ids = set(
        session.scalars(
            select(BookingReference.case_id).where(func.lower(BookingReference.value) == text)
        )
    )
    if text.isdigit():
        ids |= set(session.scalars(select(BookingCase.id).where(BookingCase.load_id == int(text))))
    if not ids:
        return []
    return list(
        session.scalars(select(BookingCase).where(BookingCase.id.in_(ids)).order_by(BookingCase.id))
    )


# ------------------------------------------------------------------ stores from before


def backfill_references(session: Session) -> int:
    """Copy the numbers older cases carry in their columns into the reference rows, once.

    POs and the delivery reference count as the load's; a pickup number or a number a person
    added before has no known source and is marked as on file. Returns the cases filled.
    """
    have = select(BookingReference.case_id)
    cases = list(session.scalars(select(BookingCase).where(BookingCase.id.not_in(have))))
    filled = 0
    for case in cases:
        when = as_utc(case.updated_at) or as_utc(case.created_at)
        before = len(case.references)
        for po in case.po_numbers:
            record_reference(
                session,
                case,
                ReferenceType.PO_NUMBER.value,
                po,
                source=ReferenceSource.LOAD,
                at=when,
            )
        record_reference(
            session,
            case,
            ReferenceType.DELIVERY_NUMBER.value,
            case.delivery_ref,
            source=ReferenceSource.LOAD,
            at=when,
        )
        record_reference(
            session,
            case,
            ReferenceType.PICKUP_NUMBER.value,
            case.pickup_number,
            source=ReferenceSource.MIGRATION,
            at=when,
        )
        for kind, value in sorted((case.reference_numbers or {}).items()):
            if kind in REFERENCE_LABELS:
                record_reference(
                    session, case, kind, value, source=ReferenceSource.MIGRATION, at=when
                )
        filled += len(case.references) > before
    if filled:
        session.flush()
    return filled
