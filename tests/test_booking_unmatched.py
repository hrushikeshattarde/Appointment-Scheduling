"""Booking mail no pickup matched: kept for a person, linked by them or by a later pass, never lost."""

from __future__ import annotations

import re
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus, UnmatchedMail
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases, scan
from facility_profiles.booking.unmatched import (
    dismiss_unmatched,
    link_unmatched,
    open_unmatched,
)
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "115802102660"
INTERNAL = ["circledelivers.com"]
UNRELATED = FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED))


def stray(
    mid: str = "u1", body: str = "Is your truck still coming at 9 tomorrow?"
) -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        thread_id=None,
        sent_at=NOW + timedelta(hours=3),
        from_addr="Ridgeline Dispatch <dispatch@ridgeline-trucking.example>",
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="Pickup appointment tomorrow?",
        body=body,
        rfc_message_id=f"<{mid}@gmail.example>",
    )


def _drafted(settings: Settings, sessions) -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(7001, po=PO)], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        draft_case(s, case, RecordingMailer(), settings, now=NOW)
        case.status = CaseStatus.PENDING.value  # a person sent it
        return case.id


@pytest.fixture
def lidl(settings: Settings) -> Settings:
    return settings.model_copy(update={"pilot_terminal_ids": [1089]})


def test_kept_mail_is_linked_by_a_person_and_read_as_that_pickups_reply(lidl, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id = _drafted(lidl, sessions)
    with session_scope(sessions) as s:
        stats = ingest(s, [stray()], UNRELATED, internal_domains=INTERNAL, settings=lidl)
        assert (stats.unmatched, stats.unmatched_kept) == (1, 1)
        [item] = open_unmatched(s)
        case = s.get(BookingCase, case_id)
        assert case is not None
        yes = ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date="2026-10-01",
            pickup_time="09:00",
            quotes=["coming at 9 tomorrow"],
        )
        link_unmatched(
            s, item, case, by="megan", classifier=FakeReplyClassifier(lambda _c: yes), settings=lidl
        )
        assert (item.status, item.case_id, item.resolved_by) == ("linked", case_id, "megan")
        assert open_kinds(case) == ["confirmation_review"]
        assert "mail_linked" in [e.action for e in case.events]
        assert open_unmatched(s) == []


def test_without_a_reader_a_linked_email_is_put_on_the_pickup_for_a_person(lidl, sessions) -> None:  # type: ignore[no-untyped-def]
    case_id = _drafted(lidl, sessions)
    with session_scope(sessions) as s:
        ingest(s, [stray()], UNRELATED, internal_domains=INTERNAL, settings=lidl)
        [item] = open_unmatched(s)
        case = s.get(BookingCase, case_id)
        assert case is not None
        link_unmatched(s, item, case, by="megan", classifier=None, settings=lidl)
        assert open_kinds(case) == ["handoff"]
        assert case.messages[-1].body == "Is your truck still coming at 9 tomorrow?"
        with pytest.raises(ValueError, match="linked already"):
            dismiss_unmatched(s, item, by="megan", note="twice")


def test_kept_mail_that_finds_its_pickup_later_is_linked_by_the_agent(lidl, sessions) -> None:  # type: ignore[no-untyped-def]
    early = stray("u2", body=f"Can you confirm the pickup for PO {PO}? Waiting on you.")
    with session_scope(sessions) as s:
        ingest(s, [early], UNRELATED, internal_domains=INTERNAL, settings=lidl)
        assert len(open_unmatched(s)) == 1  # no case yet: the scan has not run
    case_id = _drafted(lidl, sessions)
    question = ReplyClassification(
        status=ReplyStatus.QUESTION, question="confirm the pickup?", quotes=["confirm the pickup"]
    )
    with session_scope(sessions) as s:
        ingest(
            s,
            [early],
            FakeReplyClassifier(lambda _c: question),
            internal_domains=INTERNAL,
            responder=Responder(lidl, RecordingMailer(), now=NOW),
            settings=lidl,
        )
        item = s.query(UnmatchedMail).one()
        assert (item.status, item.case_id, item.resolved_by) == ("linked", case_id, "agent")
        case = s.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["facility_question"]


def test_the_board_lists_links_and_dismisses_kept_mail(settings: Settings, tmp_path: Path) -> None:
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    on = settings.model_copy(update={"pilot_terminal_ids": [1089], "database_url": url})
    sessions = session_factory(engine)
    case_id = _drafted(on, sessions)
    with session_scope(sessions) as s:
        ingest(
            s,
            [stray("m1"), stray("m2", "Second note")],
            UNRELATED,
            internal_domains=INTERNAL,
            settings=on,
        )
    engine.dispose()
    application = create_app(on)
    application.state.clock = lambda: NOW
    with TestClient(application) as client:
        overview = client.get("/api/booking/overview").json()
        assert overview["counts"]["unmatched_mail"] == 2
        assert [m["subject"] for m in overview["mail"]] == ["Pickup appointment tomorrow?"] * 2
        first, second = (m["id"] for m in client.get("/api/booking/mail").json())
        linked = client.post(
            f"/api/booking/mail/{first}/link", json={"by": "megan", "case_id": case_id}
        )
        assert linked.status_code == 200, linked.text
        assert linked.json()["id"] == case_id and "mail_linked" in [
            r["action"] for r in linked.json()["timeline"]
        ]
        gone = client.post(
            f"/api/booking/mail/{second}/dismiss", json={"by": "megan", "note": "spam"}
        )
        assert gone.status_code == 200 and gone.json()["status"] == "dismissed"
        again = client.post(
            f"/api/booking/mail/{second}/dismiss", json={"by": "megan", "note": "x"}
        )
        assert again.status_code == 409
        assert client.get("/api/booking/mail").json() == []
        assert (
            client.post("/api/booking/mail/999/dismiss", json={"by": "m", "note": "x"}).status_code
            == 404
        )


def test_the_command_line_lists_and_dismisses_kept_mail(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{(tmp_path / 'cli.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    on = settings.model_copy(update={"pilot_terminal_ids": [1089], "database_url": url})
    with session_scope(session_factory(engine)) as s:
        ingest(s, [stray()], UNRELATED, internal_domains=INTERNAL, settings=on)
    engine.dispose()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.setenv("NO_COLOR", "1")
    get_settings.cache_clear()
    try:
        runner = CliRunner()
        listed = runner.invoke(app, ["booking", "unmatched"])
        assert listed.exit_code == 0, listed.output
        assert re.search(r"mail 1\s+open\s+09/29 11:00 ET", listed.output)
        assert "Is your truck still coming at 9 tomorrow?" in listed.output
        done = runner.invoke(
            app, ["booking", "dismiss-mail", "1", "--by", "megan", "--note", "spam"]
        )
        assert done.exit_code == 0 and "mail 1 dismissed" in done.output
        assert "no unmatched mail" in runner.invoke(app, ["booking", "unmatched"]).output
        assert "dismissed" in runner.invoke(app, ["booking", "unmatched", "--all"]).output
    finally:
        get_settings.cache_clear()
