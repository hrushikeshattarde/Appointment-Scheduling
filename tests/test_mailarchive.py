"""The group-mail archive: filters, one collection pass, idempotence, backfill, the S3 reader."""

from __future__ import annotations

import base64
import io
import json
import re
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from pathlib import Path
from typing import Any

import pytest
from botocore.exceptions import ClientError
from typer.testing import CliRunner

from facility_profiles.cli import app
from facility_profiles.customers import built_in_customers
from facility_profiles.mailarchive import filters
from facility_profiles.mailarchive.collector import Stats, default_query, parse_raw
from facility_profiles.mailarchive.collector import run as collector_run
from facility_profiles.mailarchive.gmail import (
    METADATA_HEADERS,
    headers_of,
    internal_date_iso,
    load_service_account,
)
from facility_profiles.mailarchive.reader import S3MailReader, day_prefixes
from facility_profiles.mailarchive.store import (
    LAST_RUN_KEY,
    THREADS_KEY,
    Store,
    attachment_key,
    mail_base,
    message_key,
)

MEGAN = "Megan Goodwin <megan.goodwin@circledelivers.com>"
DESK = "Morgan Foods Appointments <shipping.appointments@morganfoods.com>"
GROUP = "Lidl Group <Lidl@circledelivers.com>"
NOW = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
# Lidl's group, subjects and desks, from its customer file.
LIDL = filters.rules_for(built_in_customers().get("lidl"))


def run(gmail: Any, store: Store, **kwargs: Any) -> Stats:
    """One pass over the lidl@ group with Lidl's rules."""
    return collector_run(gmail, store, rules=LIDL, **kwargs)


# ------------------------------------------------------------------------------- fakes ----


