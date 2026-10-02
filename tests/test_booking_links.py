"""Click-to-confirm: the times a request offers, the signed links, the vendor's page and click."""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr, ValidationError
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.api.app import create_app
from facility_profiles.api.links import create_links_app
from facility_profiles.booking.links import (
    LinkError,
    confirm,
    offer_state,
    offer_token,
    offer_url,
    offered_slots,
    propose,
    read_token,
)
from facility_profiles.booking.mail import RecordingMailer, build_mime
from facility_profiles.booking.models import BookingCase, CaseStatus, SlotOffer
from facility_profiles.booking.service import (
    close_case,
    draft_batch,
    draft_case,
    list_cases,
    mark_sent,
    reschedule_case,
    scan,
)
from facility_profiles.booking.templates import TemplateKind, save_template
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

BASE = "https://book.circle.example"
SLOTS = ["2026-10-01 08:00", "2026-10-01 09:00", "2026-10-01 10:00", "2026-10-01 11:00"]


def linked(settings: Settings, **more: Any) -> Settings:
    """Links on: a public address and a signing key."""
    return settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "booking_link_base_url": BASE,
            "booking_link_secret": SecretStr("test-only-signing-key"),
            **more,
        }
    )


def drafted(
    settings: Settings, sessions: sessionmaker[Session], *, sent: bool = True
) -> tuple[int, RecordingMailer]:
    """One Lidl pickup at Koch Foods (asked for Thu 10/01 09:00), drafted and sent."""
    seed_vendor(sessions)
    scan(
        FakeTPro([lidl_load(2001, po="226321092660")], {}),
        sessions,
        settings,
        days_ahead=7,
        now=NOW,
    )  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, settings, now=NOW)
        if sent:
            mark_sent(session, case, by="pod", thread_id="t1")
        return case.id, mailer


def the_offer(session: Session, case_id: int) -> SlotOffer:
    case = session.get(BookingCase, case_id)
    assert case is not None and case.offers
    return case.offers[-1]


# ------------------------------------------------------------------ the email


