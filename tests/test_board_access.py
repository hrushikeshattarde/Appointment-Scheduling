"""Signing in to the board with Google, and each person seeing only the customers given to them.

Everything here is invented: the Google client, the accounts (circledelivers.com addresses that
belong to nobody), the second customer (Northline, from tests/test_customers.py) and the cases.
No request leaves the machine: Google's token endpoint is mocked with respx.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
import respx
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

from facility_profiles.access import AccessLevel, Viewer, set_access, viewer_for
from facility_profiles.access.signin import (
    GOOGLE_TOKEN_URL,
    SESSION_COOKIE,
    SignInError,
    identity_from_claims,
    safe_next,
    sign,
    unsign,
)
from facility_profiles.api.app import create_app
from facility_profiles.booking.models import DeskMemory, ExceptionType
from facility_profiles.booking.worklist import flag
from facility_profiles.cli import app as cli_app
from facility_profiles.config import Settings, get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from tests.test_booking_api import NOW, _case
from tests.test_customers import NORTHLINE

CLIENT_ID = "board-test.apps.googleusercontent.com"
KEY = b"test-session-key-not-a-secret"
ADMIN = "admin@circledelivers.com"
AM = "am.one@circledelivers.com"
AM2 = "am.two@circledelivers.com"


def _cookie(email: str, name: str | None = None, *, hours: float = 1) -> str:
    return sign(KEY, {"e": email, "n": name, "exp": (NOW + timedelta(hours=hours)).timestamp()})


@pytest.fixture
def known(settings: Settings, tmp_path: Path) -> Settings:
    """Settings that know Lidl (built in) and the invented Northline, with sign-in off."""
    folder = tmp_path / "customers"
    folder.mkdir()
    (folder / "northline.toml").write_text(NORTHLINE, encoding="utf-8")
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    return settings.model_copy(update={"customers_dir": str(folder), "database_url": url})


@pytest.fixture
def signin(known: Settings) -> Settings:
    """The same, with Google sign-in on and one admin."""
    return known.model_copy(
        update={
            "google_client_id": CLIENT_ID,
            "google_client_secret": SecretStr("client-secret-for-tests"),
            "board_admins": [ADMIN],
            "board_session_secret": SecretStr(KEY.decode()),
        }
    )


def _seed(settings: Settings) -> dict[str, int]:
    """Two Lidl cases (one waiting for approval), one Northline case, one nobody's."""
    engine = make_engine(settings.database_url)
    init_db(engine)
    with session_scope(session_factory(engine)) as s:
        lidl = _case(s, 1, status="unscheduled", requested="2026-09-30 09:00")
        review = _case(
            s, 2, status="pending", requested="2026-10-01 09:00", confirmed="2026-10-01 11:00"
        )
        flag(s, review, ExceptionType.CONFIRMATION_REVIEW, "vendor confirmed 2026-10-01 11:00")
        north = _case(
            s,
            3,
            status="pending",
            requested="2026-10-01 08:00",
            customer="Northline Grocers - Inbound",
        )
        other = _case(s, 4, status="unscheduled", requested="2026-10-02 09:00", customer="Other Co")
        # The same vendor booked for both customers: the desk memory names Northline's case.
        lidl.facility_key = north.facility_key = "koch-erlanger"
        s.add(
            DeskMemory(
                facility_key="koch-erlanger",
                method="email",
                desk="cci@udfinc.example",
                worked_count=2,
                last_case_id=north.id,
            )
        )
        ids = {"lidl": lidl.id, "review": review.id, "north": north.id, "other": other.id}
    engine.dispose()
    return ids


@pytest.fixture
def board(signin: Settings) -> Iterator[tuple[TestClient, dict[str, int]]]:
    ids = _seed(signin)
    application = create_app(signin)
    application.state.clock = lambda: NOW
    with TestClient(application) as client:
        yield client, ids


def _as(client: TestClient, email: str, name: str | None = None) -> TestClient:
    client.cookies.set(SESSION_COOKIE, _cookie(email, name))
    return client


def _grant(client: TestClient, *changes: dict[str, Any]) -> httpx.Response:
    _as(client, ADMIN, "The Admin")
    return client.post("/api/access", json={"changes": list(changes)})


