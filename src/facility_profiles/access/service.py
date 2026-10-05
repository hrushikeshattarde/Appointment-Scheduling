"""Giving and taking back access, and what one signed-in person may see.

Everything an admin changes goes through :func:`add_person`, :func:`set_access` and
:func:`remove_person`, and each writes a line to the access history. :func:`viewer_for` reads a
person's access afresh on every request, so a change holds from their next page, without them
signing out.
"""

from __future__ import annotations

import re
import secrets
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from facility_profiles.access.models import (
    AccessChange,
    AccessLevel,
    BoardAccess,
    BoardPerson,
    BoardSecret,
)
from facility_profiles.config import Settings
from facility_profiles.customers import customers
from facility_profiles.storage.repository import as_utc

EMAIL_RE = re.compile(r"^[^@\s]+@([a-z0-9-]+(?:\.[a-z0-9-]+)+)$")
SEEN_EVERY = timedelta(minutes=5)  # how often a visit updates "last seen"


class AccessError(ValueError):
    """A change an admin asked for that cannot be made; the message says why."""


@dataclass(frozen=True)
class Viewer:
    """Who is looking at the board and which customers' pickups they may see or act on.

    ``signed_in`` is False when the board has no sign-in (FP_GOOGLE_CLIENT_* unset): then the
    viewer is whoever sits at this machine, sees everything and names themselves on each decision.
    """

    email: str | None
    name: str | None
    admin: bool
    levels: Mapping[str, AccessLevel] = field(default_factory=dict)
    signed_in: bool = True

    @property
    def label(self) -> str:
        """What a decision is recorded under: the name Google gave, else the email."""
        return (self.name or self.email or "").strip()[:128]

    def sees(self, customer_key: str) -> bool:
        """True when this person may see the customer's pickups."""
        return self.admin or customer_key in self.levels

    def acts(self, customer_key: str) -> bool:
        """True when this person may also decide on the customer's pickups."""
        return self.admin or self.levels.get(customer_key) == AccessLevel.ACT


LOCAL = Viewer(email=None, name=None, admin=True, signed_in=False)


def signin_enabled(settings: Settings) -> bool:
    """True when the board asks people to sign in with Google."""
    return bool(settings.google_client_id and settings.google_client_secret)


def normalize_email(value: str) -> str:
    """An email as the access list keeps it: trimmed, lower case."""
    return value.strip().lower()


def is_admin(settings: Settings, email: str | None) -> bool:
    """True when FP_BOARD_ADMINS names this email."""
    return bool(email) and normalize_email(email or "") in settings.board_admins


def check_email(settings: Settings, value: str) -> str:
    """The email, normalized, when it is one the board's domains own; else :class:`AccessError`."""
    email = normalize_email(value)
    match = EMAIL_RE.match(email)
    if not match:
        msg = f"{value.strip()!r} is not an email address"
        raise AccessError(msg)
    if match.group(1) not in settings.board_domains:
        allowed = ", ".join(settings.board_domains) or "no domain (FP_BOARD_DOMAINS is empty)"
        msg = f"only accounts on {allowed} can sign in to the board"
        raise AccessError(msg)
    return email


def pod_today(settings: Settings, now: datetime) -> date:
    """Today in the pod's time zone: an access ``until`` a date holds through that day."""
    return now.astimezone(ZoneInfo(settings.booking_timezone)).date()


def customer_names(settings: Settings) -> dict[str, str]:
    """Every customer file's key and name, in key order."""
    return {c.key: c.name or c.key for c in customers(settings).files}


# ------------------------------------------------------------------ who is looking


def viewer_for(
    session: Session, settings: Settings, email: str, name: str | None, *, now: datetime
) -> Viewer:
    """The signed-in person with the access they hold right now (none when nobody gave any)."""
    email = normalize_email(email)
    admin = is_admin(settings, email)
    person = session.scalar(select(BoardPerson).where(BoardPerson.email == email))
    if person is not None:
        seen = as_utc(person.last_seen_at)
        if seen is None or now - seen >= SEEN_EVERY:
            person.last_seen_at = now
        if name and person.name != name[:128]:
            person.name = name[:128]
    levels: dict[str, AccessLevel] = {}
    if not admin and person is not None and person.active:
        today = pod_today(settings, now)
        for row in session.scalars(select(BoardAccess).where(BoardAccess.email == email)):
            if row.until is None or row.until >= today:
                levels[row.customer_key] = AccessLevel(row.level)
    return Viewer(email=email, name=name, admin=admin, levels=levels)


# ------------------------------------------------------------------ changes


def _record(session: Session, *, by: str, action: str, email: str, **detail: Any) -> None:
    session.add(AccessChange(by=by[:128], action=action, email=email, **detail))


def _by(by: str) -> str:
    who = by.strip()
    if not who:
        msg = "say who is making the change"
        raise AccessError(msg)
    return who[:128]


def add_person(
    session: Session,
    settings: Settings,
    email: str,
    *,
    by: str,
    name: str | None = None,
    now: datetime,
) -> BoardPerson:
    """Put someone on the access list (with no customer yet), or back on it after a removal."""
    by = _by(by)
    email = check_email(settings, email)
    if is_admin(settings, email):
        msg = f"{email} is an admin (FP_BOARD_ADMINS) and sees every customer already"
        raise AccessError(msg)
    person = session.scalar(select(BoardPerson).where(BoardPerson.email == email))
    clean = (name or "").strip()[:128] or None
    if person is None:
        person = BoardPerson(email=email, name=clean, added_by=by, added_at=now)
        session.add(person)
    elif person.active:
        return person
    else:
        person.active = True
        person.added_by = by
        person.added_at = now
        person.name = clean or person.name
    _record(session, by=by, action="add", email=email, at=now)
    session.flush()
    return person


