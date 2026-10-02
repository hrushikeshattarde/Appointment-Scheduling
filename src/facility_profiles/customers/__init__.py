"""Customers: what differs from one customer to the next, one file each.

The agent was learned on Lidl's threads, and Lidl's specifics used to sit in the code: its
mailbox, its inbound desk, its DCT delivery references, the date inside its PO numbers, and which
group mail is about booking. They now live in ``lidl.toml`` next to this module, and nothing else
in the code names a customer. Adding a customer is adding a file:
``facility-profiles customers new <key>`` writes a starter and ``customers show <key>`` checks it.

The built-in files ship with the package. ``FP_CUSTOMERS_DIR`` adds a folder of more, for
example outside the public repository when a file names desks that should stay private. A file
there replaces a built-in one with the same key.

A load or a case belongs to the customer whose file lists its Transport Pro customer id, or else
its customer name. One that no file claims is written with the FP_BOOKING_* settings, which name
no customer.
"""

from facility_profiles.customers.profile import (
    BUILT_IN_DIR,
    FALLBACK_KEY,
    Customer,
    CustomerFileError,
    choose,
    load_customer,
    load_customers,
    load_dir,
    parse_customer,
)
from facility_profiles.customers.registry import (
    Customers,
    built_in_customers,
    customer_of,
    customers,
    fallback,
    reload,
    scope,
)

__all__ = [
    "BUILT_IN_DIR",
    "FALLBACK_KEY",
    "Customer",
    "CustomerFileError",
    "Customers",
    "built_in_customers",
    "choose",
    "customer_of",
    "customers",
    "fallback",
    "load_customer",
    "load_customers",
    "load_dir",
    "parse_customer",
    "reload",
    "scope",
]
