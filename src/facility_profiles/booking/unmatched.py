"""Booking mail the agent could not tie to a pickup: kept for a person, never dropped.

A facility's reply normally finds its pickup by the email IDs it answers, its thread, a PO it
names or the desk that wrote it (``service.match_with_how``). One that finds none (a new email
from someone else at the facility, with no PO in it) used to be skipped without a trace, and the
pickup chased again although the facility had answered. Such mail is kept here when it is about
booking, by the same rules that pick booking mail out of the group archive: its subject, a desk
the agent books with or its company, a number shaped like the customer's PO. Tour plans, tenders
and rate requests are not booking mail and are not kept.

A person links an item to its pickup (the agent then reads it as that pickup's reply) or
dismisses it. When a later pass finds the pickup itself (a case opened since), the item is
linked by the agent.

The text counts as well as the subject (``filters.body_reason``): a new desk writing under a
subject of its own is kept when its email, quoted history or attached files name a PO, a pickup
number or a pickup appointment.

An email the agent keeps failing to read (the model unreachable, a fault in the agent) is tried
again every pass while the inbox still looks back far enough to see it; one still failing when
it is about to fall out of that window is kept here too, with the error (:func:`keep_unreadable`),
so no email is ever dropped without a word.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from facility_profiles.booking.mail import InboundMessage
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    CaseStatus,
    ExceptionType,
    UnmatchedMail,
)
from facility_profiles.booking.worklist import flag
from facility_profiles.logging import get_logger
from facility_profiles.mailarchive.filters import (
    body_reason,
    is_dropped,
    match_reason,
    participants,
    rules_for,
)
from facility_profiles.storage.repository import as_utc

if TYPE_CHECKING:
    from facility_profiles.booking.classify import ReplyClassifier
    from facility_profiles.booking.respond import Responder
    from facility_profiles.booking.service import IngestStats
    from facility_profiles.config import Settings
    from facility_profiles.customers import Customers

log = get_logger(__name__)

OPEN = "open"
LINKED = "linked"
DISMISSED = "dismissed"
# The reason an email the agent kept failing to read is kept with (``unreadable: <error>``).
UNREADABLE = "unreadable"
# How long before an email leaves the inbox's look-back a failing one is kept for a person.
LAST_CHANCE = timedelta(hours=6)
# Mail services anyone can use: a shared domain there says nothing about who wrote.
FREE_MAIL = frozenset(
    {
        "gmail.com",
        "googlemail.com",
        "yahoo.com",
        "ymail.com",
        "outlook.com",
        "hotmail.com",
        "live.com",
        "msn.com",
        "aol.com",
        "icloud.com",
        "me.com",
        "comcast.net",
        "att.net",
        "verizon.net",
        "protonmail.com",
    }
)


def _find(session: Session, message: InboundMessage) -> UnmatchedMail | None:
    """The kept item for this email: by the source's id, else by its RFC Message-ID.

    One with no Message-ID is known by its sender, subject, text and time.
    """
    item = session.scalar(
        select(UnmatchedMail).where(UnmatchedMail.message_id == message.message_id)
    )
    if item is None and message.rfc_message_id:
        item = session.scalar(
            select(UnmatchedMail).where(
                func.lower(UnmatchedMail.rfc_message_id) == message.rfc_message_id.lower()
            )
        )
    if item is None and not message.rfc_message_id:
        same_subject = session.scalars(
            select(UnmatchedMail).where(
                UnmatchedMail.rfc_message_id.is_(None), UnmatchedMail.subject == message.subject
            )
        )
        item = next(
            (
                i
                for i in same_subject
                if message.looks_like(i.from_addr, i.subject, i.body, as_utc(i.sent_at))
            ),
            None,
        )
    return item


def booking_reason(
    session: Session, message: InboundMessage, known: Customers
) -> tuple[str | None, str | None]:
    """Why the email is about booking a pickup, and whose (a customer file's key), or (None, None).

    The customer is the one whose group the email went to; with none on it, every customer's
    rules are tried. A desk the agent books with, or another address at its company, also makes
    it booking mail.
    """
    addresses = participants(message.from_addr, message.to_addr, message.cc_addr)
    files = list(known.files)
    mine = [c for c in files if c.group and c.group.lower() in addresses]
    for customer in mine or files or [known.fallback]:
        rules = rules_for(customer)
        reason = match_reason(message.subject, addresses, rules)
        if reason is None and not is_dropped(message.subject, rules):
            # What the archive keeps for its text: a PO, a pickup number, a pickup appointment.
            reason = body_reason(f"{message.subject}\n{message.full_text}", rules)
        if reason is not None:
            # Whose it is only when it went to that customer's group: anything else is the
            # admins' to sort, never filed under whichever customer's rules happened to match.
            return reason, customer.key if mine and not customer.is_fallback else None
    sender = message.from_email
    domain = message.from_domain
    desks = {
        (email or "").lower()
        for email in session.scalars(select(BookingCase.contact_email).distinct())
        if email
    }
    key = mine[0].key if mine else None
    if sender in desks:
        return f"desk:{sender}", key
    if domain and domain not in FREE_MAIL and any(d.rpartition("@")[2] == domain for d in desks):
        return f"desk-company:{domain}", key
    return None, None


def keep_unmatched(session: Session, message: InboundMessage, known: Customers) -> str | None:
    """Keep booking mail no pickup matched: ``new`` when kept now, ``known`` when kept before.

    None when the email is not about booking, and nothing is kept.
    """
    if _find(session, message) is not None:
        return "known"
    reason, key = booking_reason(session, message, known)
    if reason is None:
        return None
    _keep(session, message, reason=reason, key=key)
    log.info("booking.unmatched_kept", subject=message.subject, reason=reason)
    return "new"


def leaving_soon(message: InboundMessage, *, days: int, now: datetime) -> bool:
    """Whether the email is about to fall out of the inbox's look-back of ``days`` days.

    The last :data:`LAST_CHANCE` of the window (at most half of it) counts as about to go.
    """
    window = timedelta(days=max(1, days))
    sent = message.sent_at if message.sent_at.tzinfo else message.sent_at.replace(tzinfo=UTC)
    return now - sent >= window - min(LAST_CHANCE, window / 2)


def keep_unreadable(
    session: Session, message: InboundMessage, error: str, known: Customers
) -> str | None:
    """Keep an email the agent keeps failing to read, before the look-back leaves it behind.

    Every pass tries a failed email again while it is inside the look-back; one still failing
    when it is about to fall out is kept here for a person, with the error, whatever it is
    about, so it is never dropped without a word. ``new`` when kept now, ``known`` when kept
    before (for any reason), None never.
    """
    if _find(session, message) is not None:
        return "known"
    addresses = participants(message.from_addr, message.to_addr, message.cc_addr)
    mine = [c for c in known.files if c.group and c.group.lower() in addresses]
    reason = f"{UNREADABLE}: {error}"
    _keep(session, message, reason=reason, key=mine[0].key if mine else None)
    log.warning("booking.unreadable_kept", subject=message.subject, error=error)
    return "new"


def is_kept(session: Session, message: InboundMessage) -> bool:
    """Whether the email is already with a person (kept, linked or dismissed)."""
    return _find(session, message) is not None


def _keep(session: Session, message: InboundMessage, *, reason: str, key: str | None) -> None:
    session.add(
        UnmatchedMail(
            message_id=message.message_id,
            rfc_message_id=message.rfc_message_id,
            thread_id=message.thread_id,
            in_reply_to=message.in_reply_to,
            references_header=message.references,
            sent_at=message.sent_at,
            from_addr=message.from_addr[:255],
            to_addr=message.to_addr[:512],
            cc_addr=message.cc_addr[:512],
            subject=message.subject[:512],
            body=message.body,
            quoted=message.quoted,
            customer_key=key,
            reason=reason[:128],
            status=OPEN,
        )
    )
    session.flush()


def settle_unmatched(session: Session, message: InboundMessage, case: BookingCase) -> None:
    """An email kept earlier has found its pickup on this pass: the item is linked to it."""
    item = _find(session, message)
    if item is None or item.status != OPEN:
        return
    item.status = LINKED
    item.case_id = case.id
    item.resolved_by = "agent"
    item.resolved_at = datetime.now(tz=UTC)
    item.note = "the agent found its pickup"
    session.flush()


def open_unmatched(session: Session) -> list[UnmatchedMail]:
    """Kept items still waiting for a person, newest first."""
    return list(
        session.scalars(
            select(UnmatchedMail)
            .where(UnmatchedMail.status == OPEN)
            .order_by(UnmatchedMail.sent_at.desc(), UnmatchedMail.id.desc())
        )
    )


def as_message(item: UnmatchedMail) -> InboundMessage:
    """The kept email as the agent reads mail."""
    sent = item.sent_at if item.sent_at.tzinfo else item.sent_at.replace(tzinfo=UTC)
    return InboundMessage(
        message_id=item.message_id,
        thread_id=item.thread_id,
        sent_at=sent,
        from_addr=item.from_addr or "",
        to_addr=item.to_addr or "",
        cc_addr=item.cc_addr or "",
        subject=item.subject or "",
        body=item.body or "",
        in_reply_to=item.in_reply_to,
        quoted=item.quoted or "",
        rfc_message_id=item.rfc_message_id,
        references=item.references_header,
    )


def _close(
    item: UnmatchedMail, status: str, *, by: str, note: str | None, case_id: int | None
) -> None:
    if item.status != OPEN:
        msg = f"mail {item.id} is {item.status} already"
        raise ValueError(msg)
    item.status = status
    item.case_id = case_id
    item.resolved_by = by
    item.resolved_at = datetime.now(tz=UTC)
    item.note = (note or "")[:255] or None


def link_unmatched(
    session: Session,
    item: UnmatchedMail,
    case: BookingCase,
    *,
    by: str,
    classifier: ReplyClassifier | None,
    settings: Settings,
    responder: Responder | None = None,
) -> IngestStats:
    """A person ties a kept email to its pickup; the agent reads it as that pickup's reply.

    Read the way any reply is (with the person's word as a strong tie), so a confirmation can be
    approved and a question answered from the case. Without a reader configured, the email is
    put on the case unread and a to-do asks a person to read it.
    """
    from facility_profiles.booking.service import (  # noqa: PLC0415 - service imports this module
        IngestStats,
        _inbound_record,
        _ingest_reply,
        _record_after_decision,
    )

    _close(item, LINKED, by=by, note=f"linked to case #{case.id}", case_id=case.id)
    message = as_message(item)
    stats = IngestStats(messages=1, new_mail=1)
    session.add(
        BookingEvent(
            case_id=case.id,
            action="mail_linked",
            actor=by,
            detail={"subject": message.subject, "from": message.from_addr, "mail_id": item.id},
        )
    )
    if case.status == CaseStatus.CANCELED.value:
        _record_after_decision(session, case, message)
        stats.after_decision += 1
    elif classifier is None:
        case.messages.append(
            _inbound_record(
                case,
                message,
                kind="reply",
                classification={"skipped": f"linked by {by}; no reader configured"},
            )
        )
        session.flush()
        flag(
            session,
            case,
            ExceptionType.HANDOFF,
            f"{by} linked an email from {message.from_email or 'the facility'}: read it and act",
            actor=by,
        )
    else:
        _ingest_reply(
            session,
            message,
            cases=[case],
            classifier=classifier,
            responder=responder,
            stats=stats,
            settings=settings,
            strong=True,
        )
    session.flush()
    return stats


def dismiss_unmatched(session: Session, item: UnmatchedMail, *, by: str, note: str) -> None:
    """A person says a kept email needs nothing from the agent."""
    _close(item, DISMISSED, by=by, note=note, case_id=None)
    session.flush()
