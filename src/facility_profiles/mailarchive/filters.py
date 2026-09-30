"""Which group messages are about booking a pickup, and the identifiers inside them.

The rules come from the lidl@ archive read in September 2026: the pod's own subjects ("Pick Up
Appointment: <PO>", "Pick Up Appointments", "Lidl Pick Ups"), the inbound desk's ("<PO> & <PO>",
"RESCHEDULE <PO>", "<PO> MISSED PICK UP"), the portals' ("Appointment Shipment Change: <ref>",
"DCT Transporter Too Late"), and the desks that answer them. A thread is kept as a whole once any
message in it qualifies, so a "Thank you!" or a "Both orders?" with a bare "Re:" subject rides
along with the request it answers.

Tour planning ("CIR Capacity", "CRL Daily Recap") and the Emerge marketplace notices are never
about a pickup appointment and are dropped before any other rule runs.
"""

from __future__ import annotations

import re
from email.utils import getaddresses

PO_RE = re.compile(r"\b\d{12}\b")
DELIVERY_REF_RE = re.compile(r"\b[A-Z]{3}_\d{6,}\b")
PICKUP_NUMBER_RE = re.compile(
    r"\b(?:pickup|pick\s*-?\s*up|pu|appointment|appt|confirmation|conf)\s*(?:#|number|no\.?)"
    r"\s*[:#]?\s*([A-Z]{0,4}-?\d{4,12})\b",
    re.I,
)
_PREFIX_RE = re.compile(r"^(?:\s*(?:re|fw|fwd|aw|wg)\s*:\s*)+", re.I)

# (reason, pattern) tried in order against the subject with reply prefixes removed.
KEEP_SUBJECT: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "pickup-appointment",
        re.compile(
            r"\bpick\s*-?\s*ups?\b.{0,24}\bappt|\bpick\s*-?\s*ups?\b.{0,24}\bappointments?\b|"
            r"\bappointments?\b.{0,24}\bpick\s*-?\s*ups?\b",
            re.I,
        ),
    ),
    (
        "lidl-pickups",
        re.compile(
            r"\blidl\b.{0,24}\bpick\s*-?\s*ups?\b|\bpick\s*-?\s*ups?\b.{0,24}\blidl\b", re.I
        ),
    ),
    ("po-numbers", re.compile(r"^\W*\d{12}(?:\W+(?:&|and|,|/|\+)?\W*\d{12})*\W*$", re.I)),
    (
        "po-incident",
        re.compile(
            r"\b\d{12}\b.*\b(?:missed|no coverage|break\s*down|reschedul|late|cancel|delay)|"
            r"\b(?:missed|no coverage|break\s*down|reschedul)\w*\b.*\b\d{12}\b",
            re.I,
        ),
    ),
    ("reschedule", re.compile(r"^\W*reschedul", re.I)),
    (
        "portal-appointment",
        re.compile(
            r"\bappointment\b.{0,30}\b(?:shipment change|confirm|schedul|cancel|reschedul|request|"
            r"reminder|update)|\b(?:shipment|dock|delivery)\s+appointment\b|\bopendock\b|"
            r"\bdct\b|\btransporter too late\b",
            re.I,
        ),
    ),
    ("pickup-number", re.compile(r"\bPU\s*#|\bpick\s*-?\s*up\s*#|\bpickup\s*(?:#|number)", re.I)),
)
DROP_SUBJECT: tuple[re.Pattern[str], ...] = (
    re.compile(r"^\W*cir capacity\b", re.I),
    re.compile(r"^\W*crl daily recap\b", re.I),
    re.compile(r"\bnew (?:quote|tender) request\b", re.I),
    re.compile(r"\btracking requested\b", re.I),
    re.compile(r"\bclosed for bidding\b", re.I),
    re.compile(r"\brfq\b", re.I),
)

# Desks that only ever write about appointments: the vendors' booking desks confirmed in the
# archive, the customer's inbound desk that assigns delivery slots, and the DCT portal's notifier.
DEFAULT_DESKS: frozenset[str] = frozenset(
    {
        "inbound@lidl.us",
        "shipping.appointments@morganfoods.com",
        "csrpolarbev@polarbev.com",
        "mmcdonald@polarbev.com",
        "gweaver@delgrossos.com",
        "transportation@hanoverlogistics.com",
        "lebanonvalley@rlslogistics.com",
        "wesley.brown@premiumwaters.com",
        "l.albadawi@cgroxane.com",
        "shippingjohnstown@cgroxane.com",
        "cci@udfinc.com",
        "ripondistribution@senecafoods.com",
    }
)
DEFAULT_DESK_DOMAINS: frozenset[str] = frozenset({"softhouse.nl", "opendock.com"})


def normalize_subject(subject: str | None) -> str:
    """The subject without reply and forward prefixes, whitespace squashed."""
    return re.sub(r"\s+", " ", _PREFIX_RE.sub("", subject or "")).strip()


def is_dropped(subject: str | None) -> bool:
    """True for the traffic that is never about a pickup appointment."""
    s = normalize_subject(subject)
    return any(p.search(s) for p in DROP_SUBJECT)


def subject_reason(subject: str | None) -> str | None:
    """The first keep rule the subject satisfies, as ``subject:<rule>``; None when none does."""
    s = normalize_subject(subject)
    for name, pattern in KEEP_SUBJECT:
        if pattern.search(s):
            return f"subject:{name}"
    return None


def participants(*header_values: str | None) -> set[str]:
    """Lower-cased bare addresses from From, To and Cc header values."""
    pairs = getaddresses([v for v in header_values if v])
    return {addr.strip().lower() for _, addr in pairs if addr and "@" in addr}


def desk_reason(
    addresses: set[str],
    desks: frozenset[str] | set[str] = DEFAULT_DESKS,
    desk_domains: frozenset[str] | set[str] = DEFAULT_DESK_DOMAINS,
) -> str | None:
    """``desk:<address>`` when a known appointment desk took part, else None."""
    lowered = {d.lower() for d in desks}
    domains = {d.lower() for d in desk_domains}
    for addr in sorted(addresses):
        if addr in lowered or addr.split("@")[-1] in domains:
            return f"desk:{addr}"
    return None


def match_reason(
    subject: str | None,
    addresses: set[str],
    desks: frozenset[str] | set[str] = DEFAULT_DESKS,
    desk_domains: frozenset[str] | set[str] = DEFAULT_DESK_DOMAINS,
) -> str | None:
    """Why a message is kept on its own merits, or None. Thread membership is the caller's rule."""
    if is_dropped(subject):
        return None
    return subject_reason(subject) or desk_reason(addresses, desks, desk_domains)


def identifiers(*texts: str | None) -> dict[str, list[str]]:
    """PO numbers, DCT delivery references and pickup numbers, in order of first appearance."""
    joined = "\n".join(t for t in texts if t)

    def uniq(values: list[str]) -> list[str]:
        seen: list[str] = []
        for v in values:
            if v not in seen:
                seen.append(v)
        return seen

    return {
        "po_numbers": uniq(PO_RE.findall(joined)),
        "delivery_refs": uniq([m.upper() for m in DELIVERY_REF_RE.findall(joined)]),
        "pickup_numbers": uniq(PICKUP_NUMBER_RE.findall(joined)),
    }
