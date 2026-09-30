"""What happens to a case when an outbound message leaves: drafted, or sent with its ids recorded.

Shared by the service (requests, reschedules) and the responder (answers, acknowledgements) so
that every message the agent sends is recorded the same way: the Gmail id and thread of the
sending mailbox, the RFC Message-ID that vendors' replies will point back at, and a ``sent``
event naming what went out and to whom.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from facility_profiles.booking.mail import Delivery, Mailer, OutboundDraft, Sender, deliver
from facility_profiles.booking.models import BookingCase, BookingEvent, BookingMessage
from facility_profiles.config import Settings


def is_sender(outbox: Mailer | Sender) -> bool:
    """True when the outbox delivers rather than drafts."""
    return callable(getattr(outbox, "deliver", None))


class SendRefusedError(ValueError):
    """The send gate said no; the message was neither drafted nor sent."""


def check_send_gate(
    session: Session,
    case: BookingCase,
    draft: OutboundDraft,
    settings: Settings,
    *,
    trusted_desk: str | None,
) -> None:
    """The deterministic checks every unattended send must pass.

    The recipient must be the desk the facility profile trusts (a human-set or verified email),
    the mode must allow sending, and the day's cap must not be reached. The wording is the
    caller's responsibility: only template text reaches this point today.
    """
    if settings.booking_mode != "send":
        msg = "FP_BOOKING_MODE is not 'send'; the agent may only draft"
        raise SendRefusedError(msg)
    to = draft.to_addr.strip().lower()
    if not trusted_desk or to != trusted_desk.strip().lower():
        msg = (
            f"case {case.id}: {draft.to_addr or 'no recipient'} is not the trusted desk on the "
            f"profile ({trusted_desk or 'none'})"
        )
        raise SendRefusedError(msg)
    since = datetime.now(tz=UTC) - timedelta(hours=24)
    sent_today = session.scalar(
        select(func.count())
        .select_from(BookingMessage)
        .where(BookingMessage.direction == "out", BookingMessage.sent_at >= since)
    )
    if (sent_today or 0) >= settings.booking_send_daily_cap:
        msg = f"daily send cap of {settings.booking_send_daily_cap} reached; nothing more goes out"
        raise SendRefusedError(msg)


def dispatch(
    session: Session,
    case: BookingCase,
    message: BookingMessage,
    outbox: Mailer | Sender,
    draft: OutboundDraft,
    *,
    actor: str = "agent",
) -> Delivery:
    """Draft or send ``draft``, then record the outcome on ``message`` and ``case``."""
    result = deliver(outbox, draft)
    record_delivery(session, case, message, draft, result, actor=actor)
    return result


def record_delivery(
    session: Session,
    case: BookingCase,
    message: BookingMessage,
    draft: OutboundDraft,
    result: Delivery,
    *,
    actor: str = "agent",
) -> None:
    """Write what the outbox did onto the stored message and the case.

    Drafts leave a reference a person can find. Sends leave the Gmail id and thread of the
    sending mailbox and the Message-ID that went out; the case learns its thread from the first
    send, and a ``sent`` event names the recipient. The caller sets the case status.
    """
    message.draft_ref = result.ref
    if result.sent:
        message.sent_at = datetime.now(tz=UTC)
        message.message_id = result.gmail_id or message.message_id
        message.rfc_message_id = result.rfc_message_id
        message.thread_id = result.thread_id or message.thread_id
        message.in_reply_to = draft.in_reply_to
        message.references_header = draft.references
        if not case.thread_id and result.thread_id:
            case.thread_id = result.thread_id
        session.flush()
        detail: dict[str, Any] = {
            "to": draft.to_addr,
            "subject": draft.subject,
            "gmail_id": result.gmail_id,
            "thread_id": result.thread_id,
            "rfc_message_id": result.rfc_message_id,
            "kind": message.kind,
        }
        session.add(BookingEvent(case_id=case.id, action="sent", actor=actor, detail=detail))