# ------------------------------------------------------------------ without sign-in


def test_without_sign_in_the_board_works_as_it_always_did(known: Settings) -> None:
    ids = _seed(known)
    application = create_app(known)
    application.state.clock = lambda: NOW
    with TestClient(application) as client:
        me = client.get("/api/me").json()
        assert me["sign_in"] is False and me["signed_in"] is False and me["admin"] is True
        assert len(client.get("/api/booking/cases").json()) == 4
        assert client.get(f"/api/booking/cases/{ids['north']}").json()["can_act"] is True
        # decisions still need the typed name
        assert (
            client.post(f"/api/booking/cases/{ids['review']}/approve", json={}).status_code == 422
        )
        ok = client.post(f"/api/booking/cases/{ids['review']}/approve", json={"by": "Test User"})
        assert ok.status_code == 200
        # the Access tab can be set up before sign-in is switched on
        change = {"email": AM, "customer": "lidl", "level": "view"}
        assert client.post("/api/access", json={"changes": [change]}).status_code == 422
        grid = client.post("/api/access", json={"changes": [change], "by": "Setup"}).json()
        assert grid["people"][0]["access"]["lidl"]["granted_by"] == "Setup"
        login = client.get("/auth/login", follow_redirects=False)
        assert login.status_code == 303 and login.headers["location"] == "/app/"


def test_sign_in_without_an_admin_is_refused(signin: Settings) -> None:
    with pytest.raises(ValueError, match="FP_BOARD_ADMINS"):
        create_app(signin.model_copy(update={"board_admins": []}))


# ------------------------------------------------------------------ signing in


def test_nothing_is_shown_before_signing_in(board) -> None:  # type: ignore[no-untyped-def]
    client, ids = board
    assert client.get("/app/").status_code == 200  # the page itself holds no data
    assert client.get("/health").status_code == 200
    for path in [
        "/api/me",
        "/api/booking/cases",
        "/api/booking/overview",
        f"/api/booking/cases/{ids['lidl']}",
        "/api/access",
        "/facilities?name=koch",
        "/review",
        "/digest",
    ]:
        assert client.get(path).status_code == 401, path
    # a cookie that was changed, signed with another key, or ran out counts for nothing
    good = _cookie(ADMIN)
    payload, mac = good.split(".")
    forged = base64.urlsafe_b64encode(json.dumps({"e": ADMIN, "exp": 9e9}).encode()).decode()
    for bad in [
        f"{forged.rstrip('=')}.{mac}",
        sign(b"another key", {"e": ADMIN, "exp": 9e9}),
        _cookie(ADMIN, hours=-1),
        _cookie("admin@other.example"),  # a domain the board does not take (any more)
        payload,
    ]:
        client.cookies.set(SESSION_COOKIE, bad)
        assert client.get("/api/me").status_code == 401


def _id_token(**claims: Any) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256'})}.{part(claims)}.signature"


def _claims(nonce: str, /, **changes: Any) -> dict[str, Any]:
    claims = {
        "iss": "https://accounts.google.com",
        "aud": CLIENT_ID,
        "exp": (NOW + timedelta(hours=1)).timestamp(),
        "nonce": nonce,
        "email": "Am.One@circledelivers.com",
        "email_verified": True,
        "hd": "circledelivers.com",
        "name": "Am One",
    }
    claims.update(changes)
    return {k: v for k, v in claims.items() if v is not None}


def _start(client: TestClient, next_: str = "/app/#appointments") -> tuple[str, str]:
    response = client.get("/auth/login", params={"next": next_}, follow_redirects=False)
    assert response.status_code == 303
    target = urlsplit(response.headers["location"])
    assert target.netloc == "accounts.google.com"
    query = {k: v[0] for k, v in parse_qs(target.query).items()}
    assert query["client_id"] == CLIENT_ID and query["hd"] == "circledelivers.com"
    assert query["redirect_uri"] == "http://testserver/auth/callback"
    assert query["scope"] == "openid email profile" and query["response_type"] == "code"
    return query["state"], query["nonce"]