def test_with_links_off_the_request_is_written_as_before(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, mailer = drafted(settings.model_copy(update={"pilot_terminal_ids": [1089]}), sessions)
    draft = mailer.drafts[0]
    assert draft.html is None and "http" not in draft.body
    assert draft.body.startswith(
        "Hello,\n\nCan I please schedule the following for Koch Foods going to Lidl?\n\n"
        "PO# 226321092660 on 10/01 @ 0900\n\nThank you!"
    )
    with session_scope(sessions) as session:
        assert session.get(BookingCase, case_id).offers == []  # type: ignore[union-attr]


def test_a_request_offers_times_around_the_one_asked_for(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id, mailer = drafted(linked(settings), sessions)
    draft = mailer.drafts[0]
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        url = offer_url(offer, linked(settings))
        assert offer.slots == SLOTS
        assert offer.message_id == session.get(BookingCase, case_id).messages[0].id  # type: ignore[union-attr]
        # Never later than the last time offered (11:00 New York), well inside 72 hours.
        assert offer.expires_at.replace(tzinfo=UTC) == datetime(2026, 10, 1, 15, 0, tzinfo=UTC)
    # The pod's wording, with one line added after the PO line.
    assert draft.body.startswith(
        "Hello,\n\nCan I please schedule the following for Koch Foods going to Lidl?\n\n"
        f"PO# 226321092660 on 10/01 @ 0900\n\nOr confirm a time with one click: {url}\n\n"
        "Thank you!"
    )
    assert url.startswith(f"{BASE}/c/") and len(url) < 80
    assert draft.html is not None
    for i, label in enumerate(["Thu 10/01 08:00", "Thu 10/01 09:00", "Thu 10/01 10:00"]):
        assert f'href="{url}?s={i}"' in draft.html and f">{label}</a>" in draft.html
    assert f'href="{url}#propose"' in draft.html
    mime = build_mime(draft, "lidl@circledelivers.com")
    assert mime.get_body(preferencelist=("html",)) is not None
    assert url in mime.get_body(preferencelist=("plain",)).get_content()  # type: ignore[union-attr]


def test_only_workable_times_are_offered(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(2001, po="226321092660")], {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert offered_slots(case, on, None, now=NOW) == SLOTS
        # Times inside our four-hour notice drop out: at 05:30 New York only 10:00 and 11:00
        # are left, at 07:30 none.
        early = datetime(2026, 10, 1, 9, 30, tzinfo=UTC)
        assert offered_slots(case, on, None, now=early) == ["2026-10-01 10:00", "2026-10-01 11:00"]
        assert offered_slots(case, on, None, now=datetime(2026, 10, 1, 11, 30, tzinfo=UTC)) == []
        late = datetime(2026, 10, 1, 9, 0, tzinfo=UTC) - timedelta(hours=1)  # 04:00 New York
        assert offered_slots(case, on, None, now=late) == [
            "2026-10-01 09:00",
            "2026-10-01 10:00",
            "2026-10-01 11:00",
        ]
        # A time that would miss the delivery is never offered.
        case.delivery_at_utc = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)  # 13.2 h after 09:00 ET
        assert offered_slots(case, on, None, now=NOW) == ["2026-10-01 08:00", "2026-10-01 09:00"]


def test_a_desk_given_dates_is_offered_days(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    key = seed_vendor(sessions)
    with session_scope(sessions) as session:
        Repository(session).set_field_human(
            key, Role.SHIPPER, "appointment_required", value=False, state=FieldState.HUMAN_SET
        )
    load = lidl_load(2001, po="226321092660")
    load["waypoints"][1]["appointmentTime"]["open"] = "2026-10-06T13:30:00Z"  # delivery Tue 10/06
    scan(FakeTPro([load], {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, mailer, on, now=NOW)
        # Thu 10/01, then the next weekdays: Fri 10/02 and Mon 10/05.
        assert case.offers[0].slots == ["2026-10-01", "2026-10-02", "2026-10-05"]
    assert ">Fri 10/02</a>" in (mailer.drafts[0].html or "")


def test_one_email_per_desk_carries_a_link_per_po(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    seed_vendor(sessions)
    loads = [lidl_load(2001, po="226321092660"), lidl_load(2002, po="226321092661")]
    scan(FakeTPro(loads, {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        cases = list_cases(session)
        draft_batch(session, cases, mailer, on, now=NOW)
        urls = {c.po_numbers[0]: offer_url(c.offers[0], on) for c in cases}
        assert all(c.offers[0].message_id == c.messages[0].id for c in cases)
    [draft] = mailer.drafts
    assert "\n\nOr confirm a time with one click:\nPO# " in draft.body
    for po, url in urls.items():
        assert f"\nPO# {po}: {url}" in draft.body
    assert draft.html is not None and draft.html.count("?s=0") == 2


def test_a_saved_template_gets_the_links_after_its_po_lines(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    with session_scope(sessions) as session:
        save_template(
            session,
            TemplateKind.REQUEST,
            body="Hi,\n{lines}\nThanks,\n{signature}",
            by="pod",
            customer="lidl",
        )
    case_id, mailer = drafted(on, sessions)
    with session_scope(sessions) as session:
        url = offer_url(the_offer(session, case_id), on)
    assert (
        f"PO# 226321092660 on 10/01 @ 0900\n\nOr confirm a time with one click: {url}\nThanks,"
        in (mailer.drafts[0].body)
    )


# ------------------------------------------------------------------ the links


def test_links_are_signed_and_expire(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        token = offer_token(offer, on)
        assert read_token(session, token, on).id == offer.id
        oid, exp, sig = token.split(".")
        for forged in (
            f"{int(oid) + 1}.{exp}.{sig}",  # another offer
            f"{oid}.{exp}x.{sig}",  # a later expiry
            f"{oid}.{exp}.{sig[:-1]}A",  # a guessed signature
            "not-a-token",
        ):
            with pytest.raises(LinkError):
                read_token(session, forged, on)
        other = on.model_copy(update={"booking_link_secret": SecretStr("another key")})
        with pytest.raises(LinkError):
            read_token(session, token, other)
        assert offer_state(offer, NOW) == "open"
        assert offer_state(offer, datetime(2026, 10, 1, 15, 0, tzinfo=UTC)) == "expired"


def test_the_link_address_must_be_https() -> None:
    def build(url: str) -> Settings:
        return Settings(
            _env_file=None,  # type: ignore[call-arg]
            TPRO_BASE_URL="https://tpro.test",
            TPRO_USERNAME="u",
            TPRO_PASSWORD="p",
            booking_link_base_url=url,
        )

    assert build("https://book.example/").booking_link_base_url == "https://book.example"
    assert build("http://127.0.0.1:8010").booking_link_base_url == "http://127.0.0.1:8010"
    with pytest.raises(ValidationError, match="https"):
        build("http://book.example")


# ------------------------------------------------------------------ the vendor's answer


def test_a_picked_time_is_booked_with_the_vendors_pickup_number(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, _ = drafted(on, sessions)
    clicked = NOW + timedelta(hours=1)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        said = confirm(
            session, offer, 2, settings=on, now=clicked, pickup_number=" 20463798 ", name="Shannon"
        )
        case = offer.case
        assert said.ok and said.say == "Thank you, the pickup is set for Thu 10/01 10:00."
        assert case.status == CaseStatus.SCHEDULED.value and open_kinds(case) == []
        assert case.confirmed_local == "2026-10-01 10:00"
        assert case.confirmed_start_utc.replace(tzinfo=UTC) == datetime(
            2026, 10, 1, 14, 0, tzinfo=UTC
        )  # type: ignore[union-attr]
        assert case.pickup_number == "20463798"
        assert case.reason == "vendor picked Thu 10/01 10:00 from the link"
        pu = next(r for r in case.references if r.kind == "pickup_number")
        assert pu.source == "vendor" and pu.message_id == case.messages[-1].id
        answer = case.messages[-1]
        assert answer.direction == "in" and answer.kind == "link"
        assert answer.body == "Picked Thu 10/01 10:00 from the link, PU# 20463798 (Shannon)"
        actions = [e.action for e in case.events]
        tail = actions[actions.index("vendor_confirmed") :]
        assert tail == ["vendor_confirmed", "confirmed_by_link", "approved", "desk_remembered"]
        assert offer_state(offer, clicked) == "answered"
        # A second click changes nothing and says what was booked.
        again = confirm(session, offer, 0, settings=on, now=clicked)
        assert not again.ok and "set for Thu 10/01 10:00" in again.say
        assert case.confirmed_local == "2026-10-01 10:00"


def test_with_auto_schedule_off_a_picked_time_waits_for_approval(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings, booking_link_auto_schedule=False)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        said = confirm(session, offer, 1, settings=on, now=NOW)
        assert said.ok and "will confirm it shortly" in said.say
        assert offer.case.status == CaseStatus.PENDING.value
        assert open_kinds(offer.case) == ["confirmation_review"]


def test_a_draft_nobody_marked_sent_is_sent_once_the_vendor_clicks(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, _ = drafted(on, sessions, sent=False)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        assert offer.case.status == CaseStatus.UNSCHEDULED.value
        assert confirm(session, offer, 1, settings=on, now=NOW).ok
        assert offer.case.status == CaseStatus.SCHEDULED.value
        assert offer.case.messages[0].sent_at is not None
        assert "sent" in [e.action for e in offer.case.events]


def test_a_time_too_close_by_the_click_is_refused(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        late = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)  # 06:00 New York: 08:00 is inside 4 h
        said = confirm(session, offer, 0, settings=on, now=late)
        assert not said.ok and "too close" in said.say
        assert offer.case.status == CaseStatus.PENDING.value and offer.answered_at is None
        assert (
            confirm(session, offer, 9, settings=on, now=NOW).say
            == "Please choose one of the times listed."
        )


def test_a_proposed_time_goes_to_a_person(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        assert not propose(session, offer, settings=on, now=NOW, day="10/02").ok
        assert not propose(session, offer, settings=on, now=NOW, day="2026-09-28").ok
        said = propose(
            session,
            offer,
            settings=on,
            now=NOW,
            day="2026-10-01",
            clock="14:00",
            note="dock 4 is free then",
        )
        case = offer.case
        assert said.ok and offer.answer == "proposed"
        assert case.status == CaseStatus.PENDING.value
        assert open_kinds(case) == ["proposed_time_review"]
        assert case.open_exceptions[0].detail["time"] == "14:00"
        event = case.events[-1]
        assert event.action == "proposed_by_link" and event.detail["feasible"] is True
        assert (
            case.messages[-1].body == "Proposed Thu 10/01 14:00 from the link: dock 4 is free then"
        )


def test_a_later_request_or_a_decision_closes_the_link(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings)
    case_id, mailer = drafted(on, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        first = case.offers[0]
        reschedule_case(
            session, case, mailer, on, requested_local="2026-10-01 13:00", by="pod", now=NOW
        )
        second = case.offers[-1]
        assert offer_state(first, NOW) == "superseded" and offer_state(second, NOW) == "open"
        assert second.slots == [
            "2026-10-01 12:00",
            "2026-10-01 13:00",
            "2026-10-01 14:00",
            "2026-10-01 15:00",
        ]
        assert offer_url(second, on) in mailer.drafts[-1].body
        assert "newer email" in confirm(session, first, 0, settings=on, now=NOW).say
        close_case(session, case, by="pod", reason="load canceled")
        assert offer_state(second, NOW) == "closed"
        assert not confirm(session, second, 0, settings=on, now=NOW).ok


def test_a_click_counts_as_the_vendors_answer_for_the_timers(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = linked(settings, booking_link_auto_schedule=False)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        offer = the_offer(session, case_id)
        confirm(session, offer, 1, settings=on, now=NOW + timedelta(hours=2))
        result = sweep(session, now=NOW + timedelta(hours=30))
        assert not [r for r in result.raised if r[0] == case_id]
        assert open_kinds(offer.case) == ["confirmation_review"]


# ------------------------------------------------------------------ the page


@pytest.fixture
def page(
    settings: Settings, tmp_path: Path
) -> Iterator[tuple[TestClient, str, int, sessionmaker[Session], Settings]]:
    """The public app on a store with one drafted, sent request; its link token."""
    url = f"sqlite:///{(tmp_path / 'links.db').as_posix()}"
    on = linked(settings, database_url=url)
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    case_id, _ = drafted(on, sessions)
    with session_scope(sessions) as session:
        token = offer_token(the_offer(session, case_id), on)
    app = create_links_app(on)
    app.state.clock = lambda: NOW
    with TestClient(app) as client:
        yield client, token, case_id, sessions, on
    engine.dispose()


def test_opening_the_page_changes_nothing(page) -> None:  # type: ignore[no-untyped-def]
    client, token, case_id, sessions, _ = page
    for _ in range(3):  # a mail scanner, then the vendor, then again
        shown = client.get(f"/c/{token}?s=2")
        assert shown.status_code == 200
    text = shown.text
    assert (
        "Pickup appointment, PO# 226321092660" in text and "Koch Foods, Inc., Erlanger, KY" in text
    )
    assert 'value="2" checked' in text and "Thu 10/01 09:00 (the time we asked for)" in text
    assert (
        shown.headers["cache-control"] == "no-store" and "noindex" in shown.headers["x-robots-tag"]
    )
    assert "frame-ancestors 'none'" in shown.headers["content-security-policy"]
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert case.offers[0].answered_at is None and case.messages[-1].direction == "out"


def test_confirming_on_the_page_books_the_pickup(page) -> None:  # type: ignore[no-untyped-def]
    client, token, case_id, sessions, _ = page
    done = client.post(
        f"/c/{token}",
        data={"do": "confirm", "slot": "3", "pickup_number": "PU-77", "name": "<b>Dana</b>"},
        follow_redirects=False,
    )
    assert done.status_code == 303 and done.headers["location"] == f"/c/{token}"
    after = client.get(f"/c/{token}")
    assert "Thank you, the pickup is set for Thu 10/01 11:00." in after.text
    assert "Confirm pickup time" not in after.text
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        assert case.pickup_number == "PU-77" and case.confirmed_local == "2026-10-01 11:00"
        assert case.messages[-1].body.endswith("(<b>Dana</b>)")  # kept as text, never markup


def test_the_page_says_why_a_click_did_not_count(page) -> None:  # type: ignore[no-untyped-def]
    client, token, _, _, _ = page
    refused = client.post(f"/c/{token}", data={"do": "confirm", "slot": "x"})
    assert refused.status_code == 200 and "Please choose one of the times listed." in refused.text
    proposed = client.post(
        f"/c/{token}",
        data={"do": "propose", "date": "2026-10-02", "time": "07:30"},
        follow_redirects=False,
    )
    assert proposed.status_code == 303
    assert "we have your proposed time" in client.get(f"/c/{token}").text


def test_only_the_vendor_pages_are_public(page) -> None:  # type: ignore[no-untyped-def]
    client, token, _, _, _ = page
    assert client.get("/c/1.abc.forgedsignature").status_code == 404
    assert client.get("/api/booking/overview").status_code == 404
    assert client.get("/app/").status_code == 404
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404
    assert client.get("/health").json() == {"status": "ok"}
    assert "Disallow: /" in client.get("/robots.txt").text
    assert client.post(f"/c/{token}", content=b"x=" + b"a" * 9000).status_code == 413


def test_the_board_shows_what_the_link_offered(page) -> None:  # type: ignore[no-untyped-def]
    client, token, case_id, _, on = page
    client.post(f"/c/{token}", data={"do": "confirm", "slot": "1"})
    board = create_app(on)
    board.state.clock = lambda: NOW
    with TestClient(board) as b:
        detail = b.get(f"/api/booking/cases/{case_id}").json()
        # The board app serves the vendor page too, for trying links locally.
        assert b.get(f"/c/{token}").status_code == 200
    [offer] = detail["offers"]
    assert offer["slots"][0] == "Thu 10/01 08:00" and offer["state"] == "answered"
    assert offer["answer"] == "Thu 10/01 09:00"
    titles = [row["title"] for row in detail["timeline"]]
    assert "Vendor picked a time from the link" in titles
    assert detail["messages"][-1]["kind"] == "link"
    assert re.search(r"vendor picked Thu 10/01 09:00 from the link", detail["stage"], re.I)
