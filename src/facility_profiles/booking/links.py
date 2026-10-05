"""Click-to-confirm: the pickup times a request offers, as signed links the vendor clicks.

A request can carry, after its PO lines, one link per pickup to a small page where the vendor
picks one of the times offered or proposes another. In the email's HTML part the times are
buttons that open the page with that time chosen. Opening the page changes nothing, because
mail scanners open every link in a message: the vendor confirms with one more click, and only
that POST is recorded.

A time picked is the vendor's answer without a model call. It is recorded like an emailed
confirmation, with the vendor's pickup number when they give one. With
FP_BOOKING_LINK_AUTO_SCHEDULE on (the default) it is booked straight away: every time offered
was checked against the delivery and the desk's rules before it went out, and is checked again
on the click. A time proposed is raised for a person, like a counter-offer.

Links are off until FP_BOOKING_LINK_BASE_URL (the public address the pages are served from,
``facility-profiles serve-links``) and FP_BOOKING_LINK_SECRET (the signing key) are both set.
Without them requests are written exactly as before.

Times on the buttons and the page are Eastern, as in every email; a time the vendor proposes is
read as Eastern too. A link lives FP_BOOKING_LINK_VALID_HOURS weekday hours (weekends do not
count), and never past the last time it offers.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy.orm import Session

from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
    CaseStatus,
    SlotOffer,
)
from facility_profiles.booking.respond import local_dt, offer_is_feasible
from facility_profiles.booking.rules import VendorProfile, slot_is_stale, too_early
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.timers import after_weekday_hours, fmt_slot, slot_at
from facility_profiles.clock import EASTERN, eastern_to_local
from facility_profiles.config import Settings
from facility_profiles.storage.repository import as_utc

# A desk given dates only is offered the day asked for and the next weekdays, this many in all.
DATE_ONLY_DAYS = 3
ACTOR = "vendor (link)"
_DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
_B36 = "0123456789abcdefghijklmnopqrstuvwxyz"


class LinkError(ValueError):
    """A link that is not ours (bad signature) or names no offer."""


def links_enabled(settings: Settings) -> bool:
    """True when request emails carry links: a public address and a signing key are set."""
    return bool(settings.booking_link_base_url and settings.booking_link_secret)


# ------------------------------------------------------------------ the times offered


def _feasible(case: BookingCase, slot: str, settings: Settings, *, now: datetime) -> bool:
    day, _, clock = slot.partition(" ")
    at = local_dt(day, clock or settings.booking_default_pickup_time, case.vendor_timezone)
    return offer_is_feasible(case, at, settings, now=now)[0]


def offered_slots(
    case: BookingCase, settings: Settings, profile: VendorProfile | None, *, now: datetime
) -> list[str]:
    """The times to offer for the slot asked for, earliest first; empty when there is none.

    A desk given exact times is offered the time asked for and the offsets around it on the
    same day (FP_BOOKING_LINK_OFFSETS_MINUTES). A desk given dates only is offered the day asked
    for and the next weekdays. Each one must still be asked for (not passed, not inside the
    notice window or past the desk's cut-off, not further ahead than the desk books) and must
    still make the delivery.
    """
    requested = case.requested_local
    if not requested:
        return []
    day, _, clock = requested.partition(" ")
    if (profile is not None and profile.date_only) or not clock:
        candidates = [day]
        cursor = date.fromisoformat(day)
        while len(candidates) < DATE_ONLY_DAYS:
            cursor += timedelta(days=1)
            if cursor.weekday() < 5:
                candidates.append(cursor.isoformat())
    else:
        start = datetime.strptime(requested, "%Y-%m-%d %H:%M")
        times = [
            start + timedelta(minutes=m)
            for m in sorted({0, *settings.booking_link_offsets_minutes})
        ]
        candidates = [t.strftime("%Y-%m-%d %H:%M") for t in times if t.date() == start.date()]
    tz = case.vendor_timezone
    return [
        slot
        for slot in candidates
        if slot_is_stale(slot, tz, settings, now=now, profile=profile) is None
        and too_early(slot, tz, profile, now=now) is None
        and _feasible(case, slot, settings, now=now)
    ]


def create_offer(
    session: Session,
    case: BookingCase,
    settings: Settings,
    profile: VendorProfile | None,
    *,
    now: datetime,
) -> SlotOffer | None:
    """Offer the times for the case's request; earlier open offers for it are superseded."""
    slots = offered_slots(case, settings, profile, now=now)
    if not slots:
        return None
    for old in case.offers:
        if old.answered_at is None and old.superseded_at is None:
            old.superseded_at = now
    # In UTC: SQLite keeps the wall clock and drops the zone, and the token signs this instant.
    last = max(at for s in slots if (at := slot_at(s, case.vendor_timezone)) is not None)
    lives = after_weekday_hours(now, settings.booking_link_valid_hours, EASTERN)
    expires = min(lives, last).astimezone(UTC)
    offer = SlotOffer(case_id=case.id, slots=slots, created_at=now, expires_at=expires)
    case.offers.append(offer)
    session.flush()
    return offer


# ------------------------------------------------------------------ signed links


def _secret(settings: Settings) -> bytes:
    if settings.booking_link_secret is None:
        msg = "FP_BOOKING_LINK_SECRET is not set"
        raise LinkError(msg)
    return settings.booking_link_secret.get_secret_value().encode()


def _b36(number: int) -> str:
    out = ""
    while True:
        number, digit = divmod(number, 36)
        out = _B36[digit] + out
        if not number:
            return out


def _sign(secret: bytes, payload: str) -> str:
    digest = hmac.new(secret, payload.encode(), hashlib.sha256).digest()[:15]
    return base64.urlsafe_b64encode(digest).decode()


def _expiry(offer: SlotOffer) -> int:
    expires = as_utc(offer.expires_at)
    assert expires is not None
    return int(expires.timestamp())


def offer_token(offer: SlotOffer, settings: Settings) -> str:
    """The signed token for an offer: its id and expiry, and an HMAC over both."""
    payload = f"{offer.id}.{_b36(_expiry(offer))}"
    return f"{payload}.{_sign(_secret(settings), payload)}"


def offer_url(offer: SlotOffer, settings: Settings) -> str:
    """The link the vendor opens."""
    return f"{settings.booking_link_base_url}/c/{offer_token(offer, settings)}"


def read_token(session: Session, token: str, settings: Settings) -> SlotOffer:
    """The offer a token names; :class:`LinkError` when it is not one this agent signed."""
    parts = token.split(".")
    if len(parts) != 3 or not parts[0].isdigit():
        msg = "not a booking link"
        raise LinkError(msg)
    payload = f"{parts[0]}.{parts[1]}"
    if not hmac.compare_digest(parts[2], _sign(_secret(settings), payload)):
        msg = "not a booking link"
        raise LinkError(msg)
    offer = session.get(SlotOffer, int(parts[0]))
    if offer is None or _b36(_expiry(offer)) != parts[1]:
        msg = "not a booking link"
        raise LinkError(msg)
    return offer


# ------------------------------------------------------------------ the email


def link_lines(cases: list[BookingCase], urls: Mapping[int, str]) -> str:
    """The text that carries the links: one line for one pickup, one per PO in a batch."""
    found = [(c, urls[c.id]) for c in cases if c.id in urls]
    if not found:
        return ""
    if len(cases) == 1:
        return f"Or confirm a time with one click: {found[0][1]}"
    lines = ["Or confirm a time with one click:"]
    for case, url in found:
        pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
        lines.append(f"PO# {pos}: {url}")
    return "\n".join(lines)


_BUTTON = (
    "display:inline-block;margin:4px 6px 4px 0;padding:8px 14px;border:1px solid #1a5fb4;"
    "border-radius:4px;color:#1a5fb4;text-decoration:none;font-weight:bold"
)


def html_body(text: str, offers: Mapping[str, SlotOffer]) -> str:
    """The email's HTML part: the text as written, with each link shown as one button per time.

    ``offers`` maps each link's URL to its offer. Every other line is the text, escaped.
    """
    out: list[str] = []
    for line in text.split("\n"):
        hit = next(((url, o) for url, o in offers.items() if url in line), None)
        if hit is None:
            out.append(f"{html.escape(line)}<br>")
            continue
        url, offer = hit
        label = line.replace(url, "").strip().rstrip(":").strip() or "Confirm a time"
        buttons = "".join(
            f'<a href="{html.escape(url)}?s={i}" style="{_BUTTON}">'
            f"{html.escape(fmt_slot(s, offer.case.vendor_timezone))}</a>"
            for i, s in enumerate(offer.slots)
        )
        other = f'<a href="{html.escape(url)}#propose" style="color:#1a5fb4">another time</a>'
        out.append(f"{html.escape(label)}:<br>{buttons}<br>or propose {other}<br>")
    body = "\n".join(out)
    return (
        '<!doctype html><html><body style="font-family:Arial,Helvetica,sans-serif;'
        f'font-size:14px;line-height:1.4;color:#222">\n{body}\n</body></html>'
    )


# ------------------------------------------------------------------ the vendor's answer


@dataclass(frozen=True)
class LinkAnswer:
    """What became of a click, and the one line the vendor's page says about it."""

    ok: bool
    say: str


def offer_state(offer: SlotOffer, now: datetime) -> str:
    """open, answered, superseded (a later request replaced it), closed or expired."""
    if offer.answered_at is not None:
        return "answered"
    if offer.superseded_at is not None:
        return "superseded"
    if offer.case.status in (CaseStatus.SCHEDULED.value, CaseStatus.CANCELED.value):
        return "closed"
    expires = as_utc(offer.expires_at)
    if expires is not None and expires <= now:
        return "expired"
    return "open"


def state_says(offer: SlotOffer, state: str) -> str:
    """The vendor-facing line for an offer that cannot be answered any more."""
    if state == "answered":
        if offer.answer == "proposed":
            return "Thank you, we have your proposed time and will confirm it by email."
        shown = fmt_slot(offer.answer, offer.case.vendor_timezone)
        return f"Thank you, the pickup is set for {shown}."
    return {
        "superseded": "A newer email about this pickup replaced this link. Please use that one.",
        "closed": "This pickup is already settled. Reply to the email if something changed.",
        "expired": "This link has expired. Please reply to the email with a time that works.",
    }.get(state, "This link cannot be used.")


def _clean(value: str | None, limit: int) -> str | None:
    text = re.sub(r"\s+", " ", value or "").strip()[:limit]
    return text or None


def _record(
    session: Session,
    case: BookingCase,
    offer: SlotOffer,
    *,
    now: datetime,
    body: str,
    reading: dict[str, object],
) -> BookingMessage:
    """The vendor's answer as an inbound message on the case, and the request marked sent."""
    from facility_profiles.booking.service import mark_sent  # noqa: PLC0415 - service uses links

    if case.status == CaseStatus.UNSCHEDULED.value:
        # The vendor holds the link, so the request reached them: a person sent the draft.
        mark_sent(session, case, by=ACTOR, sent_at=now)
    message = BookingMessage(
        case_id=case.id,
        direction="in",
        kind="link",
        from_addr=ACTOR,
        body=body,
        sent_at=now,
        classification={**reading, "via": "link", "offer_id": offer.id},
    )
    case.messages.append(message)
    session.flush()
    return message


def confirm(
    session: Session,
    offer: SlotOffer,
    index: int,
    *,
    settings: Settings,
    now: datetime,
    pickup_number: str | None = None,
    name: str | None = None,
) -> LinkAnswer:
    """The vendor picked an offered time: record it, and book it unless set to wait."""
    from facility_profiles.booking.service import apply_reply, approve  # noqa: PLC0415

    state = offer_state(offer, now)
    if state != "open":
        return LinkAnswer(False, state_says(offer, state))
    if not 0 <= index < len(offer.slots):
        return LinkAnswer(False, "Please choose one of the times listed.")
    case = offer.case
    slot = str(offer.slots[index])
    tz = case.vendor_timezone
    shown = fmt_slot(slot, tz)
    # The vendor's own cut-off is theirs to waive; ours, and the delivery, are not.
    if slot_is_stale(slot, tz, settings, now=now) or not _feasible(case, slot, settings, now=now):
        return LinkAnswer(
            False,
            f"{shown} is too close now for us to send a driver. "
            "Please choose a later time or reply to the email.",
        )
    number = _clean(pickup_number, 64)
    who = _clean(name, 80)
    day, _, clock = slot.partition(" ")
    said = f"Picked {shown} from the link" + (f", PU# {number}" if number else "")
    message = _record(
        session,
        case,
        offer,
        now=now,
        body=said + (f" ({who})" if who else ""),
        reading={
            "status": "confirmed",
            "pickup_date": day,
            "pickup_time": clock or None,
            "pickup_number": number,
        },
    )
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date=day,
        pickup_time=clock or None,
        pickup_number=number,
    )
    action = apply_reply(
        session, case, reading, [], actor=ACTOR, reply_sent_at=now, message_id=message.id
    )
    offer.answered_at = now
    offer.answer = slot
    offer.answer_detail = {"pickup_number": number, "name": who, "message_id": message.id}
    session.add(
        _event(
            case, "confirmed_by_link", local=slot, pickup_number=number, name=who, offer_id=offer.id
        )
    )
    if action != "vendor_confirmed":
        return LinkAnswer(False, "We could not record that time. Please reply to the email.")
    if settings.booking_link_auto_schedule:
        approve(session, case, by="agent")
        case.reason = f"vendor picked {shown} from the link"
        return LinkAnswer(True, f"Thank you, the pickup is set for {shown}.")
    return LinkAnswer(True, f"Thank you, we have {shown} and will confirm it shortly.")


