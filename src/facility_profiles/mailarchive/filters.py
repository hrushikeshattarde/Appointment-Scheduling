"""Which group messages are about booking a pickup, and the identifiers inside them.

The rules come from the lidl@ archive read in September 2026. Those every customer shares stay
here: the pod's own subjects ("Pick Up Appointment: <PO>", "Pick Up Appointments"),
"RESCHEDULE <PO>", the portals' notices ("Appointment Shipment Change: <ref>") and "PU#".
What only one customer has comes from its customer file (:func:`rules_for`): subjects that name
it ("Lidl Pick Ups"), subjects made of its PO numbers ("<PO> & <PO>", "<PO> MISSED PICK UP"), its
own portal ("DCT Transporter Too Late"), its desks, and the traffic to drop ("CIR Capacity").

A thread is kept as a whole once any message in it qualifies, so a "Thank you!" or a "Both
orders?" with a bare "Re:" subject rides along with the request it answers. Dropped subjects are
never kept, whatever else they match; marketplace notices (new tenders, tracking requests, RFQs)
are dropped for every customer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, replace
from email.utils import getaddresses

from facility_profiles.customers.profile import Customer

PICKUP_NUMBER_RE = re.compile(
    r"\b(?:pickup|pick\s*-?\s*up|pu|appointment|appt|confirmation|conf)\s*(?:#|number|no\.?)"
    r"\s*[:#]?\s*([A-Z]{0,4}-?\d{4,12})\b",
    re.I,
)
_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fw|fwd|aw|wg)\s*:\s*)+", re.I)
_INCIDENT = r"missed|no coverage|break\s*down|reschedul|late|cancel|delay"

# (reason, pattern) tried in order against the subject with reply prefixes removed. A customer's
# own rules go between the first and the rest (see :func:`rules_for`).
KEEP_FIRST: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pickup-appointment",
        re.compile(
            r"\bpick\s*-?\s*ups?\b.{0,24}\bappt|\bpick\s*-?\s*ups?\b.{0,24}\bappointments?\b|"
            r"\bappointments?\b.{0,24}\bpick\s*-?\s*ups?\b",
            re.I,
        ),
    ),
)
KEEP_LAST: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("reschedule", re.compile(r"^\W*reschedul", re.I)),
    (
        "portal-appointment",
        re.compile(
            r"\bappointment\b.{0,30}\b(?:shipment change|confirm|schedul|cancel|reschedul|request|"
            r"reminder|update)|\b(?:shipment|dock|delivery)\s+appointment\b|\bopendock\b",
            re.I,
        ),
    ),
    ("pickup-number", re.compile(r"\bPU\s*#|\bpick\s*-?\s*up\s*#|\bpickup\s*(?:#|number)", re.I)),
)
DROP_SUBJECT: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bnew (?:quote|tender) request\b", re.I),
    re.compile(r"\btracking requested\b", re.I),
    re.compile(r"\bclosed for bidding\b", re.I),
    re.compile(r"\brfq\b", re.I),
)
# Portals whose notifier writes only about appointments, whoever the customer is.
DESK_DOMAINS: frozenset[str] = frozenset({"opendock.com"})


@dataclass(frozen=True)
class MailRules:
    """What makes a group message about booking, for one customer's group."""

    keep: tuple[tuple[str, re.Pattern[str]], ...]
    drop: tuple[re.Pattern[str], ...]
    desks: frozenset[str] = frozenset()
    desk_domains: frozenset[str] = DESK_DOMAINS
    group: str | None = None
    po: re.Pattern[str] | None = None
    delivery_ref: re.Pattern[str] | None = None

    def with_desks(self, extra: Iterable[str]) -> MailRules:
        """The same rules with more appointment-desk addresses."""
        more = {d.strip().lower() for d in extra if d and d.strip()}
        return replace(self, desks=self.desks | more)


GENERIC = MailRules(keep=KEEP_FIRST + KEEP_LAST, drop=DROP_SUBJECT)


