"""Business days: Monday to Friday, less the holidays shippers and warehouses close for.

A pickup is asked for on a business day, the no-reply clock and link expiry count business
hours, and "the business day before" (a desk's cut-off) skips holidays the way it skips weekends.

The holidays are the six US freight holidays: New Year's Day, Memorial Day, Independence Day,
Labor Day, Thanksgiving and Christmas Day. One that falls on a Saturday is observed the Friday
before, one on a Sunday the Monday after, as shippers and receiving docks observe them.
"""

from __future__ import annotations

from datetime import date, timedelta
from functools import cache

SATURDAY, SUNDAY = 5, 6


def _nth(year: int, month: int, weekday: int, n: int) -> date:
    """The n-th given weekday of a month (n = -1: the last)."""
    if n > 0:
        first = date(year, month, 1)
        return first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    last = date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1)
    return last - timedelta(days=(last.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == SATURDAY:
        return day - timedelta(days=1)
    if day.weekday() == SUNDAY:
        return day + timedelta(days=1)
    return day


@cache
def _year(year: int) -> dict[date, str]:
    actual = {
        date(year, 1, 1): "New Year's Day",
        _nth(year, 5, 0, -1): "Memorial Day",
        date(year, 7, 4): "Independence Day",
        _nth(year, 9, 0, 1): "Labor Day",
        _nth(year, 11, 3, 4): "Thanksgiving",
        date(year, 12, 25): "Christmas Day",
    }
    return {_observed(day): name for day, name in actual.items()}


def holiday(day: date) -> str | None:
    """The freight holiday observed on ``day``, or None.

    Next year's New Year's Day observed on 12/31 (it fell on a Saturday) counts in this year.
    """
    return _year(day.year).get(day) or _year(day.year + 1).get(day)


def is_business_day(day: date) -> bool:
    """Monday to Friday, and not a freight holiday."""
    return day.weekday() < SATURDAY and holiday(day) is None


def why_closed(day: date) -> str | None:
    """Why ``day`` is not a business day ("a Saturday", "a holiday (Thanksgiving)"), or None."""
    name = holiday(day)
    if name is not None:
        return f"a holiday ({name})"
    if day.weekday() >= SATURDAY:
        return f"a {day:%A}"
    return None


def previous_business_day(day: date) -> date:
    """The last business day before ``day``."""
    day -= timedelta(days=1)
    while not is_business_day(day):
        day -= timedelta(days=1)
    return day


def next_business_day(day: date) -> date:
    """The first business day after ``day``."""
    day += timedelta(days=1)
    while not is_business_day(day):
        day += timedelta(days=1)
    return day