def test_signing_in_with_google_end_to_end(board) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    state, nonce = _start(client)
    with respx.mock(assert_all_called=True) as mock:
        token = mock.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(200, json={"id_token": _id_token(**_claims(nonce))})
        )
        back = client.get(
            "/auth/callback", params={"code": "code-1", "state": state}, follow_redirects=False
        )
    assert back.status_code == 303 and back.headers["location"] == "/app/#appointments"
    sent = parse_qs(token.calls.last.request.content.decode())
    assert sent["code"] == ["code-1"] and sent["client_secret"] == ["client-secret-for-tests"]
    assert sent["redirect_uri"] == ["http://testserver/auth/callback"]
    cookie = back.cookies.get(SESSION_COOKIE)
    assert unsign(KEY, cookie, now=NOW) == {
        "e": "am.one@circledelivers.com",
        "n": "Am One",
        "exp": pytest.approx((NOW + timedelta(hours=8)).timestamp()),
    }
    set_cookie = back.headers["set-cookie"].lower()
    assert "httponly" in set_cookie and "samesite=lax" in set_cookie
    me = client.get("/api/me").json()
    assert me["signed_in"] is True and me["email"] == "am.one@circledelivers.com"
    assert me["admin"] is False and me["customers"] == [] and me["admins"] == [ADMIN]
    assert client.get("/api/booking/cases").json() == []  # signed in, but given nothing yet
    out = client.post("/auth/logout", follow_redirects=False)
    assert out.status_code == 303 and SESSION_COOKIE in out.headers["set-cookie"]
    client.cookies.clear()
    assert client.get("/api/me").status_code == 401
    assert "signed out" in client.get("/auth/signed-out").text


@pytest.mark.parametrize(
    ("changes", "says"),
    [
        ({"email": "someone@gmail.com", "hd": None}, "company account"),
        ({"hd": None}, "company account"),  # a personal Google account made with a work address
        ({"hd": "other.example"}, "company account"),
        ({"email_verified": False}, "did not confirm an email"),
        ({"aud": "another-client"}, "not meant for this board"),
        ({"iss": "https://evil.example"}, "not meant for this board"),
        ({"nonce": "replayed"}, "did not match"),
        ({"exp": (NOW - timedelta(minutes=1)).timestamp()}, "expired"),
    ],
)
def test_google_accounts_the_board_turns_away(board, changes, says) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    state, nonce = _start(client)
    with respx.mock() as mock:
        mock.post(GOOGLE_TOKEN_URL).mock(
            return_value=httpx.Response(
                200, json={"id_token": _id_token(**_claims(nonce, **changes))}
            )
        )
        back = client.get("/auth/callback", params={"code": "c", "state": state})
    assert back.status_code == 403 and says in back.text
    assert SESSION_COOKIE not in back.headers.get("set-cookie", "")


def test_a_sign_in_that_did_not_start_here_or_failed_at_google(board) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    _state, _nonce = _start(client)
    assert client.get("/auth/callback", params={"code": "c", "state": "forged"}).status_code == 400
    assert client.get("/auth/callback", params={"error": "access_denied"}).status_code == 400
    state, _nonce = _start(client)
    with respx.mock() as mock:
        mock.post(GOOGLE_TOKEN_URL).mock(return_value=httpx.Response(400, json={"error": "bad"}))
        refused = client.get("/auth/callback", params={"code": "c", "state": state})
    assert refused.status_code == 403 and "did not accept" in refused.text
    client.cookies.clear()
    assert client.get("/auth/callback", params={"code": "c", "state": state}).status_code == 400


def test_after_signing_in_only_this_server_is_a_destination() -> None:
    assert safe_next("/app/#week") == "/app/#week"
    for bad in ["https://evil.example/", "//evil.example", "/\\evil.example", "", None]:
        assert safe_next(bad) == "/app/"


def test_claims_are_checked_on_their_own(signin: Settings) -> None:
    identity = identity_from_claims(_claims("n"), signin, nonce="n", now=NOW)
    assert identity.email == "am.one@circledelivers.com" and identity.name == "Am One"
    listed = _claims("n", aud=[CLIENT_ID, "other"])
    assert identity_from_claims(listed, signin, nonce="n", now=NOW).email == identity.email
    with pytest.raises(SignInError):
        identity_from_claims(_claims("n", email=None), signin, nonce="n", now=NOW)


