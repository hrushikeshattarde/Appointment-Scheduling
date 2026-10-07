"""Mail adapters: inbound from the archive, a JSONL pull or Gmail; outbound as drafts or sends.

Two kinds of outbox. A :class:`Mailer` only drafts (``.eml`` files on disk, or Gmail drafts a
person sends); a :class:`Sender` delivers the message itself. Both build the same MIME through
:func:`build_mime`, which stamps every outbound message with its own RFC ``Message-ID`` so a
vendor's reply can be tied back to the request through ``In-Reply-To`` and ``References``
whatever mailbox it is read from.
"""

from __future__ import annotations

import base64
import importlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import make_msgid, parseaddr
from pathlib import Path
from typing import Any, Protocol

from facility_profiles.booking.attachments import (
    READ,
    Attachment,
    Reading,
    triage,
    with_attachments,
)
from facility_profiles.booking.attachments import read as read_attachment

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"
SCOPE_SEND = "https://www.googleapis.com/auth/gmail.send"
_MSGID_RE = re.compile(r"<[^<>\s]+>")


@dataclass(frozen=True)
class InboundMessage:
    """One email as the agent sees it."""

    message_id: str
    thread_id: str | None
    sent_at: datetime
    from_addr: str
    to_addr: str
    cc_addr: str
    subject: str
    body: str
    in_reply_to: str | None = None
    quoted: str = ""  # the quoted history under the reply (vendors edit times in it)
    rfc_message_id: str | None = None  # the RFC Message-ID header, when the source keeps it
    references: str | None = None  # the References header, when the source keeps it
    # Attached files that should have been read and were not ("scan.pdf: a scan"); the text of
    # those that were read is in ``body``, under the email's own words (booking/attachments.py).
    unread_files: tuple[str, ...] = ()

    @property
    def full_text(self) -> str:
        """Own words plus the quoted part, for quote validation."""
        return f"{self.body}\n{self.quoted}".strip()

    @property
    def from_email(self) -> str:
        """Bare lower-cased sender address (display names may themselves contain an @)."""
        angle = re.search(r"<([^>]+)>", self.from_addr)
        if angle:
            return angle.group(1).strip().lower()
        found = re.search(r"[\w.+-]+@[\w-]+\.[\w.-]+", self.from_addr)
        return (found.group(0) if found else parseaddr(self.from_addr)[1]).lower()

    @property
    def from_domain(self) -> str:
        """Sender domain."""
        return self.from_email.split("@")[-1]

    @property
    def referenced_ids(self) -> list[str]:
        """Every Message-ID this reply points at, In-Reply-To first, lower-cased."""
        return message_ids(self.in_reply_to, self.references)


def message_ids(*headers: str | None) -> list[str]:
    """Distinct ``<id>`` tokens from In-Reply-To or References header values, lower-cased."""
    out: list[str] = []
    for value in headers:
        for token in _MSGID_RE.findall(value or ""):
            lowered = token.lower()
            if lowered not in out:
                out.append(lowered)
    return out


_QUOTE_SPLIT = re.compile(
    r"\n(?:On .{0,160}wrote:|From: .{0,200}\n(?:Sent|Date): |-----Original Message-----|>|"
    r"---- on .{0,120} wrote ----)"
)


def split_quoted(text: str) -> tuple[str, str]:
    """Split a reply into its own words and the quoted history under them."""
    match = _QUOTE_SPLIT.search(text)
    if match is None:
        return text.strip(), ""
    return text[: match.start()].strip(), text[match.start() :].strip()


def strip_quoted(text: str) -> str:
    """Keep only the reply's own words (drop quoted history and signatures' history)."""
    return split_quoted(text)[0]


def load_messages_jsonl(path: Path) -> list[InboundMessage]:
    """Read messages written by ``scripts/lidl_mail_patterns.py``."""
    out: list[InboundMessage] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            m = json.loads(line)
            own, quoted = split_quoted(m.get("body", ""))
            out.append(
                InboundMessage(
                    message_id=str(m["id"]),
                    thread_id=m.get("thread_id"),
                    sent_at=datetime.fromisoformat(m["sent_at"]),
                    from_addr=m.get("from", ""),
                    to_addr=m.get("to", ""),
                    cc_addr=m.get("cc", ""),
                    subject=m.get("subject", ""),
                    body=m.get("own_text") or own,
                    in_reply_to=m.get("in_reply_to") or None,
                    quoted=quoted,
                    rfc_message_id=m.get("message_id") or None,
                    references=m.get("references") or None,
                )
            )
    return sorted(out, key=lambda x: x.sent_at)


