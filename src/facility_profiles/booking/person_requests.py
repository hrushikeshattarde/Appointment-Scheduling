"""A pickup a person at Circle asked the facility for by their own email: it is the request.

The pod still writes some requests by hand, in its own words: "Can I please schedule the
following? PO# 115812102601 & 115812102602 (one truck) on 10/12 @ 0900". Such an email is kept
on its pickups as sent by a person (``service.attach_circle_mail``), and it also asks for them:

- the pickup moves to *Asked vendor*, with the time asked for;
- the no-reply clock starts from it, and the agent does not ask again;
- *Cannot make the delivery* and *Slot will not work* are checked again against that time, and a
  facility's decline or a moved delivery is settled by the new ask.

Only an email to the facility counts (an address outside Circle that is not the customer's group
or desk, at the pickup's desk's company when the pickup has one), and only a line of its own words
that names one of the pickup's POs with a day and a time ("10/12 @ 0900", "10/12 @ 9am"). Times
in the pod's emails are Eastern.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime

from facility_profiles.mailarchive.filters import participants

# "10/12 @ 0900", "10/12/26 @ 9:30", "10/12 @ 9am": a day and a time, as the pod writes them.
_WHEN_RE = re.compile(
    r"(?<!\d)(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?\s*@\s*(\d{1,2})(?::?(\d{2}))?\s*"
    r"(a\.?\s?m\.?|p\.?\s?m\.?)?(?![\w])",
    re.I,
)
# How far back a day may be from the email and still be this year's (a December email asking
# for January means next year).
_PAST_DAYS = 60


def asked_slot(text: str, pos: Iterable[str], *, sent: datetime) -> tuple[str, str] | None:
    """The day and time a line of ``text`` asks for one of ``pos``, Eastern, and that line.

    "YYYY-MM-DD HH:MM" and the line as written; None when no line names a PO with a day and a
    time. A day already behind the email is not an ask.
    """
    wanted = [str(p) for p in pos if p]
    for raw in text.splitlines():
        line = raw.strip()
        if not wanted or not any(p in line for p in wanted):
            continue
        found = _WHEN_RE.search(line)
        if found is None:
            continue
        when = _slot(found, sent)
        if when is not None:
            return when, line
    return None


def _slot(found: re.Match[str], sent: datetime) -> str | None:
    month, day = int(found.group(1)), int(found.group(2))
    hour, minute = int(found.group(4)), int(found.group(5) or 0)
    suffix = (found.group(6) or "").replace(".", "").replace(" ", "").lower()
    if suffix:
        if not 1 <= hour <= 12:
            return None
        hour = hour % 12 + (12 if suffix == "pm" else 0)
    year_text = found.group(3)
    year = (2000 + int(year_text) if len(year_text) == 2 else int(year_text)) if year_text else 0
    try:
        asked = date(year or sent.year, month, day)
        if not year_text and (sent.date() - asked).days > _PAST_DAYS:
            asked = date(sent.year + 1, month, day)
        clock = datetime(asked.year, asked.month, asked.day, hour, minute)
    except ValueError:
        return None
    if asked < sent.date():
        return None
    return clock.strftime("%Y-%m-%d %H:%M")


def to_facility(
    to_addr: str | None,
    cc_addr: str | None,
    *,
    desk: str | None,
    internal: Iterable[str],
    customer_addresses: Iterable[str | None],
) -> bool:
    """True when the email went to the facility: not only to Circle, the group or the customer.

    With a desk on the pickup, a recipient at the desk's company is needed.
    """
    inside = {d.lower() for d in internal}
    theirs = {a.lower() for a in customer_addresses if a}
    outside = [
        a
        for a in participants(to_addr, cc_addr)
        if a.rpartition("@")[2] not in inside and a not in theirs
    ]
    if not desk:
        return bool(outside)
    company = desk.lower().rpartition("@")[2]
    return any(a.rpartition("@")[2] == company for a in outside)
