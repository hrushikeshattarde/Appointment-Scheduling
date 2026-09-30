"""Read the archive back as the booking agent's :class:`InboundMessage` objects."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from facility_profiles.booking.mail import InboundMessage
from facility_profiles.mailarchive.store import MAIL_PREFIX, Store


def day_prefixes(*, days: int, now: datetime | None = None) -> list[str]:
    """``mail/yyyy/mm/dd/`` for today and the ``days - 1`` days before it."""
    today = (now or datetime.now(tz=UTC)).date()
    return [
        f"{MAIL_PREFIX}/{d:%Y/%m/%d}/"
        for d in (today - timedelta(days=i) for i in range(max(1, days) - 1, -1, -1))
    ]


def to_inbound(envelope: dict[str, Any], key: str) -> InboundMessage:
    """One archived envelope as the agent sees it."""
    sent = envelope.get("internal_date") or envelope.get("date") or ""
    try:
        sent_at = datetime.fromisoformat(str(sent))
    except ValueError:
        sent_at = datetime.now(tz=UTC)
    if sent_at.tzinfo is None:
        sent_at = sent_at.replace(tzinfo=UTC)
    return InboundMessage(
        message_id=str(envelope.get("key") or key),
        thread_id=str(envelope.get("thread_id") or "") or None,
        sent_at=sent_at,
        from_addr=str(envelope.get("from") or ""),
        to_addr=str(envelope.get("to") or ""),
        cc_addr=str(envelope.get("cc") or ""),
        subject=str(envelope.get("subject") or ""),
        body=str(envelope.get("own_text") or ""),
        in_reply_to=envelope.get("in_reply_to") or None,
        quoted=str(envelope.get("quoted") or ""),
        rfc_message_id=envelope.get("message_id") or None,
    )


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
                    out.append(to_inbound(envelope, key))
        return sorted(out, key=lambda m: m.sent_at)
