"""One vendor reply, several PO lines, several cases: line items, many-case matching, one answer."""

from __future__ import annotations

from datetime import UTC, datetime

from facility_profiles.booking.classify import (
    FakeReplyClassifier,
    ReplyContext,
    for_case,
    validate_classification,
)
from facility_profiles.booking.mail import InboundMessage, RecordingSender
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import ReplyClassification, ReplyItem, ReplyStatus
from facility_profiles.booking.service import (
    draft_batch,
    ingest,
    list_cases,
    match_case,
    match_cases,
    scan,
)
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import awaiting_approval
from tests.test_booking_real_thread import morgan_load, seed_morgan

EARLY = datetime(2026, 9, 23, 12, 0, tzinfo=UTC)
REPLY_AT = datetime(2026, 9, 24, 12, 35, tzinfo=UTC)
INTERNAL = ["circledelivers.com"]
A = ["115829092660", "115829092661"]
B = ["115802102660", "115802102661"]
# Vera's real answer to the pod's batched request, Outlook link cruft included.
TWO_LINES = (
    "115829092660<tel:(582)%20909-2660> & 115829092661-9/28 @ 9am pickup# 20463264\n"
    "115802102660<tel:(580)%20210-2660> & 115802102661-10/2 @ 9am pickup# 20463798 "
    "(this is showing a pickup date of 10/2 we cannot schedule early pickup's)\n\nThanks\nVera"
)
LINE_A = ReplyItem(
    po_numbers=A,
    status=ReplyStatus.CONFIRMED,
    pickup_date="2026-09-28",
    pickup_time="09:00",
    pickup_number="20463264",
    quotes=["115829092660 & 115829092661-9/28 @ 9am pickup# 20463264"],
)
LINE_B = ReplyItem(
    po_numbers=B,
    status=ReplyStatus.COUNTER_OFFER,
    pickup_date="2026-10-02",
    pickup_time="09:00",
    pickup_number="20463798",
    conditions=["we cannot schedule early pickup's"],
    quotes=[
        "115802102660 & 115802102661-10/2 @ 9am pickup# 20463798",
        "we cannot schedule early pickup's",
    ],
)
TWO_LINE_READING = ReplyClassification(
    status=ReplyStatus.COUNTER_OFFER,
    pickup_date="2026-10-02",
    pickup_time="09:00",
    pickup_number="20463798",
    quotes=["115802102660 & 115802102661-10/2 @ 9am pickup# 20463798"],
    items=[LINE_A, LINE_B],
    confidence=0.95,
)


def _batched(settings, sessions):  # type: ignore[no-untyped-def]
    """Two Morgan Foods cases sent in one email; returns (settings, sender, case ids, sent id)."""
    settings = settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "pilot_customer_ids": [7211],
            "booking_mode": "send",
            "booking_po_date_floor_desks": [],  # the thread being replayed predates the floor
        }
    )
    seed_morgan(sessions)
    loads = [
        morgan_load(8001, pos=(A[0], A[1]), pickup_open="2026-09-28T13:00:00Z"),
        morgan_load(8002, pos=(B[0], B[1]), pickup_open="2026-10-01T13:00:00Z"),
    ]
    scan(FakeTPro(loads, {}), sessions, settings, days_ahead=14, now=EARLY)  # type: ignore[arg-type]
    sender = RecordingSender()
    with session_scope(sessions) as session:
        cases = sorted(list_cases(session, CaseStatus.UNSCHEDULED.value), key=lambda c: c.id)
        messages = draft_batch(session, cases, sender, settings, now=EARLY)
        ids = [c.id for c in cases]
        sent_id = messages[0].rfc_message_id
    assert len(sender.drafts) == 1 and sent_id
    return settings, sender, ids, sent_id


def _reply(body: str, sent_id: str, rfc: str = "<vera-1@outlook.com>") -> InboundMessage:
    return InboundMessage(
        message_id=f"archive-{rfc.strip('<>')}",
        thread_id=None,
        sent_at=REPLY_AT,
        from_addr="Morgan Foods Appointments <shipping.appointments@morganfoods.com>",
        to_addr="lidl-appointments@circledelivers.com",
        cc_addr="Lidl Group <lidl@circledelivers.com>",
        subject="Re: Pick Up Appointments: 115829092660 & 115829092661 & 115802102660 & 115802102661",
        body=body,
        in_reply_to=sent_id,
        rfc_message_id=rfc,
        references=sent_id,
    )


