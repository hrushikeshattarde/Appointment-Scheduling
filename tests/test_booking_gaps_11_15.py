"""Gaps 11 to 15 of the email edge-case review, closed one by one.

11 each pickup in a batched email follows its own facility, 12 a Gmail failure is reported (and
a send Gmail never answered is recorded, not repeated), 13 mail sent straight to the sending
mailbox is read, 14 mail is kept for what its text says, 15 attached files are read.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import sys
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from openpyxl import Workbook
from typer.testing import CliRunner

from facility_profiles.booking import mail as mail_module
from facility_profiles.booking.attachments import (
    MAX_TOTAL_CHARS,
    Attachment,
    Reading,
    read,
    with_attachments,
)
from facility_profiles.booking.automation import read_mail
from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.inbox import (
    ArchiveInbox,
    FallbackInbox,
    GmailInbox,
    MergedInbox,
    group_query,
    inbox_from_settings,
)
from facility_profiles.booking.mail import (
    UNCONFIRMED,
    GmailSender,
    InboundMessage,
    MailError,
    OutboundDraft,
    RecordingMailer,
    RecordingSender,
    _from_gmail,
    _post,
    deliver,
)
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.references import ReferenceSource, record_reference
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    _auto_confirm_doubt,
    draft_batch,
    draft_case,
    ingest,
    list_cases,
    mark_sent,
    ready_to_draft,
)
from facility_profiles.booking.unmatched import keep_unmatched
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.customers import customers
from facility_profiles.domain.resolution import StopIdentity
from facility_profiles.domain.schema import FacilityIdentity, FieldState, Role
from facility_profiles.mailarchive import filters
from facility_profiles.mailarchive.collector import parse_raw
from facility_profiles.mailarchive.reader import S3MailReader
from facility_profiles.mailarchive.store import (
    BODY_CHECKED_KEY,
    KEPT_IDS_KEY,
    Store,
    attachment_key,
)
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository
from tests.test_booking import NOW, reply
from tests.test_booking_gaps import _lidl
from tests.test_booking_send import DESK, INTERNAL, _scanned, _send_settings
from tests.test_mailarchive import GROUP, LIDL, FakeGmail, FakeS3, make_mail
from tests.test_mailarchive import NOW as ARCHIVE_NOW
from tests.test_mailarchive import run as collect

MAILBOX = "lidl-appointments@circledelivers.com"


def pdf_with(text: str | None, *, picture: bool = False) -> bytes:
    """A one-page PDF with ``text`` drawn in Helvetica, or nothing on it (like a scan).

    With ``picture`` the page also shows an image, as a scan or a printed photo does.
    """
    drawn = f"BT /F1 12 Tf 72 712 Td ({text}) Tj ET".encode() if text else b""
    content = (b"q 100 0 0 100 0 0 cm /Im1 Do Q " if picture else b"") + drawn
    xobject = b" /XObject << /Im1 6 0 R >>" if picture else b""
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
        b"/Resources << /Font << /F1 5 0 R >>" + xobject + b" >> >>",
        b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Type /XObject /Subtype /Image /Width 1 /Height 1 /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Length 1 >>\nstream\n\x80\nendstream",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


# ------------------------------------------------------------------ 11. one facility per line


def second_plant(sessions, **fields: Any) -> str:  # type: ignore[no-untyped-def]
    """Another Koch plant booked through the same desk, with its own profile."""
    identity = StopIdentity(
        location_id=None,
        company_name="Koch Foods, Inc.",
        address="4404 W Berteau Ave",
        city="Chicago",
        state="IL",
        postal_code="60641",
        latitude=None,
        longitude=None,
    )
    key = f"candidate:{identity.candidate_key()}"
    with session_scope(sessions) as session:
        repo = Repository(session)
        repo.upsert_facility(
            FacilityIdentity(
                facility_id=None,
                candidate_key=key.split(":")[1],
                company_name="Koch Foods, Inc.",
                address="4404 W Berteau Ave",
                city="Chicago",
                state="IL",
                postal_code="60641",
                iana_timezone="America/New_York",
            ),
            latitude=None,
            longitude=None,
        )
        for name, value in {"booking_method": "email", "contact_email": DESK, **fields}.items():
            repo.set_field_human(key, Role.SHIPPER, name, value, state=FieldState.HUMAN_SET)
    return key


def test_a_batch_checks_each_pickup_against_its_own_facility(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660", "226321092661")
    plant = second_plant(sessions, required_refs=["bol_number"])
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        first, second = sorted(list_cases(session), key=lambda c: c.id)
        second.facility_key = plant  # the same desk books it, but this plant wants the BOL
        refused: list[tuple[list[BookingCase], str]] = []
        messages = draft_batch(session, [first, second], mailer, settings, now=NOW, refused=refused)
        assert messages == [] and mailer.drafts == []
        assert f"case {second.id}: the desk needs the BOL number" in refused[0][1]


def test_each_po_line_is_written_for_its_own_facility(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660", "226321092661")
    # This plant books by the day and wants the BOL on the line.
    plant = second_plant(sessions, required_refs=["bol_number"], appointment_required=False)
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        first, second = sorted(list_cases(session), key=lambda c: c.id)
        second.facility_key = plant
        record_reference(
            session, second, "bol_number", "778812", source=ReferenceSource.PERSON, by="tester"
        )
        draft_batch(session, [first, second], mailer, settings, now=NOW)
    lines = [line for line in mailer.drafts[0].body.splitlines() if line.startswith("PO#")]
    assert lines == ["PO# 226321092660 on 10/01 @ 0900", "PO# 226321092661 / BOL# 778812 on 10/01"]


# ------------------------------------------------------------------ 12. Gmail failures


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any] | None = None, text: str = "") -> None:
        self.status_code = status
        self._payload = payload
        self.text = text

    def json(self) -> dict[str, Any]:
        if self._payload is None:
            msg = "no JSON"
            raise ValueError(msg)
        return self._payload


class FakeSession:
    def __init__(self, outcome: Exception | FakeResponse) -> None:
        self.outcome = outcome

    def post(self, url: str, json: dict[str, Any], timeout: int) -> FakeResponse:
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class RefreshError(Exception):
    """Named like google-auth's: the login failed before anything was sent."""


