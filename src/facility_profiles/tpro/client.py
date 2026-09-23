"""HTTP client for the Transport Pro Public API.

Responsibilities:

* Attach a valid bearer token to every request (see :mod:`facility_profiles.tpro.auth`).
* Throttle to a configurable request rate and retry rate-limit, server and transport errors
  with exponential backoff, honouring ``Retry-After``.
* Retry exactly once with a fresh token when the API answers 401.
* Page through list endpoints with the zero-based ``page`` parameter.
* Refuse write endpoints unless the client was created with ``allow_writes=True`` (FR-17).

Endpoint paths come from the Transport Pro Postman collection as implemented in the Transport
Pro MCP server.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Callable, Iterator
from typing import TYPE_CHECKING, Any, TypeVar

import httpx
from pydantic import BaseModel
from pydantic.alias_generators import to_camel
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from facility_profiles import __version__
from facility_profiles.logging import get_logger
from facility_profiles.tpro.auth import TokenManager
from facility_profiles.tpro.errors import (
    TransportProApiError,
    WritesDisabledError,
)
from facility_profiles.tpro.models import (
    Dispatch,
    Facility,
    Load,
    LoadNote,
    Page,
    Terminal,
    TrackingNote,
    User,
    VoiceAiLoad,
)

if TYPE_CHECKING:
    from facility_profiles.config import Settings

log = get_logger(__name__)

M = TypeVar("M", bound=BaseModel)

PAGE_PARAM = "page"
MAX_RETRY_AFTER_SECONDS = 60.0
DEFAULT_ATTEMPTS = 5


class RateLimiter:
    """Simple thread-safe limiter: at most ``max_per_second`` calls, evenly spaced."""

    def __init__(self, max_per_second: float, clock: Callable[[], float] = time.monotonic) -> None:
        if max_per_second <= 0:
            msg = "max_per_second must be positive"
            raise ValueError(msg)
        self._interval = 1.0 / max_per_second
        self._clock = clock
        self._next_allowed = 0.0
        self._lock = threading.Lock()

    def acquire(self, sleep: Callable[[float], None] = time.sleep) -> None:
        """Block until the next call is permitted."""
        with self._lock:
            now = self._clock()
            wait_for = self._next_allowed - now
            self._next_allowed = max(now, self._next_allowed) + self._interval
        if wait_for > 0:
            sleep(wait_for)


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, TransportProApiError):
        return exc.retryable
    return isinstance(exc, httpx.TransportError)


def _wait_strategy(retry_state: RetryCallState) -> float:
    outcome = retry_state.outcome
    exc = outcome.exception() if outcome is not None else None
    if isinstance(exc, TransportProApiError) and exc.retry_after_seconds:
        return min(exc.retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
    return float(wait_exponential_jitter(initial=0.5, max=20.0)(retry_state))


def _query(params: dict[str, Any] | None) -> dict[str, str]:
    """Serialise query params the way the API expects; drop nulls and blanks."""
    out: dict[str, str] = {}
    for key, value in (params or {}).items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            out[to_camel(key)] = "true" if value else "false"
        else:
            out[to_camel(key)] = str(value)
    return out


def _unwrap_results(data: Any) -> list[Any]:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        results = data.get("results")
        if isinstance(results, list):
            return results
    return []


class TransportProClient:
    """Typed, rate-limited, read-only-by-default client for the Transport Pro Public API."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout_seconds: float = 30.0,
        max_requests_per_second: float = 5.0,
        allow_writes: bool = False,
        attempts: int = DEFAULT_ATTEMPTS,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.allow_writes = allow_writes
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={"User-Agent": f"facility-profiles/{__version__}"},
        )
        self._tokens = TokenManager(self._http, self.base_url, username, password)
        self._limiter = RateLimiter(max_requests_per_second)
        self._attempts = attempts

    @classmethod
    def from_settings(cls, settings: Settings, *, allow_writes: bool = False) -> TransportProClient:
        """Build a client from application settings."""
        return cls(
            settings.tpro_base_url,
            settings.tpro_username.get_secret_value(),
            settings.tpro_password.get_secret_value(),
            timeout_seconds=settings.timeout_seconds,
            max_requests_per_second=settings.tpro_max_requests_per_second,
            allow_writes=allow_writes,
        )

    def close(self) -> None:
        """Close the underlying HTTP connection pool."""
        self._http.close()

    def __enter__(self) -> TransportProClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------------ low level

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any | None = None,
        data: dict[str, Any] | None = None,
    ) -> Any:
        """Issue a request and return parsed JSON (or text for non-JSON bodies)."""
        retrying = Retrying(
            retry=retry_if_exception(_is_retryable),
            wait=_wait_strategy,
            stop=stop_after_attempt(self._attempts),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._send(method, path, params=params, json=json, data=data)
        return None  # pragma: no cover - Retrying always returns or raises

    def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None,
        json: Any | None,
        data: dict[str, Any] | None,
        is_retry: bool = False,
    ) -> Any:
        self._limiter.acquire()
        token = self._tokens.get_access_token(force_refresh=is_retry)
        request_label = f"{method} {path}"
        response = self._http.request(
            method,
            f"{self.base_url}{path}",
            params=_query(params),
            json=json,
            data=data,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if response.status_code == 401 and not is_retry:
            log.info("tpro.reauth", request=request_label)
            self._tokens.invalidate()
            return self._send(method, path, params=params, json=json, data=data, is_retry=True)
        if response.status_code >= 400:
            body: Any = response.text
            with contextlib.suppress(ValueError):
                body = response.json()
            retry_after = _parse_retry_after(response.headers.get("retry-after"))
            raise TransportProApiError(
                response.status_code, body, request_label, retry_after_seconds=retry_after
            )
        if not response.content:
            return {"status": response.status_code, "ok": True}
        if "json" in response.headers.get("content-type", ""):
            try:
                return response.json()
            except ValueError:
                return response.text
        return response.text

    def _get_model(self, path: str, model: type[M], params: dict[str, Any] | None = None) -> M:
        return model.model_validate(self.request("GET", path, params=params))

    def _get_list(self, path: str, model: type[M], params: dict[str, Any] | None = None) -> list[M]:
        data = self.request("GET", path, params=params)
        return [model.model_validate(item) for item in _unwrap_results(data)]

    def iter_pages(
        self, path: str, params: dict[str, Any] | None = None
    ) -> Iterator[dict[str, Any]]:
        """Yield each raw page of a paginated list endpoint, stopping at the last page."""
        page = 0
        while True:
            data = self.request("GET", path, params={**(params or {}), PAGE_PARAM: page})
            if not isinstance(data, dict):
                return
            yield data
            pagination = data.get("pagination") or {}
            total_pages = int(pagination.get("totalPages") or 0)
            results = data.get("results") or []
            page += 1
            if not results or page >= total_pages:
                return

    def _require_writes(self, operation: str) -> None:
        if not self.allow_writes:
            msg = f"{operation} is a write operation and this client is read-only (FR-17)"
            raise WritesDisabledError(msg)

    # ------------------------------------------------------------------ loads

    def search_loads(self, *, page: int = 0, **filters: Any) -> Page[Load]:
        """``GET /load/search`` with snake_case filters (terminal_id, pickup_date_start, ...)."""
        data = self.request("GET", "/load/search", params={**filters, PAGE_PARAM: page})
        return Page[Load].model_validate(data)

    def iter_loads(self, **filters: Any) -> Iterator[Load]:
        """Iterate every load matching ``filters`` across all pages."""
        for page in self.iter_pages("/load/search", filters):
            for item in page.get("results") or []:
                yield Load.model_validate(item)

    def get_load(self, load_id: int) -> Load:
        """``GET /load/{id}``."""
        return self._get_model(f"/load/{load_id}", Load)

    def get_load_notes(self, load_id: int) -> list[LoadNote]:
        """``GET /load/{id}/notes``."""
        return self._get_list(f"/load/{load_id}/notes", LoadNote)

    def get_voiceai_load(self, load_id: int) -> VoiceAiLoad | None:
        """``GET /voiceai/load/{id}``: stops with actual arrival and departure times."""
        data = self.request("GET", f"/voiceai/load/{load_id}")
        results = _unwrap_results(data)
        if results:
            return VoiceAiLoad.model_validate(results[0])
        if isinstance(data, dict) and "load_id" in data:
            return VoiceAiLoad.model_validate(data)
        return None

    # ------------------------------------------------------------------ facilities

    def get_facility(self, location_id: int) -> Facility:
        """``GET /location/{id}``."""
        return self._get_model(f"/location/{location_id}", Facility)

    # ------------------------------------------------------------------ dispatches

    def search_dispatches(self, load_id: int) -> list[Dispatch]:
        """``GET /dispatch/search?loadId=``."""
        return self._get_list("/dispatch/search", Dispatch, params={"load_id": load_id})

    def get_dispatch(self, dispatch_id: int) -> Dispatch:
        """``GET /dispatch/{id}``."""
        return self._get_model(f"/dispatch/{dispatch_id}", Dispatch)

    def get_dispatch_notes(self, dispatch_id: int) -> list[TrackingNote]:
        """``GET /dispatch/{id}/notes``."""
        return self._get_list(f"/dispatch/{dispatch_id}/notes", TrackingNote)

    # ------------------------------------------------------------------ tracking

    def get_tracking_load_notes(self, load_id: int) -> list[TrackingNote]:
        """``GET /tracking/note/load/{id}``."""
        return self._get_list(f"/tracking/note/load/{load_id}", TrackingNote)

    def get_tracking_dispatch_notes(self, dispatch_id: int) -> list[TrackingNote]:
        """``GET /tracking/note/dispatch/{id}``."""
        return self._get_list(f"/tracking/note/dispatch/{dispatch_id}", TrackingNote)

    # ------------------------------------------------------------------ terminals, users

    def list_terminals(self) -> list[Terminal]:
        """``GET /terminal/search``."""
        return self._get_list("/terminal/search", Terminal)

    def get_terminal(self, terminal_id: int) -> Terminal:
        """``GET /terminal/{id}``."""
        return self._get_model(f"/terminal/{terminal_id}", Terminal)

    def get_user(self, user_id: int) -> User:
        """``GET /user/{id}``."""
        return self._get_model(f"/user/{user_id}", User)

    # ------------------------------------------------------------------ reports

    def terminal_activity_report(
        self,
        *,
        pickup_date_start: str | None = None,
        pickup_date_end: str | None = None,
        date_created_start: str | None = None,
        date_created_end: str | None = None,
        dispatch_date_start: str | None = None,
        dispatch_date_end: str | None = None,
    ) -> str:
        """``GET /reports/tar_load_detail`` as CSV text (dates as ``YYYY-MM-DD``)."""
        data = self.request(
            "GET",
            "/reports/tar_load_detail",
            params={
                "pickup_date_start": pickup_date_start,
                "pickup_date_end": pickup_date_end,
                "date_created_start": date_created_start,
                "date_created_end": date_created_end,
                "dispatch_date_start": dispatch_date_start,
                "dispatch_date_end": dispatch_date_end,
                "format": "csv",
            },
        )
        return data if isinstance(data, str) else ""

    # --- Writes. Refused unless allow_writes is set (FR-17). ---

    def add_load_note(self, load_id: int, content: str, *, priority: bool = False) -> Any:
        """``POST /load/{id}/note``. Refused unless ``allow_writes`` is set."""
        self._require_writes("add_load_note")
        return self.request(
            "POST",
            f"/load/{load_id}/note",
            data={"content": content, "priorityNote": "true" if priority else "false"},
        )

    def set_appointment(
        self,
        load_id: int,
        waypoint_index: str,
        start_utc: str,
        end_utc: str,
        status: str | None = None,
    ) -> Any:
        """``POST /load/{id}/set_appointment``. Not used by Idea 1; guarded for later ideas."""
        self._require_writes("set_appointment")
        form: dict[str, Any] = {
            "waypointIndex": waypoint_index,
            "startDate": start_utc,
            "endDate": end_utc,
        }
        if status:
            form["appointmentStatus"] = status
        return self.request("POST", f"/load/{load_id}/set_appointment", data=form)


def _parse_retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if seconds > 0 else None