def po_subject_rules(po: re.Pattern[str]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """Subjects made of the customer's PO numbers: "A & B", or a PO and what went wrong."""
    p = po.pattern
    return (
        ("po-numbers", re.compile(rf"^\W*(?:{p})(?:\W+(?:&|and|,|/|\+)?\W*(?:{p}))*\W*$", re.I)),
        (
            "po-incident",
            re.compile(
                rf"\b(?:{p})\b.*\b(?:{_INCIDENT})|\b(?:missed|no coverage|break\s*down|reschedul)"
                rf"\w*\b.*\b(?:{p})\b",
                re.I,
            ),
        ),
    )


def rules_for(customer: Customer | None) -> MailRules:
    """The shared rules plus one customer's subjects, numbers and desks."""
    if customer is None:
        return GENERIC
    own = customer.archive_keep + (po_subject_rules(customer.po) if customer.po else ())
    desks = set(customer.archive_desks)
    if customer.customer_desk:
        desks.add(customer.customer_desk)
    return MailRules(
        keep=KEEP_FIRST + own + KEEP_LAST,
        drop=DROP_SUBJECT + customer.archive_drop,
        desks=frozenset(desks),
        desk_domains=DESK_DOMAINS | customer.archive_desk_domains,
        group=customer.group,
        po=customer.po,
        delivery_ref=customer.delivery_ref,
    )


def normalize_subject(subject: str | None) -> str:
    """The subject without reply and forward prefixes, whitespace squashed."""
    return re.sub(r"\s+", " ", _PREFIX_RE.sub("", subject or "")).strip()


def is_dropped(subject: str | None, rules: MailRules = GENERIC) -> bool:
    """True for the traffic that is never about a pickup appointment."""
    s = normalize_subject(subject)
    return any(p.search(s) for p in rules.drop)


def subject_reason(subject: str | None, rules: MailRules = GENERIC) -> str | None:
    """The first keep rule the subject satisfies, as ``subject:<rule>``; None when none does."""
    s = normalize_subject(subject)
    for name, pattern in rules.keep:
        if pattern.search(s):
            return f"subject:{name}"
    return None


def participants(*header_values: str | None) -> set[str]:
    """Lower-cased bare addresses from From, To and Cc header values."""
    pairs = getaddresses([v for v in header_values if v])
    return {addr.strip().lower() for _, addr in pairs if addr and "@" in addr}


def desk_reason(addresses: set[str], rules: MailRules = GENERIC) -> str | None:
    """``desk:<address>`` when a known appointment desk took part, else None."""
    for addr in sorted(addresses):
        if addr in rules.desks or addr.split("@")[-1] in rules.desk_domains:
            return f"desk:{addr}"
    return None


def match_reason(
    subject: str | None, addresses: set[str], rules: MailRules = GENERIC
) -> str | None:
    """Why a message is kept on its own merits, or None. Thread membership is the caller's rule."""
    if is_dropped(subject, rules):
        return None
    return subject_reason(subject, rules) or desk_reason(addresses, rules)


def identifiers(*texts: str | None, rules: MailRules = GENERIC) -> dict[str, list[str]]:
    """PO numbers, delivery references and pickup numbers, in order of first appearance.

    PO numbers and delivery references are read only for a customer whose file says what they
    look like.
    """
    joined = "\n".join(t for t in texts if t)

    def uniq(values: list[str]) -> list[str]:
        return list(dict.fromkeys(values))

    def every(pattern: re.Pattern[str] | None) -> list[str]:
        if pattern is None:
            return []
        return [m.group(0) for m in re.finditer(rf"\b(?:{pattern.pattern})\b", joined)]

    return {
        "po_numbers": uniq(every(rules.po)),
        "delivery_refs": uniq([m.upper() for m in every(rules.delivery_ref)]),
        "pickup_numbers": uniq(PICKUP_NUMBER_RE.findall(joined)),
    }
