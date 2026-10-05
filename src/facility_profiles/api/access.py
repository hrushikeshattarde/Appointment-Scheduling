"""Who is signed in (``/api/me``) and the admin's Access tab (``/api/access``).

Only admins (FP_BOARD_ADMINS) reach ``/api/access``. Who made a change is always the signed-in
admin, never a name the page sends; without sign-in (this machine only) it is the ``by`` sent.
A list of changes is applied together or not at all.
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field, StringConstraints

from facility_profiles.access import (
    AccessError,
    AccessLevel,
    access_grid,
    add_person,
    change_view,
    history,
    remove_person,
    set_access,
    signin_enabled,
)
from facility_profiles.access.service import customer_names
from facility_profiles.api.auth import AdminDep, ViewerDep, decided_by
from facility_profiles.api.booking import NowDep, SessionDep, SettingsDep

router = APIRouter(prefix="/api", tags=["access"])

Who = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=128)]
Email = Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=254)]


class NewPerson(BaseModel):
    """Someone to put on the access list."""

    email: Email
    name: Annotated[str, StringConstraints(strip_whitespace=True, max_length=128)] | None = None
    by: Who | None = None


class Change(BaseModel):
    """One cell of the grid: a person's access to one customer (``none`` takes it back)."""

    email: Email
    customer: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=40)]
    level: Literal["view", "act", "none"]
    until: date | None = None
    note: Annotated[str, StringConstraints(strip_whitespace=True, max_length=255)] | None = None


class Changes(BaseModel):
    """Several cells saved together."""

    changes: list[Change] = Field(min_length=1, max_length=500)
    by: Who | None = None


class Removal(BaseModel):
    """Who takes the person off the list (the signed-in admin, when there is sign-in)."""

    by: Who | None = None


@router.get("/me")
def get_me(viewer: ViewerDep, settings: SettingsDep) -> dict[str, Any]:
    """The signed-in person, whether they are an admin, and the customers they see."""
    names = customer_names(settings)
    if viewer.admin:
        mine = [{"key": k, "name": n, "level": "admin"} for k, n in names.items()]
    else:
        mine = [
            {"key": k, "name": names.get(k, k), "level": level.value}
            for k, level in sorted(viewer.levels.items())
        ]
    return {
        "sign_in": signin_enabled(settings),
        "signed_in": viewer.signed_in,
        "email": viewer.email,
        "name": viewer.name,
        "admin": viewer.admin,
        "customers": mine,
        # whom to ask, for someone who sees nothing yet
        "admins": [] if viewer.admin or mine else list(settings.board_admins),
    }


@router.get("/access")
def get_access(
    _admin: AdminDep, session: SessionDep, settings: SettingsDep, now: NowDep
) -> dict[str, Any]:
    """Everyone on the list, with what each holds for each customer."""
    return access_grid(session, settings, now=now)


@router.get("/access/history")
def get_history(
    _admin: AdminDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    email: str | None = None,
) -> list[dict[str, Any]]:
    """The latest access changes first."""
    return [change_view(c) for c in history(session, limit=limit, email=email)]


@router.post("/access/people")
def post_person(
    body: NewPerson, admin: AdminDep, session: SessionDep, settings: SettingsDep, now: NowDep
) -> dict[str, Any]:
    """Put someone on the list; they see nothing until a customer is given to them."""
    by = decided_by(admin, body.by)
    try:
        add_person(session, settings, body.email, by=by, name=body.name, now=now)
    except AccessError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return access_grid(session, settings, now=now)


@router.post("/access/people/{email}/remove")
def post_remove(
    email: str,
    body: Removal,
    *,
    admin: AdminDep,
    session: SessionDep,
    settings: SettingsDep,
    now: NowDep,
) -> dict[str, Any]:
    """Take someone off the list, and back every customer they held."""
    by = decided_by(admin, body.by)
    try:
        remove_person(session, email, by=by, now=now)
    except AccessError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    session.commit()
    return access_grid(session, settings, now=now)


@router.post("/access")
def post_changes(
    body: Changes, admin: AdminDep, session: SessionDep, settings: SettingsDep, now: NowDep
) -> dict[str, Any]:
    """Give, change or take back access, all of the changes or none of them."""
    by = decided_by(admin, body.by)
    try:
        for c in body.changes:
            level = None if c.level == "none" else AccessLevel(c.level)
            set_access(
                session,
                settings,
                c.email,
                c.customer,
                level,
                by=by,
                until=c.until if level else None,
                note=c.note if level else None,
                now=now,
            )
    except AccessError as exc:
        session.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    session.commit()
    return access_grid(session, settings, now=now)
