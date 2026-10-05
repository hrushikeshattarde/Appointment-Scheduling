"""The agent answering on its own: it reads new replies, sends its answers through the send gate,
and books a confirmation itself where the customer's rule says ``confirm = "auto"``.

Everything is invented and in memory: the Koch Foods test load from tests/test_booking.py, a
Lidl customer file overridden in a temporary folder, a recording sender instead of Gmail, and
scripted reply readings instead of the model.
"""

from __future__ import annotations

import tomllib
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

import facility_profiles.customers as customers_pkg
from facility_profiles.booking.automation import run_once
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.inbox import ArchiveInbox, inbox_from_settings
from facility_profiles.booking.mail import InboundMessage, RecordingMailer, RecordingSender
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases, scan
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.customers import CustomerFileError, parse_customer
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "115802102660"
REPLY_AT = NOW + timedelta(hours=2)
INTERNAL = ["circledelivers.com"]
LIDL = (Path(customers_pkg.__file__).parent / "lidl.toml").read_text(encoding="utf-8")
CONFIRMS = "Confirmed for 10/1/26 09:00 CIRCLE #115802102660 CCI-9389"
CONFIRMED = ReplyClassification(
    status=ReplyStatus.CONFIRMED,
    pickup_date="2026-10-01",
    pickup_time="09:00",
    pickup_number="CCI-9389",
    quotes=["10/1/26 09:00 CIRCLE #115802102660 CCI-9389"],
    confidence=0.95,
)


def _lidl_with(rule: str) -> str:
    """Lidl's own file, with its rules replaced by ``rule``."""
    return LIDL[: LIDL.index("[[rules]]")] + rule


AUTO = """
[[rules]]
name = "vendor pickups by email"
when = { methods = ["email"] }
do = "send"
confirm = "auto"
customer_notes = "send"
"""
REVIEW = """
[[rules]]
name = "vendor pickups by email"
when = { methods = ["email"] }
do = "send"
"""
DRAFTS = """
[[rules]]
name = "vendor pickups by email"
when = { methods = ["email"] }
do = "draft"
"""


def _settings(settings: Settings, tmp_path: Path, rule: str) -> Settings:
    folder = tmp_path / "customers"
    folder.mkdir(exist_ok=True)
    (folder / "lidl.toml").write_text(_lidl_with(rule), encoding="utf-8")
    from facility_profiles.customers import registry

    registry.reload()
    return settings.model_copy(
        update={
            "customers_dir": str(folder),
            "pilot_terminal_ids": [1089],
            "booking_mode": "send",
            "booking_po_date_floor_desks": [],
        }
    )


@pytest.fixture(autouse=True)
def _forget_customer_files():  # type: ignore[no-untyped-def]
    from facility_profiles.customers import registry

    yield
    registry.reload()


def _sent_request(settings: Settings, sessions) -> tuple[int, str, RecordingSender]:  # type: ignore[no-untyped-def]
    """The Koch pickup scanned and its request sent; returns (case id, Message-ID, sender)."""
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(7001, po=PO)], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    sender = RecordingSender()
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        message = draft_case(s, case, sender, settings, now=NOW)
        assert case.status == CaseStatus.PENDING.value and message.rfc_message_id
        return case.id, message.rfc_message_id, sender


def _reply(
    body: str,
    sent_id: str | None,
    *,
    sender: str = "Desk Staff <desk.staff@udfinc.com>",
    mid: str = "r1",
) -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        thread_id=None,
        sent_at=REPLY_AT,
        from_addr=sender,
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="Re: Pick Up Appointment: 115802102660" if sent_id else "Re: pickup",
        body=body,
        in_reply_to=sent_id,
        rfc_message_id=f"<{mid}@udfinc.example>",
        references=sent_id,
    )


def _ingest(
    sessions, settings: Settings, message: InboundMessage, reading: ReplyClassification, sender
):  # type: ignore[no-untyped-def]
    mailer = RecordingMailer()
    with session_scope(sessions) as s:
        stats = ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=INTERNAL,
            responder=Responder(settings, mailer, now=REPLY_AT, sender=sender),
            settings=settings,
        )
    return stats, mailer


