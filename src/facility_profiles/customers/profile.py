"""One customer file, read and checked.

Standard library only: the mail-archive Lambda reads customer files too, and its bundle carries
nothing but this package and the Google signer.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from functools import cached_property
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

BUILT_IN_DIR = Path(__file__).parent
FALLBACK_KEY = "default"
FALLBACK_SOURCE = "settings (FP_BOOKING_*)"
KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
DOMAIN_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)+$")
# A date read out of a PO counts only this close to the delivery; anything further is a
# coincidence of digits, not a date.
PO_DATE_WINDOW_DAYS = 60

# Every key a file may hold, by table ("" is the top level). Anything else is a typo.
ALLOWED: dict[str, frozenset[str]] = {
    "": frozenset({"name", "description", "timezone", "rules"}),
    "transport_pro": frozenset(
        {"customer_ids", "customer_names", "terminal_ids", "booking_customer_ids"}
    ),
    "mail": frozenset({"group", "sender", "cc", "signature"}),
    "customer_desk": frozenset({"email", "delivery_system"}),
    "numbers": frozenset({"po", "po_date", "delivery_ref"}),
    "mail_archive": frozenset({"keep_subjects", "drop_subjects", "desks", "desk_domains"}),
}

# A delivery slot as the customer's desk writes it, around the customer's delivery reference:
# "8/20 7AM - GRM_200826926", "9/30 at 1100" then the reference on the next line, or the
# reference first, "FRG_200526615 05/20 @ 1100".
_SLOT_BEFORE_REF = (
    r"(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\s*(?:@|at)?\s*"
    r"(?P<h>\d{1,2})(?::?(?P<min>\d{2}))?\s*(?P<ampm>AM|PM)?[\s,;:\-\u2013\u2014]*"
)
_SLOT_AFTER_REF = (
    r"\s+(?P<m>\d{1,2})/(?P<d>\d{1,2})(?:/(?P<y>\d{2,4}))?\s*(?:@|at)?\s*"
    r"(?P<h>\d{1,2})(?::?(?P<min>\d{2}))?\s*(?P<ampm>AM|PM)?"
)


class CustomerFileError(ValueError):
    """A customer file that cannot be used, with every problem found in it."""


# What a rule can tell the agent to do with a pickup when it runs on its own
# (booking/automation.py).
RULE_ACTIONS = ("draft", "send", "hold", "skip")
RULE_KEYS = frozenset(
    {
        "name",
        "when",
        "do",
        "why",
        "lead_days",
        "batch_at",
        "wait_for",
        "pickup_from",
        "follow_up",
        "replies",
        "confirm",
        "customer_notes",
    }
)
# How the agent's answers in a thread go out, and whether it books a confirmation itself.
OUTBOX = ("draft", "send")
CONFIRM = ("review", "auto")
WHEN_KEYS = frozenset({"methods", "desks", "vendors", "facilities", "customer_ids"})
METHODS = frozenset({"email", "phone", "web_portal", "fcfs", "preset_by_customer", "unknown"})
WAIT_FOR = frozenset({"delivery_slot"})
PICKUP_FROM = frozenset({"tender", "delivery"})
_CLOCK_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Rule:
    """Which pickups a rule covers, and what the agent does with them on its own.

    ``do``: ``draft`` (a person sends), ``send`` (sent, in send mode), ``hold`` (a person books;
    the agent writes nothing) or ``skip`` (not the agent's to book; no case is opened). Every
    ``when`` list narrows the rule; an empty one does not. The timing:

    - ``lead_days``: ask this many business days before the pickup, not sooner;
    - ``batch_at``: write at this time of day (the pod's), one email per desk;
    - ``wait_for = "delivery_slot"``: ask only once the delivery has its slot and reference;
    - ``pickup_from = "delivery"``: plan the pickup back from the delivery, not the tender;
    - ``follow_up``: nudge a silent desk on its own (default true).

    The conversation, once the vendor answers:

    - ``replies``: ``send`` or ``draft`` the agent's answers to the vendor (accepting an offer,
      asking for other days, answering a question, the thank-you, the follow-up). Not set: as
      ``do`` says (a ``hold`` or ``skip`` rule drafts);
    - ``confirm``: ``auto`` books a vendor's confirmation of the time asked for without a
      person, and an offer the agent accepted and sent; ``review`` (the default) waits for a
      person's approval;
    - ``customer_notes``: ``send`` or ``draft`` (the default) the note to the customer's desk
      when the vendor cannot ship.
    """

    name: str
    do: str
    why: str | None = None
    methods: frozenset[str] = frozenset()
    desks: frozenset[str] = frozenset()
    vendors: tuple[str, ...] = ()  # lower-case; a vendor whose name contains one matches
    facilities: frozenset[str] = frozenset()
    customer_ids: frozenset[int] = frozenset()
    lead_days: int | None = None
    batch_at: str | None = None
    wait_for: str | None = None
    pickup_from: str = "tender"
    follow_up: bool = True
    replies: str | None = None
    confirm: str = "review"
    customer_notes: str = "draft"

    @property
    def reply_mode(self) -> str:
        """``send`` or ``draft``: how the agent's answers to the vendor go out."""
        if self.replies:
            return self.replies
        return "send" if self.do == "send" else "draft"

    def matches(self, case: Any) -> bool:
        """True when the case is one this rule covers (a booking case, or one being opened)."""
        method = getattr(case, "booking_method", None) or "unknown"
        desk = (getattr(case, "contact_email", None) or "").lower()
        vendor = (getattr(case, "vendor_name", None) or "").lower()
        return (
            (not self.methods or method in self.methods)
            and (not self.desks or desk in self.desks)
            and (not self.vendors or any(v in vendor for v in self.vendors))
            and (not self.facilities or getattr(case, "facility_key", None) in self.facilities)
            and (not self.customer_ids or getattr(case, "customer_id", None) in self.customer_ids)
        )

    def describe(self) -> str:
        """The rule in one line, for ``customers show`` and the board."""
        when = [
            f"{label} {', '.join(sorted(map(str, values)))}"
            for label, values in (
                ("method", self.methods),
                ("desk", self.desks),
                ("vendor", self.vendors),
                ("facility", self.facilities),
                ("customer", self.customer_ids),
            )
            if values
        ]
        how = [
            text
            for text in (
                f"{self.lead_days} business day(s) ahead" if self.lead_days is not None else None,
                f"at {self.batch_at}" if self.batch_at else None,
                "once the delivery has its slot" if self.wait_for == "delivery_slot" else None,
                "planned back from the delivery" if self.pickup_from == "delivery" else None,
                None if self.follow_up else "no follow-ups",
                "replies sent" if self.reply_mode == "send" else None,
                "books confirmations itself" if self.confirm == "auto" else None,
                "customer notes sent" if self.customer_notes == "send" else None,
            )
            if text
        ]
        scope = "; ".join(when) or "every pickup"
        return f"{self.name}: {scope} -> {self.do}" + (f" ({', '.join(how)})" if how else "")


DEFAULT_RULE = Rule(name="default", do="draft", why="no rule in the customer file matched")


@dataclass(frozen=True)
class Customer:
    """Everything that differs from one customer to the next.

    ``name`` is how messages name the customer ("Lidl"); the fallback, which no file defines,
    has none and names the customer from the load instead.
    """

    key: str
    name: str | None
    source: str
    description: str | None = None
    tpro_customer_ids: tuple[int, ...] = ()
    # The records whose pickups the booking agent books (default: all of them). Lidl's outbound
    # loads are its own store tours, with nothing to book.
    booking_customer_ids: tuple[int, ...] = ()
    tpro_customer_names: tuple[str, ...] = ()
    terminal_ids: tuple[int, ...] = ()
    timezone: str | None = None
    # The mailbox the pickup threads live in (a Google Group), what drafts say they are from,
    # who is copied, and how the pod signs.
    group: str | None = None
    sender: str | None = None
    cc: tuple[str, ...] = ()
    signature: str | None = None
    # The customer's own desk: where "the vendor cannot ship" goes, and whose mail moves the
    # delivery slot. ``delivery_system`` is where the pod books that slot ("DCT" for Lidl).
    customer_desk: str | None = None
    delivery_system: str | None = None
    # What the customer's numbers look like.
    po: re.Pattern[str] | None = None
    po_date: re.Pattern[str] | None = None
    delivery_ref: re.Pattern[str] | None = None
    # Which group mail is about booking, on top of the rules every customer shares.
    archive_keep: tuple[tuple[str, re.Pattern[str]], ...] = ()
    archive_drop: tuple[re.Pattern[str], ...] = ()
    archive_desks: frozenset[str] = frozenset()
    archive_desk_domains: frozenset[str] = frozenset()
    # What the agent does on its own with this customer's pickups; the first that matches wins.
    rules: tuple[Rule, ...] = ()

    def rule_for(self, case: Any) -> Rule:
        """The first rule that covers the case, else the default (draft)."""
        return next((rule for rule in self.rules if rule.matches(case)), DEFAULT_RULE)

    @property
    def is_fallback(self) -> bool:
        """True for the settings-built customer that no file defines."""
        return self.key == FALLBACK_KEY

    @property
    def cc_header(self) -> str:
        """The Cc header value."""
        return ", ".join(self.cc)

    def label(self, customer_name: str | None) -> str:
        """How a message names the customer: the file's name, else the load's without its side.

        "Lidl - Inbound" becomes "Lidl" when no file names the customer.
        """
        if self.name:
            return self.name
        bare = re.sub(r"\s*-\s*(inbound|outbound)\s*$", "", customer_name or "", flags=re.I)
        return bare.strip() or "the customer"

    def claims(self, customer_id: int | None, customer_name: str | None) -> bool:
        """True when the load or case is this customer's: by Transport Pro id, else by name."""
        if customer_id is not None and customer_id in self.tpro_customer_ids:
            return True
        wanted = _fold(customer_name)
        return bool(wanted) and wanted in {_fold(n) for n in self.tpro_customer_names}

    def template_matches(self, customer_name: str | None) -> list[str]:
        """What a customer-scoped template may be saved under, most specific last."""
        found = [m for m in (self.key if not self.is_fallback else None, self.name) if m]
        if customer_name and customer_name.strip() not in found:
            found.append(customer_name.strip())
        return found

    def find_delivery_ref(self, text: str | None) -> str | None:
        """The first delivery reference in ``text``, when the customer has one."""
        if self.delivery_ref is None or not text:
            return None
        match = self._ref_word.search(text)
        return match.group(0).upper() if match else None

    def delivery_refs(self, text: str) -> list[str]:
        """Every delivery reference in ``text``, upper-cased."""
        if self.delivery_ref is None:
            return []
        return [m.group(0).upper() for m in self._ref_word.finditer(text)]

    def slot_patterns(self) -> tuple[re.Pattern[str], ...]:
        """The ways the customer's desk writes a delivery slot; none without a reference."""
        if self.delivery_ref is None:
            return ()
        return self._slot_patterns

    def po_embedded_date(self, po: str, *, near: date) -> date | None:
        """The date inside a PO number, when the customer encodes one and it is plausible.

        The date must be real and within :data:`PO_DATE_WINDOW_DAYS` of ``near`` (the delivery).
        """
        if self.po_date is None:
            return None
        match = self.po_date.fullmatch(str(po).strip())
        if match is None:
            return None
        parts = match.groupdict()
        year = int(parts["yyyy"]) if parts.get("yyyy") else 2000 + int(parts["yy"])
        try:
            found = date(year, int(parts["mm"]), int(parts["dd"]))
        except ValueError:
            return None
        return found if abs((found - near).days) <= PO_DATE_WINDOW_DAYS else None

    @cached_property
    def _ref_word(self) -> re.Pattern[str]:
        assert self.delivery_ref is not None
        return re.compile(rf"\b(?:{self.delivery_ref.pattern})\b")

    @cached_property
    def _slot_patterns(self) -> tuple[re.Pattern[str], ...]:
        assert self.delivery_ref is not None
        ref = f"(?P<ref>{self.delivery_ref.pattern})"
        return (
            re.compile(_SLOT_BEFORE_REF + ref, re.I),
            re.compile(ref + _SLOT_AFTER_REF, re.I),
        )


# ------------------------------------------------------------------ reading a file


def _fold(name: str | None) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


class _Reader:
    """Pulls typed values out of the parsed file and keeps every problem for one report."""

    def __init__(self, data: dict[str, Any]) -> None:
        self.data = data
        self.problems: list[str] = []

    def table(self, name: str) -> dict[str, Any]:
        value = self.data.get(name, {})
        if not isinstance(value, dict):
            self.problems.append(f"[{name}] must be a table")
            return {}
        return value

    def check_keys(self) -> None:
        for key, value in self.data.items():
            if isinstance(value, dict) and key in ALLOWED and key:
                for inner in value:
                    if inner not in ALLOWED[key]:
                        self.problems.append(f"[{key}] has no setting {inner!r}")
            elif key not in ALLOWED[""]:
                where = "table" if isinstance(value, dict) else "setting"
                self.problems.append(f"unknown {where} {key!r}")

    def text(self, where: str, table: dict[str, Any], key: str) -> str | None:
        value = table.get(key)
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            self.problems.append(f"{where}.{key} must be a non-empty string")
            return None
        return value.strip()

    def ints(self, where: str, table: dict[str, Any], key: str) -> tuple[int, ...]:
        value = table.get(key, [])
        if not isinstance(value, list) or not all(
            isinstance(v, int) and not isinstance(v, bool) for v in value
        ):
            self.problems.append(f"{where}.{key} must be a list of whole numbers")
            return ()
        return tuple(dict.fromkeys(value))

    def texts(self, where: str, table: dict[str, Any], key: str) -> tuple[str, ...] | None:
        """A list of strings; None when the key is absent (so a default can apply)."""
        if key not in table:
            return None
        value = table[key]
        if not isinstance(value, list) or not all(isinstance(v, str) and v.strip() for v in value):
            self.problems.append(f"{where}.{key} must be a list of non-empty strings")
            return ()
        return tuple(dict.fromkeys(v.strip() for v in value))

    def email(self, where: str, value: str | None) -> str | None:
        if value is None:
            return None
        address = value.strip().lower()
        if not EMAIL_RE.match(address):
            self.problems.append(f"{where} {value!r} is not an email address")
            return None
        return address

    def emails(self, where: str, values: Iterable[str]) -> tuple[str, ...]:
        found = [self.email(where, v) for v in values]
        return tuple(dict.fromkeys(a for a in found if a))

    def pattern(
        self, where: str, value: str | None, *, groups: frozenset[str] = frozenset()
    ) -> re.Pattern[str] | None:
        """Compile a regular expression; only the named groups in ``groups`` are allowed."""
        if value is None:
            return None
        try:
            compiled = re.compile(value)
        except re.error as exc:
            self.problems.append(f"{where} is not a valid regular expression: {exc}")
            return None
        extra = set(compiled.groupindex) - groups
        if extra:
            self.problems.append(
                f"{where} may not name groups ({', '.join(sorted(extra))}); use (?:...) instead"
            )
            return None
        return compiled


def _zone(r: _Reader, value: str | None) -> str | None:
    if value is None:
        return None
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        r.problems.append(f"timezone {value!r} is not an IANA time zone")
        return None
    return value


def _numbers(
    r: _Reader, numbers: dict[str, Any]
) -> tuple[re.Pattern[str] | None, re.Pattern[str] | None, re.Pattern[str] | None]:
    """The PO pattern, the PO date pattern and the delivery reference pattern."""
    po = r.pattern("numbers.po", r.text("numbers", numbers, "po"))
    po_date = r.pattern(
        "numbers.po_date",
        r.text("numbers", numbers, "po_date"),
        groups=frozenset({"dd", "mm", "yy", "yyyy"}),
    )
    if po_date is not None:
        named = set(po_date.groupindex)
        if not {"dd", "mm"} <= named or not named & {"yy", "yyyy"}:
            r.problems.append("numbers.po_date needs the named groups dd, mm and yy (or yyyy)")
            po_date = None
    delivery_ref = r.pattern("numbers.delivery_ref", r.text("numbers", numbers, "delivery_ref"))
    if delivery_ref is not None and delivery_ref.search(""):
        r.problems.append("numbers.delivery_ref matches an empty string")
        delivery_ref = None
    return po, po_date, delivery_ref


def _archive(
    r: _Reader, archive: dict[str, Any]
) -> tuple[
    tuple[tuple[str, re.Pattern[str]], ...],
    tuple[re.Pattern[str], ...],
    tuple[str, ...],
    tuple[str, ...],
]:
    """Subjects to keep (by name) and drop, desk addresses and desk domains, for the archive."""
    keep: list[tuple[str, re.Pattern[str]]] = []
    raw_keep = archive.get("keep_subjects", {})
    if not isinstance(raw_keep, dict):
        r.problems.append("mail_archive.keep_subjects must be a table of name = pattern")
        raw_keep = {}
    for rule, value in raw_keep.items():
        if not KEY_RE.match(rule):
            r.problems.append(
                f"mail_archive.keep_subjects name {rule!r}: use lower-case and dashes"
            )
            continue
        where = f"mail_archive.keep_subjects.{rule}"
        compiled = r.pattern(where, value if isinstance(value, str) else None)
        if compiled is not None:
            keep.append((rule, re.compile(compiled.pattern, re.I)))
    drop = tuple(
        re.compile(compiled.pattern, re.I)
        for i, value in enumerate(r.texts("mail_archive", archive, "drop_subjects") or ())
        if (compiled := r.pattern(f"mail_archive.drop_subjects[{i}]", value)) is not None
    )
    desks = r.emails("mail_archive.desks", r.texts("mail_archive", archive, "desks") or ())
    domains: list[str] = []
    for value in r.texts("mail_archive", archive, "desk_domains") or ():
        domain = value.lower().lstrip("@")
        if DOMAIN_RE.match(domain):
            domains.append(domain)
        else:
            r.problems.append(f"mail_archive.desk_domains {value!r} is not a domain")
    return tuple(keep), drop, desks, tuple(domains)


def _rule(r: _Reader, index: int, raw: Any, seen: set[str]) -> Rule | None:
    """One ``[[rules]]`` entry, checked; None (with the problems noted) when it cannot be used."""
    where = f"rules[{index}]"
    if not isinstance(raw, dict):
        r.problems.append(f"{where} must be a table")
        return None
    for unknown in sorted(set(raw) - RULE_KEYS):
        r.problems.append(f"{where} has no setting {unknown!r}")
    name = r.text(where, raw, "name") or f"rule {index + 1}"
    if name in seen:
        r.problems.append(f"{where}: two rules are named {name!r}")
    seen.add(name)
    where = f"rule {name!r}"
    do = raw.get("do")
    if do not in RULE_ACTIONS:
        r.problems.append(f"{where}: do must be one of {', '.join(RULE_ACTIONS)}")
        return None
    when = raw.get("when", {})
    if not isinstance(when, dict):
        r.problems.append(f"{where}: when must be a table")
        when = {}
    for unknown in sorted(set(when) - WHEN_KEYS):
        r.problems.append(f"{where}: when has no filter {unknown!r}")
    methods = r.texts(where, when, "methods") or ()
    for method in methods:
        if method not in METHODS:
            r.problems.append(
                f"{where}: method {method!r} is not one of {', '.join(sorted(METHODS))}"
            )
    return Rule(
        name=name,
        do=str(do),
        why=r.text(where, raw, "why"),
        methods=frozenset(methods),
        desks=frozenset(r.emails(f"{where} desks", r.texts(where, when, "desks") or ())),
        vendors=tuple(v.lower() for v in r.texts(where, when, "vendors") or ()),
        facilities=frozenset(r.texts(where, when, "facilities") or ()),
        customer_ids=frozenset(r.ints(where, when, "customer_ids")),
        **_timing(r, where, raw),
    )


def _timing(r: _Reader, where: str, raw: dict[str, Any]) -> dict[str, Any]:
    """A rule's lead days, batch hour, what it waits for, where the pickup is planned from."""
    lead = raw.get("lead_days")
    if lead is not None and (
        isinstance(lead, bool) or not isinstance(lead, int) or not 0 <= lead <= 30
    ):
        r.problems.append(f"{where}: lead_days must be a whole number from 0 to 30")
        lead = None
    batch_at = r.text(where, raw, "batch_at")
    if batch_at is not None and not _CLOCK_RE.match(batch_at):
        r.problems.append(f"{where}: batch_at must be HH:MM")
        batch_at = None
    wait_for = r.text(where, raw, "wait_for")
    if wait_for is not None and wait_for not in WAIT_FOR:
        r.problems.append(f"{where}: wait_for can only be {', '.join(sorted(WAIT_FOR))}")
        wait_for = None
    pickup_from = r.text(where, raw, "pickup_from") or "tender"
    if pickup_from not in PICKUP_FROM:
        r.problems.append(f"{where}: pickup_from must be tender or delivery")
        pickup_from = "tender"
    follow_up = raw.get("follow_up", True)
    if not isinstance(follow_up, bool):
        r.problems.append(f"{where}: follow_up must be true or false")
        follow_up = True
    return {
        "lead_days": lead,
        "batch_at": batch_at,
        "wait_for": wait_for,
        "pickup_from": pickup_from,
        "follow_up": follow_up,
        **_conversation(r, where, raw),
    }


def _conversation(r: _Reader, where: str, raw: dict[str, Any]) -> dict[str, Any]:
    """How a rule's answers go out, and whether it books confirmations itself."""
    found: dict[str, Any] = {}
    for name, allowed, default in (
        ("replies", OUTBOX, None),
        ("confirm", CONFIRM, "review"),
        ("customer_notes", OUTBOX, "draft"),
    ):
        value = r.text(where, raw, name)
        if value is not None and value not in allowed:
            r.problems.append(f"{where}: {name} must be {' or '.join(allowed)}")
            value = None
        found[name] = value if value is not None else default
    return found


def _rules(r: _Reader, raw: Any) -> tuple[Rule, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, list):
        r.problems.append("rules must be written as [[rules]] tables")
        return ()
    seen: set[str] = set()
    found = [_rule(r, i, entry, seen) for i, entry in enumerate(raw)]
    return tuple(rule for rule in found if rule is not None)


def parse_customer(data: dict[str, Any], *, key: str, source: str) -> Customer:
    """Build a customer from a parsed file; raises :class:`CustomerFileError` listing problems."""
    r = _Reader(data)
    if not KEY_RE.match(key) or key == FALLBACK_KEY:
        r.problems.append(
            f"the file name {key!r} is not a usable key: lower-case letters, digits and dashes, "
            f"and not {FALLBACK_KEY!r}"
        )
    r.check_keys()
    name = r.text("", data, "name")
    if name is None:
        r.problems.append('name is required (how messages name the customer, e.g. "Lidl")')
    description = r.text("", data, "description")
    timezone = _zone(r, r.text("", data, "timezone"))

    tpro = r.table("transport_pro")
    ids = r.ints("transport_pro", tpro, "customer_ids")
    names = r.texts("transport_pro", tpro, "customer_names") or ()
    if not ids and not names:
        r.problems.append(
            "[transport_pro] needs customer_ids (or customer_names): nothing else ties a load "
            "to this customer"
        )
    terminals = r.ints("transport_pro", tpro, "terminal_ids")
    booking_ids = r.ints("transport_pro", tpro, "booking_customer_ids") or ids
    if set(booking_ids) - set(ids):
        r.problems.append("[transport_pro] booking_customer_ids must be among customer_ids")

    mail = r.table("mail")
    group = r.email("mail.group", r.text("mail", mail, "group"))
    sender = r.email("mail.sender", r.text("mail", mail, "sender")) or group
    cc_given = r.texts("mail", mail, "cc")
    cc = r.emails("mail.cc", cc_given) if cc_given is not None else ((group,) if group else ())
    signature = r.text("mail", mail, "signature")

    desk = r.table("customer_desk")
    desk_email = r.email("customer_desk.email", r.text("customer_desk", desk, "email"))
    system = r.text("customer_desk", desk, "delivery_system")

    po, po_date, delivery_ref = _numbers(r, r.table("numbers"))
    keep, drop, desks, domains = _archive(r, r.table("mail_archive"))
    rules = _rules(r, data.get("rules"))

    if r.problems:
        raise CustomerFileError(f"{source}:\n  - " + "\n  - ".join(r.problems))
    return Customer(
        key=key,
        name=name,
        source=source,
        description=description,
        tpro_customer_ids=ids,
        booking_customer_ids=booking_ids,
        tpro_customer_names=names,
        terminal_ids=terminals,
        timezone=timezone,
        group=group,
        sender=sender,
        cc=cc,
        signature=signature,
        customer_desk=desk_email,
        delivery_system=system,
        po=po,
        po_date=po_date,
        delivery_ref=delivery_ref,
        archive_keep=keep,
        archive_drop=drop,
        archive_desks=frozenset(desks),
        archive_desk_domains=frozenset(domains),
        rules=rules,
    )


def load_customer(path: Path) -> Customer:
    """Read one customer file; its key is the file name without ``.toml``."""
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        msg = f"{path}:\n  - not valid TOML: {exc}"
        raise CustomerFileError(msg) from exc
    return parse_customer(data, key=path.stem, source=str(path))


def load_dir(directory: Path) -> list[Customer]:
    """Every customer file in a folder, by name; files starting with ``_`` are skipped."""
    if not directory.is_dir():
        return []
    return [
        load_customer(p) for p in sorted(directory.glob("*.toml")) if not p.name.startswith("_")
    ]


def load_customers(extra_dir: str | Path | None = None) -> list[Customer]:
    """The built-in customers, then ``extra_dir``'s, which replace built-ins with the same key.

    Raises :class:`CustomerFileError` when two files claim the same Transport Pro customer.
    """
    found: dict[str, Customer] = {c.key: c for c in load_dir(BUILT_IN_DIR)}
    if extra_dir:
        extra = Path(extra_dir)
        if not extra.is_dir():
            msg = f"FP_CUSTOMERS_DIR {extra} is not a folder"
            raise CustomerFileError(msg)
        found.update({c.key: c for c in load_dir(extra)})
    customers = sorted(found.values(), key=lambda c: c.key)
    _check_unique(customers)
    return customers


def _check_unique(customers: list[Customer]) -> None:
    owner: dict[str, Customer] = {}
    problems: list[str] = []
    for c in customers:
        claims = [f"customer id {i}" for i in c.tpro_customer_ids] + [
            f"customer name {n!r}" for n in c.tpro_customer_names
        ]
        for claim in claims:
            fold = claim.lower()
            if fold in owner and owner[fold].key != c.key:
                problems.append(f"{claim} is claimed by both {owner[fold].source} and {c.source}")
            owner.setdefault(fold, c)
    if problems:
        raise CustomerFileError("customer files disagree:\n  - " + "\n  - ".join(problems))


def choose(customers: list[Customer], key: str | None) -> Customer:
    """The customer ``key`` names, or the only one there is when no key is given."""
    if key:
        for c in customers:
            if c.key == key.strip().lower():
                return c
        known = ", ".join(c.key for c in customers) or "none"
        msg = f"no customer file {key!r} (known: {known})"
        raise CustomerFileError(msg)
    if len(customers) == 1:
        return customers[0]
    known = ", ".join(c.key for c in customers) or "none"
    msg = f"say which customer (known: {known})"
    raise CustomerFileError(msg)