def propose(
    session: Session,
    offer: SlotOffer,
    *,
    settings: Settings,
    now: datetime,
    day: str,
    clock: str | None = None,
    note: str | None = None,
    name: str | None = None,
) -> LinkAnswer:
    """The vendor proposed a time of their own: record it as an offer for a person to settle.

    The page asks for the time in Eastern, like every time we write; it is kept on the
    facility's clock.
    """
    from facility_profiles.booking.service import apply_reply  # noqa: PLC0415

    state = offer_state(offer, now)
    if state != "open":
        return LinkAnswer(False, state_says(offer, state))
    day = day.strip()
    clock = (clock or "").strip() or None
    if not _DAY_RE.match(day) or (clock is not None and not _CLOCK_RE.match(clock)):
        return LinkAnswer(False, "Please give the date (and, if you can, the time) that works.")
    if clock:  # the page asks for Eastern; the case keeps the facility's own clock
        moved = eastern_to_local(f"{day} {clock}", offer.case.vendor_timezone) or ""
        day, _, clock = moved.partition(" ")
    try:
        at = local_dt(day, clock or "23:59", offer.case.vendor_timezone)
    except ValueError:
        return LinkAnswer(False, "Please give a real date.")
    if at <= now:
        return LinkAnswer(False, "That time has passed. Please give a later one.")
    case = offer.case
    text = _clean(note, 300)
    who = _clean(name, 80)
    slot = f"{day} {clock or ''}".strip()
    feasible, verdict = offer_is_feasible(
        case,
        local_dt(day, clock or settings.booking_default_pickup_time, case.vendor_timezone),
        settings,
        now=now,
    )
    message = _record(
        session,
        case,
        offer,
        now=now,
        body=f"Proposed {fmt_slot(slot, case.vendor_timezone)} from the link"
        + (f": {text}" if text else "")
        + (f" ({who})" if who else ""),
        reading={"status": "counter_offer", "pickup_date": day, "pickup_time": clock},
    )
    reading = ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date=day,
        pickup_time=clock,
        conditions=[text] if text else [],
    )
    apply_reply(session, case, reading, [], actor=ACTOR, reply_sent_at=now, message_id=message.id)
    offer.answered_at = now
    offer.answer = "proposed"
    offer.answer_detail = {
        "date": day,
        "time": clock,
        "note": text,
        "name": who,
        "message_id": message.id,
    }
    session.add(
        _event(
            case,
            "proposed_by_link",
            local=slot,
            feasible=feasible,
            reason=verdict,
            note=text,
            offer_id=offer.id,
        )
    )
    return LinkAnswer(True, "Thank you, we have your proposed time and will confirm it by email.")


def _event(case: BookingCase, action: str, **detail: object) -> BookingEvent:
    return BookingEvent(case_id=case.id, action=action, actor=ACTOR, detail=detail)
