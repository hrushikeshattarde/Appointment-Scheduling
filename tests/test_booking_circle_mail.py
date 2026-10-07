"""A pickup's emails as one chain, whoever sent them: the agent, the vendor, or a person at Circle.

The chain is shaped like a pickup the pod booked by hand, with invented people, numbers and
ids: the PO asked of Lidl's desk, the request to the vendor's desk, its confirmation, the thanks,
and the vendor's "Your Welcome". Every one had the Lidl group on it.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime, timedelta

from facility_profiles.api.booking import _in_order, latest_updates
from facility_profiles.booking.automation import read_mail
from facility_profiles.booking.classify import FakeReplyClassifier, ReplyContext
from facility_profiles.booking.mail import InboundMessage, OutboundDraft
from facility_profiles.booking.models import PERSON_MAIL, BookingCase, BookingMessage
from facility_profiles.booking.outbox import check_send_gate
from facility_profiles.booking.respond import rounds_so_far
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.service import ingest, list_cases, scan
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "226301102660"
GROUP = "Lidl Group <Lidl@circledelivers.com>"
MEGAN = "Dana Planner <dana.planner@circledelivers.com>"
SHANNON = "Desk Staff <desk.staff@udfinc.com>"
START = datetime(2026, 9, 28, 18, 55, tzinfo=UTC)


def mail(
    mid: str,
    minutes: int,
    sender: str,
    to: str,
    body: str,
    *,
    subject: str = f"Pick Up Appointment {PO}",
    cc: str = "",
    answers: str | None = None,
) -> InboundMessage:
    return InboundMessage(
        message_id=f"g-{mid}",
        thread_id=None,
        sent_at=START + timedelta(minutes=minutes),
        from_addr=sender,
        to_addr=to,
        cc_addr=cc,
        subject=subject,
        body=body,
        in_reply_to=f"<{answers}@mail.example>" if answers else None,
        rfc_message_id=f"<{mid}@mail.example>",
        references=f"<{answers}@mail.example>" if answers else None,
    )


CHAIN = [
    mail(
        "ask-po",
        0,
        MEGAN,
        f"_US_Mail_Inbound <inbound@lidl.us>, {GROUP}",
        "Hello,\n\nCan you please provide the PO# for Erlanger to PYE?\nDelivery# PYE_021026123",
        subject="PO Request",
    ),
    mail(
        "po-given",
        3,
        '"inbound @lidl.us" <inbound@lidl.us>',
        MEGAN,
        f"Hey Dana, PO is {PO}\n\nThanks!",
        subject="Re: PO Request",
        cc=GROUP,
        answers="ask-po",
    ),
    mail(
        "request",
        24 * 60,
        MEGAN,
        f"CCI <cci@udfinc.com>, {GROUP}",
        "Hello,\n\nCan I please schedule the following for Koch Foods going to Lidl?\n\n"
        f"PO# {PO} on 10/01 @ 0900\n\nThank you!",
    ),
    mail(
        "confirmed",
        24 * 60 + 43,
        SHANNON,
        f"{MEGAN}, CCI <CCI@udfinc.com>, {GROUP}",
        f"10/1/26 9:00 AM CIRCLE #{PO} *NEW/NEEDPAPERWORK X LIDL CCI2-90001\n\nThank you!",
        subject=f"RE: Pick Up Appointment {PO}",
        answers="request",
    ),
    mail(
        "thanks",
        24 * 60 + 63,
        MEGAN,
        SHANNON,
        "Thank you!",
        subject=f"Re: Pick Up Appointment {PO}",
        cc=f"CCI <CCI@udfinc.com>, {GROUP}",
        answers="confirmed",
    ),
    mail(
        "welcome",
        24 * 60 + 66,
        SHANNON,
        MEGAN,
        "Your Welcome\n\nThank you!",
        subject=f"RE: Pick Up Appointment {PO}",
        cc=f"CCI <CCI@udfinc.com>, {GROUP}",
        answers="thanks",
    ),
]
# Circle mail that is not the pickup's conversation: no group on it, or no pickup it is about.
ASIDE = [
    mail(
        "aside", 24 * 60 + 70, MEGAN, "Sam Ops <sam.ops@circledelivers.com>", f"FYI {PO} is booked."
    ),
    mail("other", 24 * 60 + 80, MEGAN, GROUP, "Lunch order for Friday?", subject="Lunch"),
]


def _reading(ctx: ReplyContext) -> ReplyClassification:
    if ctx.body.startswith("10/1/26"):
        return ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date="2026-10-01",
            pickup_time="09:00",
            pickup_number="CCI2-90001",
            quotes=["10/1/26 9:00 AM", "CCI2-90001"],
        )
    return ReplyClassification(status=ReplyStatus.UNRELATED)


def _booked_by_hand(settings, sessions) -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    load = lidl_load(7001, po=PO)
    load["reference"]["pickupNumber"] = "CCI2-90001"
    scan(FakeTPro([load], {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.status == "scheduled" and case.messages == []
        return case.id


def test_a_pickup_booked_by_hand_shows_its_whole_email_chain(settings, sessions) -> None:
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    case_id = _booked_by_hand(settings, sessions)
    shuffled = CHAIN + ASIDE
    random.Random(7).shuffle(shuffled)  # the archive hands mail over in no particular order
    with session_scope(sessions) as session:
        stats = ingest(
            session,
            shuffled,
            FakeReplyClassifier(_reading),
            internal_domains=["circledelivers.com"],
            settings=settings,
        )
        assert stats.by_person == 3 and stats.skipped_internal == 2
        case = session.get(BookingCase, case_id)
        assert case is not None
        chain = [(m.kind, m.subject) for m in _in_order(case.messages)]
        assert chain == [
            (PERSON_MAIL, "PO Request"),
            ("customer_desk", "Re: PO Request"),
            (PERSON_MAIL, f"Pick Up Appointment {PO}"),
            ("reply", f"RE: Pick Up Appointment {PO}"),
            (PERSON_MAIL, f"Re: Pick Up Appointment {PO}"),
            ("reply", f"RE: Pick Up Appointment {PO}"),
        ]
        mine = [m for m in case.messages if m.kind == PERSON_MAIL]
        assert {m.from_addr for m in mine} == {MEGAN}
        assert all(m.sent_at is not None and m.direction == "out" for m in mine)
        # Kept, not acted on: still booked, nothing for a person to do, no round of the agent's.
        assert case.status == "scheduled" and open_kinds(case) == []
        # CCI's confirmation carries the pickup's own number: it is the booking, not a change.
        assert stats.booked_changed == 0 and stats.linked_outbound == 0
        assert case.confirmed_local == "2026-10-01 09:00"
        assert "vendor_reconfirmed" in [e.action for e in case.events]
        assert rounds_so_far(case) == 0
        assert [e.actor for e in case.events if e.action == "sent_by_person"] == [
            "dana.planner@circledelivers.com"
        ] * 3

    with session_scope(sessions) as session:  # read again: nothing is kept twice
        again = ingest(
            session,
            CHAIN,
            FakeReplyClassifier(_reading),
            internal_domains=["circledelivers.com"],
            settings=settings,
        )
        assert again.by_person == 0 and again.duplicates == 3
        case = session.get(BookingCase, case_id)
        assert case is not None and len(case.messages) == 6


def test_a_persons_mail_never_counts_against_the_agents_daily_cap(settings, sessions) -> None:
    settings = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_mode": "send", "booking_send_daily_cap": 1}
    )
    case_id = _booked_by_hand(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.messages.append(
            BookingMessage(
                case_id=case.id,
                direction="out",
                kind=PERSON_MAIL,
                to_addr="cci@udfinc.com",
                subject="by hand",
                body="Thank you!",
                message_id="g-now",
                sent_at=datetime.now(tz=UTC),
            )
        )
        session.flush()
        check_send_gate(  # one agent send still allowed today
            session,
            case,
            OutboundDraft(to_addr="cci@udfinc.com", cc_addr=None, subject="s", body="b"),
            settings,
            trusted_desk="cci@udfinc.com",
        )


class FakeInbox:
    def __init__(self, messages: list[InboundMessage]) -> None:
        self.messages = messages

    def fetch(self) -> list[InboundMessage]:
        return list(self.messages)


def test_the_board_reads_mail_without_answering_it(settings, sessions) -> None:
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    case_id = _booked_by_hand(settings, sessions)
    with session_scope(sessions) as session:
        report = read_mail(
            session,
            settings,
            inbox=FakeInbox(list(reversed(CHAIN))),
            classifier=FakeReplyClassifier(_reading),
        )
        assert report.mail_by_person == 3 and report.mail_read == 3 and report.mail_failed == 0
        assert report.mail_answered == 0
        case = session.get(BookingCase, case_id)
        assert case is not None and len(case.messages) == 6
        # Nothing of the agent's own: no thank-you, no answer, no draft.
        assert [m for m in case.messages if m.direction == "out" and m.kind != PERSON_MAIL] == []

        updates = latest_updates([case], now=START + timedelta(days=2))
        emails = [u for u in updates if u["source"] == "email"]
        assert [u["what"] for u in emails[:3]] == [
            "Desk Staff wrote",
            "Dana Planner emailed Desk Staff",
            "Desk Staff wrote, read as confirmed",
        ]
        assert emails[-1]["what"] == "Dana Planner emailed _US_Mail_Inbound"
        assert emails[-1]["about"] == "PO Request" and emails[-1]["case_id"] == case_id
        assert any(u["source"] == "load" and u["what"] == "Found on a load" for u in updates)


def test_mail_about_a_load_already_picked_up_is_kept_not_read(settings, sessions) -> None:
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    case_id = _booked_by_hand(settings, sessions)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None
        case.tpro_seen = {**(case.tpro_seen or {}), "load_status": "Delivered"}
    asked = FakeReplyClassifier(_reading)
    late = mail("late", 9 * 24 * 60, SHANNON, f"{MEGAN}, {GROUP}", f"Can we move {PO} to 10/2?")
    with session_scope(sessions) as session:
        stats = ingest(
            session, [late], asked, internal_domains=["circledelivers.com"], settings=settings
        )
        case = session.get(BookingCase, case_id)
        assert case is not None
        assert stats.after_decision == 1 and asked.calls == []
        assert open_kinds(case) == [] and case.status == "scheduled"
        kept = case.messages[-1]
        assert kept.classification == {"skipped": "the load was already picked up"}
