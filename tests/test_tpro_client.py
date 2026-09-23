import httpx
import pytest
import respx

from facility_profiles.tpro.client import RateLimiter, TransportProClient
from facility_profiles.tpro.errors import (
    TransportProApiError,
    TransportProAuthError,
    WritesDisabledError,
)
from facility_profiles.tpro.models import parse_iso

BASE = "https://tpro.test"


def make_client(**kwargs) -> TransportProClient:
    return TransportProClient(
        BASE, "user", "pass", max_requests_per_second=10_000, attempts=3, **kwargs
    )


def auth_route(router: respx.MockRouter, token: str = "tok-1"):
    return router.post(f"{BASE}/auth").mock(
        return_value=httpx.Response(
            200, json={"access_token": token, "refresh_token": "ref", "expires_in": 1800}
        )
    )


@respx.mock
def test_login_uses_basic_auth_then_bearer_on_requests():
    auth = auth_route(respx)
    facility = respx.get(f"{BASE}/location/197411").mock(
        return_value=httpx.Response(
            200, json={"id": 197411, "companyName": "STX DC", "appointments": {"method": False}}
        )
    )
    with make_client() as client:
        record = client.get_facility(197411)
    assert record.id == 197411
    assert record.appointments is not None and record.appointments.method is None
    assert auth.calls[0].request.headers["authorization"].startswith("Basic ")
    assert facility.calls[0].request.headers["authorization"] == "Bearer tok-1"
    assert "facility-profiles/" in facility.calls[0].request.headers["user-agent"]


@respx.mock
def test_401_triggers_one_reauth_and_retry():
    respx.post(f"{BASE}/auth").mock(
        side_effect=[
            httpx.Response(200, json={"token": "old"}),
            httpx.Response(200, json={"token": "new"}),
        ]
    )
    route = respx.get(f"{BASE}/terminal/1160").mock(
        side_effect=[
            httpx.Response(401, json={"error": "expired"}),
            httpx.Response(200, json={"id": 1160, "title": "POD (X)"}),
        ]
    )
    with make_client() as client:
        terminal = client.get_terminal(1160)
    assert terminal.is_pod
    assert route.calls[1].request.headers["authorization"] == "Bearer new"


@respx.mock
def test_pagination_iterates_until_last_page_with_zero_based_page_param():
    auth_route(respx)
    route = respx.get(f"{BASE}/load/search").mock(
        side_effect=[
            httpx.Response(
                200,
                json={
                    "pagination": {
                        "totalRecords": 3,
                        "perPage": 2,
                        "currentPage": 0,
                        "totalPages": 2,
                    },
                    "results": [{"id": 1, "waypoints": []}, {"id": 2, "waypoints": []}],
                },
            ),
            httpx.Response(
                200,
                json={
                    "pagination": {
                        "totalRecords": 3,
                        "perPage": 2,
                        "currentPage": 1,
                        "totalPages": 2,
                    },
                    "results": [{"id": 3, "waypoints": []}],
                },
            ),
        ]
    )
    with make_client() as client:
        ids = [
            load.id
            for load in client.iter_loads(
                terminal_id=1160, needs_appointment=True, pickup_date_start="2026-09-01"
            )
        ]
    assert ids == [1, 2, 3]
    first = route.calls[0].request.url.params
    assert (
        first["page"] == "0"
        and first["terminalId"] == "1160"
        and first["needsAppointment"] == "true"
    )
    assert route.calls[1].request.url.params["page"] == "1"


@respx.mock
def test_retries_server_errors_then_succeeds():
    auth_route(respx)
    route = respx.get(f"{BASE}/load/5").mock(
        side_effect=[
            httpx.Response(503, text="down"),
            httpx.Response(200, json={"id": 5, "waypoints": []}),
        ]
    )
    with make_client() as client:
        load = client.get_load(5)
    assert load.id == 5
    assert route.call_count == 2


@respx.mock
def test_client_errors_are_not_retried_and_carry_body():
    auth_route(respx)
    respx.get(f"{BASE}/load/9").mock(
        return_value=httpx.Response(404, json={"message": "no such load"})
    )
    with make_client() as client, pytest.raises(TransportProApiError) as exc:
        client.get_load(9)
    assert exc.value.status == 404
    assert exc.value.body == {"message": "no such load"}
    assert not exc.value.retryable


@respx.mock
def test_auth_failure_is_reported():
    respx.post(f"{BASE}/auth").mock(return_value=httpx.Response(403, text="bad creds"))
    with make_client() as client, pytest.raises(TransportProAuthError):
        client.list_terminals()


@respx.mock
def test_writes_are_refused_when_read_only():
    auth_route(respx)
    with make_client() as client, pytest.raises(WritesDisabledError):
        client.add_load_note(1, "hello")
    route = respx.post(f"{BASE}/load/1/note").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    with make_client(allow_writes=True) as client:
        client.add_load_note(1, "hello", priority=True)
    assert b"priorityNote=true" in route.calls[0].request.content


@respx.mock
def test_report_returns_csv_text_and_unwrap_handles_list_shapes():
    auth_route(respx)
    respx.get(f"{BASE}/reports/tar_load_detail").mock(
        return_value=httpx.Response(
            200, text='"Load Id","Origin Location ID"\n1,2\n', headers={"content-type": "text/csv"}
        )
    )
    respx.get(f"{BASE}/terminal/search").mock(
        return_value=httpx.Response(200, json=[{"id": 1, "title": "Office"}])
    )
    with make_client() as client:
        csv_text = client.terminal_activity_report(
            pickup_date_start="2026-09-20", pickup_date_end="2026-09-20"
        )
        terminals = client.list_terminals()
    assert csv_text.startswith('"Load Id"')
    assert terminals[0].id == 1 and not terminals[0].is_pod


def test_rate_limiter_spaces_calls():
    clock = [0.0]
    slept: list[float] = []
    limiter = RateLimiter(2, clock=lambda: clock[0])
    limiter.acquire(sleep=slept.append)
    limiter.acquire(sleep=slept.append)
    assert slept == [0.5]
    with pytest.raises(ValueError, match="positive"):
        RateLimiter(0)


def test_parse_iso_tolerates_api_quirks():
    assert parse_iso("2026-09-02T:18:22:45Z").isoformat() == "2026-09-02T18:22:45+00:00"
    assert parse_iso("2026-09-05T02:30:00Z").tzinfo is not None
    assert parse_iso("not a date") is None
    assert parse_iso(None) is None