class ReadTimeout(Exception):  # noqa: N818 - named as requests names it
    """Named like requests': Gmail took the message and never answered."""


def _fails(outcome: Exception | FakeResponse, *, sending: bool = True) -> MailError:
    with pytest.raises(MailError) as caught:
        _post(FakeSession(outcome), "https://gmail.test", {}, what="send", sending=sending)
    return caught.value


def test_every_gmail_failure_is_one_readable_error_that_says_if_it_may_have_gone() -> None:
    login = _fails(RefreshError("unauthorized_client: Client is unauthorized"))
    assert not login.maybe_sent and "RefreshError: unauthorized_client" in str(login)
    assert _fails(ReadTimeout("read timed out")).maybe_sent
    assert not _fails(ReadTimeout("read timed out"), sending=False).maybe_sent  # a draft
    no_route = ConnectionError("Failed to establish a new connection: [Errno 11001]")
    assert not _fails(no_route).maybe_sent
    assert not _fails(FakeResponse(400, text="Invalid To header")).maybe_sent
    assert _fails(FakeResponse(503, text="backendError")).maybe_sent
    assert _fails(FakeResponse(200)).maybe_sent  # it answered 200 with nothing readable
    assert _post(FakeSession(FakeResponse(200, {"id": "x"})), "u", {}, what="send", sending=True)