def _case(sessions, case_id: int) -> dict[str, Any]:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        return {
            "status": case.status,
            "open": open_kinds(case),
            "events": [(e.action, e.actor) for e in case.events],
            "exceptions": [(e.kind, e.description) for e in case.exceptions],
            "jobs": [j.kind for j in case.jobs],
            "pickup_number": case.pickup_number,
        }


# ------------------------------------------------------------------ the rule settings


def test_a_rule_says_how_answers_go_out_and_whether_to_book() -> None:
    customer = parse_customer(tomllib.loads(_lidl_with(AUTO)), key="lidl", source="test")
    rule = customer.rules[0]
    assert (rule.reply_mode, rule.confirm, rule.customer_notes) == ("send", "auto", "send")
    assert "books confirmations itself" in rule.describe()
    drafted = parse_customer(tomllib.loads(_lidl_with(DRAFTS)), key="lidl", source="test").rules[0]
    assert (drafted.reply_mode, drafted.confirm, drafted.customer_notes) == (
        "draft",
        "review",
        "draft",
    )
    bad = AUTO.replace('confirm = "auto"', 'confirm = "always"')
    with pytest.raises(CustomerFileError, match="confirm must be review or auto"):
        parse_customer(tomllib.loads(_lidl_with(bad)), key="lidl", source="test")


def test_the_inbox_setting_names_gmail_or_an_archive(settings: Settings) -> None:
    archive = settings.model_copy(update={"booking_inbox": "s3://bucket/mail"})
    assert inbox_from_settings(archive) == ArchiveInbox("bucket", "mail", 2)
    no_key = settings.model_copy(update={"booking_inbox": "gmail"})
    assert inbox_from_settings(no_key) is None  # Gmail needs the key and the mailbox
    with pytest.raises(ValueError, match="FP_BOOKING_INBOX"):
        Settings(
            _env_file=None,
            TPRO_BASE_URL="https://t",
            TPRO_USERNAME="u",
            TPRO_PASSWORD="p",
            booking_inbox="imap://x",
        )  # type: ignore[call-arg]


# ------------------------------------------------------------------ confirmations


