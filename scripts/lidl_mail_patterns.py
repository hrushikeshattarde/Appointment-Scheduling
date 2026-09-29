"""Pull the lidl@circledelivers.com booking mail and summarise how appointments get booked.

Uses a Google service-account key with domain-wide delegation to read one mailbox through the
Gmail API (read-only scope), stores the matching messages locally under ``data/lidl-mail/``
(git-ignored) and writes a pattern summary next to them. Nothing is sent or modified.

    python scripts/lidl_mail_patterns.py --key gsheets-python-350615-d8d272fb5359.json \
        --subject someone@circledelivers.com --group lidl@circledelivers.com --days 90

``--subject`` is the mailbox to read as. A Google Group has no Gmail mailbox of its own, so this
is normally a group member (the group's mail lands in their inbox); if lidl@ is a real user or
collaborative inbox, pass it directly.

Requires: ``google-auth`` and ``requests`` (``uv pip install --python .venv/Scripts/python.exe
google-auth requests``). The service account's client ID must be authorised in the Workspace
admin console (Security > API controls > Domain-wide delegation) for the scope
``https://www.googleapis.com/auth/gmail.readonly``; the script prints the client ID if that
authorisation is missing.
"""

from __future__ import annotations

import argparse
import base64
import json
import re
import statistics
import sys
from collections import Counter, defaultdict
from datetime import UTC, datetime
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path
from typing import Any

SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
API = "https://gmail.googleapis.com/gmail/v1/users/me"
INTERNAL_DOMAIN = "circledelivers.com"
DELIVERY_RE = re.compile(r"\b(PYE|FRG|GRM|[A-Z]{3})_\d{6,}\b")
LOAD_RE = re.compile(r"\b(?:load|ld|order)\s*#?\s*(\d{6,7})\b", re.I)
PO_RE = re.compile(r"\bPO\s*#?\s*:?\s*(\d{8,})\b", re.I)
KEYWORDS = (
    "appointment",
    "appt",
    "confirm",
    "reschedul",
    "cancel",
    "delivery #",
    "delivery#",
    "slot",
    "dock",
    "late",
    "wave",
    "rate con",
    "portal",
    "tender",
)


def build_session(key_path: Path, subject: str) -> Any:
    """Return an authorised requests session impersonating ``subject``."""
    try:
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2 import service_account
    except ImportError:  # pragma: no cover - environment guard
        sys.exit("install google-auth and requests first (see module docstring)")
    creds = service_account.Credentials.from_service_account_file(
        str(key_path), scopes=[SCOPE]
    ).with_subject(subject)
    return AuthorizedSession(creds)


def list_message_ids(session: Any, query: str, max_messages: int) -> list[str]:
    ids: list[str] = []
    token: str | None = None
    while True:
        params: dict[str, Any] = {"q": query, "maxResults": 500}
        if token:
            params["pageToken"] = token
        resp = session.get(f"{API}/messages", params=params, timeout=60)
        if resp.status_code != 200:
            raise RuntimeError(f"list failed {resp.status_code}: {resp.text[:400]}")
        data = resp.json()
        ids.extend(m["id"] for m in data.get("messages", []))
        token = data.get("nextPageToken")
        if not token or len(ids) >= max_messages:
            break
    return ids[:max_messages]


def _decode(data: str) -> str:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4)).decode("utf-8", "replace")


def _body_text(payload: dict[str, Any]) -> str:
    """Prefer text/plain; fall back to a crude tag-stripped text/html."""
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
    text = "\n".join(html)
    text = re.sub(r"<(script|style).*?</\1>", " ", text, flags=re.S | re.I)
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"[ \t]+", " ", text)


def strip_quoted(text: str) -> str:
    """Drop quoted history so each message contributes only its own words."""
    cut = re.split(
        r"\n(?:On .{0,120}wrote:|From: .{0,200}\nSent: |-----Original Message-----|>)", text, 1
    )
    return cut[0].strip()


