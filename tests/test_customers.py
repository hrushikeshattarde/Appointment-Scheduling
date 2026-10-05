"""Customer files: Lidl's specifics in one file, and a second customer added without code."""

from __future__ import annotations

import tomllib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from facility_profiles.booking.classify import FakeReplyClassifier
from facility_profiles.booking.mail import InboundMessage, RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.schema import RejectReason, ReplyClassification, ReplyStatus
from facility_profiles.booking.service import draft_batch, ingest, list_cases, mark_sent, scan
from facility_profiles.booking.templates import TemplateKind, pick, save_template
from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.customers import (
    CustomerFileError,
    built_in_customers,
    customer_of,
    customers,
    load_customer,
    load_customers,
    parse_customer,
    scope,
)
from facility_profiles.customers.starter import starter_text
from facility_profiles.mailarchive import filters
from facility_profiles.storage.db import session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

# An invented second customer: its own group, desk, numbers and archive rules, nothing real.
NORTHLINE = r"""
name = "Northline Grocers"
description = "An invented second customer"
timezone = "America/Chicago"

[transport_pro]
customer_ids = [4242]
customer_names = ["Northline Grocers - Inbound"]
terminal_ids = [1160]

[mail]
group = "northline@circle.example"
signature = "Circle Logistics, Inc. | northline@circle.example"

[customer_desk]
email = "Inbound@Northline.example"
delivery_system = "DockBook"

[numbers]
po = '\d{10}'
delivery_ref = 'NG-\d{6}'

[mail_archive]
keep_subjects = { northline-pickups = '\bnorthline\b.{0,24}\bpick\s*-?\s*ups?\b' }
drop_subjects = ['^\W*weekly capacity\b']
desks = ["shipping@vendor.example"]
"""


@pytest.fixture
def two(settings: Settings, tmp_path: Path) -> Settings:
    """Settings that know Lidl (built in) and Northline (from FP_CUSTOMERS_DIR)."""
    folder = tmp_path / "customers"
    folder.mkdir()
    (folder / "northline.toml").write_text(NORTHLINE, encoding="utf-8")
    return settings.model_copy(update={"customers_dir": str(folder), "pilot_terminal_ids": [1089]})


def northline_load(load_id: int, *, po: str) -> dict[str, Any]:
    """The same Koch Foods pickup as the Lidl loads, billed to the second customer."""
    load = lidl_load(load_id, po=po)
    load["billingInfo"] = {
        "customerId": 4242,
        "customer": {"id": 4242, "companyName": "Northline Grocers - Inbound"},
    }
    load["waypoints"][1]["notes"] = "Dock appointment NG-123456, lumper paid by receiver"
    return load


def acme_load(load_id: int, *, po: str) -> dict[str, Any]:
    """A pickup for a customer no file claims."""
    load = lidl_load(load_id, po=po)
    load["billingInfo"] = {
        "customerId": 5,
        "customer": {"id": 5, "companyName": "Acme Beverages - Inbound"},
    }
    return load


# ------------------------------------------------------------------ the files


def test_lidl_file_carries_what_the_code_used_to_hard_code(settings: Settings) -> None:
    known = customers(settings)
    lidl = known.get("lidl")
    assert lidl.name == "Lidl" and lidl.tpro_customer_ids == (7211, 6680)
    assert lidl.terminal_ids == (1089,)
    assert lidl.group == lidl.sender == "lidl@circledelivers.com"
    assert lidl.cc == ("lidl@circledelivers.com",)
    assert lidl.signature and lidl.signature.endswith("lidl@circledelivers.com")
    assert lidl.customer_desk == "inbound@lidl.us" and lidl.delivery_system == "DCT"
    assert lidl.po_embedded_date("115802102660", near=date(2026, 10, 1)) == date(2026, 10, 2)
    assert lidl.find_delivery_ref("DELIVERY# PYE_021026123") == "PYE_021026123"
    assert "shipping.appointments@morganfoods.com" in lidl.archive_desks
    # Nothing of Lidl's reaches a customer that has no file.
    fallback = known.fallback
    assert fallback.cc == () and fallback.sender is None and fallback.customer_desk is None
    assert fallback.signature == "Circle Logistics, Inc. | Fort Wayne | 260-208-4500"
    assert fallback.label("Acme Beverages - Inbound") == "Acme Beverages"
    assert fallback.label(None) == "the customer"
    assert fallback.find_delivery_ref("PYE_021026123") is None
    assert fallback.po_embedded_date("115802102660", near=date(2026, 10, 1)) is None