# ------------------------------------------------------------------------------ outbound ----


@dataclass(frozen=True)
class OutboundDraft:
    """What the agent wants to send."""

    to_addr: str
    cc_addr: str
    subject: str
    body: str
    thread_id: str | None = None
    in_reply_to: str | None = None  # the RFC Message-ID this answers
    references: str | None = None  # the References chain of the message this answers
    # Drafts only: the customer's group the draft is from. A send is always from the sending
    # mailbox (a group cannot send through the API).
    from_addr: str | None = None
    # Where the facility's answer goes: the customer's group, so a plain Reply (not only Reply
    # All) reaches the group, where the agent and the pod read the thread.
    reply_to: str | None = None
    # An HTML part beside the text, when the request carries one-click times (booking/links.py).
    html: str | None = None


@dataclass(frozen=True)
class Delivery:
    """What an outbox did with a draft."""

    ref: str  # a file path, a Gmail draft id, "gmail:<message id>", or UNCONFIRMED
    sent: bool = False
    gmail_id: str | None = None
    thread_id: str | None = None
    rfc_message_id: str | None = None
    # Gmail gave no answer to a send it may have carried out: why, for the person who checks.
    unconfirmed: str | None = None


# The ``ref`` of a send Gmail never answered: it may or may not have gone out.
UNCONFIRMED = "gmail:unconfirmed"


class MailError(RuntimeError):
    """Gmail could not be reached, refused the login, or refused the message.

    ``maybe_sent`` is True when the message may have gone out all the same (the connection
    dropped or timed out after the message was handed over, or Gmail failed on its side), so it
    must not simply be sent again. ``rfc_message_id`` is the Message-ID it would carry.
    """

    def __init__(
        self, message: str, *, maybe_sent: bool = False, rfc_message_id: str | None = None
    ) -> None:
        super().__init__(message)
        self.maybe_sent = maybe_sent
        self.rfc_message_id = rfc_message_id


class Mailer(Protocol):
    """An outbox that only drafts; a person sends."""

    def create_draft(self, draft: OutboundDraft) -> str:
        """Create the draft; return a reference a person can find it by."""
        ...


class Sender(Protocol):
    """An outbox that sends."""

    def deliver(self, draft: OutboundDraft) -> Delivery:
        """Send the message; return what went out and the ids it carries."""
        ...


def deliver(outbox: Mailer | Sender, draft: OutboundDraft) -> Delivery:
    """Hand a draft to whichever kind of outbox this is.

    A send Gmail may have carried out without saying so comes back as an unconfirmed
    :class:`Delivery` (``ref`` :data:`UNCONFIRMED`), so it is recorded and not sent twice; any
    other :class:`MailError` is raised: nothing went out.
    """
    send = getattr(outbox, "deliver", None)
    if callable(send):
        try:
            result: Delivery = send(draft)
        except MailError as exc:
            if not exc.maybe_sent:
                raise
            return Delivery(
                ref=UNCONFIRMED, rfc_message_id=exc.rfc_message_id, unconfirmed=str(exc)[:300]
            )
        return result
    return Delivery(ref=outbox.create_draft(draft))  # type: ignore[union-attr]


def build_mime(
    draft: OutboundDraft,
    sender: str,
    *,
    message_id: str | None = None,
    extra_headers: dict[str, str] | None = None,
) -> EmailMessage:
    """The message as it will travel: headers, threading ids and the plain-text body."""
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    msg["To"] = draft.to_addr
    if draft.cc_addr:
        msg["Cc"] = draft.cc_addr
    msg["Subject"] = draft.subject
    if draft.reply_to:
        msg["Reply-To"] = draft.reply_to
    msg["Message-ID"] = message_id or new_message_id(sender)
    if draft.in_reply_to:
        msg["In-Reply-To"] = draft.in_reply_to
        chain = message_ids(draft.references, draft.in_reply_to)
        msg["References"] = " ".join(chain)
    for name, value in (extra_headers or {}).items():
        msg[name] = value
    msg.set_content(draft.body)
    if draft.html:
        msg.add_alternative(draft.html, subtype="html")
    return msg


