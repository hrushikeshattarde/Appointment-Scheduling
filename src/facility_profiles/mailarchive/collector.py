"""One collection pass: the group's recent mail, sifted for pickup-appointment threads, into S3.

No bookmark. The group carries a few dozen messages a day, so each pass simply lists the last few
days with a Gmail search and asks S3 whether each message is already there. Every key is a pure
function of the message, so a pass can stop anywhere (its cap, its deadline, an error) and the
next one redoes only what is missing. What the pass must remember between runs is one small file:
the thread ids that qualified and why, so a bare "Re:" in a kept thread is kept too.

When a message qualifies and its thread is new, the earlier messages of that thread are collected
as well (Gmail knows the thread), so the request a confirmation answers is never missing from the
archive even when it arrived before the collector existed.
"""

from __future__ import annotations

import base64
import collections
import email
import email.message
import hashlib
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email import policy
from typing import Any

from facility_profiles.booking.mail import split_quoted
from facility_profiles.mailarchive import filters
from facility_profiles.mailarchive.gmail import GmailClient, headers_of, internal_date_iso
from facility_profiles.mailarchive.store import (
    LAST_RUN_KEY,
    THREADS_KEY,
    Store,
    Stored,
    attachment_key,
    mail_base,
    message_key,
)


@dataclass
class Stats:
    """What a pass did."""

    listed: int = 0
    already: int = 0  # in S3 before this pass
    not_booking: int = 0  # listed, read, and not about a pickup appointment
    stored: int = 0
    backfilled: int = 0  # earlier messages of a newly matched thread
    threads_new: int = 0
    attachments_stored: int = 0
    attachments_already: int = 0
    reasons: collections.Counter[str] = field(default_factory=collections.Counter)
    deferred: int = 0
    stopped_by: str = ""
    error: str = ""

    def line(self) -> str:
        """One log line."""
        bits = [
            f"{self.listed} listed",
            f"{self.already} already archived",
            f"{self.not_booking} not about booking",
            f"{self.stored} stored ({self.backfilled} backfilled from their threads)",
            f"{self.threads_new} new thread(s)",
            f"attachments {self.attachments_stored} stored, "
            f"{self.attachments_already} already there",
        ]
        if self.reasons:
            bits.append("kept by " + ", ".join(f"{k} {v}" for k, v in self.reasons.most_common()))
        if self.deferred:
            bits.append(f"{self.deferred} left for the next run ({self.stopped_by})")
        if self.error:
            bits.append(f"STOPPED ON ERROR: {self.error}")
        return " | ".join(bits)

    def as_dict(self) -> dict[str, Any]:
        """JSON-friendly copy."""
        out = dict(self.__dict__)
        out["reasons"] = dict(self.reasons)
        return out


@dataclass(frozen=True)
class Parsed:
    """A raw message taken apart."""

    headers: dict[str, str]
    text: str
    attachments: list[dict[str, Any]]  # filename, mime, data


def default_query(group: str, days: int) -> str:
    """The Gmail search that lists the group's traffic in a member's mailbox."""
    return (
        f"(to:{group} OR cc:{group} OR from:{group} OR deliveredto:{group} OR list:{group}) "
        f"newer_than:{days}d"
    )


def run(
    gmail: GmailClient,
    store: Store,
    *,
    mailbox: str,
    rules: filters.MailRules,
    group: str | None = None,
    days: int = 3,
    max_messages: int = 300,
    deadline: float | None = None,
    verbose: bool = False,
) -> Stats:
    """One pass over one customer's group. Returns what it did; ``error`` is set on a stop.

    ``rules`` are the customer's (:func:`filters.rules_for`); ``group`` defaults to theirs.
    """
    group = group or rules.group
    if not group:
        msg = "no group address: give one, or set [mail] group in the customer file"
        raise ValueError(msg)
    st = Stats()
    threads: dict[str, str] = dict(store.get_json(THREADS_KEY) or {})
    threads_changed = False
    refs = gmail.search(default_query(group, days))
    st.listed = len(refs)
    for i, ref in enumerate(refs):
        if i >= max_messages or (deadline is not None and time.monotonic() >= deadline):
            st.deferred = len(refs) - i
            st.stopped_by = "message cap" if i >= max_messages else "time limit"
            break
        try:
            meta = gmail.message(ref["id"], fmt="metadata")
            h = headers_of(meta)
            base = mail_base(message_key(h.get("message-id"), ref["id"]), internal_date_iso(meta))
            if store.exists(base + ".json"):
                st.already += 1
                continue
            thread_id = str(meta.get("threadId") or ref.get("threadId") or "")
            reason = filters.match_reason(
                h.get("subject"),
                filters.participants(h.get("from"), h.get("to"), h.get("cc")),
                rules,
            )
            if reason is None and thread_id in threads:
                reason = f"thread:{threads[thread_id]}"
            if reason is None:
                st.not_booking += 1
                continue
            if thread_id and thread_id not in threads:
                threads[thread_id] = reason
                threads_changed = True
                st.threads_new += 1
                _backfill_thread(
                    gmail,
                    store,
                    thread_id,
                    skip_id=ref["id"],
                    reason=reason,
                    mailbox=mailbox,
                    st=st,
                    rules=rules,
                )
            stored = collect_message(
                gmail, store, ref["id"], mailbox=mailbox, reason=reason, st=st, rules=rules
            )
            if verbose and stored is not None:
                print(f"  {ref['id']} -> {stored.key}  ({reason})")  # noqa: T201 - CLI progress
        except Exception as e:  # recorded, then reported by the caller
            # Stop rather than skip: a Gmail outage or an S3 refusal is not an absence, and moving
            # past this message would hide it until the window has passed.
            st.deferred = len(refs) - i
            st.stopped_by = "error"
            st.error = f"{ref['id']}: {type(e).__name__} {str(e)[:200]}"
            break
    if threads_changed:
        store.put_json(THREADS_KEY, threads)
    store.put_json(
        LAST_RUN_KEY,
        {
            "at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
            "mailbox": mailbox,
            "group": group,
            "days": days,
            "stats": st.as_dict(),
            "line": st.line(),
        },
    )
    return st


