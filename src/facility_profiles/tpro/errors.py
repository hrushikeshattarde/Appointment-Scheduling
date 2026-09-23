"""Error types raised by the Transport Pro client."""

from __future__ import annotations

from typing import Any


class TransportProError(Exception):
    """Base class for every Transport Pro client error."""


class TransportProAuthError(TransportProError):
    """Authentication failed or no usable token came back."""


class WritesDisabledError(TransportProError):
    """A write endpoint was called while the client is read-only (FR-17)."""


class TransportProApiError(TransportProError):
    """The API answered with a non-2xx status."""

    def __init__(
        self,
        status: int,
        body: Any,
        request: str,
        *,
        retry_after_seconds: float | None = None,
    ) -> None:
        self.status = status
        self.body = body
        self.request = request
        self.retry_after_seconds = retry_after_seconds
        summary = body if isinstance(body, str) else repr(body)
        super().__init__(f"Transport Pro API error {status} on {request}: {summary[:500]}")

    @property
    def retryable(self) -> bool:
        """True for rate limiting and server-side failures."""
        return self.status == 429 or self.status >= 500