def set_access(
    session: Session,
    settings: Settings,
    email: str,
    customer_key: str,
    level: AccessLevel | None,
    *,
    by: str,
    until: date | None = None,
    note: str | None = None,
    now: datetime,
) -> str | None:
    """Give, change or (with ``level`` None) take back one person's access to one customer.

    The person is added to the list first when they are not on it. Returns what was done
    (grant, change or revoke), or None when nothing changed.
    """
    by = _by(by)
    email = check_email(settings, email)
    key = customer_key.strip().lower()
    note = (note or "").strip()[:255] or None
    row = session.scalar(
        select(BoardAccess).where(BoardAccess.email == email, BoardAccess.customer_key == key)
    )
    if level is None:
        if row is None:
            return None
        session.delete(row)
        _record(session, by=by, action="revoke", email=email, customer_key=key, at=now)
        session.flush()
        return "revoke"
    names = customer_names(settings)
    if key not in names:
        known = ", ".join(names) or "none"
        msg = f"there is no customer file {customer_key!r} (customers: {known})"
        raise AccessError(msg)
    if until is not None and until < pod_today(settings, now):
        msg = f"the end date {until.isoformat()} has passed already"
        raise AccessError(msg)
    add_person(session, settings, email, by=by, now=now)
    if row is not None and (row.level, row.until, row.note) == (level.value, until, note):
        return None
    action = "grant" if row is None else "change"
    if row is None:
        row = BoardAccess(email=email, customer_key=key)
        session.add(row)
    row.level = level.value
    row.until = until
    row.note = note
    row.granted_by = by
    row.granted_at = now
    _record(
        session,
        by=by,
        action=action,
        email=email,
        customer_key=key,
        level=level.value,
        until=until,
        note=note,
        at=now,
    )
    session.flush()
    return action


def remove_person(session: Session, email: str, *, by: str, now: datetime) -> int:
    """Take the person off the list and back every access they held; returns how many."""
    by = _by(by)
    email = normalize_email(email)
    person = session.scalar(select(BoardPerson).where(BoardPerson.email == email))
    if person is None or not person.active:
        msg = f"{email} is not on the access list"
        raise AccessError(msg)
    rows = list(session.scalars(select(BoardAccess).where(BoardAccess.email == email)))
    for row in rows:
        session.delete(row)
        _record(session, by=by, action="revoke", email=email, customer_key=row.customer_key, at=now)
    person.active = False
    _record(session, by=by, action="remove", email=email, at=now)
    session.flush()
    return len(rows)


# ------------------------------------------------------------------ reading the list


def access_grid(session: Session, settings: Settings, *, now: datetime) -> dict[str, Any]:
    """The Access tab: the customers, the admins, and everyone on the list with what they hold."""
    today = pod_today(settings, now)
    people = list(
        session.scalars(
            select(BoardPerson).where(BoardPerson.active.is_(True)).order_by(BoardPerson.email)
        )
    )
    rows: dict[str, list[BoardAccess]] = {}
    for row in session.scalars(select(BoardAccess).order_by(BoardAccess.customer_key)):
        rows.setdefault(row.email, []).append(row)
    return {
        "customers": [{"key": k, "name": n} for k, n in customer_names(settings).items()],
        "admins": list(settings.board_admins),
        "people": [
            {
                "email": p.email,
                "name": p.name,
                "added_by": p.added_by,
                "added_at": _iso(p.added_at),
                "last_seen_at": _iso(p.last_seen_at),
                "access": {
                    r.customer_key: {
                        "level": r.level,
                        "until": r.until.isoformat() if r.until else None,
                        "note": r.note,
                        "granted_by": r.granted_by,
                        "granted_at": _iso(r.granted_at),
                        "ended": r.until is not None and r.until < today,
                    }
                    for r in rows.get(p.email, [])
                },
            }
            for p in people
        ],
    }


def history(session: Session, *, limit: int = 100, email: str | None = None) -> list[AccessChange]:
    """The latest changes first."""
    stmt = select(AccessChange).order_by(AccessChange.at.desc(), AccessChange.id.desc())
    if email:
        stmt = stmt.where(AccessChange.email == normalize_email(email))
    return list(session.scalars(stmt.limit(limit)))


def change_view(change: AccessChange) -> dict[str, Any]:
    """One history line as data."""
    return {
        "at": _iso(change.at),
        "by": change.by,
        "action": change.action,
        "email": change.email,
        "customer_key": change.customer_key,
        "level": change.level,
        "until": change.until.isoformat() if change.until else None,
        "note": change.note,
    }


def _iso(value: datetime | None) -> str | None:
    aware = as_utc(value)
    return aware.isoformat() if aware else None


# ------------------------------------------------------------------ the cookie key


def session_secret(session: Session, settings: Settings) -> bytes:
    """The key that signs the sign-in cookie: FP_BOARD_SESSION_SECRET, else one kept in the store.

    The stored one is made on the first start, so every server on the same store accepts the
    same cookies and a restart signs nobody out.
    """
    if settings.board_session_secret is not None:
        return settings.board_session_secret.get_secret_value().encode()
    row = session.get(BoardSecret, "session")
    if row is None:
        try:
            with session.begin_nested():
                session.add(BoardSecret(name="session", value=secrets.token_urlsafe(48)))
        except IntegrityError:  # another server made it first
            pass
        row = session.get(BoardSecret, "session")
    if row is None:  # pragma: no cover - the insert above or the other server's made it
        msg = "could not keep the board's session key in the store"
        raise RuntimeError(msg)
    return row.value.encode()
