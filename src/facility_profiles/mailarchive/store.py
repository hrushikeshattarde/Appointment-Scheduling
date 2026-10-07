"""The archive's S3 layout, and writes that are safe to repeat.

    mail/<yyyy>/<mm>/<dd>/<key>.eml     the raw RFC822 message
    mail/<yyyy>/<mm>/<dd>/<key>.json    the parsed envelope (headers, own words, identifiers,
                                        attachment manifest, why it was kept)
    attachments/<sha256[:2]>/<sha256>   every attachment once, addressed by content
    state/threads.json                  kept thread ids and the reason each qualified
    state/kept-ids.json                 kept Message-IDs and the day kept (a reply under a new
                                        subject is kept by the email it answers)
    state/body-checked.json             Gmail ids whose text was read and found not about
                                        booking, so a pass does not fetch them again
    state/last-run.json                 what the last collection pass did

``<key>`` is the first 24 hex characters of the sha256 of the RFC ``Message-ID`` header. Gmail's
own message id differs per mailbox, so keying on it would store the same email twice when a second
member's mailbox is read for history. A message with no Message-ID falls back to ``g-<gmail id>``.
The date prefix is Gmail's receipt time, so it never moves when a message is re-fetched.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

MAIL_PREFIX = "mail"
ATTACHMENT_PREFIX = "attachments"
THREADS_KEY = "state/threads.json"
KEPT_IDS_KEY = "state/kept-ids.json"
BODY_CHECKED_KEY = "state/body-checked.json"
LAST_RUN_KEY = "state/last-run.json"
_MISSING = ("404", "NoSuchKey", "NotFound")


def message_key(rfc_message_id: str | None, gmail_id: str) -> str:
    """The archive key of a message: stable across mailboxes when it carries a Message-ID."""
    rid = (rfc_message_id or "").strip().strip("<>").lower()
    if not rid:
        return f"g-{gmail_id}"
    return hashlib.sha256(rid.encode("utf-8")).hexdigest()[:24]


def mail_base(key: str, internal_date: str | None) -> str:
    """``mail/<yyyy>/<mm>/<dd>/<key>`` without the extension."""
    day = (internal_date or "")[:10]
    if len(day) != 10 or day[4] != "-":
        return f"{MAIL_PREFIX}/unknown/{key}"
    return f"{MAIL_PREFIX}/{day[:4]}/{day[5:7]}/{day[8:10]}/{key}"


def attachment_key(sha256: str) -> str:
    """``attachments/<ab>/<sha256>``."""
    return f"{ATTACHMENT_PREFIX}/{sha256[:2]}/{sha256}"


@dataclass(frozen=True)
class Stored:
    """The outcome of one write."""

    key: str
    bytes_written: int
    skipped: bool = False  # already there; nothing was sent


class Store:
    """S3 for one bucket and prefix. Holds no state of its own."""

    def __init__(
        self, bucket: str, prefix: str = "", client: Any = None, region: str | None = None
    ) -> None:
        if not bucket:
            msg = "a bucket is required"
            raise ValueError(msg)
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        if client is not None:
            self.s3 = client
        else:  # pragma: no cover - live AWS
            import boto3  # noqa: PLC0415

            self.s3 = boto3.session.Session(region_name=region).client("s3")

    def full(self, key: str) -> str:
        """The object key with the prefix applied."""
        return self.prefix + key

    # -- writes -----------------------------------------------------------------------------

    def put(
        self,
        key: str,
        data: bytes,
        *,
        content_type: str = "application/octet-stream",
        metadata: dict[str, str] | None = None,
        if_absent: bool = True,
    ) -> Stored:
        """Write one object; with ``if_absent`` an object already at that key is left alone."""
        k = self.full(key)
        if if_absent and self.exists(key):
            return Stored(k, 0, skipped=True)
        extra: dict[str, Any] = {"ContentType": content_type}
        if metadata:
            extra["Metadata"] = {m_k: _header_safe(m_v) for m_k, m_v in metadata.items()}
        self.s3.put_object(Bucket=self.bucket, Key=k, Body=data, **extra)
        return Stored(k, len(data))

    def put_json(self, key: str, obj: Any) -> Stored:
        """Write (or overwrite) a small JSON document such as the thread register."""
        data = json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True).encode("utf-8")
        return self.put(key, data, content_type="application/json", if_absent=False)

    # -- reads ------------------------------------------------------------------------------

    def exists(self, key: str) -> bool:
        """HEAD the object; a missing key is a plain no."""
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self.s3.head_object(Bucket=self.bucket, Key=self.full(key))
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in _MISSING:
                return False
            raise

    def get(self, key: str) -> bytes:
        """The object's bytes."""
        body: bytes = self.s3.get_object(Bucket=self.bucket, Key=self.full(key))["Body"].read()
        return body

    def get_json(self, key: str) -> Any:
        """A JSON document, or None when the key does not exist."""
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            return json.loads(self.get(key))
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in _MISSING:
                return None
            raise

    def list_keys(self, prefix: str) -> list[str]:
        """Every key under ``prefix`` (relative to the store prefix), paginated."""
        out: list[str] = []
        token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"Bucket": self.bucket, "Prefix": self.full(prefix)}
            if token:
                kwargs["ContinuationToken"] = token
            page = self.s3.list_objects_v2(**kwargs)
            out.extend(str(o["Key"])[len(self.prefix) :] for o in page.get("Contents") or [])
            token = page.get("NextContinuationToken") if page.get("IsTruncated") else None
            if not token:
                break
        return out

    def writable(self) -> tuple[bool, str]:
        """Can this process write here? Asked before a run, not on the first failure."""
        from botocore.exceptions import ClientError  # noqa: PLC0415

        probe = self.full(".write-probe")
        try:
            self.s3.put_object(Bucket=self.bucket, Key=probe, Body=b"ok")
            self.s3.delete_object(Bucket=self.bucket, Key=probe)
            return True, f"s3://{self.bucket}/{self.prefix} is writable"
        except ClientError as e:
            err = e.response.get("Error", {})
            detail = f"{err.get('Code', '?')} {err.get('Message', '')[:120]}"
            return False, f"s3://{self.bucket}/{self.prefix}: {detail}"


def _header_safe(s: str) -> str:
    """S3 user metadata travels in HTTP headers: ASCII, no newlines, short."""
    return "".join(c for c in (s or "") if 32 <= ord(c) < 127)[:900]
