"""Gaps 32 to 36 of the email edge-case review: rounds, pleasantries, Bcc, Transport Pro's time, AWS.

32 a holding reply that answered nothing is not one of the agent's rounds,
33 a reply that only thanks or says "Your Welcome" books nothing,
34 a person's email that reached the group only by Bcc is kept on its pickup,
35 a change Transport Pro shows counts at the time Transport Pro gives for it,
36 an archive the board cannot read (a lapsed AWS login) is stood in for by the group's Gmail.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path

import pytest

from facility_profiles.api.booking import latest_updates
from facility_profiles.booking.automation import read_mail
from facility_profiles.booking.classify import (
    COURTESY,
    FakeReplyClassifier,
    courtesy_only,
    validate_classification,
)
from facility_profiles.booking.inbox import (
    ArchiveInbox,
    FallbackInbox,
    GmailInbox,
    aws_login_lapsed,
    group_query,
    inbox_from_settings,
    mail_problem,
)
from facility_profiles.booking.mail import InboundMessage, via_groups_of
from facility_profiles.booking.models import (
    PERSON_MAIL,
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseStatus,
)
from facility_profiles.booking.respond import is_holding, rounds_so_far
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest, list_cases
from facility_profiles.booking.writer import FOLLOW_UP_LINE, HOLD_LINE, FakeReplyWriter
from facility_profiles.clock import tpro_time
from facility_profiles.config import Settings
from facility_profiles.mailarchive.reader import to_inbound
from facility_profiles.mailarchive.store import Store
from facility_profiles.storage.db import session_scope
from tests.test_booking_autonomy import (  # noqa: F401 - the fixture resets the customer files
    AUTO,
    CONFIRMED,
    CONFIRMS,
    INTERNAL,
    REPLY_AT,
    _case,
    _forget_customer_files,
    _ingest,
    _reply,
    _sent_request,
    _settings,
)
from tests.test_booking_refresh import _case as refreshed_case
from tests.test_booking_refresh import _load, _scan, pod  # noqa: F401 - pod is a fixture
from tests.test_booking_situation_replies import _run, _written, tpro
from tests.test_mailarchive import DESK, MEGAN, FakeGmail, FakeS3, run

GROUP = "lidl@circledelivers.com"


# ------------------------------------------------------------------ 32. rounds


def _out(kind: str, body: str = "", **reading: object) -> BookingMessage:
    return BookingMessage(direction="out", kind=kind, body=body, classification=dict(reading))


def test_a_holding_reply_and_a_note_to_the_customer_are_not_rounds() -> None:
    case = BookingCase(load_id=1, po_numbers=["1"])
    case.messages = [
        _out("request"),
        _out("answer_question", f"Thank you!\n\n{HOLD_LINE}", holding=True),
        _out("answer_question", f"Thank you!\n\n{HOLD_LINE}"),  # written before replies were marked
        _out("escalate_to_customer"),
        _out(PERSON_MAIL),
    ]
    assert rounds_so_far(case) == 0
    case.messages.append(_out("answer_question", f"It is 40,000 lbs.\n\n{FOLLOW_UP_LINE}"))
    case.messages.append(_out("accept_offer", "Yes, 10/01 @ 0900 works. Thank you!"))
    assert rounds_so_far(case) == 2
    assert not is_holding(case.messages[-2])


def test_a_holding_reply_the_agent_writes_is_marked_and_uses_no_round(settings, sessions) -> None:
    asked = "Please send the driver name and cell."
    reading = ReplyClassification(status=ReplyStatus.QUESTION, question=asked, questions=[asked])

    def script(_situation):  # type: ignore[no-untyped-def]
        return _written("Thank you!", (asked, False, []))

    mailer, _view, _ = _run(
        settings,
        sessions,
        reading,
        FakeReplyWriter(script),
        text=asked,
        facts=tpro(dispatched=False),
    )
    assert HOLD_LINE in mailer.drafts[-1].body
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        held = [m for m in case.messages if m.kind == "answer_question"]
        assert held and held[-1].classification == {"holding": True}
        assert rounds_so_far(case) == 0


# ------------------------------------------------------------------ 33. pleasantries


@pytest.mark.parametrize(
    ("text", "courtesy"),
    [
        ("Your Welcome", True),
        ("Your welcome\n\nVera", True),
        ("You're welcome!\n\nDana Pike\nShipping Clerk\nAcme Foods", True),
        ("No problem, have a great day!", True),
        ("Confirmed", False),
        ("Sounds good", False),
        ("Thanks, 10/5 @ 9am works", False),
        ("Thank you! What is the trailer number?", False),
        ("Your welcome\nSee you Monday", False),
        ("", False),
    ],
)
def test_only_a_pleasantry_is_told_apart(text: str, courtesy: bool) -> None:
    assert courtesy_only(text) is courtesy


def test_a_pleasantry_read_as_a_confirmation_books_nothing(settings, sessions, tmp_path) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    words = "Your Welcome\n\nDana Pike\nShipping Clerk"
    misread = ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["Your Welcome"])
    checked, issues = validate_classification(misread, words)
    assert checked.status == ReplyStatus.UNRELATED
    assert [(i.field_name, i.reason) for i in issues] == [("status", COURTESY)]
    _ingest(sessions, settings, _reply(words, sent_id), misread, sender)
    view = _case(sessions, case_id)
    assert view["status"] == CaseStatus.PENDING.value and view["open"] == []
    assert ("vendor_confirmed", "agent") not in view["events"]


def test_a_pleasantry_on_a_booked_pickup_is_not_a_confirmation(
    settings, sessions, tmp_path
) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    _ingest(sessions, settings, _reply(CONFIRMS, sent_id), CONFIRMED, sender)
    misread = ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["Your Welcome"])
    _ingest(sessions, settings, _reply("Your Welcome", sent_id, mid="r9"), misread, sender)
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.SCHEDULED.value
        last = [m for m in case.messages if m.direction == "in"][-1]
        assert last.classification["status"] == "unrelated"
        assert "vendor_reconfirmed" not in [e.action for e in case.events]


# ------------------------------------------------------------------ 34. Bcc


def test_the_group_a_copy_came_through_is_read_from_its_headers() -> None:
    headers = {
        "mailing-list": "list Lidl@circledelivers.com; contact Lidl+owners@circledelivers.com",
        "list-id": "<Lidl.circledelivers.com>",
    }
    assert via_groups_of(headers) == (GROUP,)
    assert via_groups_of({}) == ()


def _bcc_raw(sent_at: datetime) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = "Re: Pick Up Appointments"
    msg["From"] = MEGAN
    msg["To"] = DESK
    msg["Date"] = format_datetime(sent_at)
    msg["Message-ID"] = "<bcc-1@mail.gmail.com>"
    msg["Mailing-list"] = "list Lidl@circledelivers.com; contact Lidl+owners@circledelivers.com"
    msg["List-ID"] = "<Lidl.circledelivers.com>"
    msg.set_content("Hello,\n\nCan we move PO# 115802102660 to 10/05 @ 0900?\n\nThank you!")
    return bytes(msg)


def test_the_archive_keeps_the_group_a_bcc_copy_came_through() -> None:
    sent = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    gm, s3 = FakeGmail(), FakeS3()
    gm.add("b1", _bcc_raw(sent), thread_id="T9", sent_at=sent)
    store = Store("bucket", client=s3)
    run(gm, store, mailbox="me@circledelivers.com", days=3)
    key, raw = next((k, v) for k, v in s3.objects.items() if k.endswith(".json") and "mail/" in k)
    envelope = json.loads(raw)
    assert envelope["via_groups"] == [GROUP] and GROUP not in envelope["to"].lower()
    assert to_inbound(envelope, key).via_groups == (GROUP,)


def _person_bcc(via: tuple[str, ...]) -> InboundMessage:
    return InboundMessage(
        message_id=f"bcc-{len(via)}",
        thread_id=None,
        sent_at=REPLY_AT + timedelta(hours=1),
        from_addr="Jordan Lake <jordan.lake@circledelivers.com>",
        to_addr="desk.staff@udfinc.com",
        cc_addr="",
        subject="Re: Pick Up Appointment: 115802102660",
        body="We will have a 53 ft reefer there.",
        in_reply_to="<r1@udfinc.example>",
        rfc_message_id=f"<bcc-{len(via)}@circledelivers.com>",
        via_groups=via,
    )


def test_a_persons_email_with_the_group_only_in_bcc_is_kept(settings, sessions, tmp_path) -> None:
    settings = _settings(settings, tmp_path, AUTO)
    case_id, sent_id, sender = _sent_request(settings, sessions)
    asked = "Are you able to bring a 53 ft reefer?"
    question = ReplyClassification(status=ReplyStatus.QUESTION, question=asked, quotes=[asked])
    _ingest(sessions, settings, _reply(asked, sent_id), question, sender)
    with session_scope(sessions) as s:
        kept = []
        for via in ((), (GROUP,)):  # not on the group at all, then only by Bcc
            stats = ingest(
                s,
                [_person_bcc(via)],
                FakeReplyClassifier(lambda _c: CONFIRMED),
                internal_domains=INTERNAL,
                settings=settings,
            )
            kept.append(stats.by_person)
    assert kept == [0, 1]
    view = _case(sessions, case_id)
    assert ("sent_by_person", "jordan.lake@circledelivers.com") in view["events"]
    assert view["open"] == []  # and, sent to the facility after its question, it settled it


# ------------------------------------------------------------------ 35. Transport Pro's time


def test_a_time_transport_pro_gives_is_kept_unless_it_is_in_the_future() -> None:
    now = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
    assert tpro_time(datetime(2026, 9, 28, 22, 14), now) == "2026-09-28T22:14:00+00:00"
    assert tpro_time(now + timedelta(minutes=5), now) is None
    assert tpro_time(None, now) is None


def test_a_change_made_overnight_shows_at_its_own_time(pod, sessions) -> None:  # type: ignore[no-untyped-def]  # noqa: F811
    _scan(pod, sessions, _load(delivery_notes="Please ensure driver has a load bar."))
    moved = _load()
    moved["lastUpdated"] = "2026-09-29T02:14:00Z"  # the pod booked the DCT slot overnight
    morning = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
    _scan(pod, sessions, moved, now=morning)
    case = refreshed_case(sessions)
    event = next(e for e in case.events if e.action == "delivery_from_tpro")
    assert event.detail["changed_at"] == "2026-09-29T02:14:00+00:00"
    rows = [r for r in latest_updates([case], now=morning) if r["source"] == "load"]
    moved_row = next(r for r in rows if r["what"] == "Delivery slot from Transport Pro")
    assert moved_row["at"].startswith("2026-09-29T02:14")


def test_a_kept_time_is_used_when_transport_pro_gives_none() -> None:
    kept = datetime(2026, 9, 29, 11, 0, tzinfo=UTC)
    case = BookingCase(id=1, load_id=1, po_numbers=["1"], vendor_name="Koch Foods, Inc.")
    case.events = [
        BookingEvent(action="tender_changed", detail={"reason": "x"}, created_at=kept),
        BookingEvent(
            action="booked_in_tpro",
            detail={
                "reason": "booked",
                "changed_at": "2026-09-30T09:00:00+00:00",
            },  # later: ignored
            created_at=kept,
        ),
    ]
    rows = latest_updates([case], now=kept)
    assert {r["at"][:16] for r in rows} == {"2026-09-29T11:00"}


# ------------------------------------------------------------------ 36. the AWS login


class TokenRetrievalError(Exception):
    """What botocore raises when the SSO token has expired."""


class ClientError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.response = {"Error": {"Code": code}}


class _Failing:
    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def fetch(self) -> list[InboundMessage]:
        raise self.exc

    def __str__(self) -> str:
        return "the archive s3://circle-lidl-appointments"


class _Gmail:
    def __init__(self, found: list[InboundMessage]) -> None:
        self.found = found

    def fetch(self) -> list[InboundMessage]:
        return list(self.found)

    def __str__(self) -> str:
        return "the mailbox member@circledelivers.com"


def test_a_lapsed_aws_login_is_recognised(monkeypatch: pytest.MonkeyPatch) -> None:
    assert aws_login_lapsed(TokenRetrievalError("Token has expired and refresh failed"))
    assert aws_login_lapsed(ClientError("ExpiredToken"))
    wrapped = RuntimeError("reading the archive failed")
    wrapped.__cause__ = TokenRetrievalError("x")
    assert aws_login_lapsed(wrapped)
    assert not aws_login_lapsed(ClientError("NoSuchKey"))
    monkeypatch.setenv("AWS_PROFILE", "paybot-admin")
    assert mail_problem(ClientError("ExpiredToken")) == (
        "the AWS login on this machine has lapsed; run aws sso login --profile paybot-admin"
    )
    assert mail_problem(ClientError("NoSuchKey")) is None


def test_the_groups_gmail_stands_in_for_an_archive_that_cannot_be_read(
    settings: Settings, sessions, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AWS_PROFILE", "paybot-admin")
    one = InboundMessage(
        message_id="g1",
        thread_id=None,
        sent_at=REPLY_AT,
        from_addr="desk.staff@udfinc.com",
        to_addr=GROUP,
        cc_addr="",
        subject="hello",
        body="hello",
    )
    inbox = FallbackInbox(_Failing(TokenRetrievalError("expired")), _Gmail([one]))
    assert inbox.fetch() == [one]
    assert inbox.notes == [
        "read from the mailbox member@circledelivers.com: the archive s3://circle-lidl-appointments "
        "was not read (the AWS login on this machine has lapsed; run aws sso login --profile "
        "paybot-admin)"
    ]
    reader = FakeReplyClassifier(lambda _c: CONFIRMED)
    with session_scope(sessions) as s:
        report = read_mail(s, settings, inbox=inbox, classifier=reader)
        assert report.mail_failed == 0 and report.mail_note == inbox.notes[0]
        failed = read_mail(
            s, settings, inbox=_Failing(TokenRetrievalError("expired")), classifier=reader
        )
    assert failed.mail_failed == 1
    assert failed.mail_problem == (
        "the AWS login on this machine has lapsed; run aws sso login --profile paybot-admin"
    )


def test_the_archive_has_a_stand_in_only_with_a_gmail_key_and_mailbox(settings: Settings) -> None:
    archive = settings.model_copy(update={"booking_inbox": "s3://bucket/mail"})
    assert inbox_from_settings(archive) == ArchiveInbox("bucket", "mail", 2)
    member = "member@circledelivers.com"
    keyed = archive.model_copy(
        update={"booking_gmail_key": "key.json", "mail_archive_gmail_user": member}
    )
    stand_in = GmailInbox(Path("key.json"), member, group_query(keyed, 2) or "")
    assert inbox_from_settings(keyed) == FallbackInbox(ArchiveInbox("bucket", "mail", 2), stand_in)
