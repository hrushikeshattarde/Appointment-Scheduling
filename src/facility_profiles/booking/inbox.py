"""Where the agent reads the vendors' replies when it runs on its own, and what reads them.

``FP_BOOKING_INBOX`` names the source: ``gmail`` (the sending mailbox, FP_BOOKING_GMAIL_USER,
read with FP_BOOKING_GMAIL_KEY for mail to or copying the customers' groups) or
``s3://bucket[/prefix]`` (the group-mail archive, ``mailarchive``). Each pass reads the last
FP_BOOKING_INBOX_DAYS days; mail already on a case is skipped by its email ID, so a reply is
never answered twice however often the inbox is read.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from facility_profiles.booking.classify import OpenRouterReplyClassifier, ReplyClassifier
from facility_profiles.booking.mail import GmailReader, InboundMessage
from facility_profiles.booking.writer import OpenRouterReplyWriter, ReplyWriter
from facility_profiles.config import Settings
from facility_profiles.customers import customers


class Inbox(Protocol):
    """Somewhere new mail can be read from."""

    def fetch(self) -> list[InboundMessage]:
        """The recent messages, oldest first."""
        ...


@dataclass
class GmailInbox:
    """The sending mailbox in Gmail, searched for the customers' group mail."""

    key_path: Path
    user: str
    query: str

    def fetch(self) -> list[InboundMessage]:  # pragma: no cover - live API
        """The messages the query finds."""
        return GmailReader(self.key_path, self.user).fetch(self.query)


@dataclass
class ArchiveInbox:
    """The group-mail archive in S3 (``mailarchive``)."""

    bucket: str
    prefix: str
    days: int

    def fetch(self) -> list[InboundMessage]:  # pragma: no cover - live AWS
        """The archived messages of the last ``days`` days."""
        from facility_profiles.mailarchive.reader import S3MailReader  # noqa: PLC0415
        from facility_profiles.mailarchive.store import Store  # noqa: PLC0415

        return S3MailReader(Store(self.bucket, self.prefix)).fetch(days=self.days)


def group_query(settings: Settings, days: int) -> str | None:
    """A Gmail search for the customers' group mail of the last ``days`` days, or None."""
    known = customers(settings)
    chosen = [known.get(k) for k in settings.customers] or list(known.files)
    groups = [g for g in dict.fromkeys(c.group for c in chosen) if g] or (
        [settings.booking_sender] if settings.booking_sender else []
    )
    if not groups:
        return None
    where = " OR ".join(f"to:{g} OR cc:{g} OR deliveredto:{g}" for g in groups)
    return f"({where}) newer_than:{days}d"


def inbox_from_settings(settings: Settings) -> Inbox | None:
    """The inbox FP_BOOKING_INBOX names, or None when it is not set (or cannot be read)."""
    where = settings.booking_inbox
    if not where:
        return None
    if where.startswith("s3://"):
        bucket, _, prefix = where.removeprefix("s3://").strip("/").partition("/")
        return ArchiveInbox(bucket, prefix, settings.booking_inbox_days)
    if not (settings.booking_gmail_key and settings.booking_gmail_user):
        return None
    query = group_query(settings, settings.booking_inbox_days)
    if query is None:
        return None
    return GmailInbox(Path(settings.booking_gmail_key), settings.booking_gmail_user, query)


def reader_tools(settings: Settings) -> tuple[ReplyClassifier | None, ReplyWriter | None]:
    """What reads a reply and writes the answer: the model, when OpenRouter is set up."""
    key = settings.openrouter_api_key
    if settings.llm_provider != "openrouter" or key is None:
        return None, None
    secret = key.get_secret_value()
    model, base = settings.llm_model, settings.openrouter_base_url
    return (
        OpenRouterReplyClassifier(secret, model=model, base_url=base),
        OpenRouterReplyWriter(secret, model=model, base_url=base),
    )
