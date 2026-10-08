"""A request a person at Circle emailed the facility themselves counts as the pickup's request."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import InboundMessage
from facility_profiles.booking.models import (
    PERSON_MAIL,
    BookingCase,
    BookingMessage,
    CaseStatus,
    ExceptionType,
)
from facility_profiles.booking.person_requests import asked_slot, to_facility
from facility_profiles.booking.service import has_request, ingest, list_cases, ready_to_draft, scan
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.worklist import flag, open_kinds
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "226321092660"
SENT = NOW + timedelta(hours=1)  # Tue 09/29 09:00 ET
GROUP = "Lidl Group <lidl@circledelivers.com>"
ASK = "Hello,\n\nCan I please schedule the following?\n\nPO# 226321092660 on 10/01 @ 0800\n\nThank you!"


@pytest.mark.parametrize(
    ("line", "asked"),
    [
        ("PO# 115812102601 & 115812102602 (one truck) on 10/12 @ 0900", "2026-10-12 09:00"),
        ("PO# 115812102601 on 10/12 @ 1000", "2026-10-12 10:00"),
        ("PO# 115812102601 on 10/12 @ 9am", "2026-10-12 09:00"),
        ("PO# 115812102601 on 10/12/26 @ 2:30 pm", "2026-10-12 14:30"),
        ("PO# 115812102601 on 1/5 @ 0700", "2027-01-05 07:00"),  # January, asked in the fall
        ("PO# 115812102601 on 9/20 @ 0900", None),  # already behind the email
        ("PO# 115812102601, can you confirm?", None),  # no day and time
        ("PO# 999999999999 on 10/12 @ 0900", None),  # another pickup's PO
    ],
)
def test_the_day_and_time_asked_for_a_po_are_read_off_its_line(
    line: str, asked: str | None
) -> None:
    sent = datetime(2026, 10, 8, 10, 0, tzinfo=UTC)
    found = asked_slot(f"Hello,\n\n{line}\n\nThank you!", ["115812102601"], sent=sent)
    assert (found[0] if found else None) == asked


def test_only_an_email_to_the_facility_is_a_request() -> None:
    def check(to: str, desk: str | None = "shipping.appointments@morganfoods.com") -> bool:
        return to_facility(
            to,
            "",
            desk=desk,
            internal=["circledelivers.com"],
            customer_addresses=["lidl@circledelivers.com", "inbound@lidl.us"],
        )

    assert check(f"Morgan Foods Appointments <shipping.appointments@morganfoods.com>, {GROUP}")
    assert check(f"Vera <vera@morganfoods.com>, {GROUP}")  # someone else at the desk's company
    assert not check(f"inbound@lidl.us, {GROUP}")  # the customer's desk
    assert not check(f"colleague@circledelivers.com, {GROUP}")
    assert not check(f"someone@elsewhere.example, {GROUP}")  # not the desk's company
    assert check(f"someone@elsewhere.example, {GROUP}", desk=None)  # no desk on the pickup yet


def _pickup(settings: Settings, sessions) -> tuple[Settings, int]:  # type: ignore[no-untyped-def]
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(7001, po=PO)], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        assert case.status == CaseStatus.UNSCHEDULED.value and not has_request(case)
        return settings, case.id


def _person(to: str, body: str = ASK, *, mid: str = "p1") -> InboundMessage:
    return InboundMessage(
        message_id=mid,
        thread_id="T-person",
        sent_at=SENT,
        from_addr="Jordan Lake <jordan.lake@circledelivers.com>",
        to_addr=to,
        cc_addr="",
        subject="Pick up Appointments: Lidl",
        body=body,
        rfc_message_id=f"<{mid}@circledelivers.com>",
    )


def _read(sessions, settings: Settings, message: InboundMessage) -> None:  # type: ignore[no-untyped-def]
    with session_scope(sessions) as s:
        stats = ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: None),  # type: ignore[arg-type,return-value]
            internal_domains=["circledelivers.com"],
            settings=settings,
        )
    assert stats.by_person == 1


def test_a_persons_request_moves_the_pickup_to_asked_and_starts_the_clock(
    settings: Settings, sessions
) -> None:
    settings, case_id = _pickup(settings, sessions)
    _read(sessions, settings, _person(f"CCI Desk <cci@udfinc.com>, {GROUP}"))
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        assert case.status == CaseStatus.PENDING.value
        assert case.requested_local == "2026-10-01 08:00" and case.thread_id == "T-person"
        assert has_request(case) and case not in ready_to_draft(s)  # the agent will not ask again
        kept = next(m for m in case.messages if m.kind == PERSON_MAIL)
        assert kept.classification["request"] == {
            "local": "2026-10-01 08:00",
            "line": "PO# 226321092660 on 10/01 @ 0800",
        }
        event = next(e for e in case.events if e.action == "requested_by_person")
        assert event.actor == "jordan.lake@circledelivers.com"
        assert event.detail["previous"] == "2026-10-01 09:00"
    # A day of weekday hours with no answer from the desk: the no-reply to-do.
    with session_scope(sessions) as s:
        sweep(s, now=SENT + timedelta(hours=25), settings=settings)
        case = s.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == [ExceptionType.UNANSWERED_24H.value]


def test_a_note_to_the_customers_desk_is_not_a_request(settings: Settings, sessions) -> None:
    settings, case_id = _pickup(settings, sessions)
    _read(sessions, settings, _person(f"inbound@lidl.us, {GROUP}"))
    _read(sessions, settings, _person(f"cci@udfinc.com, {GROUP}", "PO# 226321092660?", mid="p2"))
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        assert case.status == CaseStatus.UNSCHEDULED.value and not has_request(case)


def test_an_ask_that_makes_the_delivery_settles_cannot_make_the_delivery(
    settings: Settings, sessions
) -> None:
    settings, case_id = _pickup(settings, sessions)
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        flag(s, case, ExceptionType.LOAD_INFEASIBLE, "no pickup on 10/02 makes the delivery")
    _read(sessions, settings, _person(f"cci@udfinc.com, {GROUP}"))
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == []
        done = next(e for e in case.exceptions if e.kind == "load_infeasible")
        assert (
            done.resolution
            == "jordan.lake@circledelivers.com asked the facility for Thu 10/01 08:00 ET"
        )


def test_an_email_kept_before_is_read_for_its_request(settings: Settings, sessions) -> None:
    settings, case_id = _pickup(settings, sessions)
    message = _person(f"cci@udfinc.com, {GROUP}")
    with session_scope(sessions) as s:  # kept by a board that did not read requests yet
        case = s.get(BookingCase, case_id)
        assert case is not None
        case.messages.append(
            BookingMessage(
                direction="out",
                kind=PERSON_MAIL,
                to_addr=message.to_addr,
                from_addr=message.from_addr,
                body=message.body,
                message_id=message.message_id,
                rfc_message_id=message.rfc_message_id,
                sent_at=SENT,
                classification={},
            )
        )
    with session_scope(sessions) as s:
        ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: None),  # type: ignore[arg-type,return-value]
            internal_domains=["circledelivers.com"],
            settings=settings,
        )
        case = s.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.PENDING.value
        assert len([m for m in case.messages if m.kind == PERSON_MAIL]) == 1
