"""Fill a demo store with made-up pickup appointments in every state, to show the board.

Every vendor, PO, address and email here is invented (``.example`` domains). The cases are built
the way the agent builds them: a request drafted and sent, a vendor reply read by a scripted
classifier and checked by the real validator, the conversation policy answering, a person
approving or booking by phone, the timers raising what time alone brings. Times are spread over
the last and next few business days so the overview, the list, the week view and the daily
summary all have something to show.

    python scripts/seed_booking_demo.py --db sqlite:///./data/booking-demo.db
    facility-profiles serve --db sqlite:///./data/booking-demo.db

Two cases carry click-to-confirm links signed with a demo-only key (one picked, one waiting for
the vendor). To open the waiting one's vendor page, serve the board with that key and address:
the seeder prints both and the link.

Every time the agent writes is Eastern; the vendors on Central time answer naming their zone,
except one, whose bare time a person has to check. One booked pickup is moved by its vendor,
and one email matches no pickup at all, for the board's "Emails no pickup matched".

The store must be new or empty: the demo is never mixed into a real store.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from pydantic import SecretStr
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.links import confirm, offer_url
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import (
    BookingCase,
    BookingEvent,
    BookingMessage,
    BookingReference,
    CaseException,
    DeskMemory,
    ExceptionType,
)
from facility_profiles.booking.references import record_load_numbers
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.rules import check_desk_rules, vendor_profile
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import (
    approve,
    close_case,
    draft_case,
    ingest,
    mark_booked,
    mark_sent,
    plan_request,
)
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.unmatched import keep_unmatched
from facility_profiles.booking.worklist import flag, method_exception
from facility_profiles.config import Settings
from facility_profiles.customers import customers
from facility_profiles.domain.schema import FacilityIdentity, FieldState, Role
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository

ET = ZoneInfo("America/New_York")
# Demo links: signed with a key that guards nothing, pointing at the board on this machine.
LINK_SECRET = "booking-demo-links-not-a-secret"  # noqa: S105 - a demo key that guards nothing
LINK_BASE = "http://127.0.0.1:8000"
CT = ZoneInfo("America/Chicago")
CUSTOMER = "Demo Grocer - Inbound"
DC = "Demo Grocer DC (Harrisburg, PA)"
INTERNAL = ["circledelivers.com"]


def demo_settings(db: str) -> Settings:
    """Settings for the demo only: no real desk, mailbox or signature is used."""
    return Settings(
        _env_file=None,  # type: ignore[call-arg]
        TPRO_BASE_URL="https://tpro.invalid",
        TPRO_USERNAME="demo",
        TPRO_PASSWORD="demo",  # noqa: S106 - a placeholder; the demo never calls Transport Pro
        database_url=db,
        booking_sender="booking-demo@example.com",
        booking_cc=["booking-demo@example.com"],
        booking_signature="Circle Logistics, Inc. (demo)",
        booking_customer_desk="inbound@demo-grocer.example",
        booking_shared_desks=[],
        booking_po_date_floor_desks=[],
    )


class Demo:
    """Builds the cases; ``now`` is when the demo pretends it is."""

    def __init__(self, session: Session, settings: Settings, now: datetime) -> None:
        self.session = session
        self.settings = settings
        self.now = now
        self.today = now.astimezone(ET).date()
        self.last_step: dict[int, datetime] = {}

    # -- time

    def day(self, offset: int) -> date:
        """The business day ``offset`` business days from today (negative: before)."""
        day, step, left = self.today, (1 if offset >= 0 else -1), abs(offset)
        while day.weekday() >= 5:
            day += timedelta(days=1)
        while left:
            day += timedelta(days=step)
            if day.weekday() < 5:
                left -= 1
        return day

    def at(self, offset: int, clock: str, tz: ZoneInfo = ET) -> datetime:
        """A moment on a business day, as an aware UTC datetime."""
        hour, minute = (int(x) for x in clock.split(":"))
        d = self.day(offset)
        return datetime(d.year, d.month, d.day, hour, minute, tzinfo=tz).astimezone(UTC)

    def ago(self, hours: float) -> datetime:
        """A moment ``hours`` before the demo's now."""
        return self.now - timedelta(hours=hours)

    def weekday_hours_ago(self, hours: float) -> datetime:
        """A moment ``hours`` Monday-to-Friday hours before now, the way the timers count."""
        at, left, step = self.now, hours, timedelta(minutes=15)
        while left > 0:
            at -= step
            if at.astimezone(ET).weekday() < 5:
                left -= 0.25
        return at

    @contextmanager
    def step(self, case: BookingCase, when: datetime, *, sent: bool = True) -> Iterator[None]:
        """Whatever the block writes happened at ``when`` (events, exceptions, messages).

        With ``sent``, a message the agent drafted in the block is treated as sent ten minutes
        later by the person on duty, so threads read the way they would in the mailbox.
        """
        s = self.session
        s.flush()
        last_event = s.scalar(select(func.max(BookingEvent.id))) or 0
        last_exc = s.scalar(select(func.max(CaseException.id))) or 0
        last_msg = s.scalar(select(func.max(BookingMessage.id))) or 0
        last_ref = s.scalar(select(func.max(BookingReference.id))) or 0
        open_before = {e.id for e in s.scalars(select(CaseException)) if e.resolved_at is None}
        yield
        s.flush()
        for event in s.scalars(select(BookingEvent).where(BookingEvent.id > last_event)):
            event.created_at = when
        for exc in s.scalars(select(CaseException).where(CaseException.id > last_exc)):
            exc.raised_at = when
            if exc.resolved_at is not None:
                exc.resolved_at = when
        for exc in s.scalars(select(CaseException).where(CaseException.id.in_(open_before))):
            if exc.resolved_at is not None:
                exc.resolved_at = when
        for message in s.scalars(select(BookingMessage).where(BookingMessage.id > last_msg)):
            message.created_at = when
            if sent and message.direction == "out" and message.sent_at is None:
                message.sent_at = when + timedelta(minutes=10)
                message.draft_ref = None
        for ref in s.scalars(select(BookingReference).where(BookingReference.id > last_ref)):
            ref.created_at = when
            if ref.replaced_at is not None:
                ref.replaced_at = when
        for memory in s.scalars(select(DeskMemory).where(DeskMemory.last_case_id == case.id)):
            memory.last_worked_at = when
            if memory.worked_count <= 1:
                memory.first_worked_at = when
        self.last_step[case.id] = max(when, self.last_step.get(case.id, when))
        s.flush()

    # -- building blocks

    def case(
        self,
        n: int,
        vendor: str,
        city: str,
        *,
        pickup: tuple[int, str],
        desk: str | None,
        method: str | None = "email",
        tz: ZoneInfo = ET,
        found: datetime | None = None,
    ) -> BookingCase:
        """A case as the scan opens it.

        It is found three business days before the pickup, and never later than three
        business days ago unless ``found`` says otherwise.
        """
        day = self.day(pickup[0])
        delivery_day = self.day(pickup[0] + 1)
        delivery = datetime(
            delivery_day.year, delivery_day.month, delivery_day.day, 7, 30, tzinfo=ET
        )
        po = f"77{n:02d}{day:%d%m%y}{n:02d}"
        case = BookingCase(
            load_id=2_700_000 + n,
            waypoint_index=0,
            customer_id=900,
            customer_name=CUSTOMER,
            facility_key=f"demo:{n}",
            vendor_name=vendor,
            vendor_city=city,
            vendor_timezone=tz.key,
            po_numbers=[po],
            booking_method=method,
            contact_email=desk,
            delivery_site=DC,
            delivery_ref=f"DG_{delivery_day:%d%m%y}{n:03d}",
            delivery_at_utc=delivery.astimezone(UTC),
            tendered_pickup_utc=self.at(pickup[0], pickup[1], tz),
            requested_local=f"{day:%Y-%m-%d} {pickup[1]}",
            miles=280,
        )
        when = found or self.at(min(pickup[0] - 3, -3), "07:05")
        with self.step(case, when):
            self.session.add(case)
            self.session.flush()
            case.created_at = when
            case.events.append(BookingEvent(action="scanned", detail={"status": case.status}))
            record_load_numbers(self.session, case)
        return case

    def send(self, case: BookingCase, when: datetime, settings: Settings | None = None) -> None:
        """The request drafted by the agent and sent by the person on duty."""
        with self.step(case, when, sent=False):
            draft_case(self.session, case, RecordingMailer(), settings or self.settings, now=when)
            mark_sent(
                self.session,
                case,
                by="Demo user",
                thread_id=f"demo-thread-{case.id}",
                rfc_message_id=f"<demo-request-{case.id}@booking-demo.example>",
                sent_at=when + timedelta(minutes=5),
            )

    def reply(
        self,
        case: BookingCase,
        when: datetime,
        body: str,
        reading: ReplyClassification,
        *,
        respond: bool = True,
    ) -> None:
        """A vendor reply, read by a scripted classifier and checked by the real validator."""
        message = InboundMessage(
            message_id=f"demo-reply-{case.id}-{int(when.timestamp())}",
            thread_id=f"demo-thread-{case.id}",
            sent_at=when,
            from_addr=f"Appointments desk <{case.contact_email}>",
            to_addr="Booking desk <booking-demo@example.com>",
            cc_addr="booking-demo@example.com",
            subject=f"Re: Pick Up Appointment: {case.po_numbers[0]}",
            body=body,
            rfc_message_id=f"<demo-reply-{case.id}-{int(when.timestamp())}@vendor.example>",
        )
        responder = Responder(self.settings, RecordingMailer(), now=when) if respond else None
        with self.step(case, when):
            ingest(
                self.session,
                [message],
                FakeReplyClassifier(lambda _ctx: reading),
                internal_domains=INTERNAL,
                responder=responder,
            )

    @staticmethod
    def mmdd(d: date) -> str:
        """A date the way desks write it: MM/DD."""
        return f"{d:%m/%d}"


