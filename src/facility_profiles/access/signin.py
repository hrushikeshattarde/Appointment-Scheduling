"""Signing in with Google (OpenID Connect, authorization code flow) and the signed cookies.

The board sends the person to Google with a one-time ``state`` and ``nonce`` (kept in a short
cookie), Google sends them back with a code, and the server trades the code for an ID token
directly with Google, using the client secret. Because that token comes straight from Google's
token endpoint over HTTPS, its claims are trusted without checking the signature (OpenID Connect
Core 3.1.3.7), but every claim that says who the person is gets checked: issuer, audience,
expiry, nonce, a verified email, and a Google Workspace domain (``hd``) the board accepts. A
personal Google account made with a work address has no ``hd`` and is turned away.

Cookies are ``base64url(json).base64url(hmac-sha256)``: tamper with either half and it is
ignored, and each carries its own expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import httpx

from facility_profiles.config import Settings

SESSION_COOKIE = "fp_session"
SIGNIN_COOKIE = "fp_signin"
SIGNIN_MINUTES = 10  # how long a sign-in may take between leaving for Google and coming back

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a password
GOOGLE_ISSUERS = frozenset({"accounts.google.com", "https://accounts.google.com"})


class SignInError(Exception):
    """A sign-in that did not work; the message is shown to the person."""


@dataclass(frozen=True)
class Identity:
    """Who Google says signed in."""

    email: str
    name: str | None


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def sign(secret: bytes, data: dict[str, Any]) -> str:
    """A cookie value carrying ``data``; give it an ``exp`` (Unix seconds)."""
    payload = _b64(json.dumps(data, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    return f"{payload}.{mac}"


def unsign(secret: bytes, token: str | None, *, now: datetime) -> dict[str, Any] | None:
    """The data in a cookie value this server signed and that has not expired, else None."""
    if not token or token.count(".") != 1:
        return None
    payload, mac = token.split(".")
    want = _b64(hmac.new(secret, payload.encode(), hashlib.sha256).digest())
    if not hmac.compare_digest(mac, want):
        return None
    try:
        data = json.loads(_unb64(payload))
    except ValueError:
        return None
    if not isinstance(data, dict) or float(data.get("exp", 0)) <= now.timestamp():
        return None
    return data


def safe_next(target: str | None, default: str = "/app/") -> str:
    """Where to go after signing in: a path on this server, never another site."""
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return default
    return target


def authorize_url(settings: Settings, *, redirect_uri: str, state: str, nonce: str) -> str:
    """Google's sign-in page for this board."""
    params = {
        "client_id": settings.google_client_id or "",
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "nonce": nonce,
        "prompt": "select_account",
    }
    if len(settings.board_domains) == 1:
        params["hd"] = settings.board_domains[0]  # Google offers only that domain's accounts
    return f"{GOOGLE_AUTH_URL}?{urlencode(params)}"


def exchange_code(
    settings: Settings, code: str, *, redirect_uri: str, http: httpx.Client | None = None
) -> dict[str, Any]:
    """Trade Google's code for the ID token and return its claims."""
    secret = settings.google_client_secret
    form = {
        "code": code,
        "client_id": settings.google_client_id or "",
        "client_secret": secret.get_secret_value() if secret else "",
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }
    client = http or httpx.Client(timeout=10.0)
    try:
        response = client.post(GOOGLE_TOKEN_URL, data=form)
    except httpx.HTTPError as exc:
        msg = "Google could not be reached to finish signing in. Try again."
        raise SignInError(msg) from exc
    finally:
        if http is None:
            client.close()
    if response.status_code != 200:
        msg = "Google did not accept this sign-in. Sign in again."
        raise SignInError(msg)
    token = response.json().get("id_token")
    if not isinstance(token, str) or token.count(".") != 2:
        msg = "Google's answer had no ID token. Sign in again."
        raise SignInError(msg)
    try:
        claims = json.loads(_unb64(token.split(".")[1]))
    except ValueError as exc:
        msg = "Google's ID token could not be read. Sign in again."
        raise SignInError(msg) from exc
    if not isinstance(claims, dict):
        msg = "Google's ID token could not be read. Sign in again."
        raise SignInError(msg)
    return claims


def identity_from_claims(
    claims: dict[str, Any], settings: Settings, *, nonce: str, now: datetime
) -> Identity:
    """Who signed in, when every claim checks out; else :class:`SignInError`."""
    audience = claims.get("aud")
    audiences = audience if isinstance(audience, list) else [audience]
    if claims.get("iss") not in GOOGLE_ISSUERS or settings.google_client_id not in audiences:
        msg = "That sign-in was not meant for this board. Sign in again."
        raise SignInError(msg)
    if float(claims.get("exp", 0)) <= now.timestamp():
        msg = "That sign-in expired. Sign in again."
        raise SignInError(msg)
    if not hmac.compare_digest(str(claims.get("nonce", "")), nonce):
        msg = "That sign-in did not match the one this browser started. Sign in again."
        raise SignInError(msg)
    email = str(claims.get("email", "")).strip().lower()
    if not email or claims.get("email_verified") is not True:
        msg = "Google did not confirm an email address for that account."
        raise SignInError(msg)
    domain = email.rpartition("@")[2]
    hosted = str(claims.get("hd", "")).lower()
    if domain not in settings.board_domains or hosted not in settings.board_domains:
        allowed = ", ".join(settings.board_domains)
        msg = f"Sign in with your company account ({allowed}); {email} cannot open this board."
        raise SignInError(msg)
    name = claims.get("name")
    return Identity(email=email, name=str(name).strip()[:128] if name else None)