def _backfill_thread(
    gmail: GmailClient,
    store: Store,
    thread_id: str,
    *,
    skip_id: str,
    reason: str,
    mailbox: str,
    st: Stats,
    rules: filters.MailRules,
) -> None:
    """Collect the other messages of a thread that just qualified, oldest first.

    A message that would have qualified on its own keeps its own reason; the rest are recorded
    as kept for the thread they belong to.
    """
    messages = list(gmail.thread(thread_id).get("messages") or [])
    messages.sort(key=lambda m: int(m.get("internalDate") or 0))
    for m in messages:
        mid = str(m.get("id") or "")
        if not mid or mid == skip_id:
            continue
        h = headers_of(m)
        base = mail_base(message_key(h.get("message-id"), mid), internal_date_iso(m))
        if store.exists(base + ".json"):
            continue
        own = filters.match_reason(
            h.get("subject"),
            filters.participants(h.get("from"), h.get("to"), h.get("cc")),
            rules,
        )
        if collect_message(
            gmail, store, mid, mailbox=mailbox, reason=own or f"thread:{reason}", st=st, rules=rules
        ):
            st.backfilled += 1


def collect_message(
    gmail: GmailClient,
    store: Store,
    gmail_id: str,
    *,
    mailbox: str,
    reason: str,
    st: Stats,
    rules: filters.MailRules = filters.GENERIC,
) -> Stored | None:
    """Fetch one message raw and store its attachments, its ``.eml`` and its ``.json``."""
    msg = gmail.message(gmail_id, fmt="raw")
    raw = base64.urlsafe_b64decode(msg["raw"] + "=" * (-len(msg["raw"]) % 4))
    parsed = parse_raw(raw)
    key = message_key(parsed.headers.get("message-id"), gmail_id)
    internal = internal_date_iso(msg)
    base = mail_base(key, internal)
    if store.exists(base + ".json"):
        st.already += 1
        return None

    manifest: list[dict[str, Any]] = []
    for part in parsed.attachments:
        data: bytes = part["data"]
        sha = hashlib.sha256(data).hexdigest()
        put = store.put(
            attachment_key(sha),
            data,
            content_type=str(part["mime"] or "application/octet-stream"),
            metadata={"filename": str(part["filename"]), "first-seen-message": key},
        )
        if put.skipped:
            st.attachments_already += 1
        else:
            st.attachments_stored += 1
        manifest.append(
            {
                "filename": part["filename"],
                "mime": part["mime"],
                "bytes": len(data),
                "sha256": sha,
                "key": store.full(attachment_key(sha)),
            }
        )

    own, quoted = split_quoted(parsed.text)
    h = parsed.headers
    envelope = {
        "key": key,
        "gmail_id": gmail_id,
        "thread_id": str(msg.get("threadId") or ""),
        "mailbox": mailbox,
        "collected_at": datetime.now(tz=UTC).isoformat(timespec="seconds"),
        "matched_by": reason,
        "message_id": h.get("message-id"),
        "in_reply_to": h.get("in-reply-to"),
        "references": h.get("references"),
        "subject": h.get("subject", ""),
        "from": h.get("from", ""),
        "to": h.get("to", ""),
        "cc": h.get("cc", ""),
        "date": h.get("date"),
        "internal_date": internal,
        "labels": msg.get("labelIds") or [],
        "own_text": own,
        "quoted": quoted,
        "body_text": parsed.text,
        "identifiers": filters.identifiers(h.get("subject"), parsed.text, rules=rules),
        "attachments": manifest,
        "eml_key": store.full(base + ".eml"),
    }
    store.put(base + ".eml", raw, content_type="message/rfc822")
    stored = store.put_json(base + ".json", envelope)
    st.stored += 1
    st.reasons[reason.split(":", 1)[0] if reason.startswith("thread") else reason] += 1
    return stored


def parse_raw(raw: bytes) -> Parsed:
    """Headers, the text body and every attachment-like part of an RFC822 message."""
    message = email.message_from_bytes(raw, policy=policy.default)
    headers = {k.lower(): str(v) for k, v in message.items()}
    plain: list[str] = []
    html: list[str] = []
    attachments: list[dict[str, Any]] = []
    for part in message.walk():
        if part.is_multipart():
            continue
        filename = part.get_filename() or ""
        maintype = part.get_content_maintype()
        if not filename and maintype == "text":
            text = _part_text(part)
            (plain if part.get_content_subtype() == "plain" else html).append(text)
            continue
        payload = part.get_payload(decode=True)
        if not isinstance(payload, bytes) or not payload:
            continue
        attachments.append({"filename": filename, "mime": part.get_content_type(), "data": payload})
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html))
    return Parsed(headers=headers, text=text, attachments=attachments)


def _part_text(part: email.message.Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        return str(payload or "")
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, "replace")
    except LookupError:
        return payload.decode("utf-8", "replace")


def _html_to_text(html: str) -> str:
    text = re.sub(r"<(?:script|style).*?</(?:script|style)>", " ", html, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</tr>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()
