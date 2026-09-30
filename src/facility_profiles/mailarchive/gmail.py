"""Gmail as one delegated mailbox, over urllib: only the calls the collector needs.

Kept free of the Google HTTP client so the Lambda zip carries ``google-auth`` for the JWT signer
and nothing else. Domain-wide delegation: the ``sub`` claim makes the service account act as the
member whose mailbox holds the group's mail; a Workspace admin must have authorised the client id
for ``gmail.readonly`` or Google answers ``unauthorized_client`` however valid the key is.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

API = "https://gmail.googleapis.com/gmail/v1"
TOKEN_URI = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
JWT_BEARER = "urn:ietf:params:oauth:grant-type:jwt-bearer"
SCOPE_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
TOKEN_LIFETIME = 3600
EXPIRY_SKEW = 120
METADATA_HEADERS = (
    "Subject",
    "From",
    "To",
    "Cc",
    "Date",
    "Message-ID",
    "In-Reply-To",
    "References",
)


class GmailError(RuntimeError):
    """A non-2xx answer from the Gmail API."""

    def __init__(self, status: int, path: str, body: str) -> None:
        super().__init__(f"Gmail {status} on {path}: {body[:300]}")
        self.status = status
        self.path = path
        self.body = body


class GmailClient(Protocol):
    """What the collector asks of Gmail; :class:`Delegated` for real, a fake in tests."""

    def search(self, query: str, cap: int = 2000) -> list[dict[str, str]]:
        """Message refs ({id, threadId}) matching a Gmail search, newest first."""
        ...

    def message(self, message_id: str, fmt: str = "raw") -> dict[str, Any]:
        """One message: ``raw`` (RFC822 base64url) or ``metadata`` (headers only)."""
        ...

    def thread(self, thread_id: str) -> dict[str, Any]:
        """Every message of a thread, headers only."""
        ...


def load_service_account(path: str | Path) -> dict[str, Any]:
    """The service account's JSON key file."""
    info = json.loads(Path(path).read_text(encoding="utf-8"))
    missing = [k for k in ("client_email", "private_key") if not info.get(k)]
    if missing:
        msg = f"{path} is missing {missing}; it must be the service account's JSON key"
        raise ValueError(msg)
    return dict(info)