def test_a_file_with_mistakes_lists_every_one(tmp_path: Path) -> None:
    bad = tmp_path / "Bad Name.toml"
    bad.write_text(
        r"""
timezone = "Mars/Base"
colour = "blue"

[mailbox]
group = "x@y.z"

[transport_pro]
customer_ids = ["7211"]
booking_customer_ids = [8]

[mail]
group = "not-an-address"
cc = ["ok@circle.example", ""]

[numbers]
po = '('
po_date = '\d{6}'
delivery_ref = '(?P<ref>X)'

[mail_archive]
keep_subjects = { "Has Spaces" = 'x' }
desk_domains = ["not a domain"]
""",
        encoding="utf-8",
    )
    with pytest.raises(CustomerFileError) as caught:
        load_customer(bad)
    text = str(caught.value)
    for expected in (
        "not a usable key",
        "unknown table 'mailbox'",
        "unknown setting 'colour'",
        "name is required",
        "'Mars/Base' is not an IANA time zone",
        "customer_ids must be a list of whole numbers",
        "needs customer_ids",
        "booking_customer_ids must be among customer_ids",
        "'not-an-address' is not an email address",
        "mail.cc must be a list of non-empty strings",
        "numbers.po is not a valid regular expression",
        "po_date needs the named groups dd, mm and yy",
        "numbers.delivery_ref may not name groups (ref)",
        "keep_subjects name 'Has Spaces'",
        "'not a domain' is not a domain",
    ):
        assert expected in text, expected
    broken = tmp_path / "broken.toml"
    broken.write_text("name = ", encoding="utf-8")
    with pytest.raises(CustomerFileError, match="not valid TOML"):
        load_customer(broken)


def test_two_files_cannot_claim_one_customer_and_a_folder_replaces_a_built_in(
    tmp_path: Path,
) -> None:
    clash = tmp_path / "clash"
    clash.mkdir()
    (clash / "lidl-us.toml").write_text(
        'name = "Lidl US"\n[transport_pro]\ncustomer_ids = [7211]\n', encoding="utf-8"
    )
    with pytest.raises(CustomerFileError, match="customer id 7211 is claimed by both"):
        load_customers(clash)

    override = tmp_path / "override"
    override.mkdir()
    (override / "lidl.toml").write_text(
        'name = "Lidl"\n[transport_pro]\ncustomer_ids = [7211]\n[mail]\n'
        'group = "lidl@circledelivers.com"\ncc = []\n',
        encoding="utf-8",
    )
    (override / "_draft.toml").write_text("not even toml =", encoding="utf-8")  # skipped
    [lidl] = load_customers(override)
    assert lidl.source.endswith("lidl.toml") and str(override) in lidl.source
    assert lidl.cc == () and lidl.customer_desk is None
    with pytest.raises(CustomerFileError, match="is not a folder"):
        load_customers(tmp_path / "missing")


def test_loads_and_cases_find_their_customer(two: Settings) -> None:
    known = customers(two)
    assert known.keys() == ["lidl", "northline"]
    assert known.for_customer(4242, None).key == "northline"
    assert known.for_customer(None, "  northline grocers -  INBOUND").key == "northline"
    assert known.for_customer(6680, "Lidl-Outbound").key == "lidl"
    assert known.for_customer(5, "Acme Beverages").is_fallback
    assert known.customer_desks() == {"inbound@lidl.us", "inbound@northline.example"}
    case = BookingCase(customer_id=None, customer_name="Lidl - Inbound")
    assert customer_of(case, two).key == "lidl"
    with pytest.raises(CustomerFileError, match=r"no customer file 'costco' \(known: lidl"):
        known.get("costco")
    with pytest.raises(CustomerFileError, match="say which customer"):
        known.only_or(None)
    assert built_in_customers().only_or(None).key == "lidl"