def new_message_id(sender: str) -> str:
    """A fresh RFC Message-ID in the sender's domain."""
    domain = parseaddr(sender)[1].split("@")[-1] or "circledelivers.com"
    return make_msgid(domain=domain)


@dataclass
class LocalDraftMailer:
    """Write each draft as an ``.eml`` file under ``directory`` (draft mode)."""

    directory: Path
    sender: str = ""  # From when the draft names none; blank lets the mail client fill it in
    created: list[Path] = field(default_factory=list)

    def create_draft(self, draft: OutboundDraft) -> str:
        """Write the draft and return its path."""
        self.directory.mkdir(parents=True, exist_ok=True)
        msg = build_mime(
            draft,
            draft.from_addr or self.sender,
            extra_headers={"X-Facility-Profiles-Draft": "draft mode; not sent"},
        )
        stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
        safe = re.sub(r"[^A-Za-z0-9]+", "-", draft.subject)[:60].strip("-")
        path = self.directory / f"{stamp}-{safe or 'draft'}.eml"
        path.write_bytes(bytes(msg))
        self.created.append(path)
        return str(path)


@dataclass
class RecordingMailer:
    """Keeps drafts in memory (tests)."""

    drafts: list[OutboundDraft] = field(default_factory=list)

    def create_draft(self, draft: OutboundDraft) -> str:
        """Record and return a fake reference."""
        self.drafts.append(draft)
        return f"memory:{len(self.drafts)}"


@dataclass
class RecordingSender:
    """Pretends to send (tests): records the draft and hands back ids like Gmail would."""

    sender: str = "lidl-appointments@circledelivers.com"
    drafts: list[OutboundDraft] = field(default_factory=list)
    deliveries: list[Delivery] = field(default_factory=list)

    def deliver(self, draft: OutboundDraft) -> Delivery:
        """Record the draft; the Message-ID is minted the way the real sender mints it."""
        self.drafts.append(draft)
        n = len(self.drafts)
        result = Delivery(
            ref=f"gmail:sent-{n}",
            sent=True,
            gmail_id=f"sent-{n}",
            thread_id=draft.thread_id or f"thread-{n}",
            rfc_message_id=str(build_mime(draft, self.sender)["Message-ID"]),
        )
        self.deliveries.append(result)
        return result


def _gmail_session(key_path: Path, subject: str, scope: str) -> Any:
    """An authorised Gmail session; the Google client is the optional ``gmail`` extra."""
    try:
        transport = importlib.import_module("google.auth.transport.requests")
        service_account = importlib.import_module("google.oauth2.service_account")
    except ImportError as exc:  # pragma: no cover - environment guard
        msg = "install the gmail extra (google-auth, requests) to use Gmail"
        raise MailError(msg) from exc
    try:
        creds = service_account.Credentials.from_service_account_file(
            str(key_path), scopes=[scope]
        ).with_subject(subject)
    except (OSError, ValueError, KeyError) as exc:
        msg = f"the Gmail key {key_path} could not be loaded ({type(exc).__name__}: {exc})"
        raise MailError(msg[:400]) from exc
    return transport.AuthorizedSession(creds)


# Errors raised before anything reached Gmail: the login (minted on the first call), or no
# connection at all. Anything else raised during a send may come after Gmail took the message.
_BEFORE_SENDING = ("RefreshError", "DefaultCredentialsError", "TransportError", "ConnectTimeout")
_NO_CONNECTION = (
    "NewConnectionError",
    "NameResolutionError",
    "Failed to establish a new connection",
)


def _may_have_gone(exc: Exception) -> bool:
    """Whether a send that raised ``exc`` may still have reached Gmail."""
    names = {type(e).__name__ for e in (exc, exc.__cause__, exc.__context__) if e is not None}
    if names & set(_BEFORE_SENDING):
        return False
    return not any(marker in str(exc) for marker in _NO_CONNECTION)


