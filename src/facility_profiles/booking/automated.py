"""Mail no person wrote: bounces, out-of-office replies and delivery delays.

None of these is the facility's answer, so none is read by the model or answered. Each is kept
on its pickup's email chain all the same, because each says something a person may need:

- a **bounce** (the mail server sent the email back) means the facility never got it. It raises
  "Email did not arrive" at once, with the address and the server's reason, instead of looking
  like silence until the 24-hour to-do;
- an **out-of-office** or another automatic reply is kept and shown; the vendor's silence still
  counts, so the no-reply to-dos and the follow-up go on as if it had not come;
- a **delay** notice (the server is still trying) is kept and nothing is raised: the email may
  yet arrive, and a bounce follows if it does not.

A portal's own notices ("Appointment confirmed" from a scheduling system) are automatic too, but
they carry the booking, so they are read as replies; only an auto-reply is set aside.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from email.utils import parseaddr

from facility_profiles.booking.mail import InboundMessage, message_ids

BOUNCE = "bounce"
AUTO_REPLY = "auto_reply"
DELAYED = "delivery_delayed"
AUTOMATED_KINDS = frozenset({BOUNCE, AUTO_REPLY, DELAYED})

_DAEMON_ADDRESS = re.compile(r"^(?:mailer-?daemon|postmaster|mail-?daemon|mdaemon)@", re.I)
_DAEMON_NAME = re.compile(r"\bmail delivery (?:subsystem|system|service)\b|\bpostmaster\b", re.I)
_BOUNCE_SUBJECT = re.compile(
    r"^\W*(?:undeliverable|undelivered mail|delivery status notification \(failure\)|"
    r"mail delivery failed|delivery failure|failure notice|returned mail|message not delivered|"
    r"delivery has failed|could not be delivered|delivery incomplete|non-?delivery)",
    re.I,
)
_DELAY_SUBJECT = re.compile(
    r"delivery status notification \(delay\)|delivery delayed|delayed mail|message delayed|"
    r"warning: could not send message",
    re.I,
)
_AUTO_REPLY_SUBJECT = re.compile(
    r"^\W*(?:automatic reply|auto(?:matic)?[- ]?(?:reply|response)|autoreply|auto:|"
    r"out of (?:the )?office|ooo\b|away from (?:my |the )?(?:desk|office)|on vacation)",
    re.I,
)
# Auto-Submitted values (and stand-ins) that mark an automatic reply; "auto-generated" is a
# notification, such as a portal's, and is read.
_AUTO_REPLIED = ("auto-replied", "x-autoreply", "x-autorespond", "auto_reply")
_FAILED_TO = re.compile(
    r"(?:wasn't delivered to|was not delivered to|couldn't be delivered to|could not be delivered "
    r"to|delivery to the following recipients? failed|address not found|failed recipient|"
    r"final-recipient:\s*rfc822;)[^@\n]{0,80}?([\w.+-]+@[\w-]+(?:\.[\w-]+)+)",
    re.I,
)
_REASON = re.compile(
    r"[^\n]*(?:\b5\d\d\b|\b5\.\d{1,3}\.\d{1,3}\b|does ?n[o']t exist|not found|no such user|"
    r"user unknown|unknown user|rejected|unavailable|mailbox (?:is )?full|over quota|blocked|"
    r"couldn't be found|could not be found|wasn't delivered|was not delivered)[^\n]*",
    re.I,
)
_HEADER_ID = re.compile(r"message-id:\s*(<[^<>\s]+>)", re.I)


def automated_kind(message: InboundMessage) -> str | None:
    """:data:`BOUNCE`, :data:`DELAYED` or :data:`AUTO_REPLY` for mail no person wrote, else None."""
    name, address = parseaddr(message.from_addr)
    daemon = bool(_DAEMON_ADDRESS.match(address or message.from_email)) or bool(
        _DAEMON_NAME.search(name or "")
    )
    subject = message.subject or ""
    if _DELAY_SUBJECT.search(subject):
        return DELAYED
    if daemon or _BOUNCE_SUBJECT.match(subject):
        return BOUNCE
    auto = (message.auto_submitted or "").lower()
    if any(auto.startswith(a) for a in _AUTO_REPLIED) or _AUTO_REPLY_SUBJECT.match(subject):
        return AUTO_REPLY
    return None


def bounced_ids(message: InboundMessage) -> list[str]:
    """The Message-IDs of the email a bounce sent back, lower case, with angle brackets.

    Those it answers (In-Reply-To, References), then any the returned email's headers show in
    the bounce's text ("Message-ID: <...>").
    """
    found = message.referenced_ids
    for token in _HEADER_ID.findall(message.full_text):
        lowered = token.lower()
        if lowered not in found:
            found.append(lowered)
    return message_ids(" ".join(found))


def bounce_details(message: InboundMessage, desks: Iterable[str | None]) -> tuple[str | None, str]:
    """Who the email did not reach, and the server's reason, from a bounce's text.

    ``desks`` are the addresses the email was sent to; one named in the bounce is the answer.
    """
    text = message.full_text
    lowered = text.lower()
    recipient = next((d for d in desks if d and d.lower() in lowered), None)
    if recipient is None:
        failed = _FAILED_TO.search(text)
        recipient = failed.group(1) if failed else None
    # The fullest line the server wrote ("Your message wasn't delivered to ... because ...")
    # says more than a heading ("Address not found").
    lines = [" ".join(m.group(0).split()) for m in _REASON.finditer(text)]
    said = max(lines, key=len, default="")[:160]
    return recipient, said or "the mail server sent it back"