# ------------------------------------------------------------------ who sees what


def test_an_account_manager_sees_only_the_customers_given_to_them(board) -> None:  # type: ignore[no-untyped-def]
    client, ids = board
    assert _grant(client, {"email": AM, "customer": "lidl", "level": "view"}).status_code == 200
    _as(client, AM, "Am One")
    cases = client.get("/api/booking/cases").json()
    assert sorted(c["id"] for c in cases) == [ids["lidl"], ids["review"]]
    assert client.get("/api/booking/customers").json() == ["Lidl - Inbound"]
    assert client.get("/api/booking/overview").json()["counts"]["total"] == 2
    today = client.get("/api/booking/today").json()
    assert "Northline" not in today["text"] and "Other Co" not in today["text"]
    # another customer's case, or nobody's, is not there at all
    assert client.get(f"/api/booking/cases/{ids['north']}").status_code == 404
    assert client.get(f"/api/booking/cases/{ids['other']}").status_code == 404
    north_approve = client.post(f"/api/booking/cases/{ids['north']}/approve", json={})
    assert north_approve.status_code == 404
    # their own customer, view only: shown, and nothing can be decided
    detail = client.get(f"/api/booking/cases/{ids['lidl']}").json()
    assert detail["can_act"] is False
    assert detail["desk_history"][0]["desk"] == "cci@udfinc.example"
    assert detail["desk_history"][0]["last_case_id"] is None  # Northline's case stays hidden
    approve = client.post(f"/api/booking/cases/{ids['review']}/approve", json={"by": "x"})
    assert approve.status_code == 403 and "ask an admin" in approve.json()["detail"]
    cancel = {"reason": "not needed"}
    assert client.post(f"/api/booking/cases/{ids['lidl']}/cancel", json=cancel).status_code == 403
    # admin pages
    for path in [
        "/api/access",
        "/api/access/history",
        "/facilities?name=koch",
        "/review",
        "/digest",
    ]:
        assert client.get(path).status_code == 403, path
    assert client.post("/api/access/people", json={"email": AM2}).status_code == 403
    me = client.get("/api/me").json()
    assert me["customers"] == [{"key": "lidl", "name": "Lidl", "level": "view"}]


def test_act_access_lets_them_decide_and_records_their_name(board) -> None:  # type: ignore[no-untyped-def]
    client, ids = board
    _grant(client, {"email": AM, "customer": "lidl", "level": "act"})
    _as(client, AM, "Am One")
    detail = client.get(f"/api/booking/cases/{ids['review']}").json()
    assert detail["can_act"] is True
    done = client.post(f"/api/booking/cases/{ids['review']}/approve", json={"by": "Someone Else"})
    assert done.status_code == 200 and done.json()["status"] == "scheduled"
    actors = {t["actor"] for t in done.json()["timeline"] if t["actor"]}
    assert "Am One" in actors and "Someone Else" not in actors


def test_an_admin_sees_every_customer(board) -> None:  # type: ignore[no-untyped-def]
    client, ids = board
    _as(client, ADMIN, "The Admin")
    assert len(client.get("/api/booking/cases").json()) == 4
    detail = client.get(f"/api/booking/cases/{ids['lidl']}").json()
    assert detail["can_act"] is True and detail["desk_history"][0]["last_case_id"] == ids["north"]
    me = client.get("/api/me").json()
    assert me["admin"] is True and {c["key"] for c in me["customers"]} == {"lidl", "northline"}
    assert client.get("/review").status_code == 200


# ------------------------------------------------------------------ the Access tab