def _post(
    session: Any,
    url: str,
    payload: dict[str, Any],
    *,
    what: str,
    sending: bool,
    rfc_message_id: str | None = None,
) -> dict[str, Any]:
    """POST to Gmail; every failure becomes a :class:`MailError` that says what went wrong.

    ``sending`` marks a call that delivers mail: a timeout, a dropped connection or a 5xx from
    Gmail may then have sent it, and the error says so.
    """
    try:
        resp = session.post(url, json=payload, timeout=60)
    except Exception as exc:  # the login's and the transport's errors, whichever library raised
        maybe = sending and _may_have_gone(exc)
        reason = f"Gmail {what} failed: {type(exc).__name__}: {str(exc)[:240]}"
        raise MailError(reason, maybe_sent=maybe, rfc_message_id=rfc_message_id) from exc
    if resp.status_code not in (200, 201):
        maybe = sending and resp.status_code >= 500
        reason = f"Gmail {what} failed {resp.status_code}: {resp.text[:300]}"
        raise MailError(reason, maybe_sent=maybe, rfc_message_id=rfc_message_id)
    try:
        data = resp.json()
    except ValueError as exc:  # it answered 200: the message went, the answer is unreadable
        reason = f"Gmail {what} answered {resp.status_code} with no readable body"
        raise MailError(reason, maybe_sent=sending, rfc_message_id=rfc_message_id) from exc
    return dict(data) if isinstance(data, dict) else {}


def _raw(msg: EmailMessage) -> str:
    return base64.urlsafe_b64encode(bytes(msg)).decode()


@dataclass
class GmailDraftMailer:
    """Create Gmail drafts as ``subject_user`` (needs domain-wide delegation for gmail.compose)."""

    key_path: Path
    subject_user: str
    sender: str

    def create_draft(self, draft: OutboundDraft) -> str:
        """POST users/me/drafts; returns the draft ID. Raises :class:`MailError` on failure."""
        raw = _raw(build_mime(draft, draft.from_addr or self.sender))
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if draft.thread_id:
            payload["message"]["threadId"] = draft.thread_id
        session = _gmail_session(self.key_path, self.subject_user, SCOPE_COMPOSE)
        data = _post(session, f"{GMAIL_API}/drafts", payload, what="draft", sending=False)
        return str(data.get("id"))


@dataclass
class GmailSender:
    """Send as ``subject_user`` through the Gmail API (domain-wide delegation for gmail.send).

    The From address is the mailbox itself: a Google Group cannot send through the API, so the
    agent writes from a member mailbox and copies the group. The Message-ID is minted here, before
    the send, so the case can record it whatever Gmail does with the message afterwards.
    """

    key_path: Path
    subject_user: str
    sender: str | None = None

    def deliver(self, draft: OutboundDraft) -> Delivery:
        """POST users/me/messages/send; returns the Gmail ids and the Message-ID that went out.

        Raises :class:`MailError` when it fails, with ``maybe_sent`` when Gmail may have sent
        the message all the same.
        """
        sender = self.sender or self.subject_user
        message_id = new_message_id(sender)
        payload: dict[str, Any] = {"raw": _raw(build_mime(draft, sender, message_id=message_id))}
        if draft.thread_id:
            payload["threadId"] = draft.thread_id
        session = _gmail_session(self.key_path, self.subject_user, SCOPE_SEND)
        data = _post(
            session,
            f"{GMAIL_API}/messages/send",
            payload,
            what="send",
            sending=True,
            rfc_message_id=message_id,
        )
        return Delivery(
            ref=f"gmail:{data.get('id')}",
            sent=True,
            gmail_id=str(data.get("id")),
            thread_id=str(data.get("threadId") or draft.thread_id or ""),
            rfc_message_id=message_id,
        )


