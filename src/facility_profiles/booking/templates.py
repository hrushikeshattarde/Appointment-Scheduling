"""Email templates: the agent's wording, per desk or per customer, with fill-in fields.

Five kinds of email are written from a template:

- ``request``: one pickup asked for ("Can I please schedule the following?");
- ``batch_request``: several pickups for one desk in one email, one line per PO;
- ``reschedule``: a new slot asked for in the same thread;
- ``follow_up``: the nudge after a day of silence ("Following up on this.");
- ``check_back``: the nudge on the day a vendor said to ask again ("Checking in on this!").

A template is a subject (requests only: the others answer in the thread) and a body with fields
in braces, ``{po}`` or ``{lines}`` (see :data:`FIELDS`). The one used is the most specific
saved: for the desk's address, else for the customer, else the pod's default, else the built-in
one, which is the pod's own wording word for word. A customer template is saved under the
customer's key (``lidl``), its name, or a Transport Pro customer name. Templates are checked when
saved: an unknown field, an unmatched brace, or a request without its PO lines is refused.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from string import Formatter
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles.booking.models import BookingCase, BookingTemplate
from facility_profiles.booking.rules import VendorProfile, extra_references
from facility_profiles.config import Settings
from facility_profiles.customers import customer_of
from facility_profiles.storage.models import utcnow
from facility_profiles.storage.repository import as_utc


class TemplateKind(StrEnum):
    """The emails the agent writes from a template."""

    REQUEST = "request"
    BATCH_REQUEST = "batch_request"
    RESCHEDULE = "reschedule"
    FOLLOW_UP = "follow_up"
    CHECK_BACK = "check_back"


# Every fill-in field, and what it becomes in the email.
FIELDS: dict[str, str] = {
    "lines": "the PO lines asked for, one per line: PO# X on MM/DD @ HHMM, with any number the "
    "desk needs",
    "line": "the PO line asked for again (reschedule)",
    "ask": "the pod's ask, 'Can I please schedule the following?'; on a shared desk it names the "
    "shipper and the customer",
    "po": "the PO numbers, A & B, or 'load N' when the load has none",
    "load": "Circle's load number (A & B in a batch)",
    "date": "the pickup date asked for, MM/DD (the first one in a batch)",
    "time": "the pickup time asked for, HHMM; empty for a desk that is given a date only",
    "vendor": "the shipper's name without Inc. or LLC",
    "customer": "the customer's name from its customer file, e.g. Lidl",
    "desk_name": "the desk contact's name on the vendor profile, or empty",
    "delivery_ref": "the customer's delivery reference (the DCT number), or empty",
    "delivery_date": "the delivery date, MM/DD, or empty",
    "refs": "the numbers the desk needs besides the PO, e.g. Shipment# 7781234, or empty",
    "previous": "the slot asked for before, MM/DD @ HHMM (reschedule)",
    "note": "the one line of context given with a reschedule, or empty",
    "carrier": "Circle's name as the carrier",
    "signature": "the pod's signature block",
    "links": "one-click links to confirm a time (empty when links are off); a template without "
    "it gets them after its PO lines",
}
_COMMON = frozenset(
    {
        "po",
        "load",
        "date",
        "time",
        "vendor",
        "customer",
        "desk_name",
        "delivery_ref",
        "delivery_date",
        "refs",
        "carrier",
        "signature",
    }
)
KIND_FIELDS: dict[TemplateKind, frozenset[str]] = {
    TemplateKind.REQUEST: _COMMON | {"lines", "ask", "links"},
    TemplateKind.BATCH_REQUEST: _COMMON | {"lines", "ask", "links"},
    TemplateKind.RESCHEDULE: _COMMON | {"line", "previous", "note", "links"},
    TemplateKind.FOLLOW_UP: _COMMON,
    TemplateKind.CHECK_BACK: _COMMON,
}
# A request without its PO lines asks for nothing.
REQUIRED: dict[TemplateKind, frozenset[str]] = {
    TemplateKind.REQUEST: frozenset({"lines"}),
    TemplateKind.BATCH_REQUEST: frozenset({"lines"}),
    TemplateKind.RESCHEDULE: frozenset({"line"}),
}
# Only a request starts a thread; everything else answers in it, under the thread's subject.
HAS_SUBJECT = frozenset({TemplateKind.REQUEST, TemplateKind.BATCH_REQUEST})
# {links} is empty unless click-to-confirm is on; the blank lines around it then collapse, so the
# email is the pod's wording exactly.
_REQUEST_BODY = "Hello,\n\n{ask}\n\n{lines}\n\n{links}\n\nThank you!\n\n{signature}"


@dataclass(frozen=True)
class Template:
    """A subject and body with fields, and where it came from."""

    kind: TemplateKind
    subject: str | None
    body: str
    source: str  # "built-in", "default", "customer <name>", "desk <address>"


# The pod's own wording, word for word, from the lidl@ threads.
BUILT_IN: dict[TemplateKind, Template] = {
    TemplateKind.REQUEST: Template(
        TemplateKind.REQUEST, "Pick Up Appointment: {po}", _REQUEST_BODY, "built-in"
    ),
    TemplateKind.BATCH_REQUEST: Template(
        TemplateKind.BATCH_REQUEST, "Pick Up Appointments: {po}", _REQUEST_BODY, "built-in"
    ),
    TemplateKind.RESCHEDULE: Template(
        TemplateKind.RESCHEDULE,
        None,
        "Hello,\n\n{note}\n\nCan we please reschedule {line}?\n\n{links}\n\n"
        "Thank you!\n\n{signature}",
        "built-in",
    ),
    TemplateKind.FOLLOW_UP: Template(
        TemplateKind.FOLLOW_UP, None, "Hello,\n\nFollowing up on this.\n\n{signature}", "built-in"
    ),
    TemplateKind.CHECK_BACK: Template(
        TemplateKind.CHECK_BACK,
        None,
        "Good Morning,\n\nChecking in on this!\n\n{signature}",
        "built-in",
    ),
}


# ------------------------------------------------------------------ the pod's wording


def fmt_local(value: str | None) -> tuple[str, str]:
    """Turn "YYYY-MM-DD HH:MM" into ("MM/DD", "HHMM"), the way the pod writes it."""
    if not value:
        return ("(date to confirm)", "")
    day, _, clock = value.partition(" ")
    try:
        parsed = datetime.strptime(day, "%Y-%m-%d")
    except ValueError:
        return (day, clock.replace(":", ""))
    return (parsed.strftime("%m/%d"), clock.replace(":", ""))


def request_lines(
    case: BookingCase, *, date_only: bool = False, extra: Iterable[str] = ()
) -> list[str]:
    """The PO lines exactly as the pod writes them: "PO# X on MM/DD @ HHMM".

    ``extra`` holds the numbers the desk needs besides the PO ("Shipment# 7781234"); they follow
    the PO: "PO# X / Shipment# 7781234 on MM/DD @ HHMM".
    """
    mmdd, clock = fmt_local(case.requested_local)
    when = f"on {mmdd}" + (f" @ {clock}" if clock and not date_only else "")
    refs = "".join(f" / {ref}" for ref in extra)
    pos = [str(p) for p in case.po_numbers]
    if not pos:
        return [f"Load {case.load_id}{refs} {when}"]
    if len(pos) > 1:
        return [f"PO# {' & '.join(pos)} (ALL IN ONE TRUCK){refs} {when}"]
    return [f"PO# {pos[0]}{refs} {when}"]


def short_vendor(name: str | None) -> str:
    """Drop the corporate suffix: "Koch Foods, Inc." becomes "Koch Foods"."""
    return re.sub(r",?\s*\b(inc|llc|corp|co)\b\.?$", "", name or "the shipper", flags=re.I).strip()


def is_shared_desk(case: BookingCase, settings: Settings) -> bool:
    """A desk that books for several shippers needs the shipper and customer named."""
    return (case.contact_email or "").lower() in {d.lower() for d in settings.booking_shared_desks}


# ------------------------------------------------------------------ fill-in values


def case_values(
    cases: Sequence[BookingCase], settings: Settings, profile: VendorProfile | None
) -> dict[str, str]:
    """The fields every template can use, for one case or a batch for one desk."""
    first = cases[0]
    pos = [str(p) for c in cases for p in c.po_numbers]
    mmdd, clock = fmt_local(first.requested_local)
    date_only = bool(profile and profile.date_only)
    delivery = as_utc(first.delivery_at_utc)
    tz = ZoneInfo(first.vendor_timezone or "America/New_York")
    return {
        "po": " & ".join(pos) or " & ".join(f"load {c.load_id}" for c in cases),
        "load": " & ".join(str(c.load_id) for c in cases),
        "date": mmdd,
        "time": "" if date_only else clock,
        "vendor": short_vendor(first.vendor_name),
        "customer": customer_of(first, settings).label(first.customer_name),
        "desk_name": first.contact_name or (profile.contact_name if profile else None) or "",
        "delivery_ref": first.delivery_ref or "",
        "delivery_date": delivery.astimezone(tz).strftime("%m/%d") if delivery else "",
        "refs": ", ".join(ref for c in cases for ref in extra_references(c, profile)),
        "carrier": settings.booking_carrier_name,
        "signature": customer_of(first, settings).signature or settings.booking_signature,
    }


def request_values(
    cases: Sequence[BookingCase],
    settings: Settings,
    profile: VendorProfile | None,
    *,
    links: str = "",
) -> dict[str, str]:
    """The fields of a request: the PO lines, the ask and any links, on top of the common ones."""
    first = cases[0]
    date_only = bool(profile and profile.date_only)
    ask = (
        f"Can I please schedule the following for {short_vendor(first.vendor_name)} going to "
        f"{customer_of(first, settings).label(first.customer_name)}?"
        if is_shared_desk(first, settings)
        else "Can I please schedule the following?"
    )
    lines = [
        line
        for c in cases
        for line in request_lines(c, date_only=date_only, extra=extra_references(c, profile))
    ]
    return {
        **case_values(cases, settings, profile),
        "lines": "\n".join(lines),
        "ask": ask,
        "links": links,
    }


def reschedule_values(
    case: BookingCase,
    settings: Settings,
    profile: VendorProfile | None,
    *,
    previous: str | None,
    note: str | None,
    links: str = "",
) -> dict[str, str]:
    """The fields of a reschedule: the line asked for again, the slot before, the note."""
    date_only = bool(profile and profile.date_only)
    line = request_lines(case, date_only=date_only, extra=extra_references(case, profile))[0]
    mmdd, clock = fmt_local(previous)
    return {
        **case_values([case], settings, profile),
        "line": line,
        "previous": f"{mmdd} @ {clock}" if previous and clock else (mmdd if previous else ""),
        "note": (note or "").strip(),
        "links": links,
    }


def with_links(template: Template) -> Template:
    """A template without ``{links}`` carries them after the line with its PO lines.

    Applied only when there are links to show, so a saved template written before they existed
    reads exactly as it did while links are off.
    """
    body = template.body
    if "{links}" in body:
        return template
    for anchor in ("{lines}", "{line}"):
        at = body.find(anchor)
        if at < 0:
            continue
        end = body.find("\n", at)
        end = len(body) if end < 0 else end
        return replace(template, body=f"{body[:end]}\n\n{{links}}{body[end:]}")
    return template


# ------------------------------------------------------------------ checking and rendering


def template_fields(text: str) -> list[str]:
    """The field names a template text uses; raises ValueError on an unmatched brace."""
    return [name for _, name, _, _ in Formatter().parse(text) if name is not None]


def check_template(kind: TemplateKind, subject: str | None, body: str) -> list[str]:
    """What is wrong with a template before it is saved; empty when it is fine."""
    problems: list[str] = []
    allowed = KIND_FIELDS[kind]
    used: set[str] = set()
    for part, text in (("subject", subject), ("body", body)):
        if text is None:
            continue
        try:
            names = template_fields(text)
        except ValueError as exc:
            problems.append(f"the {part} has an unmatched brace ({exc}); write {{{{ for a brace")
            continue
        for name in names:
            if name not in allowed:
                problems.append(
                    f"the {part} uses {{{name}}}, which a {kind.value} cannot fill; "
                    f"use {', '.join('{' + f + '}' for f in sorted(allowed))}"
                )
        used.update(names)
    if subject is not None and kind not in HAS_SUBJECT:
        problems.append(f"a {kind.value} answers in the thread and keeps its subject")
    if subject is not None and ("\n" in subject or not subject.strip()):
        problems.append("the subject must be one line of text")
    if not body.strip():
        problems.append("the body is empty")
    for name in sorted(REQUIRED.get(kind, frozenset()) - used):
        problems.append(f"a {kind.value} needs {{{name}}}: {FIELDS[name]}")
    return problems


def _clean(text: str) -> str:
    """Blank fields leave no gaps: three or more line breaks become one blank line."""
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def render(template: Template, values: dict[str, str]) -> tuple[str | None, str]:
    """The subject (requests only) and body with every field filled."""
    subject = template.subject.format_map(values).strip() if template.subject else None
    return subject, _clean(template.body.format_map(values))


# ------------------------------------------------------------------ the saved templates


def _scope(desk: str | None, customer: str | None) -> tuple[str, str]:
    if desk and customer:
        msg = "a template is for one desk or one customer, not both"
        raise ValueError(msg)
    if desk:
        return "desk", desk.strip().lower()
    if customer:
        return "customer", customer.strip()
    return "default", ""


def _template(row: BookingTemplate) -> Template:
    kind = TemplateKind(row.kind)
    source = row.scope if row.scope == "default" else f"{row.scope} {row.match}"
    built_in = BUILT_IN[kind]
    return Template(kind, row.subject or built_in.subject, row.body, source)


def pick(
    session: Session,
    kind: TemplateKind,
    *,
    desk: str | None,
    customer: str | Sequence[str] | None,
) -> Template:
    """The template to write with: the desk's, else the customer's, else the default.

    ``customer`` is what a customer template may be saved under, tried in order: the
    customer's key, its name, the case's Transport Pro customer name.
    """
    rows = {
        (r.scope, r.match): r
        for r in session.scalars(select(BookingTemplate).where(BookingTemplate.kind == kind.value))
    }
    wanted: list[tuple[str, str]] = []
    if desk:
        wanted.append(("desk", desk.strip().lower()))
    names = [customer] if isinstance(customer, str) else list(customer or [])
    wanted.extend(("customer", n.strip()) for n in names if n and n.strip())
    wanted.append(("default", ""))
    for key in wanted:
        row = rows.get(key)
        if row is not None:
            return _template(row)
    return BUILT_IN[kind]


def save_template(
    session: Session,
    kind: TemplateKind,
    *,
    body: str,
    by: str,
    subject: str | None = None,
    desk: str | None = None,
    customer: str | None = None,
) -> Template:
    """Save (or replace) the template for a desk, a customer or the pod; refused if it is wrong."""
    problems = check_template(kind, subject, body)
    if problems:
        raise ValueError("; ".join(problems))
    scope, match = _scope(desk, customer)
    row = session.scalar(
        select(BookingTemplate).where(
            BookingTemplate.kind == kind.value,
            BookingTemplate.scope == scope,
            BookingTemplate.match == match,
        )
    )
    if row is None:
        row = BookingTemplate(kind=kind.value, scope=scope, match=match)
        session.add(row)
    row.subject = subject
    row.body = body.replace("\r\n", "\n")
    row.updated_by = by
    row.updated_at = utcnow()
    session.flush()
    return _template(row)


def remove_template(
    session: Session, kind: TemplateKind, *, desk: str | None = None, customer: str | None = None
) -> bool:
    """Remove a saved template, so the next one down applies; True if there was one."""
    scope, match = _scope(desk, customer)
    row = session.scalar(
        select(BookingTemplate).where(
            BookingTemplate.kind == kind.value,
            BookingTemplate.scope == scope,
            BookingTemplate.match == match,
        )
    )
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def saved_templates(session: Session) -> list[BookingTemplate]:
    """Every saved template: defaults first, then customers, then desks."""
    order = {"default": 0, "customer": 1, "desk": 2}
    rows = list(session.scalars(select(BookingTemplate)))
    return sorted(rows, key=lambda r: (order.get(r.scope, 3), r.match, r.kind))
