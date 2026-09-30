"""Sending as the agent, and tying replies back to requests through RFC Message-IDs."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import inspect, text
from typer.testing import CliRunner

from facility_profiles.booking.classify import FakeReplyClassifier, ReplyClassification, ReplyStatus
from facility_profiles.booking.mail import (
    InboundMessage,
    LocalDraftMailer,
    OutboundDraft,
    RecordingMailer,
    RecordingSender,
    build_mime,
    message_ids,
)
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.outbox import SendRefusedError
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.service import (
    draft_batch,
    draft_case,
    ingest,
    link_outbound,
    list_cases,
    match_case,
    reschedule_case,
    scan,
)
from facility_profiles.cli import app
from facility_profiles.storage.db import ensure_columns, init_db, make_engine
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, reply, seed_vendor

DESK = "cci@udfinc.com"
INTERNAL = ["circledelivers.com"]


def _send_settings(settings):  # type: ignore[no-untyped-def]
    return settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_mode": "send", "booking_send_daily_cap": 5}
    )


def _scanned(settings, sessions, *pos: str) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    loads = [lidl_load(6000 + i, po=po) for i, po in enumerate(pos)]
    scan(FakeTPro(loads, {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]


def _confirmed(_ctx) -> ReplyClassification:  # type: ignore[no-untyped-def]
    return ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["SET!"], confidence=0.9)


# ------------------------------------------------------------------------------- MIME ----


def test_build_mime_stamps_a_message_id_and_threads_replies(tmp_path: Path) -> None:
    draft = OutboundDraft(
        to_addr=DESK,
        cc_addr="lidl@circledelivers.com",
        subject="Re: Pick Up Appointment: 226321092660",
        body="Thank you!",
        in_reply_to="<conf-1@outlook.com>",
        references="<req-1@mail.gmail.com> <conf-1@outlook.com>",
    )
    msg = build_mime(draft, "Lidl Appointments <lidl-appointments@circledelivers.com>")
    assert msg["Message-ID"].endswith("@circledelivers.com>")
    assert msg["In-Reply-To"] == "<conf-1@outlook.com>"
    assert msg["References"] == "<req-1@mail.gmail.com> <conf-1@outlook.com>"
    assert msg["Cc"] == "lidl@circledelivers.com" and msg.get_content().strip() == "Thank you!"
    assert message_ids("<A@x> <b@y>", "<a@x>") == ["<a@x>", "<b@y>"]

    mailer = LocalDraftMailer(tmp_path, sender="lidl@circledelivers.com")
    raw = Path(mailer.create_draft(draft)).read_text(encoding="utf-8")
    assert "Message-ID: <" in raw and "X-Facility-Profiles-Draft" in raw


# ------------------------------------------------------------------------------- sending ----


def test_send_moves_the_case_to_sent_with_the_ids_a_reply_will_carry(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    sender = RecordingSender()
    with_ids = None
    with sessions() as session:
        case = list_cases(session)[0]
        message = draft_case(session, case, sender, settings, by="megan")
        session.commit()
        assert case.status == CaseStatus.SENT.value
        assert message.sent_at is not None and message.message_id == "sent-1"
        assert message.rfc_message_id == sender.deliveries[0].rfc_message_id
        assert message.thread_id == "thread-1" and case.thread_id == "thread-1"
        assert message.draft_ref == "gmail:sent-1"
        actions = [(e.action, e.actor) for e in case.events]
        assert ("sent", "megan") in actions and ("drafted", "agent") in actions
        with_ids = message.rfc_message_id
    assert with_ids and sender.drafts[0].to_addr == DESK
    assert "PO# 226321092660 on 10/01 @ 0900" in sender.drafts[0].body


def test_send_gate_refuses_draft_mode_untrusted_desks_and_the_daily_cap(settings, sessions):
    draft_only = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    _scanned(draft_only, sessions, "226321092660", "226321092661", "226321092662")
    sender = RecordingSender()
    with sessions() as session:
        first, second, third = sorted(list_cases(session), key=lambda c: c.id)
        with pytest.raises(SendRefusedError, match="not 'send'"):
            draft_case(session, first, sender, draft_only)
        assert first.status == CaseStatus.NEW.value and not sender.drafts

    send = _send_settings(settings).model_copy(update={"booking_send_daily_cap": 1})
    with sessions() as session:
        first, second, third = sorted(list_cases(session), key=lambda c: c.id)
        second.contact_email = "someone-else@example.com"
        with pytest.raises(SendRefusedError, match="not the trusted desk"):
            draft_case(session, second, sender, send)
        draft_case(session, first, sender, send)
        session.commit()
        with pytest.raises(SendRefusedError, match="daily send cap"):
            draft_case(session, third, sender, send)
        assert len(sender.drafts) == 1 and third.status == CaseStatus.NEW.value


def test_batched_send_records_the_same_message_on_every_case(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660", "226321092661")
    sender = RecordingSender()
    with sessions() as session:
        cases = list_cases(session, CaseStatus.NEW.value)
        messages = draft_batch(session, cases, sender, settings)
        session.commit()
        assert len(sender.drafts) == 1 and len(messages) == 2
        assert {m.rfc_message_id for m in messages} == {sender.deliveries[0].rfc_message_id}
        assert all(c.status == CaseStatus.SENT.value and c.thread_id == "thread-1" for c in cases)


# ------------------------------------------------------------------------------- matching ----


def _sent_case(settings, sessions):  # type: ignore[no-untyped-def]
    """One case sent by the agent; returns (case id, its Message-ID)."""
    sender = RecordingSender()
    with sessions() as session:
        case = list_cases(session)[0]
        message = draft_case(session, case, sender, settings)
        session.commit()
        return case.id, message.rfc_message_id


def test_reply_is_matched_by_in_reply_to_without_thread_or_po(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    case_id, sent_id = _sent_case(settings, sessions)
    vendor = InboundMessage(
        message_id="archive-key-1",
        thread_id=None,  # read from a different mailbox than the one that sent
        sent_at=datetime(2026, 9, 30, 15, 0, tzinfo=UTC),
        from_addr="Shannon Humphrey <shumphre@udfinc.com>",
        to_addr="lidl-appointments@circledelivers.com",
        cc_addr="lidl@circledelivers.com",
        subject="RE: Pick Up Appointment: 226321092660",
        body="SET!",
        in_reply_to=sent_id.upper(),  # header case must not matter
        rfc_message_id="<conf-9@udfinc.com>",
        references=sent_id,
    )
    with sessions() as session:
        assert match_case(session, vendor).id == case_id
        stats = ingest(
            session, [vendor], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL
        )
        session.commit()
        case = session.get(BookingCase, case_id)
        assert stats.proposed == 1 and case is not None
        assert case.status == CaseStatus.PROPOSED.value
        inbound = next(m for m in case.messages if m.direction == "in")
        assert inbound.rfc_message_id == "<conf-9@udfinc.com>"
        assert inbound.in_reply_to == sent_id.upper() and inbound.references_header == sent_id


def test_reply_is_matched_through_the_references_chain(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    case_id, sent_id = _sent_case(settings, sessions)
    later = InboundMessage(
        message_id="archive-key-2",
        thread_id=None,
        sent_at=datetime(2026, 9, 30, 16, 0, tzinfo=UTC),
        from_addr="CCI <cci@udfinc.com>",
        to_addr="lidl-appointments@circledelivers.com",
        cc_addr="",
        subject="RE: something else entirely",
        body="Both orders?",
        in_reply_to="<an-intermediate-reply@udfinc.com>",
        references=f"{sent_id} <an-intermediate-reply@udfinc.com>",
    )
    with sessions() as session:
        assert match_case(session, later).id == case_id


def test_a_persons_send_seen_in_the_archive_links_the_drafted_case(settings, sessions):
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    _scanned(settings, sessions, "226321092660")
    with sessions() as session:
        case = list_cases(session)[0]
        draft_case(session, case, RecordingMailer(), settings)
        session.commit()
        case_id = case.id
        assert case.status == CaseStatus.DRAFTED.value and case.thread_id is None

    megan_sent = InboundMessage(
        message_id="archive-key-3",
        thread_id="mailbox-thread-7",
        sent_at=datetime(2026, 9, 30, 14, 0, tzinfo=UTC),
        from_addr="Megan Goodwin <megan.goodwin@circledelivers.com>",
        to_addr=f"CCI <{DESK}>",
        cc_addr="Lidl Group <lidl@circledelivers.com>",
        subject="Pick Up Appointment: 226321092660",
        body="Hello,\n\nCan I please schedule the following?\n\nPO# 226321092660 on 10/01 @ 0900",
        rfc_message_id="<megan-1@mail.gmail.com>",
    )
    vendor = reply("SET!", thread=None, subject="RE: Pick Up Appointment: 226321092660", mid="v1")
    vendor = InboundMessage(
        **{**vendor.__dict__, "in_reply_to": "<megan-1@mail.gmail.com>", "rfc_message_id": "<c@u>"}
    )
    with sessions() as session:
        stats = ingest(
            session,
            [megan_sent, vendor],
            FakeReplyClassifier(_confirmed),
            internal_domains=INTERNAL,
        )
        session.commit()
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert stats.linked_outbound == 1 and stats.skipped_internal == 0 and stats.proposed == 1
        request = next(m for m in case.messages if m.direction == "out")
        assert request.rfc_message_id == "<megan-1@mail.gmail.com>"
        assert request.thread_id == "mailbox-thread-7" and request.sent_at is not None
        assert case.thread_id == "mailbox-thread-7" and case.status == CaseStatus.PROPOSED.value
        assert any(e.action == "sent" and e.actor == "archive" for e in case.events)


def test_the_agents_own_send_seen_in_the_archive_is_recognised_not_re_recorded(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    case_id, sent_id = _sent_case(settings, sessions)
    echo = InboundMessage(
        message_id="archive-key-4",
        thread_id="archive-thread",
        sent_at=datetime(2026, 9, 30, 14, 1, tzinfo=UTC),
        from_addr="lidl-appointments@circledelivers.com",
        to_addr=DESK,
        cc_addr="lidl@circledelivers.com",
        subject="Pick Up Appointment: 226321092660",
        body="Hello,",
        rfc_message_id=sent_id,
    )
    with sessions() as session:
        assert link_outbound(session, echo) == "own"
        stats = ingest(session, [echo], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL)
        session.commit()
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.own_outbound == 1
        assert len(case.messages) == 1 and case.messages[0].message_id == "sent-1"
        assert case.thread_id == "thread-1"  # the sending mailbox's thread wins


def test_the_same_vendor_reply_from_two_sources_is_one_reply(settings, sessions):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    case_id, sent_id = _sent_case(settings, sessions)
    base = reply("SET!", thread=None, subject="RE: Pick Up Appointment: 226321092660", mid="pull-1")
    from_pull = InboundMessage(
        **{**base.__dict__, "in_reply_to": sent_id, "rfc_message_id": "<Same@u>"}
    )
    from_archive = InboundMessage(
        **{
            **base.__dict__,
            "message_id": "archive-key-5",
            "in_reply_to": sent_id,
            "rfc_message_id": "<same@U>",
        }
    )
    with sessions() as session:
        stats = ingest(
            session,
            [from_pull, from_archive],
            FakeReplyClassifier(_confirmed),
            internal_domains=INTERNAL,
        )
        session.commit()
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.duplicates == 1 and stats.classified == 1
        assert len([m for m in case.messages if m.direction == "in"]) == 1


# ------------------------------------------------------------------------- answering back ----


def test_agent_replies_answer_the_vendors_message_id_and_are_sent_when_the_outbox_sends(
    settings, sessions
):
    settings = _send_settings(settings)
    _scanned(settings, sessions, "226321092660")
    case_id, sent_id = _sent_case(settings, sessions)
    sender = RecordingSender()
    vendor = InboundMessage(
        message_id="archive-key-6",
        thread_id=None,
        sent_at=datetime(2026, 9, 30, 15, 0, tzinfo=UTC),
        from_addr=f"CCI <{DESK}>",
        to_addr="lidl-appointments@circledelivers.com",
        cc_addr="",
        subject="RE: Pick Up Appointment: 226321092660",
        body="SET!",
        in_reply_to=sent_id,
        rfc_message_id="<conf-77@udfinc.com>",
        references=sent_id,
    )
    with sessions() as session:
        ingest(
            session,
            [vendor],
            FakeReplyClassifier(_confirmed),
            internal_domains=INTERNAL,
            responder=Responder(settings, sender, now=NOW),
        )
        session.commit()
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PROPOSED.value
        thanks = sender.drafts[-1]
        assert thanks.body.startswith("Thank you!")
        assert thanks.in_reply_to == "<conf-77@udfinc.com>" and thanks.references == sent_id
        assert thanks.thread_id == "thread-1"
        ack = next(m for m in case.messages if m.kind == "acknowledge")
        assert (
            ack.sent_at is not None and ack.rfc_message_id == sender.deliveries[-1].rfc_message_id
        )
        assert ack.in_reply_to == "<conf-77@udfinc.com>"

        # A reschedule in the same thread answers the vendor's last message too.
        message = reschedule_case(
            session, case, sender, settings, requested_local="2026-10-02 09:00", by="megan"
        )
        assert sender.drafts[-1].in_reply_to == "<conf-77@udfinc.com>"
        assert message.sent_at is not None and case.status == CaseStatus.SENT.value


# ------------------------------------------------------------------------------ bootstrap ----


def test_ensure_columns_adds_what_older_stores_lack() -> None:
    engine = make_engine("sqlite://")
    init_db(engine)
    with engine.begin() as conn:
        conn.execute(text('DROP INDEX "ix_booking_messages_rfc_message_id"'))
        conn.execute(text('ALTER TABLE "booking_messages" DROP COLUMN "rfc_message_id"'))
        conn.execute(text('ALTER TABLE "booking_messages" DROP COLUMN "references_header"'))
    assert "rfc_message_id" not in {
        c["name"] for c in inspect(engine).get_columns("booking_messages")
    }
    added = ensure_columns(engine)
    assert sorted(added) == [
        "booking_messages.ix_booking_messages_rfc_message_id",
        "booking_messages.references_header",
        "booking_messages.rfc_message_id",
    ]
    assert "ix_booking_messages_rfc_message_id" in {
        i["name"] for i in inspect(engine).get_indexes("booking_messages")
    }
    names = {c["name"] for c in inspect(engine).get_columns("booking_messages")}
    assert {"rfc_message_id", "references_header", "in_reply_to"} <= names
    assert ensure_columns(engine) == []


def test_booking_send_cli_refuses_without_send_mode_or_a_mailbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    from facility_profiles.config import get_settings

    get_settings.cache_clear()
    runner = CliRunner()
    try:
        result = runner.invoke(app, ["booking", "send", "1"])
        assert result.exit_code == 2 and "FP_BOOKING_MODE" in result.output
        monkeypatch.setenv("FP_BOOKING_MODE", "send")
        get_settings.cache_clear()
        result = runner.invoke(app, ["booking", "send", "1"])
        assert result.exit_code == 2 and "FP_BOOKING_GMAIL_KEY" in result.output
        assert runner.invoke(app, ["booking", "sent", "--help"]).exit_code == 0
    finally:
        get_settings.cache_clear()