@dataclass
class GmailReader:
    """Read recent messages as ``subject_user`` (gmail.readonly)."""

    key_path: Path
    subject_user: str

    def fetch(
        self, query: str, *, max_messages: int = 500
    ) -> list[InboundMessage]:  # pragma: no cover - live API
        """Messages matching a Gmail search query, oldest first."""
        session = _gmail_session(self.key_path, self.subject_user, SCOPE_READ)
        ids: list[str] = []
        token: str | None = None
        while True:
            params: dict[str, Any] = {"q": query, "maxResults": 500}
            if token:
                params["pageToken"] = token
            resp = session.get(f"{GMAIL_API}/messages", params=params, timeout=60)
            if resp.status_code != 200:
                msg = f"Gmail list failed {resp.status_code}: {resp.text[:300]}"
                raise MailError(msg)
            data = resp.json()
            ids.extend(m["id"] for m in data.get("messages", []))
            token = data.get("nextPageToken")
            if not token or len(ids) >= max_messages:
                break
        out: list[InboundMessage] = []
        for mid in ids[:max_messages]:
            resp = session.get(f"{GMAIL_API}/messages/{mid}", params={"format": "full"}, timeout=60)
            if resp.status_code != 200:
                continue

            def attachment(attachment_id: str, mid: str = mid) -> bytes:
                got = session.get(
                    f"{GMAIL_API}/messages/{mid}/attachments/{attachment_id}", timeout=60
                )
                if got.status_code != 200:
                    msg = f"Gmail attachment failed {got.status_code}: {got.text[:200]}"
                    raise MailError(msg)
                return _decode_bytes(str(got.json().get("data") or ""))

            out.append(_from_gmail(resp.json(), attachment=attachment))
        return sorted(out, key=lambda m: m.sent_at)


def _decode_bytes(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _decode(data: str) -> str:
    return _decode_bytes(data).decode("utf-8", "replace")


def _part_headers(part: dict[str, Any]) -> dict[str, str]:
    headers = part.get("headers") or []
    return {str(h.get("name", "")).lower(): str(h.get("value", "")) for h in headers}


def gmail_body_text(payload: dict[str, Any]) -> str:
    """The text of a Gmail ``full`` message: its plain part, else its HTML part as text."""
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        if body.get("data") and not part.get("filename"):
            if mime == "text/plain":
                plain.append(_decode(body["data"]))
            elif mime == "text/html":
                html.append(_decode(body["data"]))
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    if plain:
        return "\n".join(plain)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", "\n".join(html), flags=re.I)
    return re.sub(r"<[^>]+>", " ", text)


def _file_parts(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """Every part of a Gmail ``full`` message that is a file (it has a file name)."""
    found: list[dict[str, Any]] = []

    def walk(part: dict[str, Any]) -> None:
        if part.get("filename"):
            found.append(part)
        for child in part.get("parts") or []:
            walk(child)

    walk(payload)
    return found


def _gmail_readings(
    payload: dict[str, Any], attachment: Callable[[str], bytes] | None
) -> list[Reading]:
    """What each attached file says, fetching only the ones worth reading."""
    readings: list[Reading] = []
    for part in _file_parts(payload):
        body = part.get("body") or {}
        headers = _part_headers(part)
        inline = headers.get("content-disposition", "").lower().startswith("inline") or bool(
            headers.get("content-id")
        )
        name, mime = str(part["filename"]), str(part.get("mimeType") or "")
        verdict = triage(name, mime, int(body.get("size") or 0), inline=inline)
        if verdict is None:
            continue
        if verdict != READ:
            readings.append(Reading(name, why=verdict))
            continue
        if body.get("data"):
            data = _decode_bytes(str(body["data"]))
        elif body.get("attachmentId") and attachment is not None:
            data = attachment(str(body["attachmentId"]))
        else:
            continue
        reading = read_attachment(Attachment(name, mime, data, inline=inline))
        if reading is not None:
            readings.append(reading)
    return readings


def _from_gmail(
    msg: dict[str, Any], *, attachment: Callable[[str], bytes] | None = None
) -> InboundMessage:
    """A Gmail ``full`` message as the agent sees it, with the text of its attached files.

    ``attachment`` fetches a file's bytes by its attachment id (a large file is not in the
    message itself); without it only the files carried inline are read.
    """
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC)
    own, quoted = split_quoted(gmail_body_text(msg["payload"]))
    body, unread = with_attachments(own, _gmail_readings(msg["payload"], attachment))
    return InboundMessage(
        message_id=str(msg["id"]),
        thread_id=msg.get("threadId"),
        sent_at=sent_at,
        from_addr=headers.get("from", ""),
        to_addr=headers.get("to", ""),
        cc_addr=headers.get("cc", ""),
        subject=headers.get("subject", ""),
        body=body,
        in_reply_to=headers.get("in-reply-to"),
        quoted=quoted,
        rfc_message_id=headers.get("message-id"),
        references=headers.get("references"),
        unread_files=unread,
    )