def test_access_changes_are_kept_with_the_admin_who_made_them(board) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    _as(client, ADMIN, "The Admin")
    added = client.post(
        "/api/access/people", json={"email": " AM.Two@circledelivers.com ", "by": "x"}
    )
    assert added.status_code == 200
    assert [p["email"] for p in added.json()["people"]] == [AM2]
    # one bad change in a list: none of it is applied
    bad = client.post(
        "/api/access",
        json={
            "changes": [
                {"email": AM2, "customer": "lidl", "level": "act"},
                {"email": AM2, "customer": "acme", "level": "view"},
            ]
        },
    )
    assert bad.status_code == 422 and "no customer file 'acme'" in bad.json()["detail"]
    assert client.get("/api/access").json()["people"][0]["access"] == {}
    grid = client.post(
        "/api/access",
        json={
            "changes": [
                {"email": AM2, "customer": "lidl", "level": "act", "note": "covers a colleague"},
                {"email": AM2, "customer": "northline", "level": "view", "until": "2026-12-31"},
            ],
            "by": "Ignored When Signed In",
        },
    ).json()
    held = grid["people"][0]["access"]
    assert held["lidl"]["level"] == "act" and held["lidl"]["granted_by"] == "The Admin"
    assert held["northline"] == {
        "level": "view",
        "until": "2026-12-31",
        "note": None,
        "granted_by": "The Admin",
        "granted_at": NOW.isoformat(),
        "ended": False,
    }
    client.post(
        "/api/access", json={"changes": [{"email": AM2, "customer": "lidl", "level": "view"}]}
    )
    client.post(
        "/api/access", json={"changes": [{"email": AM2, "customer": "lidl", "level": "none"}]}
    )
    removed = client.post(f"/api/access/people/{AM2}/remove", json={})
    assert removed.status_code == 200 and removed.json()["people"] == []
    lines = client.get("/api/access/history").json()
    assert [(c["action"], c["customer_key"], c["level"]) for c in reversed(lines)] == [
        ("add", None, None),
        ("grant", "lidl", "act"),
        ("grant", "northline", "view"),
        ("change", "lidl", "view"),
        ("revoke", "lidl", None),
        ("revoke", "northline", None),
        ("remove", None, None),
    ]
    assert {c["by"] for c in lines} == {"The Admin"}
    assert client.post(f"/api/access/people/{AM2}/remove", json={}).status_code == 404


@pytest.mark.parametrize(
    ("change", "says"),
    [
        ({"email": "someone@gmail.com", "customer": "lidl", "level": "view"}, "only accounts on"),
        ({"email": "not an email", "customer": "lidl", "level": "view"}, "not an email"),
        ({"email": ADMIN, "customer": "lidl", "level": "view"}, "is an admin"),
        ({"email": AM, "customer": "lidl", "level": "view", "until": "2026-09-28"}, "has passed"),
    ],
)
def test_changes_the_access_tab_refuses(board, change, says) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    refused = _grant(client, change)
    assert refused.status_code == 422 and says in refused.json()["detail"]


