"""Who may open the appointments board, and which customers' pickups each person sees.

Admins are named in FP_BOARD_ADMINS and see every customer. Everyone else sees the customers an
admin gave them, one row per person and customer, at one of two levels: ``view`` (watch the
bookings) or ``act`` (also approve, mark booked, add a number, cancel). ``board_access_history``
keeps every grant, change and revoke, and is only ever added to.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from sqlalchemy import Boolean, Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from facility_profiles.storage.models import Base, utcnow


class AccessLevel(StrEnum):
    """What a person may do with one customer's pickups."""

    VIEW = "view"  # see the cases, the to-dos and the emails
    ACT = "act"  # also approve, mark booked, add a number, resolve and cancel


class BoardPerson(Base):
    """Someone an admin added to the board (admins themselves are in the settings)."""

    __tablename__ = "board_people"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), unique=True, index=True)  # lower case
    name: Mapped[str | None] = mapped_column(String(128))  # from Google, once they sign in
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    added_by: Mapped[str] = mapped_column(String(128))
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class BoardAccess(Base):
    """One person's access to one customer (by the customer file's key, such as ``lidl``)."""

    __tablename__ = "board_access"
    __table_args__ = (UniqueConstraint("email", "customer_key", name="uq_board_access"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    email: Mapped[str] = mapped_column(String(254), index=True)
    customer_key: Mapped[str] = mapped_column(String(40))
    level: Mapped[str] = mapped_column(String(8))  # AccessLevel value
    until: Mapped[date | None] = mapped_column(Date)  # last day it holds, in the pod's time zone
    note: Mapped[str | None] = mapped_column(String(255))
    granted_by: Mapped[str] = mapped_column(String(128))
    granted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AccessChange(Base):
    """One line of the access history: who added, gave, changed, took back or removed what."""

    __tablename__ = "board_access_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    by: Mapped[str] = mapped_column(String(128))
    action: Mapped[str] = mapped_column(String(16))  # add | grant | change | revoke | remove
    email: Mapped[str] = mapped_column(String(254), index=True)
    customer_key: Mapped[str | None] = mapped_column(String(40))
    level: Mapped[str | None] = mapped_column(String(8))
    until: Mapped[date | None] = mapped_column(Date)
    note: Mapped[str | None] = mapped_column(String(255))


class BoardSecret(Base):
    """A key the board made for itself, such as the one that signs the sign-in cookie."""

    __tablename__ = "board_secrets"

    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
