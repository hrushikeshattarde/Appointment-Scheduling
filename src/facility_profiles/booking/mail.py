"""Mail adapters: inbound messages from a JSONL pull or Gmail, outbound drafts to disk or Gmail.

The prototype runs in draft mode: :class:`LocalDraftMailer` writes ``.eml`` files a person can
open and send. :class:`GmailDraftMailer` creates real Gmail drafts once the service account has
the ``gmail.compose`` scope; it never sends.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import parseaddr
from pathlib import Path
from typing import Any, Protocol

GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
SCOPE_READ = "https://www.googleapis.com/auth/gmail.readonly"
SCOPE_COMPOSE = "https://www.googleapis.com/auth/gmail.compose"


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

    @property
    def from_email(self) -> str:
        """Bare lower-cased sender address."""
        return parseaddr(self.from_addr)[1].lower()

    @property
    def from_domain(self) -> str:
        """Sender domain."""
        return self.from_email.split("@")[-1]


def strip_quoted(text: str) -> str:
    """Keep only the reply's own words (drop quoted history and signatures' history)."""
    cut = re.split(
        r"\n(?:On .{0,160}wrote:|From: .{0,200}\n(?:Sent|Date): |-----Original Message-----|>)",
        text,
        maxsplit=1,
    )
    return cut[0].strip()


def load_messages_jsonl(path: Path) -> list[InboundMessage]:
    """Read messages written by ``scripts/lidl_mail_patterns.py``."""
    out: list[InboundMessage] = []
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            m = json.loads(line)
            out.append(
                InboundMessage(
                    message_id=str(m["id"]),
                    thread_id=m.get("thread_id"),
                    sent_at=datetime.fromisoformat(m["sent_at"]),
                    from_addr=m.get("from", ""),
                    to_addr=m.get("to", ""),
                    cc_addr=m.get("cc", ""),
                    subject=m.get("subject", ""),
                    body=m.get("own_text") or strip_quoted(m.get("body", "")),
                    in_reply_to=m.get("in_reply_to") or None,
                )
            )
    return sorted(out, key=lambda x: x.sent_at)


@dataclass(frozen=True)
class OutboundDraft:
    """What the agent wants to send."""

    to_addr: str
    cc_addr: str
    subject: str
    body: str
    thread_id: str | None = None
    in_reply_to: str | None = None


class Mailer(Protocol):
    """Where drafts go."""

    def create_draft(self, draft: OutboundDraft) -> str:
        """Create the draft; return a reference a person can find it by."""
        ...


@dataclass
class LocalDraftMailer:
    """Write each draft as an ``.eml`` file under ``directory`` (draft mode)."""

    directory: Path
    sender: str = "lidl@circledelivers.com"
    created: list[Path] = field(default_factory=list)

    def create_draft(self, draft: OutboundDraft) -> str:
        """Write the draft and return its path."""
        self.directory.mkdir(parents=True, exist_ok=True)
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = draft.to_addr
        if draft.cc_addr:
            msg["Cc"] = draft.cc_addr
        msg["Subject"] = draft.subject
        if draft.in_reply_to:
            msg["In-Reply-To"] = draft.in_reply_to
            msg["References"] = draft.in_reply_to
        msg["X-Facility-Profiles-Draft"] = "draft mode; not sent"
        msg.set_content(draft.body)
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


def _gmail_session(key_path: Path, subject: str, scope: str) -> Any:
    try:
        from google.auth.transport.requests import AuthorizedSession  # noqa: PLC0415
        from google.oauth2 import service_account  # noqa: PLC0415
    except ImportError as exc:  # pragma: no cover - environment guard
        msg = "install google-auth and requests to use Gmail"
        raise RuntimeError(msg) from exc
    creds = service_account.Credentials.from_service_account_file(  # type: ignore[no-untyped-call]
        str(key_path), scopes=[scope]
    ).with_subject(subject)
    return AuthorizedSession(creds)  # type: ignore[no-untyped-call]


@dataclass
class GmailDraftMailer:
    """Create Gmail drafts as ``subject_user`` (needs domain-wide delegation for gmail.compose)."""

    key_path: Path
    subject_user: str
    sender: str

    def create_draft(self, draft: OutboundDraft) -> str:  # pragma: no cover - live API
        """POST users/me/drafts; returns the draft ID."""
        msg = EmailMessage()
        msg["From"] = self.sender
        msg["To"] = draft.to_addr
        if draft.cc_addr:
            msg["Cc"] = draft.cc_addr
        msg["Subject"] = draft.subject
        if draft.in_reply_to:
            msg["In-Reply-To"] = draft.in_reply_to
            msg["References"] = draft.in_reply_to
        msg.set_content(draft.body)
        raw = base64.urlsafe_b64encode(bytes(msg)).decode()
        payload: dict[str, Any] = {"message": {"raw": raw}}
        if draft.thread_id:
            payload["message"]["threadId"] = draft.thread_id
        session = _gmail_session(self.key_path, self.subject_user, SCOPE_COMPOSE)
        resp = session.post(f"{GMAIL_API}/drafts", json=payload, timeout=60)
        if resp.status_code not in (200, 201):
            msg_text = f"Gmail draft failed {resp.status_code}: {resp.text[:300]}"
            raise RuntimeError(msg_text)
        return str(resp.json().get("id"))


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
                raise RuntimeError(msg)
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
            out.append(_from_gmail(resp.json()))
        return sorted(out, key=lambda m: m.sent_at)


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body_text(payload: dict[str, Any]) -> str:
    plain: list[str] = []
    html: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime = part.get("mimeType", "")
        body = part.get("body") or {}
        if body.get("data"):
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


def _from_gmail(msg: dict[str, Any]) -> InboundMessage:
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC)
    body = _body_text(msg["payload"])
    return InboundMessage(
        message_id=str(msg["id"]),
        thread_id=msg.get("threadId"),
        sent_at=sent_at,
        from_addr=headers.get("from", ""),
        to_addr=headers.get("to", ""),
        cc_addr=headers.get("cc", ""),
        subject=headers.get("subject", ""),
        body=strip_quoted(body),
        in_reply_to=headers.get("in-reply-to"),
    )