def test_a_reply_answering_two_po_lines_moves_each_case_on_its_own_line(settings, sessions):
    settings, sender, (a_id, b_id), sent_id = _batched(settings, sessions)
    classifier = FakeReplyClassifier(lambda _c: TWO_LINE_READING)
    with session_scope(sessions) as session:
        reply = _reply(TWO_LINES, sent_id)
        assert [c.id for c in match_cases(session, reply)] == [a_id, b_id]
        stats = ingest(
            session,
            [reply],
            classifier,
            internal_domains=INTERNAL,
            responder=Responder(settings, sender, now=datetime(2026, 9, 24, 12, 40, tzinfo=UTC)),
        )
        session.commit()
        a, b = session.get(BookingCase, a_id), session.get(BookingCase, b_id)
        assert a is not None and b is not None
        # The classifier saw every request in the thread, once.
        ctx = classifier.calls[0]
        assert len(ctx.requests) == 2 and ctx.po_numbers == A + B
        assert stats.classified == 1 and stats.proposed == 1 and stats.needs_human == 1
        # Line one books A as asked, with its own pickup number.
        assert awaiting_approval(a)
        assert a.confirmed_local == "2026-09-28 09:00" and a.pickup_number == "20463264"
        # Line two pushes B to 10/02; it still makes the 10/06 delivery, so the agent accepts.
        assert awaiting_approval(b)
        assert b.confirmed_local == "2026-10-02 09:00" and b.pickup_number == "20463798"
        # One reply in, one message out: the acceptance carries the thanks, no separate ack.
        assert len(sender.drafts) == 2 and stats.responded == 1
        assert sender.drafts[-1].body.startswith("Yes, 10/02 @ 0900 works. Thank you!")
        assert sender.drafts[-1].in_reply_to == "<vera-1@outlook.com>"
        assert not [m for m in a.messages if m.kind == "acknowledge"]
        stored = next(m for m in a.messages if m.direction == "in")
        assert stored.classification["status"] == "confirmed"
        assert stored.classification["line_items"] == 2 and not stored.classification["issues"]


def test_a_reply_with_one_reading_applies_to_every_case_and_is_thanked_once(settings, sessions):
    settings, sender, (a_id, b_id), sent_id = _batched(settings, sessions)
    classifier = FakeReplyClassifier(
        lambda _c: ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_number="4119085",
            quotes=["All set!", "PU# 4119085"],
            confidence=0.9,
        )
    )
    with session_scope(sessions) as session:
        stats = ingest(
            session,
            [_reply("All set! PU# 4119085\n\nThanks\nVera", sent_id)],
            classifier,
            internal_domains=INTERNAL,
            responder=Responder(settings, sender, now=datetime(2026, 9, 24, 12, 40, tzinfo=UTC)),
        )
        session.commit()
        a, b = session.get(BookingCase, a_id), session.get(BookingCase, b_id)
        assert a is not None and b is not None
        assert stats.proposed == 2 and stats.responded == 1
        assert a.confirmed_local == "2026-09-28 09:00" and a.pickup_number == "4119085"
        assert b.confirmed_local == "2026-10-01 09:00" and b.pickup_number == "4119085"
        assert len(sender.drafts) == 2 and sender.drafts[-1].body.startswith("Thank you!")


def test_a_reply_naming_only_one_cases_pos_leaves_the_other_alone(settings, sessions):
    settings, sender, (a_id, b_id), sent_id = _batched(settings, sessions)
    only_a = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-09-28",
        pickup_time="09:00",
        pickup_number="20463264",
        quotes=["115829092660 & 115829092661-9/28 @ 9am pickup# 20463264"],
        items=[LINE_A],
    )
    with session_scope(sessions) as session:
        stats = ingest(
            session,
            [_reply(TWO_LINES.split("\n", maxsplit=1)[0] + "\n\nThanks\nVera", sent_id)],
            FakeReplyClassifier(lambda _c: only_a),
            internal_domains=INTERNAL,
            responder=Responder(settings, sender, now=REPLY_AT),
        )
        session.commit()
        a, b = session.get(BookingCase, a_id), session.get(BookingCase, b_id)
        assert a is not None and b is not None
        assert stats.proposed == 1 and stats.not_about_case == 1
        assert awaiting_approval(a) and a.pickup_number == "20463264"
        assert b.status == CaseStatus.PENDING.value and b.confirmed_local is None
        assert b.events[-1].action == "reply_not_about_this_po"
        skipped = next(m for m in b.messages if m.direction == "in")
        assert skipped.classification["skipped"] == "reply names other POs"