def test_the_sender_reports_a_lost_answer_with_the_message_id_it_minted(monkeypatch) -> None:
    monkeypatch.setattr(mail_module, "_gmail_session", lambda *_a: FakeSession(ReadTimeout("t")))
    draft = OutboundDraft(to_addr=DESK, cc_addr="lidl@circledelivers.com", subject="s", body="b")
    sender = GmailSender(Path("key.json"), MAILBOX)
    with pytest.raises(MailError) as caught:
        sender.deliver(draft)
    minted = caught.value.rfc_message_id or ""
    assert caught.value.maybe_sent and minted.startswith("<") and minted.endswith(".com>")
    # Handed to an outbox, that send is recorded as unconfirmed rather than raised.
    result = deliver(sender, draft)
    assert result.ref == UNCONFIRMED and not result.sent and result.unconfirmed
    # A key that cannot be loaded is reported as such.
    monkeypatch.undo()
    with pytest.raises(MailError, match="could not be loaded"):
        GmailSender(Path("no-such-key.json"), MAILBOX).deliver(draft)


class SilentGmail:
    """A sender whose Gmail took the message and never answered."""

    def __init__(self, rfc_id: str) -> None:
        self.rfc_id = rfc_id

    def deliver(self, draft: OutboundDraft) -> Any:
        msg = "Gmail send failed: ReadTimeout: read timed out"
        raise MailError(msg, maybe_sent=True, rfc_message_id=self.rfc_id)


class RefusingGmail:
    def deliver(self, draft: OutboundDraft) -> Any:
        msg = "Gmail send failed 400: Invalid To header"
        raise MailError(msg)


