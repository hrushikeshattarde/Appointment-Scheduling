"""Contact hygiene: Circle's own addresses and numbers, and a person-name heuristic.

The customer's loads carry Circle's rate-confirmation mailbox and office numbers in the stop
contact block, and stop contact names are usually desk labels ("CBG Warehouse",
"7335 Scheduling"). Neither belongs on a facility profile.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from facility_profiles.domain.normalize import (
    normalize_company_name,
    normalize_email,
    normalize_phone,
)

DEFAULT_INTERNAL_EMAIL_DOMAINS: frozenset[str] = frozenset({"circledelivers.com"})
DEFAULT_INTERNAL_PHONES: frozenset[str] = frozenset({"260-208-4500"})

_NON_PERSON_TOKENS: frozenset[str] = frozenset(
    {
        "warehouse",
        "whse",
        "scheduling",
        "schedule",
        "scheduler",
        "shipping",
        "receiving",
        "dock",
        "docks",
        "desk",
        "office",
        "logistics",
        "dc",
        "inc",
        "llc",
        "corp",
        "co",
        "dry",
        "plant",
        "traffic",
        "appointments",
        "appointment",
        "appt",
        "appts",
        "team",
        "group",
        "beverage",
        "brewing",
        "brewery",
        "packaging",
        "industries",
        "distribution",
        "center",
        "ctr",
        "data",
        "system",
        "portal",
        "dispatch",
        "dispatcher",
        "carrier",
        "customer",
        "supply",
        "foods",
        "company",
        "services",
        "transport",
        "freight",
        "warehousing",
        "mfg",
        "manufacturing",
        "bottling",
        "yard",
        "terminal",
        "main",
        "front",
        "gate",
        "guard",
        "security",
        "shift",
        "night",
        "day",
        "ar",
        "ap",
        "billing",
        "accounts",
        "payable",
        "receivable",
        "ratecon",
        "rate",
        "con",
        "planning",
        "plan",
        "ops",
        "operations",
        "sales",
        "cs",
        "csr",
        "contact",
        "n/a",
        "na",
        "none",
        "unknown",
        "tbd",
        "driver",
        "dr",
        "inbound",
        "outbound",
        "lab",
        "store",
        "market",
        "supermarket",
        "retail",
        "service",
        "svc",
    }
)
_WORD_RE = re.compile("^[A-Za-z\u00c0-\u024f'.\\-]+$")


def base_phone(value: str | None) -> str | None:
    """``NNN-NNN-NNNN`` without any extension."""
    phone = normalize_phone(value)
    return phone.split(" x")[0] if phone else None


@dataclass(frozen=True)
class InternalContacts:
    """Email domains and phone numbers that belong to Circle, never to a facility."""

    email_domains: frozenset[str] = DEFAULT_INTERNAL_EMAIL_DOMAINS
    phone_numbers: frozenset[str] = DEFAULT_INTERNAL_PHONES

    @classmethod
    def build(cls, domains: Iterable[str] = (), phones: Iterable[str] = ()) -> InternalContacts:
        """Build from settings values, keeping the defaults."""
        domain_set = {d.strip().lower().lstrip("@") for d in domains if d and d.strip()}
        phone_set = {p for p in (base_phone(x) for x in phones) if p}
        return cls(
            email_domains=DEFAULT_INTERNAL_EMAIL_DOMAINS | frozenset(domain_set),
            phone_numbers=DEFAULT_INTERNAL_PHONES | frozenset(phone_set),
        )

    def with_phones(self, phones: Iterable[str | None]) -> InternalContacts:
        """Return a copy that also treats ``phones`` (for example terminal numbers) as internal."""
        extra = {p for p in (base_phone(x) for x in phones) if p}
        return InternalContacts(self.email_domains, self.phone_numbers | frozenset(extra))

    def is_internal_email(self, value: str | None) -> bool:
        """True for addresses on an internal domain."""
        email = normalize_email(value)
        if not email:
            return False
        domain = email.rsplit("@", 1)[-1]
        return any(domain == d or domain.endswith("." + d) for d in self.email_domains)

    def is_internal_phone(self, value: str | None) -> bool:
        """True for internal office numbers, ignoring extensions."""
        base = base_phone(value)
        return base is not None and base in self.phone_numbers


def looks_like_person_name(value: str | None, facility_names: Iterable[str | None] = ()) -> bool:
    """Heuristic: one to four alphabetic words, no organisation or desk words, not the facility."""
    text = " ".join((value or "").split())
    if not text or any(ch.isdigit() for ch in text) or "@" in text or "/" in text:
        return False
    words = text.split(" ")
    if not 1 <= len(words) <= 4 or not all(_WORD_RE.match(w) for w in words):
        return False
    lowered = {w.lower().strip(".'-") for w in words}
    if lowered & _NON_PERSON_TOKENS:
        return False
    norm = normalize_company_name(text)
    if not norm:
        return False
    for name in facility_names:
        other = normalize_company_name(name)
        if not other:
            continue
        if norm == other or re.search(rf"\b{re.escape(norm)}\b", other):
            return False
        long_words = {w for w in norm.split() if len(w) >= 4}
        if long_words & {w for w in other.split() if len(w) >= 4}:
            return False
    return True