def test_scope_turns_customer_keys_into_ids_and_pods(two: Settings) -> None:
    two = two.model_copy(update={"pilot_terminal_ids": [1089], "pilot_customer_ids": [9]})
    assert scope(two, ["lidl"]) == ([1089], [7211, 6680])
    # A booking scan covers only the records the agent books for: Lidl's inbound loads.
    assert scope(two, ["lidl"], booking=True) == ([1089], [7211])
    assert scope(two, ["northline"], booking=True) == ([1160], [4242])
    assert scope(two, ["Lidl", "northline"]) == ([1089, 1160], [7211, 6680, 4242])
    assert scope(two, ["lidl"], [99]) == ([99], [7211, 6680])
    assert scope(two, ["4242"]) == ([1089], [4242])
    assert scope(two) == ([1089], [9])  # nothing chosen: the pilot settings, as before
    assert scope(two.model_copy(update={"customers": ["northline"]})) == ([1160], [4242])
    with pytest.raises(CustomerFileError, match="no customer file 'nobody'"):
        scope(two, ["nobody"])


# ------------------------------------------------------------------ booking with two customers


def test_a_second_customer_books_from_its_own_file_beside_lidl(two: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)  # Koch Foods, booked at the CCI desk, which serves several shippers
    client = FakeTPro(
        [lidl_load(2001, po="226321092660"), northline_load(3001, po="4400123456")], {}
    )
    stats = scan(client, sessions, two, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    assert stats.created == 2
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        lidl_case, north_case = sorted(list_cases(session), key=lambda c: c.load_id)
        assert lidl_case.delivery_ref == "PYE_021026123"
        assert north_case.delivery_ref == "NG-123456"
        # One desk, two customers: one email each, never one batch copied to Lidl's group.
        messages = draft_batch(session, [lidl_case, north_case], mailer, two, now=NOW)
        assert len(messages) == 2 and len(mailer.drafts) == 2
    lidl_draft, north_draft = mailer.drafts
    assert lidl_draft.cc_addr == "lidl@circledelivers.com"
    assert lidl_draft.from_addr == "lidl@circledelivers.com"
    assert "going to Lidl?" in lidl_draft.body
    assert north_draft.to_addr == "cci@udfinc.com"
    assert north_draft.cc_addr == north_draft.from_addr == "northline@circle.example"
    assert "for Koch Foods going to Northline Grocers?" in north_draft.body
    assert "PO# 4400123456 on 10/01 @ 0900" in north_draft.body
    assert north_draft.body.endswith("Circle Logistics, Inc. | northline@circle.example")
    assert "lidl" not in f"{north_draft.cc_addr} {north_draft.body}".lower()


def test_each_customer_desk_moves_only_its_own_deliveries(two: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    scan(
        FakeTPro([northline_load(3001, po="4400123456")], {}), sessions, two, days_ahead=7, now=NOW
    )  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        draft_batch(session, [case], mailer, two, now=NOW)
        mark_sent(session, case, by="pod", thread_id="t-north")
        case_id = case.id

    def desk_mail(sender: str, mid: str, body: str) -> InboundMessage:
        return InboundMessage(
            message_id=mid,
            thread_id=f"desk-{mid}",
            sent_at=datetime(2026, 9, 29, 14, 0, tzinfo=UTC),
            from_addr=sender,
            to_addr="northline@circle.example",
            cc_addr="",
            subject="4400123456 RESCHEDULE",
            body=body,
        )

    unrelated = FakeReplyClassifier(lambda _ctx: ReplyClassification(status=ReplyStatus.UNRELATED))
    responder = Responder(two, mailer, now=NOW)
    with session_scope(sessions) as session:
        # Lidl's desk is not this customer's desk: its mail is a reply like any other.
        stats = ingest(
            session,
            [desk_mail("inbound@lidl.us", "d1", "PO 4400123456: 10/6 0700 - NG-778899")],
            unrelated,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        assert stats.delivery_updates == 0 and stats.classified == 1
        assert session.get(BookingCase, case_id).delivery_ref == "NG-123456"  # type: ignore[union-attr]
        # Its own desk moves the delivery, read with its own reference format.
        stats = ingest(
            session,
            [
                desk_mail(
                    "Inbound <inbound@northline.example>",
                    "d2",
                    "PO 4400123456: 10/6 0700 - NG-778899",
                )
            ],
            unrelated,
            internal_domains=["circledelivers.com"],
            responder=responder,
        )
        case = session.get(BookingCase, case_id)
        assert case is not None and stats.delivery_updates == 1
        assert case.delivery_ref == "NG-778899"
        assert (
            case.status == CaseStatus.PENDING.value and case.requested_local == "2026-10-02 09:00"
        )
    # The re-request went to the vendor in its thread, copied to this customer's group.
    again = mailer.drafts[-1]
    assert again.to_addr == "cci@udfinc.com" and again.thread_id == "t-north"
    assert again.cc_addr == "northline@circle.example"
    assert "Can we please reschedule PO# 4400123456 on 10/02 @ 0900?" in again.body


def test_a_vendor_that_cannot_ship_goes_to_the_case_customers_desk(two: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    loads = [northline_load(3001, po="4400123456"), acme_load(3002, po="5500123456")]
    scan(FakeTPro(loads, {}), sessions, two, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        for case in list_cases(session):
            draft_batch(session, [case], mailer, two, now=NOW)
            mark_sent(session, case, by="pod", thread_id=f"t-{case.load_id}")

    def cannot(load_id: int, po: str) -> InboundMessage:
        return InboundMessage(
            message_id=f"no-{load_id}",
            thread_id=f"t-{load_id}",
            sent_at=datetime(2026, 9, 29, 15, 0, tzinfo=UTC),
            from_addr="CCI <cci@udfinc.com>",
            to_addr="pod@circle.example",
            cc_addr="",
            subject=f"Re: Pick Up Appointment: {po}",
            body="The PO will not be ready until 10/07, sorry.",
        )

    rejected = FakeReplyClassifier(
        lambda _ctx: ReplyClassification(
            status=ReplyStatus.REJECTED,
            reject_reason=RejectReason.NOT_READY,
            question="PO will not be ready until 10/07",
        )
    )
    with session_scope(sessions) as session:
        ingest(
            session,
            [cannot(3001, "4400123456"), cannot(3002, "5500123456")],
            rejected,
            internal_domains=["circledelivers.com"],
            responder=Responder(two, mailer, now=NOW),
        )
        acme = next(c for c in list_cases(session) if c.load_id == 3002)
        # No file, so no desk to write to: a person takes it, and nothing goes to Lidl.
        assert "no customer desk set for Acme Beverages" in acme.open_exceptions[-1].description
    note = mailer.drafts[-1]
    assert note.to_addr == "inbound@northline.example"
    assert note.cc_addr == "northline@circle.example"
    assert note.body.endswith("Circle Logistics, Inc. | northline@circle.example")
    assert not any(d.to_addr == "inbound@lidl.us" for d in mailer.drafts)


def test_a_customer_without_a_file_gets_no_lidl_mailbox(settings: Settings, sessions) -> None:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    settings = settings.model_copy(update={"pilot_terminal_ids": [1089]})
    scan(
        FakeTPro([acme_load(3002, po="5500123456")], {}), sessions, settings, days_ahead=7, now=NOW
    )  # type: ignore[arg-type]
    mailer = RecordingMailer()
    with session_scope(sessions) as session:
        case = list_cases(session)[0]
        assert case.delivery_ref is None  # nobody said what Acme's references look like
        draft_batch(session, [case], mailer, settings, now=NOW)
    draft = mailer.drafts[0]
    assert draft.cc_addr == "" and draft.from_addr is None
    assert "going to Acme Beverages?" in draft.body
    assert draft.body.endswith("Circle Logistics, Inc. | Fort Wayne | 260-208-4500")
    assert "lidl" not in draft.body.lower()


def test_a_customer_template_saved_under_the_key_covers_both_sides(settings, session) -> None:  # type: ignore[no-untyped-def]
    lidl = customers(settings).get("lidl")
    assert lidl.template_matches("Lidl-Outbound") == ["lidl", "Lidl", "Lidl-Outbound"]
    save_template(
        session,
        TemplateKind.FOLLOW_UP,
        body="Hello,\n\nAny news?\n\n{signature}",
        by="pod",
        customer="lidl",
    )
    for side in ("Lidl - Inbound", "Lidl-Outbound"):
        chosen = pick(
            session, TemplateKind.FOLLOW_UP, desk=None, customer=lidl.template_matches(side)
        )
        assert chosen.source == "customer lidl"
    # A template saved under a Transport Pro name before customer files existed still applies,
    # and one saved under the key comes first.
    body = "Good Morning,\n\nAny news on this?\n\n{signature}"
    save_template(session, TemplateKind.CHECK_BACK, body=body, by="pod", customer="Lidl - Inbound")
    names = lidl.template_matches("Lidl - Inbound")
    assert pick(session, TemplateKind.CHECK_BACK, desk=None, customer=names).source == (
        "customer Lidl - Inbound"
    )
    save_template(session, TemplateKind.CHECK_BACK, body=body, by="pod", customer="lidl")
    assert pick(session, TemplateKind.CHECK_BACK, desk=None, customer=names).source == (
        "customer lidl"
    )


# ------------------------------------------------------------------ the mail archive


def test_archive_rules_come_from_each_customers_file(two: Settings) -> None:
    known = customers(two)
    north = filters.rules_for(known.get("northline"))
    lidl = filters.rules_for(known.get("lidl"))
    assert north.group == "northline@circle.example"
    assert filters.match_reason("4400123456 & 4400123457", set(), north) == "subject:po-numbers"
    assert filters.match_reason("Re: Northline Pick Ups", set(), north) == (
        "subject:northline-pickups"
    )
    assert filters.match_reason("Weekly Capacity 10/1", {"shipping@vendor.example"}, north) is None
    assert filters.match_reason("Re: x", {"inbound@northline.example"}, north) == (
        "desk:inbound@northline.example"
    )
    # Each customer's PO shape is its own.
    assert filters.match_reason("115802102660 & 115802102661", set(), north) is None
    assert filters.match_reason("4400123456 & 4400123457", set(), lidl) is None
    found = filters.identifiers("PO 4400123456 dock NG-778899", rules=north)
    assert found["po_numbers"] == ["4400123456"] and found["delivery_refs"] == ["NG-778899"]


# ------------------------------------------------------------------ the CLI


@pytest.fixture
def cli_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    folder = tmp_path / "customers"
    folder.mkdir()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.setenv("FP_CUSTOMERS_DIR", str(folder))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield folder
    get_settings.cache_clear()


def test_customers_new_show_and_list(cli_env: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["customers", "new", "acme", "--name", "Acme"])
    assert result.exit_code == 0, result.output
    assert "still to fill in" in result.output and "needs customer_ids" in result.output
    assert runner.invoke(app, ["customers", "new", "acme", "--name", "Acme"]).exit_code == 1
    (cli_env / "acme.toml").unlink()

    result = runner.invoke(
        app,
        [
            "customers", "new", "acme", "--name", "Acme", "--tpro-customer", "4242",
            "--tpro-name", "Acme - Inbound", "--terminal", "1160",
            "--group", "acme@circle.example", "--desk", "inbound@acme.example",
        ],
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "it reads cleanly" in result.output
    acme = load_customer(cli_env / "acme.toml")
    assert acme.tpro_customer_ids == (4242,) and acme.tpro_customer_names == ("Acme - Inbound",)
    assert acme.group == "acme@circle.example" and acme.customer_desk == "inbound@acme.example"
    assert acme.signature and acme.signature.endswith("acme@circle.example")

    result = runner.invoke(app, ["customers", "list"])
    assert result.exit_code == 0
    assert "acme" in result.output and "lidl" in result.output and "(no file)" in result.output

    result = runner.invoke(app, ["customers", "show", "acme"])
    assert result.exit_code == 0 and "not set: [numbers] delivery_ref" in result.output

    # `show` reads a PO date within two months of today, so the sample PO carries today's date.
    today = date.today()
    po = f"1158{today:%d%m%y}60"
    result = runner.invoke(
        app,
        ["customers", "show", "lidl", "--sample", f"10/6 730AM - PYE_061026919 PO {po}"],
    )
    assert result.exit_code == 0, result.output
    assert f"date in {po}" in result.output and f"{today:%a %m/%d/%Y}" in result.output
    assert "delivery slot" in result.output and "PYE_061026919" in result.output
    assert runner.invoke(app, ["customers", "show", "nobody"]).exit_code == 1

    result = runner.invoke(app, ["booking", "scan", "--customer", "nobody"])
    assert result.exit_code == 2 and "no customer file 'nobody'" in result.output


def test_the_starter_file_reads_once_the_customer_is_named() -> None:
    text = starter_text("acme", name='Acme "West"', customer_ids=[1, 2], terminal_ids=[3])
    assert "customer_ids = [1, 2]" in text and "terminal_ids = [3]" in text
    assert '# group = "group@circledelivers.com"' in text
    customer = parse_customer(tomllib.loads(text), key="acme", source="acme.toml")
    assert customer.name == "Acme 'West'" and customer.cc == () and customer.po is None
