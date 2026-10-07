"""Where the agent reads the vendors' replies when it runs on its own, and what reads them.

``FP_BOOKING_INBOX`` names the source: ``gmail`` (the sending mailbox, FP_BOOKING_GMAIL_USER,
read with FP_BOOKING_GMAIL_KEY for mail to or copying the customers' groups) or
``s3://bucket[/prefix]`` (the group-mail archive, ``mailarchive``). Each pass reads the last
FP_BOOKING_INBOX_DAYS days; mail already on a case is skipped by its email ID, so a reply is
never answered twice however often the inbox is read.

The agent's requests ask for answers at the group (Reply-To), but some facilities answer the
mailbox that sent the request, and that mail never reaches the group. So whenever the sending
mailbox is set up, the mail addressed to it is read too: in the Gmail search beside the groups',
or, with the archive, from the mailbox beside the archive. An email found in both is read once.
"""

from __future__ import annotations

from dataclasses import dataclass, field
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

    def __str__(self) -> str:
        return f"the mailbox {self.user}"


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

    def __str__(self) -> str:
        return f"the archive s3://{self.bucket}/{self.prefix}".rstrip("/")


def identity(message: InboundMessage) -> str:
    """One email's identity across sources: its RFC Message-ID, else the source's own id."""
    rfc = (message.rfc_message_id or "").strip().strip("<>").lower()
    return f"rfc:{rfc}" if rfc else f"id:{message.message_id}"


@dataclass
class MergedInbox:
    """Several sources read as one; an email found in two of them is read once.

    A source that cannot be read does not stop the others: its failure is listed in
    ``problems`` (the pass reports it), and only when every source fails is the error raised.
    """

    sources: list[Inbox]
    problems: list[str] = field(default_factory=list)

    def fetch(self) -> list[InboundMessage]:
        """Every source's messages, oldest first, each email once (the first source's copy)."""
        self.problems = []
        seen: set[str] = set()
        out: list[InboundMessage] = []
        failed: list[Exception] = []
        for source in self.sources:
            try:
                found = source.fetch()
            except Exception as exc:  # one unreadable source must not hide the others' mail
                failed.append(exc)
                self.problems.append(f"{source}: {exc}")
                continue
            for message in found:
                key = identity(message)
                if key not in seen:
                    seen.add(key)
                    out.append(message)
        if failed and len(failed) == len(self.sources):
            raise failed[0]
        return sorted(out, key=lambda m: m.sent_at)


def _groups(settings: Settings) -> list[str]:
    known = customers(settings)
    chosen = [known.get(k) for k in settings.customers] or list(known.files)
    return [g for g in dict.fromkeys(c.group for c in chosen) if g] or (
        [settings.booking_sender] if settings.booking_sender else []
    )


def group_query(settings: Settings, days: int, *, mailbox: str | None = None) -> str | None:
    """A Gmail search for the customers' group mail of the last ``days`` days, or None.

    With ``mailbox`` (the sending mailbox) its own address is searched too: a reply sent to it
    alone is a reply.
    """
    groups = _groups(settings)
    if not groups and not mailbox:
        return None
    terms = [f"to:{g} OR cc:{g} OR deliveredto:{g}" for g in groups]
    if mailbox:
        terms.append(f"to:{mailbox} OR cc:{mailbox}")
    return f"({' OR '.join(terms)}) newer_than:{days}d"


def direct_query(settings: Settings, days: int) -> str | None:
    """A Gmail search for mail sent to the sending mailbox without the group on it, or None.

    What the group archive cannot hold: a facility that answered the address the request came
    from. Mail the group also got is left to the archive.
    """
    mailbox = settings.booking_gmail_user
    if not mailbox:
        return None
    skip = "".join(f" -to:{g} -cc:{g}" for g in _groups(settings))
    return f"(to:{mailbox} OR cc:{mailbox}){skip} newer_than:{days}d"


def inbox_from_settings(settings: Settings) -> Inbox | None:
    """The inbox FP_BOOKING_INBOX names, or None when it is not set (or cannot be read).

    With the archive and the sending mailbox both set up, both are read (:class:`MergedInbox`).
    """
    where = settings.booking_inbox
    if not where:
        return None
    mailbox = settings.booking_gmail_user
    key = settings.booking_gmail_key
    if where.startswith("s3://"):
        bucket, _, prefix = where.removeprefix("s3://").strip("/").partition("/")
        archive = ArchiveInbox(bucket, prefix, settings.booking_inbox_days)
        direct = direct_query(settings, settings.booking_inbox_days)
        if not (key and mailbox and direct):
            return archive
        return MergedInbox([archive, GmailInbox(Path(key), mailbox, direct)])
    if not (key and mailbox):
        return None
    query = group_query(settings, settings.booking_inbox_days, mailbox=mailbox)
    if query is None:
        return None
    return GmailInbox(Path(key), mailbox, query)


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
