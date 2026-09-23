"""Token management for the Transport Pro Public API.

Auth flow, taken from the Transport Pro Postman collection and mirrored by the MCP server:

* Login:   ``POST {base}/auth`` with HTTP Basic credentials.
* Refresh: ``POST {base}/auth`` with JSON ``{"grant_type": "refresh_token", ...}``.
* Every other endpoint expects ``Authorization: Bearer {access_token}``.

The manager caches the token, refreshes shortly before expiry, falls back to a fresh login
when the refresh is rejected, and serialises concurrent acquisitions behind a lock.
"""

from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass
from typing import Any

import httpx

from facility_profiles.tpro.errors import TransportProAuthError

EXPIRY_BUFFER_SECONDS = 60
DEFAULT_TTL_SECONDS = 30 * 60
MIN_TTL_SECONDS = 30


@dataclass(frozen=True)
class TokenSet:
    """An access token and the monotonic instant after which it is considered stale."""

    access_token: str
    refresh_token: str | None
    expires_at: float

    def is_valid(self, now: float | None = None) -> bool:
        """True while the access token is inside its safe lifetime."""
        current = time.monotonic() if now is None else now
        return current < self.expires_at


class TokenManager:
    """Acquire and cache bearer tokens for the Transport Pro API."""

    def __init__(self, http: httpx.Client, base_url: str, username: str, password: str) -> None:
        self._http = http
        self._auth_url = f"{base_url}/auth"
        self._username = username
        self._password = password
        self._tokens: TokenSet | None = None
        self._lock = threading.Lock()

    def get_access_token(self, *, force_refresh: bool = False) -> str:
        """Return a valid access token, logging in or refreshing as needed."""
        with self._lock:
            if force_refresh:
                self._tokens = None
            if self._tokens is not None and self._tokens.is_valid():
                return self._tokens.access_token
            self._tokens = self._acquire()
            return self._tokens.access_token

    def invalidate(self) -> None:
        """Drop the cached token so the next call re-authenticates."""
        with self._lock:
            self._tokens = None

    def _acquire(self) -> TokenSet:
        if self._tokens is not None and self._tokens.refresh_token:
            try:
                return self._refresh(self._tokens.refresh_token)
            except TransportProAuthError:
                pass  # Refresh token expired or rejected: fall through to a fresh login.
        return self._login()

    def _login(self) -> TokenSet:
        basic = base64.b64encode(f"{self._username}:{self._password}".encode()).decode()
        data = self._post_auth(headers={"Authorization": f"Basic {basic}"})
        return parse_token_response(data, "login")

    def _refresh(self, refresh_token: str) -> TokenSet:
        data = self._post_auth(
            headers={"Content-Type": "application/json"},
            json={"grant_type": "refresh_token", "refresh_token": refresh_token},
        )
        return parse_token_response(data, "refresh")

    def _post_auth(self, *, headers: dict[str, str], json: Any | None = None) -> Any:
        try:
            response = self._http.post(
                self._auth_url,
                headers={"Accept": "application/json", **headers},
                json=json,
            )
        except httpx.HTTPError as exc:
            msg = f"Could not reach the Transport Pro auth endpoint at {self._auth_url}: {exc}"
            raise TransportProAuthError(msg) from exc
        if response.status_code >= 400:
            msg = (
                f"Authentication request failed with HTTP {response.status_code}: "
                f"{response.text[:300]}"
            )
            raise TransportProAuthError(msg)
        try:
            return response.json()
        except ValueError as exc:
            msg = f"Auth endpoint returned a non-JSON response: {response.text[:300]}"
            raise TransportProAuthError(msg) from exc


def _first_string(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def parse_token_response(data: Any, operation: str) -> TokenSet:
    """Extract a token set from the auth response, tolerating the common field-name variants."""
    record: dict[str, Any] = data if isinstance(data, dict) else {}
    access_token = _first_string(
        record.get("access_token"), record.get("accessToken"), record.get("token")
    )
    if access_token is None:
        keys = ", ".join(record.keys()) or "(none)"
        msg = (
            f"Auth {operation} succeeded but no access token was found in the response. "
            f"Response keys: {keys}"
        )
        raise TransportProAuthError(msg)
    refresh_token = _first_string(record.get("refresh_token"), record.get("refreshToken"))
    raw_ttl = record.get("expires_in", record.get("expiresIn"))
    try:
        ttl = float(raw_ttl) if raw_ttl is not None else float(DEFAULT_TTL_SECONDS)
    except (TypeError, ValueError):
        ttl = float(DEFAULT_TTL_SECONDS)
    if ttl <= 0:
        ttl = float(DEFAULT_TTL_SECONDS)
    lifetime = max(ttl - EXPIRY_BUFFER_SECONDS, MIN_TTL_SECONDS)
    return TokenSet(
        access_token=access_token,
        refresh_token=refresh_token,
        expires_at=time.monotonic() + lifetime,
    )
