"""The mail SES saves for a circle-analytics.com test address, read as the board's group mail.

A test address stands in for a customer's group: SES saves each email it receives as the raw
RFC822 in S3, and ``FP_BOOKING_INBOX=ses://bucket/prefix`` reads that folder. There is no Gmail
thread, so a facility's answer is tied to the person's request by the Message-ID it answers.
"""

from __future__ import annotations

from datetime import timedelta

from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.inbox import SesInbox, inbox_from_settings
from facility_profiles.booking.models import PERSON_MAIL, BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.config import Settings
from facility_profiles.mailarchive.reader import SES_SETUP_NOTICE, SesMailReader, raw_to_inbound
from facility_profiles.mailarchive.store import Store
from facility_profiles.storage.db import session_scope
from tests.test_booking_person_requests import ASK, GROUP, PO, SENT, _pickup
from tests.test_mailarchive import FakeS3, make_mail

FOLDER = "lidl-booking-test/group"
DESK = "CCI Desk <cci@udfinc.com>"
PERSON = "Jordan Lake <jordan.lake@circledelivers.com>"
CONFIRMS = "SET! PU# 55501 10/01 @ 0800\n\nThank you,\nCCI desk"


class CountingS3(FakeS3):
    """The fake S3, counting downloads."""

    def __init__(self) -> None:
        super().__init__()
        self.gets = 0

    def get_object(self, Bucket: str, Key: str):  # type: ignore[no-untyped-def]  # noqa: N803
        self.gets += 1
        return super().get_object(Bucket, Key)


def _request() -> bytes:
    return make_mail(
        subject="Pick Up Appointment",
        frm=PERSON,
        to=DESK,
        cc=GROUP,
        message_id="<ask-1@mail.gmail.com>",
        body=ASK,
        sent_at=SENT,
    )


def _confirmation() -> bytes:
    return make_mail(
        subject="RE: Pick Up Appointment",
        frm=DESK,
        to=PERSON,
        cc=GROUP,
        message_id="<0100019a-confirm@email.amazonses.com>",
        in_reply_to="<ask-1@mail.gmail.com>",
        body=f"{CONFIRMS}\n\nOn Tue, Sep 29, 2026, Jordan Lake wrote:\n> {ASK}",
        sent_at=SENT + timedelta(minutes=20),
    )


def test_an_email_ses_saved_reads_like_one_from_the_archive() -> None:
    message = raw_to_inbound(_confirmation(), "abc123")
    assert message is not None
    assert message.from_email == "cci@udfinc.com"
    assert message.thread_id is None  # no Gmail thread: the Message-ID it answers ties it
    assert message.referenced_ids == ["<ask-1@mail.gmail.com>"]
    assert message.body.startswith("SET! PU# 55501 10/01 @ 0800")
    assert "Can I please schedule" not in message.body  # the quoted request is kept apart
    assert message.sent_at == SENT + timedelta(minutes=20)
    assert raw_to_inbound(b"not an email at all", "x") is None


def test_the_reader_skips_the_setup_notice_and_old_mail_and_downloads_each_email_once() -> None:
    s3 = CountingS3()
    s3.objects[f"{FOLDER}/{SES_SETUP_NOTICE}"] = b"Subject: Amazon SES Setup Notification\n\nhi"
    s3.objects[f"{FOLDER}/req1"] = _request()
    s3.objects[f"{FOLDER}/conf1"] = _confirmation()
    s3.objects[f"{FOLDER}/old1"] = make_mail(
        subject="old",
        frm=DESK,
        to=PERSON,
        message_id="<old@x>",
        body="old",
        sent_at=SENT - timedelta(days=10),
    )
    reader = SesMailReader(Store("trucklists", FOLDER, client=s3))
    first = reader.fetch(days=2, now=SENT + timedelta(hours=1))
    assert [m.subject for m in first] == ["Pick Up Appointment", "RE: Pick Up Appointment"]
    downloads = s3.gets
    assert downloads == 3  # the notice is never fetched
    assert reader.fetch(days=2, now=SENT + timedelta(hours=1)) == first
    assert s3.gets == downloads


def test_the_inbox_setting_names_a_test_address_folder(settings: Settings) -> None:
    chosen = settings.model_copy(update={"booking_inbox": "ses://trucklists/" + FOLDER + "/"})
    assert inbox_from_settings(chosen) == SesInbox("trucklists", FOLDER, 2)


def test_a_persons_request_and_the_facilitys_answer_through_ses_reach_the_pickup(
    settings: Settings, sessions
) -> None:
    settings, case_id = _pickup(settings, sessions)
    s3 = FakeS3()
    s3.objects[f"{FOLDER}/req1"] = _request()
    s3.objects[f"{FOLDER}/conf1"] = _confirmation()
    mail = SesMailReader(Store("trucklists", FOLDER, client=s3)).fetch(
        days=2, now=SENT + timedelta(hours=1)
    )
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="08:00",
        pickup_number="55501",
        quotes=["SET! PU# 55501 10/01 @ 0800"],
        confidence=0.95,
    )
    with session_scope(sessions) as s:
        stats = ingest(
            s,
            mail,
            FakeReplyClassifier(lambda _c: reading),  # type: ignore[arg-type,return-value]
            internal_domains=["circledelivers.com"],
            settings=settings,
        )
        assert stats.by_person == 1
        case = s.get(BookingCase, case_id)
        assert case is not None
        kinds = [m.kind for m in case.messages]
        assert kinds == [PERSON_MAIL, "reply"]  # the answer was matched to the person's request
        assert case.requested_local == "2026-10-01 08:00"
        assert case.status == CaseStatus.PENDING.value  # Lidl's rule: a person approves it
        assert ExceptionType.CONFIRMATION_REVIEW.value in open_kinds(case)
        assert PO in case.po_numbers
