"""Gaps 16 to 21 of the email edge-case review, closed one by one.

16 HTML codes are decoded, 17 a signature or a phone number backs no pickup number, 18 bounces,
out-of-office replies and delays are recognised, 19 an email that keeps failing is kept for a
person before it is lost, 20 an email with no Message-ID read from two mailboxes is read once,
21 a written date must match by month as well as day.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from facility_profiles.booking.automated import (
    AUTO_REPLY,
    BOUNCE,
    DELAYED,
    automated_kind,
    bounce_details,
    bounced_ids,
)
from facility_profiles.booking.automation import read_mail
from facility_profiles.booking.classify import (
    FakeReplyClassifier,
    ReplyContext,
    date_is_backed,
    validate_classification,
)
from facility_profiles.booking.inbox import MergedInbox
from facility_profiles.booking.mail import (
    InboundMessage,
    RecordingMailer,
    RecordingSender,
    gmail_body_text,
    tidy_text,
)
from facility_profiles.booking.models import BookingCase, UnmatchedMail
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_case, ingest, list_cases
from facility_profiles.booking.timers import waiting_since
from facility_profiles.booking.unmatched import keep_unmatched, leaving_soon
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.customers import customers
from facility_profiles.mailarchive.collector import parse_raw
from facility_profiles.mailarchive.reader import S3MailReader
from facility_profiles.mailarchive.store import Store
from facility_profiles.storage.db import session_scope
from tests.test_booking import NOW, reply
from tests.test_booking_respond import _prepared_case
from tests.test_booking_send import DESK, INTERNAL, _scanned, _send_settings
from tests.test_mailarchive import NOW as ARCHIVE_NOW
from tests.test_mailarchive import FakeS3

NBSP = chr(0xA0)
ZERO_WIDTH = chr(0x200B)


def _never(_ctx: ReplyContext) -> ReplyClassification:
    msg = "the model must not be asked about mail no person wrote"
    raise AssertionError(msg)


def _confirmed(_ctx: Any) -> ReplyClassification:
    return ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["SET!"], confidence=0.9)


def _mail(body: str, **fields: Any) -> InboundMessage:
    """A message from the Koch desk; any field can be swapped."""
    return InboundMessage(**{**reply(body, thread=None, mid="m-1").__dict__, **fields})


def _sent_case(settings: Settings, sessions) -> tuple[int, str]:  # type: ignore[no-untyped-def]
    """One request sent by the agent; returns (case id, its Message-ID)."""
    send = _send_settings(settings)
    _scanned(send, sessions, "226321092660")
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        message = draft_case(session, case, RecordingSender(), send, now=NOW)
        return case.id, message.rfc_message_id or ""


# ------------------------------------------------------------------ 16. HTML codes


def test_html_codes_and_odd_spaces_are_made_plain_from_every_source() -> None:
    assert tidy_text(f"Set for 9/16&nbsp;@ 1700{NBSP}ET") == "Set for 9/16 @ 1700 ET"
    assert tidy_text(f"Smith &amp; Sons{ZERO_WIDTH} &#39;OK&#39;") == "Smith & Sons 'OK'"
    assert tidy_text("AT&T and R&D stay as written") == "AT&T and R&D stay as written"
    # Gmail, HTML only.
    html = "<p>Set for 9/16&nbsp;@ 1700</p><p>Thanks &amp; regards</p>"
    payload = {"mimeType": "text/html", "body": {"data": _b64(html)}}
    assert gmail_body_text(payload).split() == [
        "Set",
        "for",
        "9/16",
        "@",
        "1700",
        "Thanks",
        "&",
        "regards",
    ]
    # The archive collector, HTML only.
    raw = (
        b"From: desk@vendor.example\r\nTo: lidl@circledelivers.com\r\nSubject: s\r\n"
        b"Content-Type: text/html\r\n\r\n<div>PU#&nbsp;4471 &amp; dock 4</div>"
    )
    assert parse_raw(raw).text == "PU# 4471 & dock 4"


def test_envelopes_archived_before_the_fix_are_decoded_when_read() -> None:
    s3 = FakeS3()
    envelope = {"key": "k1", "internal_date": "2026-09-30T10:00:00+00:00", "subject": "s"}
    envelope |= {"own_text": "SET for 10/01&nbsp;@ 0900", "quoted": "On Mon&nbsp;wrote:"}
    s3.objects["mail/2026/09/30/k1.json"] = json.dumps(envelope).encode()
    message = S3MailReader(Store("bucket", client=s3)).fetch(days=1, now=ARCHIVE_NOW)[0]
    assert message.body == "SET for 10/01 @ 0900" and message.quoted == "On Mon wrote:"


def test_a_quote_matches_text_that_carried_html_codes() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-09-16",
        pickup_time="17:00",
        quotes=["Set for 9/16 @ 1700"],
    )
    result, issues = validate_classification(reading, f"Set for 9/16{NBSP}@ 1700")
    assert issues == [] and result.pickup_time == "17:00"


def _b64(text: str) -> str:
    import base64

    return base64.urlsafe_b64encode(text.encode()).decode()


# ------------------------------------------------------------------ 17. signatures


def _number(body: str, number: str) -> tuple[str | None, list[str]]:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED, pickup_number=number, quotes=["SET!"]
    )
    result, issues = validate_classification(reading, body)
    return result.pickup_number, [i.reason for i in issues]


def test_a_number_from_a_phone_or_the_signature_is_not_a_pickup_number() -> None:
    signed = (
        "SET!\n\nThanks,\nDana Reyes\nShipping Lead\nPhone: 555-010-4477 x12\nCell 555.010.0199"
    )
    assert _number(signed, "555-010-4477") == (
        None,
        ["number is only in a phone number or the signature"],
    )
    assert _number(signed, "555.010.0199")[0] is None
    # A phone number in the reply itself is not a pickup number either.
    assert _number("SET! Call the dock at 555-010-4477 when here", "555-010-4477")[0] is None
    # Signed off, then a reference number in the signature block: not the pickup's.
    assert _number("SET!\n\nRegards,\nDana\nRef 778812", "778812")[0] is None
    # A real pickup number above the signature stands.
    assert _number(f"SET! PU# 4471\n\n{signed.split(chr(10), 2)[2]}", "4471") == ("4471", [])
    # "Thanks" over booking content is not a signature.
    assert _number("SET!\nThanks\nPU# 4471 for 10/01 @ 0900", "4471") == ("4471", [])


def test_the_signature_backs_no_confirmation() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED, pickup_time="07:00", quotes=["Mon-Fri 7am-3pm"]
    )
    body = "We will get back to you.\n\nThanks,\nDana\nShipping hours Mon-Fri 7am-3pm"
    result, issues = validate_classification(reading, body)
    assert result.status == ReplyStatus.UNRELATED and result.pickup_time is None
    assert "quote not found in reply" in [i.reason for i in issues]


# ------------------------------------------------------------------ 18. mail no person wrote


def test_bounces_out_of_office_and_delays_are_told_apart() -> None:
    daemon = "Mail Delivery Subsystem <mailer-daemon@googlemail.com>"
    assert (
        automated_kind(
            _mail("x", from_addr=daemon, subject="Delivery Status Notification (Failure)")
        )
        == BOUNCE
    )
    assert (
        automated_kind(
            _mail(
                "x", from_addr="postmaster@udfinc.com", subject="Undeliverable: Pick Up Appointment"
            )
        )
        == BOUNCE
    )
    assert (
        automated_kind(_mail("x", subject="Undeliverable: Pick Up Appointment: 226321092660"))
        == BOUNCE
    )
    assert (
        automated_kind(_mail("x", from_addr=daemon, subject="Delivery Status Notification (Delay)"))
        == DELAYED
    )
    assert (
        automated_kind(_mail("x", subject="Automatic reply: Pick Up Appointment: 226321092660"))
        == AUTO_REPLY
    )
    assert (
        automated_kind(_mail("I am away", subject="Re: Pick Up", auto_submitted="auto-replied"))
        == AUTO_REPLY
    )
    # A portal's notice is automatic but carries the booking: it is read.
    portal = _mail(
        "Appointment confirmed", subject="Appointment Confirmed", auto_submitted="auto-generated"
    )
    assert automated_kind(portal) is None
    assert automated_kind(_mail("I am out of the office today but PU# 4471 is set")) is None


GOOGLE_BOUNCE = (
    "Address not found\n\nYour message wasn't delivered to cci@udfinc.com because the address "
    "couldn't be found, or is unable to receive mail.\n\nThe response from the remote server "
    "was:\n550 5.1.1 The email account that you tried to reach does not exist."
)


def test_a_bounce_names_the_address_and_the_reason() -> None:
    bounce = _mail(GOOGLE_BOUNCE, quoted="Message-ID: <Lost-7@circledelivers.com>\nSubject: x")
    assert bounce_details(bounce, [DESK]) == (
        DESK,
        "Your message wasn't delivered to cci@udfinc.com because the address couldn't be found, "
        "or is unable to receive mail.",
    )
    assert (
        bounce_details(_mail("Delivery has failed."), [None])[1] == "the mail server sent it back"
    )
    assert bounced_ids(bounce) == ["<lost-7@circledelivers.com>"]


def test_a_bounce_raises_email_did_not_arrive_at_once(settings, sessions) -> None:
    case_id, sent_id = _sent_case(settings, sessions)
    bounce = _mail(
        GOOGLE_BOUNCE,
        from_addr="Mail Delivery Subsystem <mailer-daemon@googlemail.com>",
        to_addr="lidl-appointments@circledelivers.com",
        cc_addr="",
        subject="Delivery Status Notification (Failure)",
        in_reply_to=sent_id,
        references=sent_id,
        rfc_message_id="<dsn-1@mx.google.com>",
    )
    with session_scope(sessions) as session:
        stats = ingest(session, [bounce], FakeReplyClassifier(_never), internal_domains=INTERNAL)
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.automated == 1 and stats.bounced == 1
        assert open_kinds(case) == ["email_bounced"]
        assert case.open_exceptions[0].description.startswith(
            "the email to cci@udfinc.com did not arrive: Your message wasn't delivered"
        )
        assert case.messages[-1].kind == "bounce" and stats.classified == 0


def test_a_bounce_found_only_by_the_headers_it_quotes(settings, sessions) -> None:
    case_id, sent_id = _sent_case(settings, sessions)
    bounce = _mail(
        "Delivery has failed to these recipients: cci@udfinc.com",
        from_addr="postmaster@udfinc.com",
        subject="Undeliverable: hello",
        quoted=f"Message-ID: {sent_id}\nSubject: Pick Up Appointment",
    )
    with session_scope(sessions) as session:
        ingest(session, [bounce], FakeReplyClassifier(_never), internal_domains=INTERNAL)
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["email_bounced"]


def test_an_out_of_office_is_kept_but_the_desk_is_still_silent(settings, sessions) -> None:
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_follow_up_hours": 24}
    )
    mailer = RecordingMailer()
    case_id = _prepared_case(settings, sessions, mailer)
    away = _mail(
        "I am out of the office until 10/05 with limited access to email.",
        subject="Automatic reply: Pick Up Appointment: 226321092660",
        thread_id="t1",
        sent_at=NOW + timedelta(minutes=2),
    )
    with session_scope(sessions) as session:
        stats = ingest(session, [away], FakeReplyClassifier(_never), internal_domains=INTERNAL)
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.automated == 1 and open_kinds(case) == []
        assert case.messages[-1].kind == "auto_reply"
        assert waiting_since(case) is case.messages[0]  # the request is still unanswered
        nudge = Responder(settings, mailer, now=NOW + timedelta(hours=30)).follow_up(session, case)
        assert nudge is not None and nudge.kind == "follow_up"
        assert any(e.action == "auto_reply" for e in case.events)


def test_a_real_reply_after_a_bounce_clears_it(settings, sessions) -> None:
    case_id, sent_id = _sent_case(settings, sessions)
    bounce = _mail(GOOGLE_BOUNCE, from_addr="mailer-daemon@googlemail.com", in_reply_to=sent_id)
    later = _mail(
        "SET!", in_reply_to=sent_id, rfc_message_id="<set-1@udfinc.com>", message_id="m-2"
    )
    with session_scope(sessions) as session:
        ingest(session, [bounce], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL)
        ingest(session, [later], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL)
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == ["confirmation_review"]


# ------------------------------------------------------------------ 19. mail that keeps failing


class Down:
    """The model, unreachable."""

    def classify(self, _context: ReplyContext) -> Any:
        msg = "OpenRouter 503"
        raise RuntimeError(msg)


class Box:
    def __init__(self, messages: list[InboundMessage]) -> None:
        self.messages = messages

    def fetch(self) -> list[InboundMessage]:
        return self.messages


def test_the_last_hours_of_the_look_back_count_as_leaving() -> None:
    now = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
    young = _mail("x", sent_at=now - timedelta(hours=30))
    old = _mail("x", sent_at=now - timedelta(hours=43))
    assert not leaving_soon(young, days=2, now=now) and leaving_soon(old, days=2, now=now)
    assert leaving_soon(_mail("x", sent_at=now - timedelta(hours=19)), days=1, now=now)
    assert not leaving_soon(_mail("x", sent_at=now - timedelta(hours=13)), days=1, now=now)


def test_an_email_that_keeps_failing_is_kept_for_a_person_before_it_is_lost(
    settings, sessions
) -> None:
    case_id, sent_id = _sent_case(settings, sessions)
    now = datetime.now(tz=UTC)
    fresh = _mail("SET!", in_reply_to=sent_id, sent_at=now - timedelta(hours=3), message_id="f-1")
    aging = _mail("SET!", in_reply_to=sent_id, sent_at=now - timedelta(hours=44), message_id="f-2")
    with session_scope(sessions) as session:
        report = read_mail(session, settings, inbox=Box([fresh]), classifier=Down())
        assert report.mail_failed == 1 and session.query(UnmatchedMail).count() == 0
        assert "trying next pass" in report.lines[-1]
        report = read_mail(session, settings, inbox=Box([aging]), classifier=Down())
        kept = session.query(UnmatchedMail).one()
        assert kept.reason == "unreadable: RuntimeError: OpenRouter 503" and kept.status == "open"
        assert report.mail_failed == 1 and "kept for a person" in report.lines[-1]
        # A person has it now: still failing, but no longer a red notice.
        report = read_mail(session, settings, inbox=Box([aging]), classifier=Down())
        assert report.mail_failed == 0 and "a person has it" in report.lines[-1]
        # Once the model is back, the email is read and the kept item links itself.
        read_mail(session, settings, inbox=Box([aging]), classifier=FakeReplyClassifier(_confirmed))
        case = session.get(BookingCase, case_id)
        assert case is not None and case.messages[-1].message_id == "f-2"  # read onto the pickup
        assert kept.status == "linked" and kept.case_id == case_id


# ------------------------------------------------------------------ 20. no Message-ID


def test_an_email_with_no_message_id_from_two_mailboxes_is_read_once(settings, sessions) -> None:
    case_id, _ = _sent_case(settings, sessions)
    archive = _mail(
        "SET!\n\nThanks", subject="RE: Pick Up Appointment: 226321092660", rfc_message_id=None
    )
    mailbox = InboundMessage(
        **{
            **archive.__dict__,
            "message_id": "gmail-77",
            "body": "SET!  Thanks",  # the same words, spaced by another source
            "sent_at": archive.sent_at + timedelta(minutes=2),
        }
    )
    other = InboundMessage(**{**archive.__dict__, "message_id": "m-9", "body": "Not yet."})
    assert [m.message_id for m in MergedInbox([Box([archive]), Box([mailbox, other])]).fetch()] == [
        "m-1",
        "m-9",
    ]
    with session_scope(sessions) as session:
        stats = ingest(
            session, [archive, mailbox], FakeReplyClassifier(_confirmed), internal_domains=INTERNAL
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.duplicates == 1 and stats.classified == 1
        known = customers(settings)
        news = _mail(
            "PU# 9912 is set",
            subject="Friday",
            from_addr="ann@newvendor.example",
            rfc_message_id=None,
        )
        assert keep_unmatched(session, news, known) == "new"
        again = InboundMessage(**{**news.__dict__, "message_id": "gmail-78"})
        assert keep_unmatched(session, again, known) == "known"


# ------------------------------------------------------------------ 21. the month counts


@pytest.mark.parametrize(
    ("quote", "backed"),
    [
        ("Confirmed for 11/01 at 0900", False),
        ("Confirmed for 10/01 at 0900", True),
        ("Set Oct 1st @ 9am", True),
        ("Set 1 November @ 9am", False),
        ("Pickup 2026-10-01 0900", True),
        ("Pickup 11-01-26 0900", False),
        ("115802102660 & 115802102661-10/1 @ 9am", True),
        ("Tomorrow at 9", True),  # nothing written out: a relative day still backs it
        ("the 1st at 9", True),
    ],
)
def test_a_written_date_must_match_by_month(quote: str, backed: bool) -> None:
    assert date_is_backed("2026-10-01", quote) is backed


def test_a_confirmation_for_another_month_is_not_read_as_ours() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="09:00",
        quotes=["Confirmed for 11/01 at 0900"],
    )
    result, issues = validate_classification(reading, "Confirmed for 11/01 at 0900")
    assert result.pickup_date is None
    assert [(i.field_name, i.reason) for i in issues] == [
        ("pickup_date", "no quote backs this value")
    ]