def fetch_message(session: Any, message_id: str) -> dict[str, Any]:
    resp = session.get(f"{API}/messages/{message_id}", params={"format": "full"}, timeout=60)
    if resp.status_code != 200:
        raise RuntimeError(f"get {message_id} failed {resp.status_code}: {resp.text[:200]}")
    msg = resp.json()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    body = _body_text(msg["payload"])
    own = strip_quoted(body)
    try:
        sent_at = parsedate_to_datetime(headers.get("date", "")).astimezone(UTC).isoformat()
    except (TypeError, ValueError):
        sent_at = datetime.fromtimestamp(int(msg["internalDate"]) / 1000, tz=UTC).isoformat()
    return {
        "id": msg["id"],
        "thread_id": msg["threadId"],
        "sent_at": sent_at,
        "from": headers.get("from", ""),
        "to": headers.get("to", ""),
        "cc": headers.get("cc", ""),
        "subject": headers.get("subject", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "labels": msg.get("labelIds", []),
        "snippet": msg.get("snippet", ""),
        "body": body,
        "own_text": own,
        "attachments": [
            p.get("filename") for p in _iter_parts(msg["payload"]) if p.get("filename")
        ],
    }


def _iter_parts(part: dict[str, Any]) -> list[dict[str, Any]]:
    out = [part]
    for child in part.get("parts") or []:
        out.extend(_iter_parts(child))
    return out


def domain(addr: str) -> str:
    return parseaddr(addr)[1].split("@")[-1].lower() if addr else ""


def addresses(field: str) -> list[str]:
    return [a.strip().lower() for _, a in _parse_list(field) if a]


def _parse_list(field: str) -> list[tuple[str, str]]:
    from email.utils import getaddresses

    return getaddresses([field]) if field else []


def subject_template(subject: str) -> str:
    s = re.sub(r"^(?:(?:re|fw|fwd)\s*:\s*)+", "", subject.strip(), flags=re.I)
    s = DELIVERY_RE.sub("<DELIVERY#>", s)
    s = re.sub(r"\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}(?:/\d{2,4})?", "<DATE>", s)
    s = re.sub(r"\d+", "#", s)
    return s.strip()[:90]


def summarise(messages: list[dict[str, Any]], group: str) -> str:
    if not messages:
        return "No messages matched the query."
    by_thread: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for m in messages:
        by_thread[m["thread_id"]].append(m)
    for thread in by_thread.values():
        thread.sort(key=lambda m: m["sent_at"])

    from_domains = Counter(domain(m["from"]) for m in messages)
    external_to = Counter(
        a
        for m in messages
        if domain(m["from"]) == INTERNAL_DOMAIN
        for a in addresses(m["to"]) + addresses(m["cc"])
        if not a.endswith("@" + INTERNAL_DOMAIN)
    )
    opener_side = Counter(
        "Circle opens" if domain(t[0]["from"]) == INTERNAL_DOMAIN else "outside party opens"
        for t in by_thread.values()
    )
    first_reply_hours: list[float] = []
    for t in by_thread.values():
        opener = domain(t[0]["from"])
        reply = next((m for m in t[1:] if domain(m["from"]) != opener), None)
        if reply:
            dt = datetime.fromisoformat(reply["sent_at"]) - datetime.fromisoformat(t[0]["sent_at"])
            first_reply_hours.append(dt.total_seconds() / 3600)
    templates = Counter(subject_template(m["subject"]) for m in messages)
    delivery_hits = Counter(
        DELIVERY_RE.search(m["subject"] + " " + m["own_text"]) is not None for m in messages
    )
    delivery_prefixes = Counter(
        h.group(1)
        for m in messages
        for h in DELIVERY_RE.finditer(m["subject"] + " " + m["own_text"])
    )
    load_hits = sum(1 for m in messages if LOAD_RE.search(m["subject"] + " " + m["own_text"]))
    po_hits = sum(1 for m in messages if PO_RE.search(m["subject"] + " " + m["own_text"]))
    keyword_counts = Counter(
        k
        for m in messages
        for k in KEYWORDS
        if k in m["own_text"].lower() or k in m["subject"].lower()
    )
    weekday = Counter(datetime.fromisoformat(m["sent_at"]).strftime("%a") for m in messages)
    hour = Counter(datetime.fromisoformat(m["sent_at"]).hour for m in messages)
    attachments = Counter(Path(a).suffix.lower() for m in messages for a in m["attachments"] if a)
    days = max(
        1,
        (
            datetime.fromisoformat(max(m["sent_at"] for m in messages))
            - datetime.fromisoformat(min(m["sent_at"] for m in messages))
        ).days,
    )

    lines = [
        f"# {group} mail patterns",
        "",
        f"- Messages: {len(messages)} in {len(by_thread)} threads over {days} days "
        f"({len(messages) / days:.1f} messages/day, {len(by_thread) / days:.1f} threads/day)",
        f"- Messages per thread: median {statistics.median(len(t) for t in by_thread.values()):.0f}, "
        f"max {max(len(t) for t in by_thread.values())}",
        "",
        "## Who writes",
        "",
        *[f"- from {d or '(unknown)'}: {n}" for d, n in from_domains.most_common(12)],
        "",
        "## Where Circle sends (outside addresses on Circle-sent mail)",
        "",
        *[f"- {a}: {n}" for a, n in external_to.most_common(15)],
        "",
        "## Who starts a thread",
        "",
        *[f"- {k}: {n} threads" for k, n in opener_side.most_common()],
        (
            f"- first reply from the other side: median {statistics.median(first_reply_hours):.1f} h, "
            f"90th pct {sorted(first_reply_hours)[int(0.9 * (len(first_reply_hours) - 1))]:.1f} h "
            f"({len(first_reply_hours)} threads with a reply)"
            if first_reply_hours
            else "- no cross-party replies found"
        ),
        "",
        "## Subject templates (digits and delivery numbers normalised)",
        "",
        *[f"- {n:4d}  {t}" for t, n in templates.most_common(25)],
        "",
        "## Identifiers used",
        "",
        f"- messages carrying a Lidl delivery number: {delivery_hits[True]} of {len(messages)}",
        *[f"  - prefix {p}: {n}" for p, n in delivery_prefixes.most_common()],
        f"- messages carrying a Transport Pro load number: {load_hits}",
        f"- messages carrying a PO number: {po_hits}",
        "",
        "## Words that appear",
        "",
        *[f"- {k}: {n}" for k, n in keyword_counts.most_common()],
        "",
        "## When",
        "",
        "- by weekday: " + ", ".join(f"{d} {n}" for d, n in sorted(weekday.items())),
        "- by hour (UTC): " + ", ".join(f"{h:02d}:{n}" for h, n in sorted(hour.items())),
        "",
        "## Attachments",
        "",
        *([f"- {s or '(none)'}: {n}" for s, n in attachments.most_common()] or ["- none"]),
    ]
    return "\n".join(lines)


def main() -> None:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--key", type=Path, required=True, help="service-account JSON key")
    ap.add_argument("--subject", required=True, help="mailbox to read as (a member of the group)")
    ap.add_argument("--group", default="lidl@circledelivers.com")
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--max", type=int, default=3000, help="max messages to fetch")
    ap.add_argument("--out", type=Path, default=Path("data/lidl-mail"))
    ap.add_argument("--query", default=None, help="override the Gmail search query")
    args = ap.parse_args()

    session = build_session(args.key, args.subject)
    query = args.query or (
        f"(to:{args.group} OR cc:{args.group} OR from:{args.group} OR deliveredto:{args.group}) "
        f"newer_than:{args.days}d"
    )
    print(f"reading {args.subject} with query: {query}")
    try:
        ids = list_message_ids(session, query, args.max)
    except RuntimeError as exc:
        text = str(exc)
        if "unauthorized_client" in text or "403" in text or "401" in text:
            key = json.loads(args.key.read_text(encoding="utf-8"))
            print(
                "Gmail refused the service account. A Workspace admin must add domain-wide "
                f"delegation for client ID {key.get('client_id')} with scope {SCOPE}, and "
                f"{args.subject} must be a real user in the domain.",
                file=sys.stderr,
            )
        raise
    print(f"{len(ids)} messages match; fetching")
    args.out.mkdir(parents=True, exist_ok=True)
    messages: list[dict[str, Any]] = []
    with (args.out / "messages.jsonl").open("w", encoding="utf-8") as fh:
        for i, mid in enumerate(ids, start=1):
            msg = fetch_message(session, mid)
            messages.append(msg)
            fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
            if i % 100 == 0:
                print(f"  {i}/{len(ids)}")
    summary = summarise(messages, args.group)
    (args.out / "summary.md").write_text(summary, encoding="utf-8")
    print(summary)
    print(f"\nwrote {args.out / 'messages.jsonl'} and {args.out / 'summary.md'}")


if __name__ == "__main__":
    main()
