"""Read the archive back as the booking agent's :class:`InboundMessage` objects.

Attached files are read too (``booking/attachments.py``): the collector stores each one once by
its content, and the text of those worth reading goes under the email's own words. A file's text
is kept in memory by its content hash, so a board that reads the last days' mail every few
minutes fetches each file once.
"""

from __future__ import annotations

from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from typing import Any

from facility_profiles.booking.attachments import (
    READ,
    Attachment,
    Reading,
    read,
    triage,
    with_attachments,
)
from facility_profiles.booking.mail import InboundMessage, tidy_text
from facility_profiles.mailarchive.store import MAIL_PREFIX, Store, attachment_key

_CACHE_SIZE = 512
# What each attached file said, by its sha256; None for a file not worth reading.
_READINGS: OrderedDict[str, Reading | None] = OrderedDict()


def day_prefixes(*, days: int, now: datetime | None = None) -> list[str]:
    """``mail/yyyy/mm/dd/`` for today and the ``days - 1`` days before it."""
    today = (now or datetime.now(tz=UTC)).date()
    return [
        f"{MAIL_PREFIX}/{d:%Y/%m/%d}/"
        for d in (today - timedelta(days=i) for i in range(max(1, days) - 1, -1, -1))
    ]


def to_inbound(
    envelope: dict[str, Any], key: str, readings: list[Reading] | None = None
) -> InboundMessage:
    """One archived envelope as the agent sees it, with the text of its attached files."""
    sent = envelope.get("internal_date") or envelope.get("date") or ""
    try:
        sent_at = datetime.fromisoformat(str(sent))
    except ValueError:
        sent_at = datetime.now(tz=UTC)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    # Envelopes written before the collector decoded HTML codes are tidied here too.
    own = tidy_text(str(envelope.get("own_text") or ""))
    body, unread = with_attachments(own, readings or [])
    return InboundMessage(
        message_id=str(envelope.get("key") or key),
        thread_id=str(envelope.get("thread_id") or "") or None,
        sent_at=sent_at,
        from_addr=str(envelope.get("from") or ""),
        to_addr=str(envelope.get("to") or ""),
        cc_addr=str(envelope.get("cc") or ""),
        subject=str(envelope.get("subject") or ""),
        body=body,
        in_reply_to=envelope.get("in_reply_to") or None,
        quoted=tidy_text(str(envelope.get("quoted") or "")),
        rfc_message_id=envelope.get("message_id") or None,
        references=envelope.get("references") or None,
        unread_files=unread,
        auto_submitted=envelope.get("auto_submitted") or None,
    )


def attachment_readings(store: Store, envelope: dict[str, Any]) -> list[Reading]:
    """What each file in the envelope's manifest says; only files worth reading are fetched.

    An envelope written before the collector noted which parts were inline treats a picture as
    inline: a logo in a signature, not a confirmation.
    """
    out: list[Reading] = []
    for item in envelope.get("attachments") or []:
        if not isinstance(item, dict):
            continue
        sha = str(item.get("sha256") or "")
        name = str(item.get("filename") or "") or "attachment"
        mime = str(item.get("mime") or "")
        inline = bool(item.get("inline", str(mime).startswith("image/")))
        if sha in _READINGS:
            _READINGS.move_to_end(sha)
            cached = _READINGS[sha]
            if cached is not None:
                out.append(Reading(name, text=cached.text, why=cached.why))
            continue
        verdict = triage(name, mime, int(item.get("bytes") or 0), inline=inline)
        if verdict is None or not sha:
            reading: Reading | None = None
        elif verdict != READ:
            reading = Reading(name, why=verdict)
        else:
            reading = read(Attachment(name, mime, store.get(attachment_key(sha)), inline=inline))
        if sha:
            _READINGS[sha] = reading
            while len(_READINGS) > _CACHE_SIZE:
                _READINGS.popitem(last=False)
        if reading is not None:
            out.append(reading)
    return out


class S3MailReader:
    """Messages archived in the last ``days`` days, oldest first."""

    def __init__(self, store: Store) -> None:
        self.store = store

    def fetch(self, *, days: int = 7, now: datetime | None = None) -> list[InboundMessage]:
        """Every ``.json`` envelope under the day prefixes, as inbound messages."""
        out: list[InboundMessage] = []
        for prefix in day_prefixes(days=days, now=now):
            for key in self.store.list_keys(prefix):
                if not key.endswith(".json"):
                    continue
                envelope = self.store.get_json(key)
                if isinstance(envelope, dict):
                    readings = attachment_readings(self.store, envelope)
                    out.append(to_inbound(envelope, key, readings))
        return sorted(out, key=lambda m: m.sent_at)