class FakeS3:
    """Enough of the S3 client for the store: objects by key, 404s as ClientError."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.extra: dict[str, dict[str, Any]] = {}

    def put_object(self, Bucket: str, Key: str, Body: bytes, **kw: Any) -> dict[str, Any]:  # noqa: N803
        self.objects[Key] = Body
        self.extra[Key] = kw
        return {"ETag": "x"}

    def head_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "404", "Message": "Not Found"}}, "HeadObject")
        return {"ContentLength": len(self.objects[Key])}

    def get_object(self, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "missing"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key])}

    def delete_object(self, Bucket: str, Key: str) -> None:  # noqa: N803
        self.objects.pop(Key, None)

    def list_objects_v2(self, Bucket: str, Prefix: str, **kw: Any) -> dict[str, Any]:  # noqa: N803
        keys = sorted(k for k in self.objects if k.startswith(Prefix))
        return {"Contents": [{"Key": k} for k in keys], "IsTruncated": False}


class FakeGmail:
    """A mailbox of raw messages; search lists the ones inside the window, newest first."""

    def __init__(self) -> None:
        self.msgs: dict[str, dict[str, Any]] = {}
        self.calls: list[str] = []
        self.fail_raw_for: set[str] = set()

    def add(
        self,
        gmail_id: str,
        raw: bytes,
        *,
        thread_id: str,
        sent_at: datetime,
        in_window: bool = True,
    ) -> None:
        self.msgs[gmail_id] = {
            "raw": raw,
            "threadId": thread_id,
            "internalDate": str(int(sent_at.timestamp() * 1000)),
            "in_window": in_window,
        }

    def _meta(self, gmail_id: str) -> dict[str, Any]:
        m = self.msgs[gmail_id]
        parsed = parse_raw(m["raw"])
        wanted = {h.lower() for h in METADATA_HEADERS}
        return {
            "id": gmail_id,
            "threadId": m["threadId"],
            "internalDate": m["internalDate"],
            "payload": {
                "headers": [
                    {"name": k, "value": v} for k, v in parsed.headers.items() if k in wanted
                ]
            },
        }

    def search(self, query: str, cap: int = 2000) -> list[dict[str, str]]:
        self.calls.append(f"search {query}")
        listed = [(i, m) for i, m in self.msgs.items() if m["in_window"]]
        listed.sort(key=lambda im: -int(im[1]["internalDate"]))
        return [{"id": i, "threadId": m["threadId"]} for i, m in listed][:cap]

    def message(self, message_id: str, fmt: str = "raw") -> dict[str, Any]:
        self.calls.append(f"message {message_id} {fmt}")
        if fmt == "metadata":
            return self._meta(message_id)
        if message_id in self.fail_raw_for:
            raise RuntimeError("gmail 503")
        m = self.msgs[message_id]
        return {
            "id": message_id,
            "threadId": m["threadId"],
            "internalDate": m["internalDate"],
            "labelIds": ["INBOX"],
            "raw": base64.urlsafe_b64encode(m["raw"]).decode("ascii").rstrip("="),
        }

    def thread(self, thread_id: str) -> dict[str, Any]:
        self.calls.append(f"thread {thread_id}")
        return {
            "id": thread_id,
            "messages": [self._meta(i) for i, m in self.msgs.items() if m["threadId"] == thread_id],
        }


def make_mail(
    *,
    subject: str,
    frm: str,
    to: str,
    cc: str = "",
    message_id: str,
    in_reply_to: str | None = None,
    body: str,
    sent_at: datetime,
    attachments: list[tuple[str, str, bytes]] | None = None,
) -> bytes:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = frm
    msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Date"] = format_datetime(sent_at)
    msg["Message-ID"] = message_id
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        msg["References"] = in_reply_to
    msg.set_content(body)
    for filename, mime, data in attachments or []:
        maintype, subtype = mime.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)
    return bytes(msg)


def seeded_mailbox() -> FakeGmail:
    gm = FakeGmail()
    t = NOW - timedelta(days=6)
    # T1: the pod's request (outside the search window), the desk's answer with two attachments,
    # and the pod's "Thank you!" (both inside the window).
    gm.add(
        "g1",
        make_mail(
            subject="Pick Up Appointments",
            frm=MEGAN,
            to=f"{DESK}, {GROUP}",
            message_id="<req-1@mail.gmail.com>",
            body="Hello,\n\nCan I please schedule the following?\n\nPO# 115802102660 & 115802102661 (all in one truck) on 10/01 @ 0900\n\nThank you!",
            sent_at=t,
        ),
        thread_id="T1",
        sent_at=t,
        in_window=False,
    )
    gm.add(
        "g2",
        make_mail(
            subject="Re: Pick Up Appointments",
            frm=DESK,
            to=MEGAN,
            cc=GROUP,
            message_id="<conf-1@outlook.com>",
            in_reply_to="<req-1@mail.gmail.com>",
            body=(
                "115802102660 & 115802102661-10/5 @ 9am pickup# 20463798\n\nThanks\nVera\n\n"
                "________________________________\nFrom: Megan Goodwin <megan.goodwin@circledelivers.com>\n"
                "Sent: Wednesday, September 23, 2026 3:54 PM\nSubject: Pick Up Appointments\n\n"
                "Hello,\n\nCan I please schedule the following?\n\nPO# 115802102660 on 10/01 @ 0900"
            ),
            sent_at=NOW - timedelta(days=2),
            attachments=[
                ("image001.png", "image/png", b"\x89PNG" + b"\x00" * 40),
                ("bol.pdf", "application/pdf", b"%PDF-1.4 fake"),
            ],
        ),
        thread_id="T1",
        sent_at=NOW - timedelta(days=2),
    )
    gm.add(
        "g3",
        make_mail(
            subject="Re: Pick Up Appointments",
            frm=MEGAN,
            to=DESK,
            cc=GROUP,
            message_id="<thanks-1@mail.gmail.com>",
            in_reply_to="<conf-1@outlook.com>",
            body="Thank you!\n\nOn Mon, Sep 28, 2026 at 8:35 AM Morgan Foods Appointments wrote:\n> 115802102660 & 115802102661-10/5 @ 9am pickup# 20463798",
            sent_at=NOW - timedelta(days=2, hours=-1),
        ),
        thread_id="T1",
        sent_at=NOW - timedelta(days=2, hours=-1),
    )
    # T2: tour planning, never about booking.
    gm.add(
        "g4",
        make_mail(
            subject="CIR Capacity 9/29, DD 9/30",
            frm="Whitney Pantella <whitney.pantella@lidl.us>",
            to="Michael Perez <mpdenterprise01@gmail.com>",
            cc=GROUP,
            message_id="<tour-1@lidl.us>",
            body="Tour 2609291170 stores 1560, 8025 ...",
            sent_at=NOW - timedelta(days=1),
        ),
        thread_id="T2",
        sent_at=NOW - timedelta(days=1),
    )
    # T3: the inbound desk's PO-number subject with a delivery slot.
    gm.add(
        "g5",
        make_mail(
            subject="Re: 115802102660 & 115802102661",
            frm='"inbound @lidl.us" <inbound@lidl.us>',
            to=MEGAN,
            cc=GROUP,
            message_id="<desk-1@lidl.us>",
            body="What caused the driver to miss delivery? New info below:\n9/30 at 1100\nPYE_300926723\n\nBest Regards,",
            sent_at=NOW - timedelta(hours=20),
        ),
        thread_id="T3",
        sent_at=NOW - timedelta(hours=20),
    )
    # T4: Emerge marketplace notice, dropped.
    gm.add(
        "g6",
        make_mail(
            subject="New Quote Request from Lidl for Quote Q114266889",
            frm="'Zachary Pease' via Lidl Group <Lidl@circledelivers.com>",
            to="Megan Goodwin <lidl@circledelivers.com>",
            message_id="<emerge-1@emergemarket.io>",
            body="A new quote request ...",
            sent_at=NOW - timedelta(hours=10),
        ),
        thread_id="T4",
        sent_at=NOW - timedelta(hours=10),
    )
    # T5: a bare subject from a known vendor desk, and an older nudge in the same thread from the
    # pod to the group alone (no desk in its addresses: only thread membership can keep it).
    gm.add(
        "g7a",
        make_mail(
            subject="Re: hello",
            frm=MEGAN,
            to=GROUP,
            message_id="<polar-0@mail.gmail.com>",
            body="Following up on this.",
            sent_at=NOW - timedelta(days=5),
        ),
        thread_id="T5",
        sent_at=NOW - timedelta(days=5),
        in_window=False,
    )
    gm.add(
        "g7",
        make_mail(
            subject="Re: hello",
            frm="Polar CS <csrpolarbev@polarbev.com>",
            to=MEGAN,
            cc=GROUP,
            message_id="<polar-1@polarbev.com>",
            body="PO 118830092663 can load 09/30. PU# 4119085",
            sent_at=NOW - timedelta(hours=5),
        ),
        thread_id="T5",
        sent_at=NOW - timedelta(hours=5),
    )
    return gm


# ------------------------------------------------------------------------------ filters ----


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("Pick Up Appointment: 115824092601", "subject:pickup-appointment"),
        ("Re: Pick Up Appointments", "subject:pickup-appointment"),
        ("Lidl Pick Up Appointment // PO# 115825092601", "subject:pickup-appointment"),
        ("Re: Lidl Pick Ups", "subject:lidl-pickups"),
        ("115802102660 & 115802102661", "subject:po-numbers"),
        ("Re: 200527082601 & 288726082601", "subject:po-numbers"),
        ("226321092660 MISSED PICK UP", "subject:po-incident"),
        ("RESCHEDULE 226321092660", "subject:po-incident"),
        ("Appointment Shipment Change: 5847944", "subject:portal-appointment"),
        ("DCT Transporter Too Late", "subject:dct-portal"),
        ("Fwd: SET! PU# 4119085", "subject:pickup-number"),
        ("CIR Capacity 9/29, DD 9/30", None),
        ("CRL Daily Recap 09/30/26", None),
        ("New Tender Request from Lidl for Tender S114250973", None),
        ("Tracking Requested by Lidl for Shipment S114250954", None),
        ("LIDL US - Incumbent Lanes for upcoming RFQ", None),
        ("Re: hello", None),
    ],
)
def test_subject_rules_from_the_archive(subject: str, expected: str | None) -> None:
    assert filters.match_reason(subject, set(), LIDL) == expected


def test_only_the_shared_subject_rules_apply_without_a_customer() -> None:
    assert filters.match_reason("Pick Up Appointment: 115824092601", set()) == (
        "subject:pickup-appointment"
    )
    assert filters.match_reason("RESCHEDULE 226321092660", set()) == "subject:reschedule"
    # Lidl's own subjects, PO shapes, portal and tour planning are Lidl's alone.
    for subject in ("Re: Lidl Pick Ups", "115802102660 & 115802102661", "DCT Transporter Too Late"):
        assert filters.match_reason(subject, set()) is None
    assert filters.subject_reason("CIR Capacity 9/29, DD 9/30") is None
    assert not filters.is_dropped("CIR Capacity 9/29, DD 9/30")
    assert filters.is_dropped("CIR Capacity 9/29, DD 9/30", LIDL)
    assert filters.is_dropped("New Tender Request from Acme for Tender S1")


def test_desk_participants_keep_a_bare_subject_but_never_a_dropped_one() -> None:
    addrs = filters.participants("Polar CS <csrpolarbev@polarbev.com>", MEGAN, GROUP)
    assert filters.match_reason("Re: hello", addrs, LIDL) == "desk:csrpolarbev@polarbev.com"
    assert filters.match_reason("CIR Capacity 9/29", {"inbound@lidl.us"}, LIDL) is None
    assert (
        filters.match_reason("Re: x", {"noreply@softhouse.nl"}, LIDL) == "desk:noreply@softhouse.nl"
    )
    assert filters.match_reason("Re: x", {"noreply@softhouse.nl"}) is None  # Lidl's portal only
    assert filters.match_reason("Re: x", {"noreply@opendock.com"}) == "desk:noreply@opendock.com"
    assert (
        filters.match_reason(
            "Re: x", {"someone@example.com"}, LIDL.with_desks(["Someone@Example.com"])
        )
        == "desk:someone@example.com"
    )


def test_identifiers_find_po_dct_and_pickup_numbers() -> None:
    texts = (
        "Pick Up Appointment: 115802102660",
        "PO# 115802102660 & 115802102661 on 10/05 @ 0900\n10/5 @ 9am pickup# 20463798\n"
        "New Appointment: PYE_061026919\nSET! PU# 4119085\nCCI pickup number CCI-9389",
    )
    found = filters.identifiers(*texts, rules=LIDL)
    assert found["po_numbers"] == ["115802102660", "115802102661"]
    assert found["delivery_refs"] == ["PYE_061026919"]
    assert found["pickup_numbers"] == ["20463798", "4119085", "CCI-9389"]
    # Without a customer file nothing says what a PO or a delivery reference looks like.
    bare = filters.identifiers(*texts)
    assert bare["po_numbers"] == [] and bare["delivery_refs"] == []
    assert bare["pickup_numbers"] == found["pickup_numbers"]


# ------------------------------------------------------------------------------- keys ----


def test_message_key_is_stable_across_mailboxes_and_falls_back_to_the_gmail_id() -> None:
    assert message_key("<abc@x.com>", "g1") == message_key(" <ABC@x.com> ", "g2")
    assert message_key(None, "g1") == "g-g1"
    assert mail_base("k", "2026-09-30T12:00:00+00:00") == "mail/2026/09/30/k"
    assert mail_base("k", None) == "mail/unknown/k"
    assert attachment_key("abcdef") == "attachments/ab/abcdef"
    assert default_query("lidl@circledelivers.com", 3).endswith("newer_than:3d")
    assert "to:lidl@circledelivers.com" in default_query("lidl@circledelivers.com", 3)


def test_gmail_helpers(tmp_path: Path) -> None:
    meta = {
        "internalDate": "1790000000000",
        "payload": {"headers": [{"name": "Subject", "value": "X"}]},
    }
    assert headers_of(meta) == {"subject": "X"}
    assert internal_date_iso(meta) == datetime.fromtimestamp(1790000000, tz=UTC).isoformat(
        timespec="seconds"
    )
    assert internal_date_iso({}) is None
    key = tmp_path / "sa.json"
    key.write_text(json.dumps({"client_email": "a@b", "private_key": "k"}), encoding="utf-8")
    assert load_service_account(key)["client_email"] == "a@b"
    key.write_text(json.dumps({"client_email": "a@b"}), encoding="utf-8")
    with pytest.raises(ValueError, match="missing"):
        load_service_account(key)


# --------------------------------------------------------------------------- collection ----


def test_one_pass_keeps_booking_threads_backfills_and_is_idempotent() -> None:
    gm = seeded_mailbox()
    s3 = FakeS3()
    store = Store("bucket", "lidl/", client=s3)
    assert store.writable()[0]

    st = run(gm, store, mailbox="me@circledelivers.com", days=3)
    # Newest first: g7 (T5) keeps and backfills g7a; g5 (T3) keeps; g3 (T1) keeps and backfills
    # g1 and g2; g2 then comes up in the list and is already there; g4 and g6 are dropped.
    assert st.listed == 6 and st.not_booking == 2 and st.stored == 6 and st.backfilled == 3
    assert st.already == 1 and st.threads_new == 3 and st.error == "" and st.deferred == 0
    assert st.attachments_stored == 2 and st.attachments_already == 0
    assert dict(st.reasons) == {
        "subject:pickup-appointment": 3,
        "thread": 1,
        "subject:po-numbers": 1,
        "desk:csrpolarbev@polarbev.com": 1,
    }

    threads = json.loads(s3.objects["lidl/" + THREADS_KEY])
    assert set(threads) == {"T1", "T3", "T5"} and threads["T1"] == "subject:pickup-appointment"
    last = json.loads(s3.objects["lidl/" + LAST_RUN_KEY])
    assert last["stats"]["stored"] == 6 and "6 stored" in last["line"]

    conf_key = message_key("<conf-1@outlook.com>", "g2")
    day = (NOW - timedelta(days=2)).strftime("%Y/%m/%d")
    env = json.loads(s3.objects[f"lidl/mail/{day}/{conf_key}.json"])
    assert env["matched_by"] == "subject:pickup-appointment" and env["gmail_id"] == "g2"
    assert (
        env["message_id"] == "<conf-1@outlook.com>"
        and env["in_reply_to"] == "<req-1@mail.gmail.com>"
    )
    assert env["own_text"].startswith("115802102660 & 115802102661-10/5 @ 9am pickup# 20463798")
    assert "Can I please schedule" in env["quoted"]
    assert env["identifiers"]["pickup_numbers"] == ["20463798"]
    assert env["identifiers"]["po_numbers"] == ["115802102660", "115802102661"]
    assert [a["filename"] for a in env["attachments"]] == ["image001.png", "bol.pdf"]
    for a in env["attachments"]:
        assert a["key"] in s3.objects and len(s3.objects[a["key"]]) == a["bytes"]
    raw = s3.objects[f"lidl/mail/{day}/{conf_key}.eml"]
    assert (
        raw.startswith(b"Subject: Re: Pick Up Appointments")
        and s3.extra[f"lidl/mail/{day}/{conf_key}.eml"]["ContentType"] == "message/rfc822"
    )

    # The request that arrived before the collector existed came along with its thread, under
    # its own reason; the bare nudge in the Polar thread is kept for the thread it belongs to.
    req_key = message_key("<req-1@mail.gmail.com>", "g1")
    req_day = (NOW - timedelta(days=6)).strftime("%Y/%m/%d")
    req = json.loads(s3.objects[f"lidl/mail/{req_day}/{req_key}.json"])
    assert req["matched_by"] == "subject:pickup-appointment" and req["gmail_id"] == "g1"
    nudge_key = message_key("<polar-0@mail.gmail.com>", "g7a")
    nudge_day = (NOW - timedelta(days=5)).strftime("%Y/%m/%d")
    nudge = json.loads(s3.objects[f"lidl/mail/{nudge_day}/{nudge_key}.json"])
    assert nudge["matched_by"] == "thread:desk:csrpolarbev@polarbev.com"

    # A second pass stores nothing and touches no attachment.
    before = dict(s3.objects)
    again = run(gm, store, mailbox="me@circledelivers.com", days=3)
    assert again.stored == 0 and again.already == 4 and again.not_booking == 2
    assert again.attachments_stored == 0 and again.attachments_already == 0
    changed = {k for k in s3.objects if s3.objects[k] != before.get(k)}
    assert changed == {"lidl/" + LAST_RUN_KEY}


def test_a_later_message_in_a_kept_thread_is_kept_by_thread_membership() -> None:
    gm = seeded_mailbox()
    s3 = FakeS3()
    store = Store("bucket", client=s3)
    run(gm, store, mailbox="me")
    later = NOW - timedelta(hours=1)
    # Pod to the group only: no desk among the addresses, a bare subject, so only the thread keeps it.
    gm.add(
        "g8",
        make_mail(
            subject="Re: hello",
            frm=MEGAN,
            to=GROUP,
            message_id="<polar-2@mail.gmail.com>",
            body="Yes that works!",
            sent_at=later,
        ),
        thread_id="T5",
        sent_at=later,
    )
    st = run(gm, store, mailbox="me")
    assert st.stored == 1 and st.threads_new == 0 and st.backfilled == 0
    key = message_key("<polar-2@mail.gmail.com>", "g8")
    env = json.loads(s3.objects[f"mail/{later:%Y/%m/%d}/{key}.json"])
    assert env["matched_by"] == "thread:desk:csrpolarbev@polarbev.com"


def test_same_message_from_a_second_mailbox_is_one_object() -> None:
    gm = seeded_mailbox()
    s3 = FakeS3()
    store = Store("bucket", client=s3)
    run(gm, store, mailbox="me")
    other = FakeGmail()
    other.add("z9", gm.msgs["g5"]["raw"], thread_id="Z", sent_at=NOW - timedelta(hours=20))
    st = run(other, store, mailbox="megan")
    assert st.listed == 1 and st.already == 1 and st.stored == 0


def test_cap_deadline_and_errors_leave_the_rest_for_the_next_pass() -> None:
    gm = seeded_mailbox()
    s3 = FakeS3()
    store = Store("bucket", client=s3)
    st = run(gm, store, mailbox="me", max_messages=1)
    assert st.deferred == 5 and st.stopped_by == "message cap"

    gm.fail_raw_for.add("g5")
    st = run(gm, store, mailbox="me")
    assert st.error.startswith("g5: RuntimeError") and st.stopped_by == "error" and st.deferred >= 1
    last = json.loads(s3.objects[LAST_RUN_KEY])
    assert "STOPPED ON ERROR" in last["line"]

    line = Stats(listed=3, deferred=2, stopped_by="time limit").line()
    assert "2 left for the next run (time limit)" in line


def test_parse_raw_falls_back_to_html_and_keeps_inline_images() -> None:
    msg = EmailMessage()
    msg["Subject"] = "Re: x"
    msg["From"] = DESK
    msg.set_content(
        "<html><body><p>Set for 9/16 @ 1700</p><div>Thanks</div></body></html>", subtype="html"
    )
    msg.add_related(b"\x89PNG" + b"\x00" * 10, maintype="image", subtype="png", filename="sig.png")
    parsed = parse_raw(bytes(msg))
    assert parsed.text.startswith("Set for 9/16 @ 1700") and "Thanks" in parsed.text
    assert [a["filename"] for a in parsed.attachments] == ["sig.png"]
    assert parsed.headers["subject"] == "Re: x"


# ------------------------------------------------------------------------------- reader ----


def test_reader_returns_inbound_messages_for_the_window() -> None:
    gm = seeded_mailbox()
    s3 = FakeS3()
    store = Store("bucket", "lidl", client=s3)
    run(gm, store, mailbox="me")
    assert day_prefixes(days=2, now=NOW) == ["mail/2026/09/29/", "mail/2026/09/30/"]
    messages = S3MailReader(store).fetch(days=3, now=NOW)
    assert [m.subject for m in messages] == [
        "Re: Pick Up Appointments",
        "Re: Pick Up Appointments",
        "Re: 115802102660 & 115802102661",
        "Re: hello",
    ]
    conf = messages[0]
    assert conf.message_id == message_key("<conf-1@outlook.com>", "g2")
    assert (
        conf.rfc_message_id == "<conf-1@outlook.com>"
        and conf.in_reply_to == "<req-1@mail.gmail.com>"
    )
    assert conf.from_email == "shipping.appointments@morganfoods.com" and conf.thread_id == "T1"
    assert (
        conf.body.startswith("115802102660 & 115802102661-10/5") and "Can I please" in conf.quoted
    )
    # The older request and nudge are outside a three-day window but inside a ten-day one.
    assert len(S3MailReader(store).fetch(days=10, now=NOW)) == 6


def test_store_reads_and_lists() -> None:
    s3 = FakeS3()
    store = Store("b", "p/", client=s3)
    assert store.get_json("state/nothing.json") is None
    store.put_json("state/x.json", {"a": 1})
    assert store.get_json("state/x.json") == {"a": 1}
    assert store.list_keys("state/") == ["state/x.json"]
    first = store.put("k", b"one")
    second = store.put("k", b"two")
    assert not first.skipped and second.skipped and store.get("k") == b"one"
    with pytest.raises(ValueError, match="bucket"):
        Store("", client=s3)


def test_mail_archive_cli_help(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    # CI runners report a colour-capable terminal; keep the help text plain and wide.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    from facility_profiles.config import get_settings

    get_settings.cache_clear()
    runner = CliRunner()
    try:
        assert runner.invoke(app, ["mail-archive", "--help"]).exit_code == 0
        assert runner.invoke(app, ["mail-archive", "collect", "--help"]).exit_code == 0
        result = runner.invoke(app, ["mail-archive", "status"])
        assert result.exit_code == 2 and "FP_MAIL_ARCHIVE_BUCKET" in result.output
        key = tmp_path / "sa.json"
        key.write_text("{}", encoding="utf-8")
        result = runner.invoke(app, ["mail-archive", "collect", "--key", str(key)])
        assert result.exit_code == 2 and "--bucket" in result.output
        help_text = runner.invoke(app, ["booking", "inbox", "--help"]).output
        assert "--s3" in re.sub(r"\x1b\[[0-9;]*m", "", help_text), help_text
    finally:
        get_settings.cache_clear()
