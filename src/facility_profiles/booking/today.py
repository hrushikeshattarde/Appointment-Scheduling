"""What needs a person today: the daily summary, and the words the board uses for a case.

The summary is one page a pod lead reads in the morning (``booking today``, or ``GET
/api/booking/today`` for a scheduler or the board's "Daily summary" button):

- every case with something open, listed once under its most urgent to-do, most urgent first;
- the pickups today and on the next business day, with where each one stands;
- the drafts nobody has sent yet (in draft mode the agent writes, a person sends);
- how many requests are out and waiting on the vendor, and what changed in the last 24 hours.

It is plain text, so it reads the same in a terminal, a file, an email or a chat. Every time
in it is Eastern.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.service import has_request
from facility_profiles.booking.timers import fmt_slot, pickup_passed
from facility_profiles.booking.worklist import KINDS, UNANSWERED
from facility_profiles.clock import LABEL, local_to_eastern, stamp
from facility_profiles.storage.repository import as_utc

SILENCE = frozenset(k.value for k in UNANSWERED)

# Most urgent first. A pickup that already slipped, or a vendor silent for two days, comes before
# a desk missing from a profile.
URGENCY: tuple[str, ...] = (
    ExceptionType.LOAD_CANCELED.value,
    ExceptionType.BOOKED_SLOT_CHANGED.value,
    ExceptionType.PICKUP_EXPIRED.value,
    ExceptionType.LOAD_INFEASIBLE.value,
    ExceptionType.CHECK_BACK_TOO_LATE.value,
    ExceptionType.UNANSWERED_48H.value,
    ExceptionType.TIME_ZONE_UNCLEAR.value,
    ExceptionType.CONFIRMATION_REVIEW.value,
    ExceptionType.CONFIRMED_OUTSIDE_WINDOW.value,
    ExceptionType.STALE_CONFIRMATION.value,
    ExceptionType.PROPOSED_TIME_REVIEW.value,
    ExceptionType.FACILITY_QUESTION.value,
    ExceptionType.FACILITY_DECLINED.value,
    ExceptionType.DELIVERY_MOVED.value,
    ExceptionType.SLOT_UNWORKABLE.value,
    ExceptionType.HANDOFF.value,
    ExceptionType.AUTOMATION_FAILED.value,
    ExceptionType.TPRO_MISMATCH.value,
    ExceptionType.UNANSWERED_24H.value,
    ExceptionType.MISSING_METHOD.value,
    ExceptionType.METHOD_NOT_SUPPORTED.value,
)
BOOKED_EVENTS = frozenset({"approved", "marked_booked"})


def label(kind: str) -> str:
    """The plain-language name of an exception kind."""
    return KINDS.get(kind, (kind.replace("_", " ").capitalize(), ""))[0]


def pickup_slot(case: BookingCase) -> tuple[str | None, str]:
    """The pickup to show, on the Eastern clock: the confirmed slot, else the requested one."""
    if case.confirmed_local:
        return local_to_eastern(case.confirmed_local, case.vendor_timezone), "confirmed"
    return local_to_eastern(case.requested_local, case.vendor_timezone), "requested"


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def stage(case: BookingCase) -> str:
    """Where the case stands, in a few words for the person reading the board.

    A case with something open says who it is waiting on: a pending case whose vendor asked a
    question is waiting on us, not on the vendor.
    """
    open_now = case.open_exceptions
    first = label(open_now[0].kind).lower() if open_now else ""
    if case.status == CaseStatus.UNSCHEDULED.value:
        if open_now:
            return f"Not requested: {first}"
        if has_request(case):
            return "Draft waiting to be sent"
        # A desk that does not book that far ahead yet leaves its reason here.
        return f"Not requested yet: {case.reason}" if case.reason else "Not requested yet"
    if case.status == CaseStatus.PENDING.value:
        if any(e.kind == ExceptionType.CONFIRMATION_REVIEW for e in open_now):
            return "Confirmed by the vendor, needs approval"
        if open_now and all(e.kind in SILENCE for e in open_now):
            return f"Waiting on the vendor: {first}"  # their silence, not something on us
        if open_now:
            return f"Waiting on us: {first}"
        return _sentence(case.reason or "waiting on the vendor")
    if case.status == CaseStatus.SCHEDULED.value:
        return _sentence(case.reason or "booked")
    if case.status == CaseStatus.DECLINED.value:
        return _sentence(case.reason or "vendor cannot book")
    return _sentence(case.reason or "canceled")


# ------------------------------------------------------------------ the summary


def _rank(kind: str) -> int:
    return URGENCY.index(kind) if kind in URGENCY else len(URGENCY)


def _next_business_day(day: date) -> date:
    day += timedelta(days=1)
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def _age(raised: datetime | None, now: datetime) -> str:
    at = as_utc(raised)
    if at is None:
        return ""
    hours = (now - at).total_seconds() / 3600
    if hours < 1:
        return "just raised"
    if hours < 48:
        return f"open {int(hours)} h"
    return f"open {int(hours // 24)} d"


def _row(case: BookingCase, now: datetime) -> dict[str, Any]:
    local, source = pickup_slot(case)
    return {
        "id": case.id,
        "load_id": case.load_id,
        "vendor": case.vendor_name,
        "customer": case.customer_name,
        "po_numbers": [str(p) for p in case.po_numbers],
        "status": case.status,
        "stage": stage(case),
        "pickup_local": local,
        "pickup_source": source,
        "pickup_number": case.pickup_number,
        "past_due": pickup_passed(case, now),
    }


def _since(stamps: Iterable[datetime | None], cutoff: datetime) -> int:
    return sum(1 for t in stamps if (at := as_utc(t)) is not None and at >= cutoff)


def today_summary(
    cases: list[BookingCase], *, now: datetime, timezone: str, customer: str | None = None
) -> dict[str, Any]:
    """Everything the daily summary says, as data (the board and the text both use it)."""
    tz = ZoneInfo(timezone)
    local_now = now.astimezone(tz)
    today = local_now.date()
    next_day = _next_business_day(today)
    days = {today.isoformat(): today, next_day.isoformat(): next_day}
    if customer:
        cases = [c for c in cases if c.customer_name == customer]
    live = [c for c in cases if c.status != CaseStatus.CANCELED.value]

    needs: list[dict[str, Any]] = []
    for case in cases:
        open_now = sorted(case.open_exceptions, key=lambda e: (_rank(e.kind), e.id))
        if not open_now:
            continue
        top = open_now[0]
        needs.append(
            {
                "case": _row(case, now),
                "kind": top.kind,
                "label": label(top.kind),
                "description": top.description,
                "raised_by": top.raised_by,
                "raised_at": (as_utc(top.raised_at) or now).isoformat(),
                "age": _age(top.raised_at, now),
                "also": [label(e.kind) for e in open_now[1:]],
            }
        )
    needs.sort(
        key=lambda n: (_rank(n["kind"]), n["case"]["pickup_local"] or "9999", n["case"]["id"])
    )
    groups: list[dict[str, Any]] = []
    for item in needs:
        if not groups or groups[-1]["kind"] != item["kind"]:
            groups.append(
                {
                    "kind": item["kind"],
                    "label": item["label"],
                    "hint": KINDS.get(item["kind"], ("", ""))[1],
                    "items": [],
                }
            )
        groups[-1]["items"].append(item)

    pickups: list[dict[str, Any]] = []
    for case in live:
        local, _ = pickup_slot(case)
        day = (local or "").partition(" ")[0]
        if day in days:
            pickups.append(_row(case, now))
    pickups.sort(key=lambda r: (r["pickup_local"] or "", r["id"]))
    unbooked_soon = sum(1 for r in pickups if r["status"] != CaseStatus.SCHEDULED.value)

    drafts = [
        _row(c, now)
        for c in live
        if c.status == CaseStatus.UNSCHEDULED.value and has_request(c) and not c.open_exceptions
    ]
    drafts.sort(key=lambda r: (r["pickup_local"] or "9999", r["id"]))
    waiting = sum(1 for c in live if c.status == CaseStatus.PENDING.value and not c.open_exceptions)

    cutoff = now - timedelta(hours=24)
    exceptions = [e for c in cases for e in c.exceptions]
    return {
        "generated_at": now.isoformat(),
        "local_time": stamp(now, "%A %m/%d/%Y, %H:%M"),
        "timezone": timezone,
        "customer": customer,
        "today": today.isoformat(),
        "next_day": next_day.isoformat(),
        "counts": {
            "cases_need_you": len(needs),
            "todos": sum(len(c.open_exceptions) for c in cases),
            "pickups_soon": len(pickups),
            "pickups_soon_unbooked": unbooked_soon,
            "drafts_waiting": len(drafts),
            "waiting_on_vendor": waiting,
            "raised_24h": _since((e.raised_at for e in exceptions), cutoff),
            "resolved_24h": _since((e.resolved_at for e in exceptions), cutoff),
            "booked_24h": _since(
                (ev.created_at for c in cases for ev in c.events if ev.action in BOOKED_EVENTS),
                cutoff,
            ),
        },
        "needs_you": groups,
        "pickups": pickups,
        "drafts": drafts,
    }


def _pos(row: dict[str, Any]) -> str:
    return ", ".join(row["po_numbers"]) or "-"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def render_today(data: dict[str, Any]) -> str:
    """The summary as plain text: what needs you, the next pickups, drafts to send, what changed.

    No markup, so it pastes cleanly into an email or a chat and reads the same in a terminal.
    """
    c = data["counts"]
    scope = data["customer"] or "all customers"
    lines = [
        "Pickup appointments: what needs you today",
        f"{data['local_time']} · {scope}",
        "",
        f"{_plural(c['cases_need_you'], 'pickup')} need you ({_plural(c['todos'], 'to-do')}) · "
        f"{_plural(c['pickups_soon'], 'pickup')} today or next business day, "
        f"{c['pickups_soon_unbooked']} not booked · "
        f"{_plural(c['drafts_waiting'], 'draft')} waiting to be sent · "
        f"{c['waiting_on_vendor']} waiting on the vendor",
        f"Last 24 hours: {_plural(c['raised_24h'], 'to-do')} raised, {c['resolved_24h']} "
        f"resolved, {_plural(c['booked_24h'], 'pickup')} booked.",
        "",
        f"NEEDS YOU ({c['cases_need_you']})",
    ]
    if not data["needs_you"]:
        lines.append("  Nothing needs a person right now.")
    for group in data["needs_you"]:
        lines += ["", f"{group['label']} ({len(group['items'])})"]
        if group["hint"]:
            lines.append(f"  What to do: {group['hint']}")
        for item in group["items"]:
            row = item["case"]
            said = [_sentence(item["description"].rstrip(". "))]
            if item["age"]:
                said.append(_sentence(item["age"]))
            if item["also"]:
                said.append(f"Also: {', '.join(item['also'])}")
            lines.append(
                f"  - {row['vendor'] or 'Unknown vendor'}, PO {_pos(row)}, "
                f"{fmt_slot(row['pickup_local'])} (case #{row['id']}): " + ". ".join(said) + "."
            )
    today, next_day = data["today"], data["next_day"]
    lines += [
        "",
        f"PICKUPS {fmt_slot(today).upper()} AND {fmt_slot(next_day).upper()} ({c['pickups_soon']})",
    ]
    if not data["pickups"]:
        lines.append("  No pickups on those days.")
    for day in (today, next_day):
        rows = [r for r in data["pickups"] if (r["pickup_local"] or "").startswith(day)]
        if not rows:
            continue
        lines += ["", fmt_slot(day)]
        for row in rows:
            clock = (row["pickup_local"] or "").partition(" ")[2]
            clock = f"{clock} {LABEL}" if clock else "any time"
            number = f", pickup# {row['pickup_number']}" if row["pickup_number"] else ""
            lines.append(
                f"  - {clock} {row['vendor'] or 'Unknown vendor'}, PO {_pos(row)}: "
                f"{row['stage']}{number} (case #{row['id']})"
            )
    lines += ["", f"DRAFTS WAITING TO BE SENT ({c['drafts_waiting']})"]
    if not data["drafts"]:
        lines.append("  None.")
    for row in data["drafts"]:
        lines.append(
            f"  - {row['vendor'] or 'Unknown vendor'}, PO {_pos(row)}, "
            f"{fmt_slot(row['pickup_local'])} (case #{row['id']})"
        )
    return "\n".join(lines) + "\n"