class Delegated:  # pragma: no cover - live Google API
    """Mints and caches an impersonated access token for one mailbox, then GETs with it."""

    def __init__(
        self,
        info: dict[str, Any],
        subject: str,
        *,
        scopes: tuple[str, ...] = (SCOPE_READONLY,),
        min_interval: float = 0.05,
    ) -> None:
        self.info = info
        self.subject = subject
        self.scopes = scopes
        self.token_uri = str(info.get("token_uri") or TOKEN_URI)
        self._token: str | None = None
        self._expires_at = 0.0
        self._min_interval = min_interval  # the per-user-per-second cap is the real limit
        self._last_call = 0.0
        self.calls = 0

    # -- auth -----------------------------------------------------------------------------

    def token(self) -> str:
        """A valid access token, minted when the cached one is near expiry."""
        now = time.time()
        if self._token and now < self._expires_at - EXPIRY_SKEW:
            return self._token
        from google.auth import jwt as google_jwt  # noqa: PLC0415 - signer only, no HTTP stack
        from google.auth.crypt import RSASigner  # noqa: PLC0415

        payload = {
            "iss": self.info["client_email"],
            "scope": " ".join(self.scopes),
            "aud": self.token_uri,
            "iat": int(now),
            "exp": int(now) + TOKEN_LIFETIME,
            "sub": self.subject,
        }
        signer: Any = RSASigner.from_service_account_info(self.info)  # type: ignore[no-untyped-call]
        assertion: Any = google_jwt.encode(signer, payload)  # type: ignore[no-untyped-call]
        if isinstance(assertion, bytes):
            assertion = assertion.decode("ascii")
        body = urllib.parse.urlencode({"grant_type": JWT_BEARER, "assertion": assertion}).encode()
        req = urllib.request.Request(  # noqa: S310 - fixed https token endpoint
            self.token_uri,
            data=body,
            method="POST",
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:  # noqa: S310
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            hint = ""
            if "unauthorized_client" in text:
                hint = (
                    " (a Workspace admin must authorise this client id for gmail.readonly under "
                    "Security > API controls > Domain-wide delegation)"
                )
            raise GmailError(e.code, "token", text + hint) from None
        self._token = str(data["access_token"])
        self._expires_at = now + float(data.get("expires_in") or TOKEN_LIFETIME)
        return self._token

    # -- transport ------------------------------------------------------------------------

    def get(self, path: str, params: list[tuple[str, str]] | None = None) -> dict[str, Any]:
        """One GET against the API, with the rate gap and a short back-off on 429 and 5xx."""
        gap = self._min_interval - (time.time() - self._last_call)
        if gap > 0:
            time.sleep(gap)
        url = f"{API}{path}" + (
            ("?" + urllib.parse.urlencode(params, doseq=True)) if params else ""
        )
        req = urllib.request.Request(  # noqa: S310
            url, headers={"Authorization": f"Bearer {self.token()}", "Accept": "application/json"}
        )
        for attempt in range(4):
            try:
                with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310
                    self._last_call = time.time()
                    self.calls += 1
                    return dict(json.loads(r.read().decode("utf-8")))
            except urllib.error.HTTPError as e:
                text = e.read().decode("utf-8", "replace")
                self._last_call = time.time()
                if e.code in (429, 500, 502, 503) and attempt < 3:
                    time.sleep(2**attempt)
                    continue
                raise GmailError(e.code, path, text) from None
        raise GmailError(0, path, "retries exhausted")

    # -- API ------------------------------------------------------------------------------

    @property
    def _me(self) -> str:
        return urllib.parse.quote(self.subject)

    def profile(self) -> dict[str, Any]:
        """The mailbox profile (address, message count, current historyId)."""
        return self.get(f"/users/{self._me}/profile")

    def search(self, query: str, cap: int = 2000) -> list[dict[str, str]]:
        """Message refs matching ``query``; ids only, so cheap."""
        out: list[dict[str, str]] = []
        page: str | None = None
        while len(out) < cap:
            params = [("q", query), ("maxResults", "500")]
            if page:
                params.append(("pageToken", page))
            payload = self.get(f"/users/{self._me}/messages", params)
            out.extend(
                {"id": str(m["id"]), "threadId": str(m.get("threadId") or "")}
                for m in payload.get("messages") or []
            )
            page = payload.get("nextPageToken")
            if not page:
                break
        return out[:cap]

    def message(self, message_id: str, fmt: str = "raw") -> dict[str, Any]:
        """One message, ``raw`` or ``metadata`` (with the headers the collector reads)."""
        params = [("format", fmt)]
        if fmt == "metadata":
            params.extend(("metadataHeaders", h) for h in METADATA_HEADERS)
        return self.get(f"/users/{self._me}/messages/{message_id}", params)

    def thread(self, thread_id: str) -> dict[str, Any]:
        """Every message of a thread with the same headers, for backfilling a matched thread."""
        params = [("format", "metadata")] + [("metadataHeaders", h) for h in METADATA_HEADERS]
        return self.get(f"/users/{self._me}/threads/{thread_id}", params)


def headers_of(message: dict[str, Any]) -> dict[str, str]:
    """Lower-cased header map from a ``metadata`` or ``full`` message."""
    payload = message.get("payload") or {}
    return {
        str(h.get("name", "")).lower(): str(h.get("value", ""))
        for h in payload.get("headers") or []
    }


def internal_date_iso(message: dict[str, Any]) -> str | None:
    """Gmail's receipt time as ISO-8601 UTC, or None."""
    raw = message.get("internalDate")
    if not raw:
        return None
    return datetime.fromtimestamp(int(raw) / 1000, tz=UTC).isoformat(timespec="seconds")