def test_line_items_are_validated_and_selected_per_case() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER,
        pickup_date="2026-10-02",
        pickup_time="09:00",
        quotes=["115802102660 & 115802102661-10/2 @ 9am pickup# 20463798"],
        items=[
            LINE_A,
            LINE_B,
            # A line about POs that are nowhere in the message: dropped.
            ReplyItem(po_numbers=["999999999999"], status=ReplyStatus.CONFIRMED, quotes=["9/28"]),
            # A line whose only quote is invented: its confirmation is not trusted.
            ReplyItem(
                po_numbers=A,
                status=ReplyStatus.CONFIRMED,
                pickup_date="2026-09-29",
                quotes=["confirmed for 9/29"],
            ),
        ],
    )
    kept, issues = validate_classification(reading, TWO_LINES)
    assert [i.po_numbers for i in kept.items] == [A, B, A]
    assert (
        kept.items[0].status == ReplyStatus.CONFIRMED and kept.items[0].pickup_number == "20463264"
    )
    assert kept.items[2].status == ReplyStatus.UNRELATED and kept.items[2].pickup_date is None
    names = [i.field_name for i in issues]
    assert "items[2].po_numbers" in names and "items[3].status" in names

    # Selection: first line naming the case's PO; a PO-less line as the default; else nothing.
    assert for_case(kept, [A[1]]) is not None and for_case(kept, [A[1]]).pickup_number == "20463264"
    assert for_case(kept, ["000000000000"]) is None
    with_default = kept.model_copy(
        update={
            "items": [
                LINE_B,
                ReplyItem(
                    po_numbers=[],
                    status=ReplyStatus.DEFERRED,
                    pickup_date="2026-10-05",
                    quotes=["Monday"],
                ),
            ]
        }
    )
    fallback = for_case(with_default, ["000000000000"])
    assert fallback is not None and fallback.status == ReplyStatus.DEFERRED
    # No lines at all: the whole reply is the reading for every case.
    plain = ReplyClassification(status=ReplyStatus.CONFIRMED, quotes=["SET!"])
    assert for_case(plain, ["anything"]) is plain
    # The prompt lists every request in the thread when there are several.
    from facility_profiles.booking.classify import render_user_message

    text = render_user_message(
        ReplyContext(
            vendor_name="Morgan Foods",
            po_numbers=A + B,
            requested_local="2026-09-28 09:00",
            reply_sent_at=REPLY_AT,
            subject="Re: Pick Up Appointments",
            body=TWO_LINES,
            requests=[(A, "2026-09-28 09:00"), (B, "2026-10-01 09:00")],
        )
    )
    assert "Our requests in this thread" in text and "PO 115802102660 & 115802102661" in text


def test_matching_returns_every_case_a_reply_belongs_to(settings, sessions):
    settings, _sender, (a_id, b_id), sent_id = _batched(settings, sessions)
    with session_scope(sessions) as session:
        by_reference = _reply("Both orders?", sent_id)
        assert [c.id for c in match_cases(session, by_reference)] == [a_id, b_id]
        assert match_case(session, by_reference).id == a_id
        by_thread = InboundMessage(
            **{
                **by_reference.__dict__,
                "in_reply_to": None,
                "references": None,
                "thread_id": "thread-1",
            }
        )
        assert [c.id for c in match_cases(session, by_thread)] == [a_id, b_id]
        by_po = InboundMessage(
            **{
                **by_reference.__dict__,
                "in_reply_to": None,
                "references": None,
                "subject": "Re: 115802102660",
                "body": "PO 115802102660 is not released",
            }
        )
        assert [c.id for c in match_cases(session, by_po)] == [b_id]
        nothing = InboundMessage(
            **{
                **by_reference.__dict__,
                "in_reply_to": None,
                "references": None,
                "subject": "hello",
                "body": "hi",
                "from_addr": "x@y.com",
            }
        )
        assert match_cases(session, nothing) == [] and match_case(session, nothing) is None
