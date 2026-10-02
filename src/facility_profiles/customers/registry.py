"""Which customer a load or case belongs to, and what a harvest or scan covers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Protocol

from facility_profiles.customers.profile import (
    FALLBACK_KEY,
    FALLBACK_SOURCE,
    Customer,
    CustomerFileError,
    choose,
    load_customers,
)

if TYPE_CHECKING:
    from facility_profiles.config import Settings


class HasCustomer(Protocol):
    """Anything that carries the Transport Pro customer (a booking case)."""

    customer_id: int | None
    customer_name: str | None


@dataclass(frozen=True)
class Customers:
    """The customer files, plus the fallback for loads no file claims."""

    files: tuple[Customer, ...]
    fallback: Customer

    def keys(self) -> list[str]:
        """The keys of every file."""
        return [c.key for c in self.files]

    def get(self, key: str) -> Customer:
        """The customer a key names; :class:`CustomerFileError` when there is no such file."""
        return choose(list(self.files), key)

    def only_or(self, key: str | None) -> Customer:
        """The customer ``key`` names, or the only file there is."""
        return choose(list(self.files), key)

    def for_customer(self, customer_id: int | None, customer_name: str | None) -> Customer:
        """The file that claims this Transport Pro customer, else the fallback."""
        for c in self.files:
            if customer_id is not None and customer_id in c.tpro_customer_ids:
                return c
        for c in self.files:
            if c.claims(None, customer_name):
                return c
        return self.fallback

    def for_case(self, case: HasCustomer) -> Customer:
        """The customer a case belongs to."""
        return self.for_customer(case.customer_id, case.customer_name)

    def customer_desks(self) -> set[str]:
        """Every customer's own desk address."""
        return {c.customer_desk for c in (*self.files, self.fallback) if c.customer_desk}


@lru_cache(maxsize=8)
def _files(extra_dir: str | None) -> tuple[Customer, ...]:
    return tuple(load_customers(extra_dir))


def fallback(settings: Settings) -> Customer:
    """The customer no file claims: the FP_BOOKING_* settings, which name no customer."""
    return Customer(
        key=FALLBACK_KEY,
        name=None,
        source=FALLBACK_SOURCE,
        timezone=settings.booking_timezone,
        group=settings.booking_sender,
        sender=settings.booking_sender,
        cc=tuple(settings.booking_cc),
        signature=settings.booking_signature,
        customer_desk=(settings.booking_customer_desk or "").strip().lower() or None,
    )


def customers(settings: Settings) -> Customers:
    """The built-in customer files, FP_CUSTOMERS_DIR's, and the fallback from ``settings``."""
    return Customers(_files(settings.customers_dir), fallback(settings))


def built_in_customers() -> Customers:
    """The built-in files and a blank fallback, for code that has no settings at hand."""
    return Customers(_files(None), Customer(key=FALLBACK_KEY, name=None, source=FALLBACK_SOURCE))


def customer_of(case: HasCustomer, settings: Settings) -> Customer:
    """The customer a case belongs to."""
    return customers(settings).for_case(case)


def reload() -> None:
    """Forget the files read so far (after a file changed in a long-running process)."""
    _files.cache_clear()


def scope(
    settings: Settings,
    customer: Sequence[str] | None = None,
    terminal: Sequence[int] | None = None,
    *,
    booking: bool = False,
) -> tuple[list[int], list[int]]:
    """The terminals and Transport Pro customer ids a harvest or a scan covers.

    ``customer`` takes customer keys (``lidl``) and Transport Pro customer ids (``7211``)
    alike. A key brings its customer ids (for a booking scan, only those it books for) and,
    unless ``terminal`` is given, its terminals. Without ``customer``, FP_CUSTOMERS names the
    keys; without that, FP_PILOT_TERMINAL_IDS and FP_PILOT_CUSTOMER_IDS apply as they always did.
    """
    wanted = [w.strip() for w in (customer or settings.customers) if w.strip()]
    if not wanted:
        return list(terminal or settings.pilot_terminal_ids), list(settings.pilot_customer_ids)
    known = customers(settings)
    ids: list[int] = []
    terminals: list[int] = []
    for item in wanted:
        if item.isdigit():
            ids.append(int(item))
            continue
        c = known.get(item)
        if not c.tpro_customer_ids:
            msg = (
                f"customer {c.key!r} lists no Transport Pro customer ids; add "
                f"[transport_pro] customer_ids to {c.source}"
            )
            raise CustomerFileError(msg)
        ids.extend(c.booking_customer_ids if booking else c.tpro_customer_ids)
        terminals.extend(c.terminal_ids)
    if terminal:
        chosen = list(terminal)
    else:
        chosen = list(dict.fromkeys(terminals)) or list(settings.pilot_terminal_ids)
    return chosen, list(dict.fromkeys(ids))
