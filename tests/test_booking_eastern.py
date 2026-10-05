"""Every time is Eastern: what people read, what facilities are sent, and how replies are read.

The store keeps instants in UTC and slots on the facility's own clock; everything shown or sent
is Eastern. A facility off Eastern time (Seneca in Ripon, WI is on Central) sees "ET" after each
time, and a time it gives without a zone that is not the one we asked for goes to a person.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.booking.classify import (
    ReplyContext,
    render_user_message,
    to_facility_clock,
    validate_classification,
    zone_doubt,
)
from facility_profiles.booking.links import create_offer
from facility_profiles.booking.mail import OutboundDraft, RecordingMailer, build_mime
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import answer_from_rules
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    compose_request,
    draft_case,
    ingest,
    list_cases,
    scan,
)
from facility_profiles.booking.timers import after_weekday_hours, fmt_slot
from facility_profiles.booking.worklist import open_kinds
from facility_profiles.booking.writeback import write_appointments
from facility_profiles.clock import (
    EASTERN,
    eastern_to_local,
    is_eastern,
    local_to_eastern,
    slot_text,
    stamp,
    zone_named,
)
from facility_profiles.config import Settings
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

PO = "115802102660"
CENTRAL = "America/Chicago"


# ------------------------------------------------------------------ the clock


def test_slots_on_a_facility_clock_are_shown_on_the_eastern_clock() -> None:
    assert is_eastern("America/New_York") and is_eastern("America/Indiana/Indianapolis")
    assert is_eastern(None) and not is_eastern(CENTRAL) and not is_eastern("America/Phoenix")
    assert local_to_eastern("2026-10-01 09:00", CENTRAL) == "2026-10-01 10:00"
    assert eastern_to_local("2026-10-01 10:00", CENTRAL) == "2026-10-01 09:00"
    assert local_to_eastern("2026-10-01", CENTRAL) == "2026-10-01"  # a day is a day
    assert local_to_eastern("2026-10-01 09:00", "America/New_York") == "2026-10-01 09:00"
    assert slot_text("2026-10-01 09:00", CENTRAL) == "Thu 10/01 10:00 ET"
    assert fmt_slot("2026-10-01") == "Thu 10/01" and fmt_slot(None) == "no date"
    assert stamp(datetime(2026, 10, 1, 13, 0, tzinfo=UTC)) == "10/01 09:00 ET"
    assert stamp(datetime(2026, 12, 1, 14, 0)) == "12/01 09:00 ET"  # naive is UTC; EST in winter


def test_a_zone_word_in_a_reply_names_a_zone() -> None:
    assert zone_named("CST", "America/New_York") == CENTRAL
    assert zone_named("central", None) == CENTRAL
    assert zone_named("ET", CENTRAL) == "America/New_York"
    assert zone_named("local", CENTRAL) == CENTRAL
    assert zone_named("MST", "America/Phoenix") == "America/Phoenix"  # Arizona's own MST
    assert zone_named("EST", "America/Indiana/Indianapolis") == "America/Indiana/Indianapolis"
    assert zone_named(None, CENTRAL) is None and zone_named("soon", CENTRAL) is None


def test_weekday_hours_skip_the_weekend() -> None:
    friday = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)  # Fri 10:00 ET
    assert after_weekday_hours(friday, 72, EASTERN) == datetime(2026, 10, 7, 14, 0, tzinfo=UTC)
    saturday = datetime(2026, 10, 3, 14, 0, tzinfo=UTC)
    assert after_weekday_hours(saturday, 1, EASTERN) == datetime(2026, 10, 5, 5, 0, tzinfo=UTC)


# ------------------------------------------------------------------ reading a reply


def _context(
    body: str, sent: datetime, tz: str | None, requested: str = "2026-10-01 09:00"
) -> ReplyContext:
    return ReplyContext(
        vendor_name="Seneca Foods",
        po_numbers=[PO],
        requested_local=requested,
        reply_sent_at=sent,
        subject="Re: Pick Up Appointment",
        body=body,
        timezone=tz,
    )


def test_the_model_is_told_the_day_the_facility_wrote_on_and_our_times_in_eastern() -> None:
    late = datetime(2026, 9, 30, 4, 30, tzinfo=UTC)  # Tue 23:30 in Chicago, Wed 00:30 ET
    prompt = render_user_message(_context("Tomorrow at 9am works", late, CENTRAL))
    assert "requested 2026-10-01 10:00 ET." in prompt
    assert (
        "The facility is on Central time (CT); our emails give times in Eastern Time (ET)."
        in prompt
    )
    assert (
        "Reply written: 2026-09-29 Tuesday 23:30 CT at the facility "
        "(2026-09-30 Wednesday 00:30 ET)" in prompt
    )
    east = render_user_message(_context("ok", late, "America/New_York"))
    assert "Reply written: 2026-09-30 Wednesday 00:30 ET (the facility's local time)" in east
    assert "Central" not in east


def test_times_are_kept_on_the_facility_clock_from_the_zone_the_reply_used() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED, pickup_date="2026-10-01", pickup_time="10:00"
    )
    # No zone named: our emails are Eastern, so 10:00 ET is 09:00 on a Central clock.
    assert to_facility_clock(reading, CENTRAL).pickup_time == "09:00"
    named = reading.model_copy(update={"time_zone": "CT"})
    assert to_facility_clock(named, CENTRAL).pickup_time == "10:00"
    # An Eastern facility that names Central: 10:00 CT is 11:00 on its clock.
    assert to_facility_clock(named, "America/New_York").pickup_time == "11:00"
    window = reading.model_copy(update={"pickup_time_end": "12:00"})
    moved = to_facility_clock(window, CENTRAL)
    assert (moved.pickup_time, moved.pickup_time_end) == ("09:00", "11:00")


def test_a_bare_time_from_a_central_desk_is_doubted_unless_it_is_ours_repeated() -> None:
    ours = ReplyClassification(
        status=ReplyStatus.CONFIRMED, pickup_date="2026-10-01", pickup_time="10:00"
    )
    on_their_clock = to_facility_clock(ours, CENTRAL)  # "1000 works": our 10:00 ET back
    assert zone_doubt(on_their_clock, timezone=CENTRAL, requested_local="2026-10-01 09:00") is None
    bare = to_facility_clock(ours.model_copy(update={"pickup_time": "09:00"}), CENTRAL)
    doubt = zone_doubt(bare, timezone=CENTRAL, requested_local="2026-10-01 09:00")
    assert doubt is not None and doubt.field_name == "time_zone"
    assert doubt.reason == (
        "the facility is on Central time and wrote 09:00 with no zone: 09:00 ET, or 09:00 CT "
        "(10:00 ET)?"
    )
    east = ours.model_copy(update={"pickup_time": "11:00"})
    assert zone_doubt(east, timezone="America/New_York", requested_local="2026-10-01 09:00") is None


def test_a_zone_the_reply_does_not_contain_is_dropped_and_a_reason_belongs_to_a_decline() -> None:
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="09:00",
        time_zone="CST",
        reject_reason=RejectReason.CLOSED,
        quotes=["10/1 at 9"],
    )
    result, issues = validate_classification(reading, "Confirmed 10/1 at 9")
    assert result.time_zone is None and result.reject_reason is None
    assert [i.field_name for i in issues] == ["time_zone"]
    declined = ReplyClassification(status=ReplyStatus.REJECTED, quotes=["cannot ship"])
    assert (
        validate_classification(declined, "We cannot ship")[0].reject_reason == RejectReason.OTHER
    )


# ------------------------------------------------------------------ a facility on Central time


def central_load(load_id: int) -> dict[str, Any]:
    load = lidl_load(load_id, po=PO)
    load["waypoints"][0]["location"]["ianaTimezone"] = CENTRAL
    return load


@pytest.fixture
def central(settings: Settings, sessions: sessionmaker[Session]) -> tuple[Settings, int]:
    on = settings.model_copy(
        update={"pilot_terminal_ids": [1089], "booking_po_date_floor_desks": []}
    )
    seed_vendor(sessions)
    scan(FakeTPro([central_load(7101)], {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        case = list_cases(s)[0]
        assert case.vendor_timezone == CENTRAL
        # Tendered 13:00 UTC: 08:00 at the facility, 09:00 ET.
        assert case.requested_local == "2026-10-01 08:00"
        return on, case.id


def test_a_central_facility_is_asked_in_eastern_with_the_zone_named(
    central: tuple[Settings, int], sessions: sessionmaker[Session]
) -> None:
    on, case_id = central
    mailer = RecordingMailer()
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        draft_case(s, case, mailer, on, now=NOW)
    assert "PO# 115802102660 on 10/01 @ 0900 ET" in mailer.drafts[0].body


def test_a_central_desks_bare_time_waits_for_a_person(
    central: tuple[Settings, int], sessions: sessionmaker[Session]
) -> None:
    from facility_profiles.booking.classify import FakeReplyClassifier
    from facility_profiles.booking.mail import InboundMessage
    from facility_profiles.booking.respond import Responder

    on, case_id = central
    mailer = RecordingMailer()
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        sent = draft_case(s, case, mailer, on, now=NOW)
        case.status = CaseStatus.PENDING.value  # a person sent the draft
        sent_id = "<req-1@circledelivers.com>"
        sent.rfc_message_id = sent_id
    message = InboundMessage(
        message_id="cr1",
        thread_id=None,
        sent_at=NOW + timedelta(hours=2),
        from_addr="Desk <cci@udfinc.com>",
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="Re: Pick Up Appointment",
        body="We can do 0800 that day.",
        in_reply_to=sent_id,
        rfc_message_id="<cr1@udfinc.example>",
    )
    reading = ReplyClassification(
        status=ReplyStatus.CONFIRMED,
        pickup_date="2026-10-01",
        pickup_time="08:00",
        quotes=["We can do 0800"],
    )
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: reading),
            internal_domains=["circledelivers.com"],
            responder=Responder(on, mailer, now=NOW + timedelta(hours=2)),
            settings=on,
        )
        assert open_kinds(case) == ["confirmation_review", "time_zone_unclear"]
        # We asked for 09:00 ET (08:00 their time); a bare 0800 is read as ours, 07:00 theirs.
        assert case.confirmed_local == "2026-10-01 07:00"
        assert (
            case.open_exceptions[0].description
            == "vendor confirmed Thu 10/01 08:00 ET; approve to accept"
        )
        assert case.open_exceptions[1].description == (
            "the facility is on Central time and wrote 08:00 with no zone: 08:00 ET, or 08:00 CT "
            "(09:00 ET)?"
        )
    assert [d.body for d in mailer.drafts[1:]] == []  # no thank-you for a time in doubt


def test_the_customer_desks_slot_is_read_in_eastern_for_a_central_vendor(
    central: tuple[Settings, int], sessions: sessionmaker[Session]
) -> None:
    from facility_profiles.booking.classify import FakeReplyClassifier
    from facility_profiles.booking.mail import InboundMessage

    on, case_id = central
    message = InboundMessage(
        message_id="desk1",
        thread_id=None,
        sent_at=NOW,
        from_addr="Lidl Inbound <inbound@lidl.us>",
        to_addr="lidl@circledelivers.com",
        cc_addr="",
        subject="Re: 115802102660",
        body="10/6 730AM - PYE_061026919",
    )
    with session_scope(sessions) as s:
        ingest(
            s,
            [message],
            FakeReplyClassifier(lambda _c: ReplyClassification(status=ReplyStatus.UNRELATED)),
            internal_domains=["circledelivers.com"],
            settings=on,
        )
        case = s.get(BookingCase, case_id)
        assert case is not None and case.delivery_ref == "PYE_061026919"
        # 7:30 at the RDC, which is on Eastern time: 11:30 UTC, whatever the vendor's zone.
        assert case.delivery_at_utc is not None
        assert case.delivery_at_utc.replace(tzinfo=UTC) == datetime(2026, 10, 6, 11, 30, tzinfo=UTC)


def test_a_time_a_person_gives_on_the_board_is_eastern(settings: Settings, tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from facility_profiles.api.app import create_app
    from facility_profiles.storage.db import init_db, make_engine, session_factory

    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    on = settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "booking_po_date_floor_desks": [],
            "database_url": url,
        }
    )
    sessions = session_factory(engine)
    seed_vendor(sessions)
    scan(FakeTPro([central_load(7102)], {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        case_id = list_cases(s)[0].id
    engine.dispose()
    app = create_app(on)
    app.state.clock = lambda: NOW
    with TestClient(app) as client:
        row = client.get(f"/api/booking/cases/{case_id}").json()
        assert row["requested_local"] == "2026-10-01 09:00"  # 08:00 on the facility's clock
        done = client.post(
            f"/api/booking/cases/{case_id}/booked",
            json={"by": "megan", "via": "phone", "date": "2026-10-01", "time": "10:00"},
        )
        assert done.status_code == 200, done.text
        assert done.json()["confirmed_local"] == "2026-10-01 10:00"  # shown in Eastern
    engine = make_engine(url)
    with session_scope(session_factory(engine)) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None and case.confirmed_local == "2026-10-01 09:00"  # Central clock
    engine.dispose()


# ------------------------------------------------------------------ what goes out


def test_every_email_asks_for_replies_to_the_customers_group(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    seed_vendor(sessions)
    scan(FakeTPro([lidl_load(7201, po=PO)], {}), sessions, on, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        draft = compose_request(list_cases(s)[0], on)
    assert draft.reply_to == "lidl@circledelivers.com"
    mime = build_mime(draft, "agent@circledelivers.com")
    assert mime["Reply-To"] == "lidl@circledelivers.com"
    plain = build_mime(
        OutboundDraft(to_addr="a@b.example", cc_addr="", subject="s", body="b"), "x@y.com"
    )
    assert plain["Reply-To"] is None


@pytest.mark.parametrize(
    ("question", "answer"),
    [
        ("What is the delivery appointment number for this PO?", "The delivery number is PYE_1."),
        ("Both orders?", "Just PO# 1158."),
        ("What PO is this for?", "Just PO# 1158."),
        ("Can you send the PO number?", "Just PO# 1158."),
        ("What time can the driver arrive for this PO?", None),
        ("What is the weight on this order?", None),
        ("Who is the carrier?", "The carrier is Circle Logistics, Inc."),
        ("What's the carrier's MC number?", None),
        ("What time will the carrier arrive?", None),
        ("Is the carrier bringing load bars?", None),
        ("Where is this delivering?", "This is delivering to Lidl RDC on 10/02 (PYE_1)."),
        ("Where should the driver drop the paperwork?", None),
        ("What's your load number?", "Our load number is 7001."),
        ("Who is this for?", "This is a Lidl order."),
        ("Please send the driver name and cell", None),
    ],
)
def test_canned_answers_answer_only_what_was_asked(question: str, answer: str | None) -> None:
    facts = {
        "po_numbers": ["1158"],
        "carrier": "Circle Logistics, Inc.",
        "customer": "Lidl",
        "delivery_site": "Lidl RDC",
        "delivery_date": "10/02",
        "delivery_ref": "PYE_1",
        "load_id": 7001,
    }
    draft = answer_from_rules(question, facts)
    assert (draft.message if draft else None) == answer


def test_a_link_sent_on_friday_lives_into_the_next_week(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    on = settings.model_copy(
        update={
            "pilot_terminal_ids": [1089],
            "booking_link_base_url": "https://book.circle.example",
            "booking_link_secret": "k",
        }
    )
    seed_vendor(sessions)
    load = lidl_load(7301, po=PO)
    for stop, (open_, close) in zip(
        load["waypoints"],
        [("2026-10-07T13:00:00Z", "2026-10-07T15:00:00Z"), ("2026-10-08T13:30:00Z",) * 2],
        strict=True,
    ):
        stop["appointmentTime"]["open"], stop["appointmentTime"]["close"] = open_, close
    friday = datetime(2026, 10, 2, 14, 0, tzinfo=UTC)
    scan(FakeTPro([load], {}), sessions, on, days_ahead=10, now=friday)  # type: ignore[arg-type]
    with session_scope(sessions) as s:
        offer = create_offer(s, list_cases(s)[0], on, None, now=friday)
        assert offer is not None and offer.expires_at.replace(tzinfo=UTC) == datetime(
            2026, 10, 7, 14, 0, tzinfo=UTC
        )  # Wed 10:00 ET: 72 weekday hours, not Monday morning


# ------------------------------------------------------------------ Transport Pro's note


def test_a_note_that_fails_is_retried_without_writing_the_time_again(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_booking_writeback import booked, on

    settings = on(settings)
    case_id, loads = booked(settings, sessions)
    loads.note_fails = 1
    with session_scope(sessions) as s:
        first = write_appointments(s, settings, loads, now=NOW)
        assert first.failed == 1 and len(loads.writes) == 1 and loads.notes == []
    later = NOW + timedelta(hours=2)
    with session_scope(sessions) as s:
        again = write_appointments(s, settings, loads, now=later)
        assert again.already == 1 and len(loads.writes) == 1 and len(loads.notes) == 1
        case = s.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == []
        assert [e.action for e in case.events].count("tpro_already_set") == 0
    with session_scope(sessions) as s:
        write_appointments(s, settings, loads, now=later + timedelta(hours=1))
    assert len(loads.notes) == 1  # once per booking


def test_writeback_note_is_eastern_and_names_the_pickup_number(
    settings: Settings, sessions
) -> None:  # type: ignore[no-untyped-def]
    from tests.test_booking_writeback import booked, on

    settings = on(settings)
    _case_id, loads = booked(settings, sessions)
    with session_scope(sessions) as s:
        write_appointments(s, settings, loads, now=NOW)
    [(_, note)] = loads.notes
    assert note.startswith(
        "Pickup appointment confirmed with Koch Foods, Inc.: Thu 10/01 10:00 ET."
    )
    assert note.endswith("Booked by the booking agent, case #1.")


def test_an_offer_with_no_day_is_shown_on_the_day_asked_in_eastern(
    central: tuple[Settings, int], sessions: sessionmaker[Session]
) -> None:
    from facility_profiles.booking.service import apply_reply

    _on, case_id = central
    offer = ReplyClassification(
        status=ReplyStatus.COUNTER_OFFER, pickup_time="07:00", pickup_time_end="09:00"
    )
    with session_scope(sessions) as s:
        case = s.get(BookingCase, case_id)
        assert case is not None
        case.status = CaseStatus.PENDING.value
        apply_reply(s, case, offer, [], reply_sent_at=NOW)
        # 07:00 to 09:00 on the Central clock, on the day asked for (Thu 10/01).
        assert (
            case.open_exceptions[0].description == "vendor offered Thu 10/01 08:00 ET to 10:00 ET"
        )