def test_a_send_gmail_never_answered_is_kept_and_not_sent_again(settings, sessions) -> None:
    send = _send_settings(settings)
    _scanned(send, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        message = draft_case(
            session, case, SilentGmail("<lost-1@circledelivers.com>"), send, now=NOW
        )
        assert message.draft_ref == UNCONFIRMED and message.sent_at is None
        assert message.rfc_message_id == "<lost-1@circledelivers.com>"
        assert case.status == CaseStatus.UNSCHEDULED.value
        assert open_kinds(case) == ["send_unconfirmed"]
        assert ready_to_draft(session) == []  # never picked up again on its own
        with pytest.raises(ValueError, match="open exceptions"):
            draft_case(session, case, RecordingSender(), send, now=NOW)
        case_id = case.id
    # The group's copy shows it went: the case moves on and the to-do clears.
    echo = InboundMessage(
        message_id="archive-key-9",
        thread_id="archive-thread",
        sent_at=datetime(2026, 9, 29, 12, 1, tzinfo=UTC),
        from_addr=MAILBOX,
        to_addr=DESK,
        cc_addr="lidl@circledelivers.com",
        subject="Pick Up Appointment: 226321092660",
        body="Hello,",
        rfc_message_id="<LOST-1@circledelivers.com>",
    )
    with session_scope(sessions) as session:
        stats = ingest(session, [echo], FakeReplyClassifier(_unrelated), internal_domains=INTERNAL)
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.own_outbound == 1
        assert case.status == CaseStatus.PENDING.value and open_kinds(case) == []
        assert case.messages[0].sent_at is not None and case.messages[0].draft_ref == "gmail:sent"
        assert any(e.action == "sent" and e.detail.get("confirmed_later") for e in case.events)


def test_a_reply_to_an_unconfirmed_send_or_a_person_who_checked_settles_it(
    settings, sessions
) -> None:
    send = _send_settings(settings)
    _scanned(send, sessions, "226321092660", "226321092661")
    with session_scope(sessions) as session:
        answered, checked = sorted(list_cases(session), key=lambda c: c.id)
        draft_case(session, answered, SilentGmail("<lost-2@circledelivers.com>"), send, now=NOW)
        draft_case(session, checked, SilentGmail("<lost-3@circledelivers.com>"), send, now=NOW)
        mark_sent(session, checked, by="megan")  # found it in the Sent folder
        assert checked.status == CaseStatus.PENDING.value and open_kinds(checked) == []
        ids = answered.id
    vendor = InboundMessage(
        **{
            **reply("SET!", thread=None, mid="set-2").__dict__,
            "in_reply_to": "<lost-2@circledelivers.com>",
        }
    )
    with session_scope(sessions) as session:
        ingest(session, [vendor], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL)
        case = session.get(BookingCase, ids)
        assert case is not None and open_kinds(case) == ["confirmation_review"]
        assert case.messages[0].sent_at is not None


def test_a_send_gmail_refused_outright_leaves_nothing_behind(settings, sessions) -> None:
    send = _send_settings(settings)
    _scanned(send, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        refused: list[tuple[list[BookingCase], str]] = []
        assert draft_batch(session, [case], RefusingGmail(), send, now=NOW, refused=refused) == []
        assert "Invalid To header" in refused[0][1]
        assert case.messages == [] and open_kinds(case) == []


def test_booking_send_reports_a_gmail_login_failure_instead_of_crashing(
    settings: Settings, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    _scanned(_send_settings(settings), sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        case.requested_local = "2030-10-01 09:00"  # well ahead of today, whenever this runs
        case_id = case.id
    engine.dispose()
    for name, value in {
        "TPRO_BASE_URL": "https://tpro.test",
        "TPRO_USERNAME": "u",
        "TPRO_PASSWORD": "p",
        "FP_DATABASE_URL": url,
        "FP_BOOKING_MODE": "send",
        "FP_BOOKING_SEND_DAILY_CAP": "5",
        "FP_BOOKING_GMAIL_KEY": str(tmp_path / "missing-key.json"),
        "FP_BOOKING_GMAIL_USER": MAILBOX,
        "NO_COLOR": "1",
        "COLUMNS": "200",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        result = CliRunner().invoke(app, ["booking", "send", str(case_id), "--anyway"])
    finally:
        get_settings.cache_clear()
    assert result.exit_code == 1, result.output
    assert "not sent: the Gmail key" in result.output and "Traceback" not in result.output


# ------------------------------------------------------------------ 13. the sending mailbox


def _mailbox(settings: Settings, inbox: str) -> Settings:
    return settings.model_copy(
        update={
            "booking_inbox": inbox,
            "booking_inbox_days": 2,
            "booking_gmail_key": "key.json",
            "booking_gmail_user": MAILBOX,
        }
    )


def test_the_sending_mailbox_is_read_beside_the_group(settings: Settings) -> None:
    gmail = inbox_from_settings(_mailbox(settings, "gmail"))
    assert isinstance(gmail, GmailInbox)
    assert gmail.query == (
        "(to:lidl@circledelivers.com OR cc:lidl@circledelivers.com OR "
        f"deliveredto:lidl@circledelivers.com OR to:{MAILBOX} OR cc:{MAILBOX}) newer_than:2d"
    )
    assert "to:" + MAILBOX not in (group_query(settings, 2) or "")  # a person's mailbox: group only
    archive = inbox_from_settings(_mailbox(settings, "s3://bucket/mail"))
    assert isinstance(archive, MergedInbox)
    # The group's mail in the mailbox stands in for the archive when it cannot be read (gap 36).
    stand_in = GmailInbox(Path("key.json"), MAILBOX, group_query(settings, 2) or "")
    assert archive.sources[0] == FallbackInbox(ArchiveInbox("bucket", "mail", 2), stand_in)
    direct = archive.sources[1]
    assert isinstance(direct, GmailInbox) and direct.query == (
        f"(to:{MAILBOX} OR cc:{MAILBOX}) -to:lidl@circledelivers.com -cc:lidl@circledelivers.com "
        "newer_than:2d"
    )


class Box:
    def __init__(self, messages: list[InboundMessage] | None = None, error: str = "") -> None:
        self.messages = messages or []
        self.error = error

    def fetch(self) -> list[InboundMessage]:
        if self.error:
            raise RuntimeError(self.error)
        return self.messages

    def __str__(self) -> str:
        return "the mailbox"


def test_an_email_in_both_sources_is_read_once_and_one_failing_source_is_reported(
    settings, sessions
) -> None:
    group_copy = reply("SET!", mid="a")
    mailbox_copy = InboundMessage(**{**group_copy.__dict__, "message_id": "gmail-id-a"})
    direct = reply("Confirmed for Thursday", mid="b")
    both = MergedInbox([Box([group_copy]), Box([mailbox_copy, direct])])
    assert [m.message_id for m in both.fetch()] == ["a", "b"]
    half = MergedInbox([Box([group_copy]), Box(error="token expired")])
    assert [m.message_id for m in half.fetch()] == ["a"]
    assert half.problems == ["the mailbox: token expired"]
    with pytest.raises(RuntimeError, match="token expired"):
        MergedInbox([Box(error="token expired")]).fetch()
    with session_scope(sessions) as session:
        report = read_mail(
            session, settings, inbox=half, classifier=FakeReplyClassifier(_unrelated)
        )
    assert report.mail_failed == 1
    assert "inbox: could not read the mailbox: token expired" in report.lines


# ------------------------------------------------------------------ 14. what the text says


def test_mail_is_kept_for_what_its_text_says_when_its_subject_says_nothing() -> None:
    assert filters.body_reason("Order 226321092660 is ready Friday", LIDL) == "body:po-number"
    assert filters.body_reason("Your PU# 44718 is set", LIDL) == "body:pickup-number"
    assert (
        filters.body_reason("Can we set a pick up appointment for tomorrow?", LIDL)
        == "body:pickup-appointment"
    )
    assert filters.body_reason("Lunch on Friday? Call 260-208-4500", LIDL) is None
    kept = {"req-1@mail.gmail.com"}
    assert filters.reply_reason(kept, "<REQ-1@mail.gmail.com>") == "reply:kept"
    assert filters.reply_reason(kept, None, "<other@x> <req-1@mail.gmail.com>") == "reply:kept"
    assert filters.reply_reason(kept, "<other@x>") is None


class TextGmail(FakeGmail):
    """The fake mailbox, also answering ``full`` reads with the message's text."""

    def message(self, message_id: str, fmt: str = "raw") -> dict[str, Any]:
        if fmt != "full":
            return super().message(message_id, fmt)
        self.calls.append(f"message {message_id} full")
        text = parse_raw(self.msgs[message_id]["raw"]).text
        data = base64.urlsafe_b64encode(text.encode()).decode()
        return {"id": message_id, "payload": {"mimeType": "text/plain", "body": {"data": data}}}


def test_the_archive_keeps_a_new_desk_and_a_reply_under_a_new_subject() -> None:
    gm = TextGmail()
    t = ARCHIVE_NOW - timedelta(hours=5)
    new_desk = "Ann <ann@newvendor.example>"
    mails = {
        "n1": (
            "Pick Up Appointment: 226321092660",
            MAILBOX,
            "<req-9@circledelivers.com>",
            None,
            "Can I please schedule PO# 226321092660 on 10/01 @ 0900?",
        ),
        "n2": (
            "Confirmed",
            new_desk,
            "<conf-9@newvendor.example>",
            "<req-9@circledelivers.com>",
            "See you then.",
        ),
        "n3": (
            "Order ready",
            new_desk,
            "<ready-9@newvendor.example>",
            None,
            "Order 226321092661 is ready for Friday.",
        ),
        "n4": (
            "Lunch",
            "Bob <bob@elsewhere.example>",
            "<lunch@elsewhere.example>",
            None,
            "Friday?",
        ),
    }
    for i, (gid, (subject, frm, mid, answers, body)) in enumerate(mails.items()):
        sent = t + timedelta(minutes=i)
        raw = make_mail(
            subject=subject,
            frm=frm,
            to=GROUP,
            message_id=mid,
            in_reply_to=answers,
            body=body,
            sent_at=sent,
        )
        gm.add(gid, raw, thread_id=f"T-{gid}", sent_at=sent)
    store = Store("bucket", client=FakeS3())
    first = collect(gm, store, mailbox=MAILBOX, group="lidl@circledelivers.com")
    # Newest first: the reply is listed before the request it answers is kept.
    assert first.stored == 2 and first.not_booking == 2
    assert first.reasons["body:po-number"] == 1
    assert "req-9@circledelivers.com" in store.get_json(KEPT_IDS_KEY)
    assert set(store.get_json(BODY_CHECKED_KEY)) == {"n2", "n4"}
    gm.calls.clear()
    second = collect(gm, store, mailbox=MAILBOX, group="lidl@circledelivers.com")
    assert second.stored == 1 and second.reasons == {"reply:kept": 1}
    assert second.not_booking == 1 and not any(c.endswith(" full") for c in gm.calls)


def test_the_agent_keeps_unmatched_mail_for_what_its_text_says(settings, sessions) -> None:
    known = customers(settings)
    news = InboundMessage(
        message_id="u-1",
        thread_id=None,
        sent_at=NOW,
        from_addr="Ann <ann@newvendor.example>",
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="Friday",
        body="Your PU# 44718 is set for 0800.",
    )
    lunch = InboundMessage(**{**news.__dict__, "message_id": "u-2", "body": "Lunch?"})
    with session_scope(sessions) as session:
        assert keep_unmatched(session, news, known) == "new"
        assert keep_unmatched(session, lunch, known) is None


# ------------------------------------------------------------------ 15. attached files


def _docx(text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(
            "word/document.xml",
            f'<w:document><w:body><w:p><w:r><w:t xml:space="preserve">{text}</w:t></w:r></w:p>'
            "<w:p><w:r><w:t>Dock 4</w:t></w:r></w:p></w:body></w:document>",
        )
    return buffer.getvalue()


def _xlsx() -> bytes:
    book = Workbook()
    sheet = book.active
    assert sheet is not None
    sheet.title = "Appointments"
    sheet.append(["PO", "Date", "Time", "PU#"])
    sheet.append(["226321092660", "10/01", "0900", "4471"])
    buffer = io.BytesIO()
    book.save(buffer)
    return buffer.getvalue()


@pytest.mark.parametrize(
    ("name", "mime", "data", "expected"),
    [
        (
            "conf.pdf",
            "application/pdf",
            pdf_with("Confirmed PU# 4471 for 10/01 at 0900"),
            "Confirmed PU# 4471 for 10/01 at 0900",
        ),
        (
            "conf.docx",
            "application/octet-stream",
            _docx("Confirmed 10/01 &amp; 10/02"),
            "Confirmed 10/01 & 10/02\nDock 4",
        ),
        (
            "sheet.xlsx",
            "application/octet-stream",
            _xlsx(),
            "[Appointments]\nPO | Date | Time | PU#\n226321092660 | 10/01 | 0900 | 4471",
        ),
        (
            "invite.ics",
            "text/calendar",
            b"BEGIN:VEVENT\r\nDTSTART:20261001T090000\r\nEND:VEVENT",
            "BEGIN:VEVENT\nDTSTART:20261001T090000\nEND:VEVENT",
        ),
        (
            "note.html",
            "text/html",
            b"<p>Set for <b>10/01</b></p><p>Dock&nbsp;4</p>",
            "Set for 10/01\nDock\xa04",
        ),
    ],
)
def test_readable_files_give_their_text(name: str, mime: str, data: bytes, expected: str) -> None:
    reading = read(Attachment(name, mime, data))
    assert reading is not None and reading.why is None and reading.text == expected


def test_files_that_cannot_be_read_are_named_and_logos_left_alone(monkeypatch) -> None:
    def why(name: str, mime: str, data: bytes, *, inline: bool = False) -> str | None:
        reading = read(Attachment(name, mime, data, inline=inline))
        return None if reading is None else reading.why

    assert why("scan.pdf", "application/pdf", pdf_with(None)) == (
        "the PDF has no text in it (a scan or a picture)"
    )
    printed = pdf_with(
        "10/2/26, 5:36 PM image0.jpeg https://mail.google.com/mail/u/0/", picture=True
    )
    assert why("printout.pdf", "application/pdf", printed) == (
        "the PDF is a picture with next to no text (a scan or a printout)"
    )
    letter = "Koch Foods confirms the pickup below for PO 226321092660 " * 3
    assert (
        why("conf.pdf", "application/pdf", pdf_with(letter, picture=True)) is None
    )  # a logo on it
    assert why("old.doc", "application/msword", b"x" * 900) == "the agent cannot read .doc files"
    assert (
        why("photo.jpg", "image/jpeg", b"x" * 80_000) == "a picture; the agent cannot read pictures"
    )
    assert read(Attachment("image001.png", "image/png", b"x" * 4_000)) is None  # a logo
    assert read(Attachment("banner.png", "image/png", b"x" * 80_000, inline=True)) is None
    assert read(Attachment("smime.p7s", "application/pkcs7-signature", b"x" * 900)) is None
    monkeypatch.setitem(sys.modules, "pypdf", None)  # the pdf extra not installed
    assert why("conf.pdf", "application/pdf", pdf_with("x")) == (
        "PDF reading is not installed here (the pdf extra)"
    )


def test_file_text_goes_under_the_emails_words_and_unread_files_are_named() -> None:
    body, unread = with_attachments(
        "Please see attached.",
        [Reading("conf.pdf", text="PU# 4471"), Reading("scan.pdf", why="a scan")],
    )
    assert body == (
        "Please see attached.\n\n--- Attached file: conf.pdf ---\nPU# 4471\n\n"
        "--- Attached file not read: scan.pdf (a scan) ---"
    )
    assert unread == ("scan.pdf: a scan",)
    many = [Reading(f"f{i}.txt", text="x" * 4000) for i in range(3)]
    body, unread = with_attachments("", many)
    assert body.count("--- Attached file: ") == 2 and len(body) < MAX_TOTAL_CHARS + 200
    assert unread == ("f2.txt: more text than the agent reads in one email",)


def test_the_archive_reader_reads_each_file_once(settings) -> None:
    s3 = FakeS3()
    store = Store("bucket", client=s3)
    pdf = pdf_with("Confirmed PU# 5512 for 10/02 at 1000")
    sha = hashlib.sha256(pdf).hexdigest()
    s3.objects[attachment_key(sha)] = pdf
    envelope = {
        "key": "k1",
        "internal_date": "2026-09-30T10:00:00+00:00",
        "from": "desk@vendor.example",
        "subject": "Your appointment",
        "own_text": "Please see attached.",
        "attachments": [
            {"filename": "conf.pdf", "mime": "application/pdf", "bytes": len(pdf), "sha256": sha},
            {"filename": "image001.png", "mime": "image/png", "bytes": 90_000, "sha256": "ab"},
        ],
    }
    s3.objects["mail/2026/09/30/k1.json"] = json.dumps(envelope).encode()
    first = S3MailReader(store).fetch(days=1, now=ARCHIVE_NOW)[0]
    assert first.body == (
        "Please see attached.\n\n--- Attached file: conf.pdf ---\n"
        "Confirmed PU# 5512 for 10/02 at 1000"
    )
    assert first.unread_files == ()  # the old envelope's picture counts as a logo
    del s3.objects[attachment_key(sha)]
    assert S3MailReader(store).fetch(days=1, now=ARCHIVE_NOW)[0].body == first.body


def test_the_collector_notes_which_pictures_sit_in_the_body() -> None:
    raw = make_mail(
        subject="s",
        frm=MAILBOX,
        to=GROUP,
        message_id="<m@x>",
        body="b",
        sent_at=ARCHIVE_NOW,
        attachments=[("conf.pdf", "application/pdf", b"%PDF")],
    )
    parsed = parse_raw(raw)
    assert parsed.attachments[0]["inline"] is False


def test_gmail_messages_read_their_files_fetching_only_those_worth_reading() -> None:
    pdf = pdf_with("Confirmed PU# 6620 for 10/03 at 0800")
    fetched: list[str] = []

    def attachment(attachment_id: str) -> bytes:
        fetched.append(attachment_id)
        return pdf

    def b64(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).decode()

    message = {
        "id": "g-1",
        "threadId": "t-1",
        "internalDate": str(int(NOW.timestamp() * 1000)),
        "payload": {
            "mimeType": "multipart/mixed",
            "headers": [{"name": "Subject", "value": "Your appointment"}],
            "parts": [
                {"mimeType": "text/plain", "body": {"data": b64(b"See attached.")}},
                {
                    "mimeType": "application/pdf",
                    "filename": "conf.pdf",
                    "body": {"attachmentId": "att-1", "size": len(pdf)},
                },
                {
                    "mimeType": "image/png",
                    "filename": "image001.png",
                    "headers": [{"name": "Content-ID", "value": "<logo>"}],
                    "body": {"attachmentId": "att-2", "size": 90_000},
                },
            ],
        },
    }
    inbound = _from_gmail(message, attachment=attachment)
    assert inbound.body == (
        "See attached.\n\n--- Attached file: conf.pdf ---\nConfirmed PU# 6620 for 10/03 at 0800"
    )
    assert fetched == ["att-1"] and inbound.unread_files == ()


def _unrelated(_ctx: Any) -> ReplyClassification:
    return ReplyClassification(status=ReplyStatus.UNRELATED)


def _confirmed(_ctx: Any) -> ReplyClassification:
    return ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["SET!"], confidence=0.9)


def _from_the_pdf(_ctx: Any) -> ReplyClassification:
    return ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="09:00",
        pickup_number="4471",
        quotes=["Confirmed PU# 4471 for 10/01 at 0900"],
        confidence=0.9,
    )


def test_a_confirmation_sent_only_as_a_pdf_is_matched_and_read(settings, sessions) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    pdf_text = (
        "Koch Foods pickup confirmation\nPO 226321092660\nConfirmed PU# 4471 for 10/01 at 0900"
    )
    body, unread = with_attachments("Please see attached.", [Reading("conf.pdf", text=pdf_text)])
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan")
        vendor = InboundMessage(
            **{
                **reply(body, thread=None, subject="Your appointment", mid="pdf-1").__dict__,
                "unread_files": unread,
            }
        )
        stats = ingest(
            session, [vendor], FakeReplyClassifier(_from_the_pdf), internal_domains=INTERNAL
        )
        assert stats.classified == 1  # matched by the PO inside the file
        assert open_kinds(case) == ["confirmation_review"]
        assert case.confirmed_local == "2026-10-01 09:00" and case.pickup_number == "4471"
        assert "--- Attached file: conf.pdf ---" in case.messages[-1].body


def test_a_file_the_agent_could_not_read_is_a_to_do_and_holds_any_booking(
    settings, sessions
) -> None:
    settings = _lidl(settings)
    _scanned(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan")
        body, unread = with_attachments("SET!", [Reading("scan.pdf", why="a scan")])
        vendor = InboundMessage(**{**reply(body, mid="scan-1").__dict__, "unread_files": unread})
        ingest(session, [vendor], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL)
        assert open_kinds(case) == ["confirmation_review", "attachment_unread"]
        assert "scan.pdf: a scan" in case.open_exceptions[-1].description
        doubt = _auto_confirm_doubt(case, settings, now=NOW, issues=[], text=body, strong=True)
        assert doubt == "also open: attachment_unread"
