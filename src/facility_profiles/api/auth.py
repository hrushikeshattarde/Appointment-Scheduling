"""Signing in to the board with Google, and who is asking.

With FP_GOOGLE_CLIENT_ID and FP_GOOGLE_CLIENT_SECRET unset there is no sign-in: every request
is :data:`~facility_profiles.access.LOCAL`, an admin, as the board always was (keep it on
127.0.0.1). With them set, every board API call needs the sign-in cookie, and
:func:`current_viewer` reads the person's access from the store on each request, so what an
admin changes holds from the person's next page.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Callable
from datetime import UTC, datetime
from html import escape
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from facility_profiles.access import LOCAL, Viewer, signin_enabled, viewer_for
from facility_profiles.access.signin import (
    SESSION_COOKIE,
    SIGNIN_COOKIE,
    SIGNIN_MINUTES,
    SignInError,
    authorize_url,
    exchange_code,
    identity_from_claims,
    safe_next,
    sign,
    unsign,
)
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope

router = APIRouter(tags=["sign-in"])


def _now(request: Request) -> datetime:
    clock: Callable[[], datetime] | None = getattr(request.app.state, "clock", None)
    return clock() if clock is not None else datetime.now(tz=UTC)


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _secret(request: Request) -> bytes:
    secret: bytes = request.app.state.session_secret
    return secret


def _base(request: Request, settings: Settings) -> str:
    return settings.board_public_url or str(request.base_url).rstrip("/")


def _secure(request: Request, settings: Settings) -> bool:
    return _base(request, settings).startswith("https://")


def redirect_uri(request: Request, settings: Settings) -> str:
    """Where Google sends people back to; it must be on the client's list in Google Cloud."""
    return f"{_base(request, settings)}/auth/callback"


# ------------------------------------------------------------------ who is asking


def current_viewer(request: Request) -> Viewer:
    """The signed-in person and their access, or 401; everyone is an admin without sign-in."""
    settings = _settings(request)
    if not signin_enabled(settings):
        return LOCAL
    now = _now(request)
    data = unsign(_secret(request), request.cookies.get(SESSION_COOKIE), now=now)
    email = str(data.get("e", "")) if data else ""
    if not email or email.rpartition("@")[2] not in settings.board_domains:
        raise HTTPException(status_code=401, detail="Sign in to open the board.")
    with session_scope(request.app.state.sessions) as session:
        return viewer_for(session, settings, email, data.get("n") if data else None, now=now)


ViewerDep = Annotated[Viewer, Depends(current_viewer)]


def require_admin(viewer: ViewerDep) -> Viewer:
    """The viewer, when they are an admin; else 403."""
    if not viewer.admin:
        raise HTTPException(status_code=403, detail="Only an admin can open this.")
    return viewer


AdminDep = Annotated[Viewer, Depends(require_admin)]


def decided_by(viewer: Viewer, by: str | None) -> str:
    """Who a decision is recorded under: the signed-in person, else the name the request gives."""
    if viewer.signed_in:
        return viewer.label
    if not by or not by.strip():
        raise HTTPException(status_code=422, detail="say who decides (by)")
    return by.strip()


# ------------------------------------------------------------------ pages


def _page(title: str, text: str, *, status: int = 200, again: bool = True) -> HTMLResponse:
    """A small page of plain text (every value escaped) with a way to sign in again."""
    link = '<p><a href="/auth/login">Sign in</a></p>' if again else ""
    body = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{escape(title)} - Pickup appointments</title>
<style>
:root {{ color-scheme: light dark; }}
body {{ font: 15px/1.5 "Segoe UI", system-ui, sans-serif; margin: 0; padding: 48px 16px;
  background: Canvas; color: CanvasText; }}
main {{ max-width: 440px; margin: 0 auto; }}
h1 {{ font-size: 20px; margin: 0 0 8px; }}
a {{ color: #2257d6; font-weight: 600; }}
@media (prefers-color-scheme: dark) {{ a {{ color: #7aa2ff; }} }}
</style></head>
<body><main><h1>{escape(title)}</h1><p>{escape(text)}</p>{link}</main></body></html>"""
    return HTMLResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


@router.get("/auth/login", include_in_schema=False)
def login(request: Request, next: str | None = None) -> Response:  # noqa: A002 - the usual name
    """Send the person to Google, remembering where they were going."""
    settings = _settings(request)
    target = safe_next(next)
    if not signin_enabled(settings):
        return RedirectResponse(target, status_code=303)
    state, nonce = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    expires = _now(request).timestamp() + SIGNIN_MINUTES * 60
    started = sign(_secret(request), {"s": state, "n": nonce, "next": target, "exp": expires})
    url = authorize_url(
        settings, redirect_uri=redirect_uri(request, settings), state=state, nonce=nonce
    )
    response = RedirectResponse(url, status_code=303)
    response.set_cookie(
        SIGNIN_COOKIE,
        started,
        max_age=SIGNIN_MINUTES * 60,
        httponly=True,
        samesite="lax",
        secure=_secure(request, settings),
        path="/auth",
    )
    return response


@router.get("/auth/callback", include_in_schema=False)
def callback(
    request: Request, code: str | None = None, state: str | None = None, error: str | None = None
) -> Response:
    """Google sent the person back: check who they are and give them the sign-in cookie."""
    settings = _settings(request)
    if not signin_enabled(settings):
        return RedirectResponse("/app/", status_code=303)
    now = _now(request)
    started = unsign(_secret(request), request.cookies.get(SIGNIN_COOKIE), now=now)
    if error:
        return _page("Not signed in", "Google did not sign you in.", status=400)
    if (
        started is None
        or not code
        or not state
        or not hmac.compare_digest(state, str(started.get("s", "")))
    ):
        text = "This sign-in expired or was started in another browser. Sign in again."
        return _page("Sign-in expired", text, status=400)
    try:
        claims = exchange_code(settings, code, redirect_uri=redirect_uri(request, settings))
        identity = identity_from_claims(claims, settings, nonce=str(started.get("n", "")), now=now)
    except SignInError as exc:
        return _page("Could not sign you in", str(exc), status=403)
    seconds = int(settings.board_session_hours * 3600)
    cookie = sign(
        _secret(request),
        {"e": identity.email, "n": identity.name, "exp": now.timestamp() + seconds},
    )
    response = RedirectResponse(safe_next(str(started.get("next", ""))), status_code=303)
    response.set_cookie(
        SESSION_COOKIE,
        cookie,
        max_age=seconds,
        httponly=True,
        samesite="lax",
        secure=_secure(request, settings),
        path="/",
    )
    response.delete_cookie(SIGNIN_COOKIE, path="/auth")
    return response


@router.post("/auth/logout", include_in_schema=False)
def logout() -> Response:
    """Forget the sign-in on this browser."""
    response = RedirectResponse("/auth/signed-out", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path="/")
    return response


@router.get("/auth/signed-out", include_in_schema=False)
def signed_out() -> HTMLResponse:
    """Said after signing out."""
    return _page("Signed out", "You're signed out of the appointments board.")