def desk_profile(
    session: Session, candidate: str, name: str, *, desk: str, required_refs: list[str]
) -> str:
    """A vendor profile with an email desk and the numbers it needs, as a person files them."""
    repo = Repository(session)
    record = repo.upsert_facility(
        FacilityIdentity(candidate_key=candidate, company_name=name), latitude=None, longitude=None
    )
    for field, value in (
        ("booking_method", "email"),
        ("contact_email", desk),
        ("required_refs", required_refs),
    ):
        repo.set_field_human(record.key, Role.SHIPPER, field, value, state=FieldState.HUMAN_SET)
    return record.key


def linked(settings: Settings, base: str = LINK_BASE) -> Settings:
    """The demo settings with click-to-confirm on."""
    return settings.model_copy(
        update={"booking_link_base_url": base, "booking_link_secret": SecretStr(LINK_SECRET)}
    )


def build(demo: Demo) -> int:
    """Twenty-four cases, one per situation the board has to show."""
    s, d = demo.session, demo
    made = 0

    # Booked: confirmed by the vendor and approved by a person.
    c = d.case(
        1,
        "Harbor Beverage Co.",
        "Baltimore, MD",
        pickup=(1, "08:00"),
        desk="appointments@harborbev.example",
    )
    d.send(c, d.at(-2, "10:15"))
    text = f"Confirmed for {d.mmdd(d.day(1))} @ 0800. Pickup# 44710"
    d.reply(
        c,
        d.at(-1, "09:40"),
        text,
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(1)}",
            pickup_time="08:00",
            pickup_number="44710",
            quotes=[text],
            confidence=0.95,
        ),
    )
    with d.step(c, d.at(-1, "11:05")):
        approve(s, c, by="Demo user")
    made += 1

    # Confirmed by the vendor, waiting for a person's approval.
    c = d.case(
        2, "Ridgeline Snacks", "Hanover, PA", pickup=(2, "10:00"), desk="shipping@ridgeline.example"
    )
    d.send(c, d.at(-1, "14:00"))
    text = f"SET! {d.mmdd(d.day(2))} @ 1000 PU# 55120"
    d.reply(
        c,
        d.ago(2),
        text,
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(2)}",
            pickup_time="10:00",
            pickup_number="55120",
            quotes=[text],
            confidence=0.9,
        ),
    )
    made += 1

    # The vendor offered another time; the agent accepted it because it makes the delivery.
    c = d.case(
        3,
        "Cedar Mill Grains",
        "Ripon, WI",
        pickup=(3, "09:00"),
        desk="dock@cedarmill.example",
        tz=CT,
    )
    d.send(c, d.at(-1, "15:30"))
    offer = f"I have {d.mmdd(d.day(3))} at 1300 CT"
    d.reply(
        c,
        d.ago(5),
        f"We are full that morning. {offer}.",
        ReplyClassification(
            status=ReplyStatus.COUNTER_OFFER,
            pickup_date=f"{d.day(3)}",
            pickup_time="13:00",
            time_zone="CT",
            quotes=[offer],
            confidence=0.85,
        ),
    )
    made += 1

    # A money question the agent will not answer: handed to a person.
    c = d.case(
        4, "Bluewater Foods", "Erlanger, KY", pickup=(2, "07:00"), desk="cci@bluewater.example"
    )
    d.send(c, d.at(-1, "09:00"))
    question = "Who pays the lumper fee at our dock?"
    d.reply(
        c,
        d.ago(1),
        question,
        ReplyClassification(
            status=ReplyStatus.QUESTION, question=question, quotes=[question], confidence=0.9
        ),
    )
    made += 1

    # A factual question the agent answered from the load; waiting on the vendor again.
    c = d.case(
        5,
        "Granite State Foods",
        "Manchester, NH",
        pickup=(4, "11:00"),
        desk="appts@granitestate.example",
    )
    d.send(c, d.at(-2, "08:30"))
    question = "Which carrier is picking this up?"
    d.reply(
        c,
        d.ago(5),
        question,
        ReplyClassification(
            status=ReplyStatus.QUESTION, question=question, quotes=[question], confidence=0.9
        ),
    )
    made += 1

    # Sent this morning, no answer yet.
    c = d.case(
        6,
        "Summit Springs Water",
        "Fitzgerald, GA",
        pickup=(5, "09:00"),
        desk="orders@summitsprings.example",
    )
    d.send(c, d.ago(4))
    made += 1

    # The vendor asked to be asked again later.
    c = d.case(
        7,
        "Lakeshore Dairy",
        "Oshkosh, WI",
        pickup=(6, "08:00"),
        desk="loads@lakeshore.example",
        tz=CT,
    )
    d.send(c, d.at(-1, "13:00"))
    later = f"please check back on {d.mmdd(d.day(1))}"
    d.reply(
        c,
        d.ago(6),
        f"PO is not released yet, {later}.",
        ReplyClassification(
            status=ReplyStatus.DEFERRED, pickup_date=f"{d.day(1)}", quotes=[later], confidence=0.85
        ),
    )
    made += 1

    # The vendor cannot ship as asked; the agent drafted a note to the customer's desk.
    c = d.case(
        8,
        "Prairie Pasta Co.",
        "Lebanon, PA",
        pickup=(3, "14:00"),
        desk="shipping@prairiepasta.example",
    )
    d.send(c, d.at(-1, "10:00"))
    text = "We cannot ship this PO until next week, the order is not ready."
    d.reply(
        c,
        d.ago(3),
        text,
        ReplyClassification(
            status=ReplyStatus.REJECTED,
            reject_reason=RejectReason.NOT_READY,
            question=text,
            quotes=[text],
            confidence=0.9,
        ),
    )
    made += 1

    # No booking desk on the vendor's profile.
    c = d.case(9, "Oak Valley Produce", "Vineland, NJ", pickup=(4, "09:00"), desk=None, method=None)
    with d.step(c, c.created_at):
        kind, why = method_exception(None)
        flag(s, c, kind, why)
    made += 1

    # The vendor books on a portal, which the agent cannot use.
    c = d.case(
        10,
        "Northgate Paper",
        "Green Bay, WI",
        pickup=(5, "13:00"),
        desk=None,
        method="web_portal",
        tz=CT,
    )
    with d.step(c, c.created_at):
        kind, why = method_exception("web_portal")
        flag(s, c, kind, why, method="web_portal")
    made += 1

    # Drafted by the agent an hour ago; nobody has sent it yet.
    c = d.case(
        11,
        "Riverbend Bottling",
        "Harrisburg, PA",
        pickup=(6, "10:00"),
        desk="dispatch@riverbend.example",
        found=d.ago(26),
    )
    with d.step(c, d.ago(1), sent=False):
        draft_case(s, c, RecordingMailer(), d.settings, now=d.ago(1))
    made += 1

    # Found on a load, nothing asked yet.
    c = d.case(
        12,
        "Copper Ridge Canning",
        "Lancaster, PA",
        pickup=(8, "09:00"),
        desk="shipping@copperridge.example",
        found=d.ago(3),
    )
    made += 1

    # Asked days ago, never answered, and the pickup time has passed.
    c = d.case(
        13,
        "Elm Street Bakery",
        "Allentown, PA",
        pickup=(-1, "09:00"),
        desk="orders@elmstreet.example",
    )
    d.send(c, d.at(-4, "10:00"))
    made += 1

    # Booked by phone by a person; the board records it.
    c = d.case(
        14, "Maple Leaf Imports", "Buffalo, NY", pickup=(3, "12:00"), desk="appts@mapleleaf.example"
    )
    with d.step(c, d.at(-1, "15:20")):
        day = d.day(3)
        mark_booked(
            s,
            c,
            by="Demo user",
            via="phone",
            local=f"{day:%Y-%m-%d} 12:00",
            pickup_number="TEL20811",
            note="no email slots left, booked with the desk",
            desk="716-555-0144",  # a fictional 555 number
        )
    made += 1

    # No longer needed.
    c = d.case(
        15, "Willow Creek Farms", "York, PA", pickup=(2, "09:00"), desk="loads@willowcreek.example"
    )
    d.send(c, d.at(-2, "09:30"))
    with d.step(c, d.at(-1, "12:00")):
        close_case(s, c, by="Demo user", reason="load canceled by the customer")
    made += 1

    # A "confirmation" of yesterday's slot, written today: a work-in note for a person.
    c = d.case(
        16, "Bayside Seafood", "Salisbury, MD", pickup=(-1, "09:00"), desk="dock@bayside.example"
    )
    d.send(c, d.at(-3, "11:00"))
    text = f"Confirmed {d.mmdd(d.day(-1))} @ 0900, latest is 9pm tonight"
    d.reply(
        c,
        d.ago(2),
        text,
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(-1)}",
            pickup_time="09:00",
            quotes=[text],
            confidence=0.6,
        ),
    )
    made += 1

    # The slot to ask for had already passed when the load was found.
    c = d.case(
        17,
        "Sunrise Citrus",
        "Lakeland, FL",
        pickup=(-1, "06:00"),
        desk="appointments@sunrise.example",
        found=d.ago(1),
    )
    with d.step(c, d.ago(1)):
        flag(
            s,
            c,
            ExceptionType.SLOT_UNWORKABLE,
            f"requested slot {c.requested_local} has already passed",
            requested=c.requested_local,
        )
    made += 1

    # Sent the day before, no answer for 24 weekday hours: the timer raises it.
    c = d.case(
        18,
        "Keystone Pretzel Co.",
        "Lititz, PA",
        pickup=(3, "08:00"),
        desk="shipping@keystonepretzel.example",
    )
    d.send(c, d.weekday_hours_ago(30))
    made += 1

    # Sent two business days ago, still no answer: past 48 weekday hours.
    c = d.case(
        19,
        "Delta Rice Mills",
        "Stuttgart, AR",
        pickup=(4, "09:00"),
        desk="orders@deltarice.example",
        tz=CT,
        found=d.weekday_hours_ago(60),
    )
    d.send(c, d.weekday_hours_ago(56))
    made += 1

    # The vendor confirmed, but a day later than asked: raised with the confirmation.
    c = d.case(
        20,
        "Harvest Moon Oats",
        "Cedar Rapids, IA",
        pickup=(2, "08:00"),
        desk="loading@harvestmoon.example",
        tz=CT,
    )
    d.send(c, d.at(-1, "11:00"))
    text = f"We can load you {d.mmdd(d.day(3))} @ 1300 CT. PU# 66210"
    d.reply(
        c,
        d.ago(3),
        text,
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(3)}",
            pickup_time="13:00",
            pickup_number="66210",
            time_zone="CT",
            quotes=[text],
            confidence=0.9,
        ),
    )
    made += 1

    # The desk will not book without the customer's shipment number, which the load lacks.
    c = d.case(
        21,
        "Prairie Gold Mills",
        "Salina, KS",
        pickup=(4, "07:00"),
        desk="appointments@prairiegold.example",
        tz=CT,
    )
    key = desk_profile(
        s,
        "demo21prairiegold",
        "Prairie Gold Mills",
        desk="appointments@prairiegold.example",
        required_refs=["shipment_number"],
    )
    with d.step(c, c.created_at):
        c.facility_key = key
        check_desk_rules(
            s, c, d.settings, now=c.created_at, profile=vendor_profile(Repository(s), key)
        )
    made += 1

    # Click-to-confirm: the request carried one-click times and the vendor picked one.
    with_links = linked(d.settings)
    c = d.case(
        22,
        "Pinecrest Bakery",
        "Lancaster, PA",
        pickup=(2, "09:00"),
        desk="shipping@pinecrest.example",
    )
    d.send(c, d.at(-1, "10:00"), with_links)
    with d.step(c, d.ago(3)):
        confirm(
            s,
            c.offers[-1],
            2,
            settings=with_links,
            now=d.ago(3),
            pickup_number="PB-3301",
            name="Dock office",
        )
    made += 1

    # A request with one-click times, still waiting for the vendor's click.
    c = d.case(
        23,
        "Orchard Valley Juice",
        "Hagerstown, MD",
        pickup=(3, "13:00"),
        desk="appointments@orchardvalley.example",
    )
    d.send(c, d.ago(2), with_links)
    made += 1

    # A long haul no pickup time can get to the customer's dock in time.
    c = d.case(
        24,
        "Coastal Citrus Packers",
        "Vero Beach, FL",
        pickup=(2, "09:00"),
        desk="loads@coastalcitrus.example",
    )
    with d.step(c, c.created_at):
        c.miles = 1450
        plan_request(s, c, d.settings, now=c.created_at)
    made += 1

    # A Central-time desk answers with a bare time: their 09:00, or ours? A person checks.
    c = d.case(
        25,
        "Lone Pine Cheese",
        "Plymouth, WI",
        pickup=(3, "09:00"),
        desk="shipping@lonepine.example",
        tz=CT,
    )
    d.send(c, d.at(-1, "14:00"))
    d.reply(
        c,
        d.ago(4),
        "We can do 0900 that day.",
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(3)}",
            pickup_time="09:00",
            quotes=["We can do 0900"],
            confidence=0.8,
        ),
    )
    made += 1

    # A booked pickup the vendor moves: the agent never moves a booking itself.
    c = d.case(
        26,
        "Blue Ridge Mills",
        "Roanoke, VA",
        pickup=(4, "10:00"),
        desk="appointments@blueridgemills.example",
    )
    d.send(c, d.at(-2, "09:00"))
    confirmed = f"Confirmed {d.mmdd(d.day(4))} @ 1000. PU# 55120"
    d.reply(
        c,
        d.ago(30),
        confirmed,
        ReplyClassification(
            status=ReplyStatus.CONFIRMED,
            pickup_date=f"{d.day(4)}",
            pickup_time="10:00",
            pickup_number="55120",
            quotes=[confirmed],
            confidence=0.95,
        ),
    )
    with d.step(c, d.ago(29)):
        approve(s, c, by="Demo user")
    moved = f"move your pickup on {d.mmdd(d.day(4))} to 1400"
    d.reply(
        c,
        d.ago(2),
        f"We need to {moved} because of a line issue. Please confirm.",
        ReplyClassification(
            status=ReplyStatus.COUNTER_OFFER,
            pickup_date=f"{d.day(4)}",
            pickup_time="14:00",
            quotes=[moved],
            confidence=0.9,
        ),
    )
    made += 1

    # An email no pickup matches: a new thread, another address, no PO. Kept for a person.
    stray = InboundMessage(
        message_id="demo-stray-1",
        thread_id="demo-thread-stray",
        sent_at=d.ago(1),
        from_addr="Ridgeline Dispatch <dispatch@ridgeline-trucking.example>",
        to_addr="Booking desk <booking-demo@example.com>",
        cc_addr="",
        subject="Pickup appointment tomorrow?",
        body="Is your truck still coming at 9 tomorrow? We have not heard back from you.",
        rfc_message_id="<demo-stray-1@vendor.example>",
    )
    keep_unmatched(s, stray, customers(d.settings))

    # The timers run as they would on the server: what went silent, what slipped.
    sweep(s, now=d.now, settings=d.settings)

    # Each case's last change is when its last step happened, not when the demo was built.
    for case_id, when in demo.last_step.items():
        s.execute(update(BookingCase).where(BookingCase.id == case_id).values(updated_at=when))
    return made


def main() -> int:
    """Seed the demo store named by --db."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--db", default="sqlite:///./data/booking-demo.db", help="demo store URL")
    args = parser.parse_args()
    settings = demo_settings(args.db)
    engine = make_engine(args.db)
    init_db(engine)
    sessions = session_factory(engine)
    with session_scope(sessions) as session:
        existing = session.scalar(select(func.count()).select_from(BookingCase)) or 0
        if existing:
            print(f"{args.db} already has {existing} cases; the demo only goes into an empty store")
            return 1
        made = build(Demo(session, settings, datetime.now(tz=UTC)))
        waiting = session.scalar(select(BookingCase).where(BookingCase.load_id == 2_700_023))
        link = (
            offer_url(waiting.offers[-1], linked(settings)) if waiting and waiting.offers else None
        )
    engine.dispose()
    print(f"{made} demo cases in {args.db}")
    print(f"run: facility-profiles serve --db {args.db}")
    if link:
        print(
            "the vendor's link page needs the demo key at serve time: set "
            f"FP_BOOKING_LINK_SECRET={LINK_SECRET} and FP_BOOKING_LINK_BASE_URL={LINK_BASE}"
        )
        print(f"waiting link (Orchard Valley Juice): {link}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
