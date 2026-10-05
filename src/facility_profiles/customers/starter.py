"""A starter customer file, for ``facility-profiles customers new``."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import date
from string import Template

_STARTER = Template(
    r"""# $name: say in a line what this customer's pickups are.
#
# Written by `facility-profiles customers new $key` on $today. Fill in what you know, leave out
# what does not apply, then check it: `facility-profiles customers show $key`.
# Lidl's file (src/facility_profiles/customers/lidl.toml) is a complete example.

name = "$name"
# description = "Inbound vendor pickups to the customer's DCs, pod NNNN"
# The pod's own time zone: what "today" means in this customer's daily summary.
# timezone = "America/New_York"

[transport_pro]
# The Transport Pro customer record(s) the loads are billed to: a load billed to any of them is
# this customer's. Pods list their customers in Transport Pro (customer search by terminal).
customer_ids = [$ids]
customer_names = [$names]
# The pod(s) that book this customer's pickups.
terminal_ids = [$terminals]
# Only some of the records need pickups booked (Lidl: inbound only)? List those here.
# booking_customer_ids = [$ids]

[mail]
# The Google Group the pickup threads run through. Drafts are from it and copy it.
$group_line
# cc = ["group@circledelivers.com"]       # defaults to the group; [] copies no one
# sender = "group@circledelivers.com"     # defaults to the group
$signature_line

[customer_desk]
# The customer's own desk that assigns delivery slots. When a vendor cannot ship, the agent
# drafts a note to it; mail from it is read for a new delivery slot. Without one, those cases
# go to a person.
$desk_line
# Where the pod books the delivery slot itself, if anywhere (Lidl: "DCT").
# delivery_system = ""

[numbers]
# Regular expressions for the customer's numbers, in single quotes. Leave out what you do not
# know yet; each one turns on something:
#   po            subjects made of PO numbers are kept by the mail archive
#   po_date       vendor desks in FP_BOOKING_PO_DATE_FLOOR_DESKS are never asked for a pickup
#                 before the date inside the PO (named groups dd, mm, and yy or yyyy)
#   delivery_ref  read off the delivery stop at scan, and the customer desk's new slots
# po = '\d{12}'
# po_date = '\d{4}(?P<dd>\d{2})(?P<mm>\d{2})(?P<yy>\d{2})\d{2}'
# delivery_ref = '[A-Z]{3}_\d{6,}'

[mail_archive]
# Only for the S3 archive of the group (`mail-archive collect --customer $key`), on top of the
# rules every customer shares (Pick Up Appointment subjects, RESCHEDULE, portal notices, PU#).
# keep_subjects = { $key-pickups = '\b$key\b.{0,24}\bpick\s*-?\s*ups?\b' }
# drop_subjects = ['^\W*weekly capacity\b']
# desks = ["shipping@vendor.example"]
# desk_domains = ["portal.example"]
"""
)


def starter_text(
    key: str,
    *,
    name: str,
    customer_ids: Sequence[int] = (),
    customer_names: Sequence[str] = (),
    terminal_ids: Sequence[int] = (),
    group: str | None = None,
    desk: str | None = None,
    today: date | None = None,
) -> str:
    """The text of a new customer file, with what is known filled in."""
    return _STARTER.substitute(
        key=key,
        name=name.replace('"', "'"),
        today=(today or date.today()).isoformat(),
        ids=", ".join(str(i) for i in customer_ids),
        names=", ".join(json.dumps(n) for n in customer_names),
        terminals=", ".join(str(t) for t in terminal_ids),
        group_line=f'group = "{group}"' if group else '# group = "group@circledelivers.com"',
        signature_line=(
            f'signature = "Circle Logistics, Inc. | Fort Wayne | 260-208-4500 | {group}"'
            if group
            else '# signature = "Circle Logistics, Inc. | Fort Wayne | 260-208-4500 | <group>"'
        ),
        desk_line=f'email = "{desk}"' if desk else '# email = "inbound@customer.example"',
    )