def test_a_confirmation_of_the_time_asked_for_is_booked_and_thanked(
    settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    stats, mailer = _ingest(sessions, settings, _reply(CONFIRMS, sent_id), CONFIRMED, sender)
    assert stats.auto_confirmed == 1 and stats.responded == 1
    case = _case(sessions, case_id)
    assert case["status"] == CaseStatus.SCHEDULED.value and case["open"] == []
    assert ("auto_confirmed", "agent") in case["events"] and ("approved", "agent") in case["events"]
    assert case["pickup_number"] == "CCI-9389"
    assert "tpro_write" in case["jobs"]  # written back when write-back is on, as for a person's
    thanks = sender.drafts[-1]
    assert thanks.body.startswith("Thank you!") and "desk.staff@udfinc.com" in thanks.to_addr
    assert mailer.drafts == []  # sent, not drafted


def test_with_the_review_rule_a_person_still_approves(settings, sessions, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, REVIEW)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    stats, _ = _ingest(sessions, settings, _reply(CONFIRMS, sent_id), CONFIRMED, sender)
    assert stats.auto_confirmed == 0
    case = _case(sessions, case_id)
    assert case["status"] == CaseStatus.PENDING.value and case["open"] == ["confirmation_review"]
    assert sender.drafts[-1].body.startswith("Thank you!")  # the thanks still goes out


@pytest.mark.parametrize(
    ("body", "reading", "sent", "doubt"),
    [
        (
            "Confirmed for 10/1/26 13:00 CIRCLE #115802102660 CCI-9389",
            CONFIRMED.model_copy(
                update={
                    "pickup_time": "13:00",
                    "quotes": ["10/1/26 13:00 CIRCLE #115802102660 CCI-9389"],
                }
            ),
            True,
            "also open: confirmed_outside_window",
        ),
        (f"{CONFIRMS}. Note a $125 late fee applies.", CONFIRMED, True, "money or a claim"),
        (
            "Confirmed for 10/1/26 09:00 CCI-9389",
            CONFIRMED.model_copy(update={"quotes": ["10/1/26 09:00 CCI-9389"]}),
            False,
            "by its sender only",
        ),
    ],
)
def test_a_confirmation_in_doubt_is_left_for_a_person(
    settings, sessions, tmp_path, body, reading, sent, doubt
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    message = _reply(body, sent_id if sent else None, sender="CCI desk <cci@udfinc.com>")
    stats, _ = _ingest(sessions, settings, message, reading, sender)
    assert stats.auto_confirmed == 0
    case = _case(sessions, case_id)
    assert case["status"] == CaseStatus.PENDING.value
    assert "confirmation_review" in case["open"]
    assert ("auto_confirm_held", "agent") in case["events"]
    review = next(d for k, d in case["exceptions"] if k == "confirmation_review")
    assert "not booked automatically" in review and doubt in review


def test_an_offer_the_agent_accepts_is_booked_once_its_yes_is_sent(
    settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    offer = ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date="2026-10-01",
        pickup_time="11:00",
        quotes=["10/1/26 11:00 works better for us"],
        confidence=0.9,
    )
    _ingest(
        sessions,
        settings,
        _reply("Can't do 9. 10/1/26 11:00 works better for us", sent_id),
        offer,
        sender,
    )
    yes = sender.drafts[-1]
    assert yes.body.startswith("Yes, 10/01 @ 1100 works.") and yes.to_addr == "cci@udfinc.com"
    case = _case(sessions, case_id)
    assert case["status"] == CaseStatus.SCHEDULED.value
    assert ("auto_confirmed", "agent") in case["events"]


# ------------------------------------------------------------------ what goes out, and where


def test_an_answer_the_gate_refuses_is_kept_as_a_draft_for_a_person(
    settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        case.contact_email = "someone@elsewhere.example"  # not the desk the profile trusts
    question = ReplyClassification(
        status=ReplyStatus.QUESTION,
        question="What is the delivery number?",
        quotes=["What is the delivery number?"],
        confidence=0.9,
    )
    before = len(sender.drafts)
    _, mailer = _ingest(
        sessions, settings, _reply("What is the delivery number?", sent_id), question, sender
    )
    assert len(sender.drafts) == before  # nothing sent
    assert len(mailer.drafts) == 1 and "PYE_021026123" in mailer.drafts[0].body
    case_view = _case(sessions, case_id)
    assert ("reply_not_sent", "agent") in case_view["events"]
    assert any("was not sent" in d for _, d in case_view["exceptions"])


def test_a_draft_rule_drafts_every_answer(settings, sessions, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, DRAFTS)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    question = ReplyClassification(
        status=ReplyStatus.QUESTION,
        question="What is the delivery number?",
        quotes=["What is the delivery number?"],
        confidence=0.9,
    )
    before = len(sender.drafts)
    _, mailer = _ingest(
        sessions, settings, _reply("What is the delivery number?", sent_id), question, sender
    )
    assert len(sender.drafts) == before and len(mailer.drafts) == 1
    assert "facility_question" not in _case(sessions, case_id)["open"]  # answered, as a draft


def test_a_note_to_the_customer_desk_is_sent_when_the_rule_says_so(
    settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    no = ReplyClassification(
        status=ReplyStatus.REJECTED,
        reject_reason=RejectReason.NOT_READY,
        question="This PO is not ready, we cannot ship it this week.",
        quotes=["This PO is not ready, we cannot ship it this week."],
        confidence=0.9,
    )
    _ingest(
        sessions,
        settings,
        _reply("This PO is not ready, we cannot ship it this week.", sent_id),
        no,
        sender,
    )
    note = sender.drafts[-1]
    assert note.to_addr == "inbound@lidl.us" and note.subject == f"RESCHEDULE {PO}"
    case = _case(sessions, case_id)
    assert case["status"] == CaseStatus.DECLINED.value
    assert any("note to the customer desk sent" in d for _, d in case["exceptions"])


# ------------------------------------------------------------------ the pass reads the inbox


class FakeInbox:
    def __init__(self, messages: list[InboundMessage], fail: bool = False) -> None:
        self.messages, self.fail, self.reads = messages, fail, 0

    def fetch(self) -> list[InboundMessage]:
        self.reads += 1
        if self.fail:
            msg = "mailbox unreachable"
            raise RuntimeError(msg)
        return list(self.messages)


def test_each_pass_reads_new_replies_and_answers_each_once(settings, sessions, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    inbox = FakeInbox([_reply(CONFIRMS, sent_id)])
    classifier = FakeReplyClassifier(lambda _c: CONFIRMED)
    with session_scope(sessions) as s:
        report = run_once(
            s,
            settings,
            now=REPLY_AT,
            mailer=RecordingMailer(),
            sender=sender,
            inbox=inbox,
            classifier=classifier,
        )
    assert (report.mail_read, report.mail_answered, report.auto_confirmed) == (1, 1, 1)
    assert _case(sessions, case_id)["status"] == CaseStatus.SCHEDULED.value
    sent = len(sender.drafts)
    with session_scope(sessions) as s:
        again = run_once(
            s,
            settings,
            now=REPLY_AT + timedelta(minutes=5),
            mailer=RecordingMailer(),
            sender=sender,
            inbox=inbox,
            classifier=classifier,
        )
    assert (
        again.mail_read == 0 and len(sender.drafts) == sent
    )  # the same email is not answered twice
    assert len(classifier.calls) == 1


def test_without_send_mode_the_pass_only_drafts(settings, sessions, tmp_path) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    _case_id, sent_id, sender = _sent_request(settings, sessions)
    drafting = settings.model_copy(update={"booking_mode": "draft"})
    mailer = RecordingMailer()
    before = len(sender.drafts)
    with session_scope(sessions) as s:
        run_once(
            s,
            drafting,
            now=REPLY_AT,
            mailer=mailer,
            sender=sender,
            inbox=FakeInbox([_reply(CONFIRMS, sent_id)]),
            classifier=FakeReplyClassifier(lambda _c: CONFIRMED),
        )
    assert (
        len(sender.drafts) == before
        and mailer.drafts
        and mailer.drafts[0].body.startswith("Thank you!")
    )


def test_an_unreadable_inbox_or_a_failing_reply_does_not_stop_the_pass(
    settings, sessions, tmp_path
) -> None:  # type: ignore[no-untyped-def]
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    with session_scope(sessions) as s:
        report = run_once(
            s,
            settings,
            now=REPLY_AT,
            mailer=RecordingMailer(),
            sender=sender,
            inbox=FakeInbox([], fail=True),
            classifier=FakeReplyClassifier(lambda _c: CONFIRMED),
        )
    assert report.mail_failed == 1 and any("could not read" in line for line in report.lines)

    def broken(_ctx):  # type: ignore[no-untyped-def]
        msg = "model unavailable"
        raise RuntimeError(msg)

    inbox = FakeInbox([_reply(CONFIRMS, sent_id)])
    with session_scope(sessions) as s:
        failed = run_once(
            s,
            settings,
            now=REPLY_AT,
            mailer=RecordingMailer(),
            sender=sender,
            inbox=inbox,
            classifier=FakeReplyClassifier(broken),
        )
    assert failed.mail_failed == 1 and failed.mail_read == 0
    assert _case(sessions, case_id)["status"] == CaseStatus.PENDING.value  # nothing half-applied
    with session_scope(sessions) as s:
        retried = run_once(
            s,
            settings,
            now=REPLY_AT + timedelta(minutes=5),
            mailer=RecordingMailer(),
            sender=sender,
            inbox=inbox,
            classifier=FakeReplyClassifier(lambda _c: CONFIRMED),
        )
    assert retried.mail_read == 1 and retried.auto_confirmed == 1