def test_access_ends_after_its_last_day(signin: Settings) -> None:
    engine = make_engine(signin.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with session_scope(sessions) as s:
        # 2026-09-29 12:00 UTC is the 29th in Fort Wayne; access until the 30th holds through it
        set_access(
            s,
            signin,
            AM,
            "lidl",
            AccessLevel.VIEW,
            by="Admin",
            until=NOW.date() + timedelta(days=1),
            now=NOW,
        )
    with session_scope(sessions) as s:
        assert viewer_for(s, signin, AM, None, now=NOW).sees("lidl")
        late = NOW + timedelta(days=1, hours=5)  # 30th, 13:00 in Fort Wayne
        assert viewer_for(s, signin, AM, None, now=late).sees("lidl")
        after = NOW + timedelta(days=2)
        assert not viewer_for(s, signin, AM, None, now=after).sees("lidl")
    engine.dispose()


def test_a_viewer_who_can_act_on_one_customer_only() -> None:
    v = Viewer(
        email=AM,
        name=None,
        admin=False,
        levels={"lidl": AccessLevel.ACT, "northline": AccessLevel.VIEW},
    )
    assert v.sees("lidl") and v.acts("lidl")
    assert v.sees("northline") and not v.acts("northline")
    assert not v.sees("default") and not v.acts("acme")
    assert v.label == AM


def test_changes_from_another_site_are_refused(board) -> None:  # type: ignore[no-untyped-def]
    client, _ids = board
    _as(client, ADMIN)
    change = {"changes": [{"email": AM, "customer": "lidl", "level": "view"}]}
    forged = client.post("/api/access", json=change, headers={"Origin": "https://evil.example"})
    assert forged.status_code == 403
    cross = client.post("/api/access", json=change, headers={"Sec-Fetch-Site": "cross-site"})
    assert cross.status_code == 403
    same = client.post("/api/access", json=change, headers={"Origin": "http://testserver"})
    assert same.status_code == 200


def test_the_cookie_key_is_made_once_and_kept_in_the_store(signin: Settings) -> None:
    unset = signin.model_copy(update={"board_session_secret": None})
    first = create_app(unset).state.session_secret
    again = create_app(unset).state.session_secret
    assert first == again and len(first) >= 32 and first != KEY


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    folder = tmp_path / "customers"
    folder.mkdir()
    (folder / "northline.toml").write_text(NORTHLINE, encoding="utf-8")
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.setenv("FP_CUSTOMERS_DIR", str(folder))
    monkeypatch.setenv("FP_BOARD_ADMINS", "Admin@CircleDelivers.com")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield tmp_path
    get_settings.cache_clear()


def test_access_from_the_command_line(cli_env: Path) -> None:
    runner = CliRunner()

    def run(*args: str) -> str:
        result = runner.invoke(cli_app, ["access", *args])
        assert result.exit_code == 0, result.output
        return result.output

    assert "admin@circledelivers.com" in run("list") and "every customer" in run("list")
    assert "grant: am.one@circledelivers.com act on lidl" in run(
        "grant", "Am.One@circledelivers.com", "lidl", "--act", "--by", "The Admin"
    )
    assert "change: am.one@circledelivers.com view on lidl" in run(
        "grant", AM, "lidl", "--view", "--until", "2099-01-31", "--by", "The Admin"
    )
    assert "nothing to change" in run("grant", AM, "lidl", "--until", "2099-01-31", "--by", "H")
    run("grant", AM, "northline", "--by", "The Admin")
    listed = run("list")
    assert "Lidl view until 2099-01-31" in listed and "Northline Grocers view" in listed
    assert "revoked: am.one@circledelivers.com on lidl" in run("revoke", AM, "lidl", "--by", "H")
    assert "nothing to revoke" in run("revoke", AM, "lidl", "--by", "H")
    assert "1 customer taken back" in run("remove", AM, "--by", "The Admin")
    history = run("history", "--email", AM)
    assert "The Admin: remove am.one@circledelivers.com" in history
    assert "The Admin: grant am.one@circledelivers.com lidl act" in history
    bad = runner.invoke(cli_app, ["access", "grant", "x@gmail.com", "lidl", "--by", "H"])
    assert bad.exit_code == 1 and "only accounts on circledelivers.com" in bad.output


def test_serve_says_whether_people_sign_in(cli_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: None)
    runner = CliRunner()
    plain = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--port", "8124"])
    assert plain.exit_code == 0 and "anyone who can reach this address" in plain.output
    monkeypatch.setenv("FP_GOOGLE_CLIENT_ID", CLIENT_ID)
    monkeypatch.setenv("FP_GOOGLE_CLIENT_SECRET", "client-secret-for-tests")
    get_settings.cache_clear()
    on = runner.invoke(cli_app, ["serve", "--port", "8124"])
    assert on.exit_code == 0, on.output
    assert "Google sign-in on; admins admin@circledelivers.com" in on.output
    assert "http://localhost:8124/auth/callback" in on.output
    # for testing before Google knows the address: off for one run, on this machine only
    off = runner.invoke(cli_app, ["serve", "--port", "8124", "--no-sign-in"])
    assert off.exit_code == 0, off.output
    assert "sign-in off for this run" in off.output and "Google sign-in on" not in off.output
    outside = runner.invoke(cli_app, ["serve", "--host", "0.0.0.0", "--no-sign-in"])
    assert outside.exit_code == 2 and "this machine only" in outside.output
    monkeypatch.setenv("FP_BOARD_ADMINS", "")
    get_settings.cache_clear()
    nobody = runner.invoke(cli_app, ["serve", "--port", "8124"])
    assert nobody.exit_code == 2 and "FP_BOARD_ADMINS" in nobody.output
