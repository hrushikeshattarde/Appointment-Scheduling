"""Facility rules: a desk's cut-off, booking horizon and required numbers; portal vendors by URL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from typer.testing import CliRunner

from facility_profiles.api.app import create_app
from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, CaseStatus, ExceptionType
from facility_profiles.booking.rules import (
    business_day_before,
    cutoff_at,
    slot_is_stale,
    too_early,
    vendor_profile,
)
from facility_profiles.booking.service import (
    add_reference,
    draft_case,
    list_cases,
    prepare_drafts,
    scan,
)
from facility_profiles.booking.timers import sweep
from facility_profiles.booking.today import stage
from facility_profiles.booking.worklist import method_exception, open_kinds, resolve
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.domain.normalize import portal_vendor_from_url
from facility_profiles.domain.rules import to_tpro_write
from facility_profiles.domain.schema import (
    FacilityIdentity,
    FieldState,
    ProfileField,
    Role,
    SourceType,
    ValueOrigin,
)
from facility_profiles.domain.scoring import Mention
from facility_profiles.extraction.validate import coerce
from facility_profiles.pipeline.profile import assemble_profile
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.models import AuditEntry, ReviewItem
from facility_profiles.storage.repository import Repository, unwrap
from tests.conftest import FakeTPro
from tests.test_booking import NOW, koch_key, lidl_load, seed_vendor

# NOW is Tuesday 09/29 08:00 in New York; the Lidl test load picks up Thursday 10/01 09:00.
WED_3PM = datetime(2026, 9, 30, 19, 0, tzinfo=UTC)


def _rules(sessions, **values: Any) -> None:  # type: ignore[no-untyped-def]
    """File desk rules on the Koch Foods test profile, as a person would."""
    with session_scope(sessions) as session:
        repo = Repository(session)
        for field, value in values.items():
            repo.set_field_human(koch_key(), Role.SHIPPER, field, value, state=FieldState.HUMAN_SET)


def _scanned(settings, sessions, **rules: Any) -> int:  # type: ignore[no-untyped-def]
    seed_vendor(sessions)
    if rules:
        _rules(sessions, **rules)
    client = FakeTPro([lidl_load(7301, po="226321092660")], {})
    scan(client, sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        return list_cases(session)[0].id


# ------------------------------------------------------------------ profile fields


def test_people_can_write_rules_the_way_desks_say_them():
    assert coerce("cutoff_time", "2 PM") == "14:00"
    assert coerce("cutoff_time", "1400") == "14:00"
    assert coerce("cutoff_time", "9:30am") == "09:30"
    assert coerce("cutoff_time", "13pm") is None and coerce("cutoff_time", "noonish") is None
    assert coerce("max_days_ahead", "3 days") == 3
    assert coerce("max_days_ahead", "0") is None and coerce("max_days_ahead", "90") is None
    assert coerce("required_refs", "TI shipment number") == ["shipment_number"]
    assert coerce("required_refs", "SO#, PO") == ["po_number", "sales_order_number"]
    assert coerce("required_refs", "shipment_number; bol") == ["bol_number", "shipment_number"]
    assert coerce("required_refs", "widget number") is None


@pytest.mark.parametrize(
    ("url", "vendor"),
    [
        ("https://appointments.cwtraffic.com/depot/12", "costco"),
        ("https://costcotraffic.com/", "costco"),
        ("https://www.myunfi.com/scheduling", "unfi"),
        ("https://ahold-tlm.logistics.com/appt", "ahold"),
        ("https://portal.publix.io/dock", "publix"),
        ("www.bozzutos.net/carriers", "bozzutos"),
        ("https://www.ncrpowertraffic.com/", "retalix"),
        ("https://schedule.opendock.com/warehouse/1", "opendock"),
        ("https://scheduling.kuebix.com/", None),
        ("not a url", None),
    ],
)
def test_a_portal_url_names_its_scheduling_system(url: str, vendor: str | None):
    assert portal_vendor_from_url(url) == vendor


def _mention(field: str, value: Any, conf: float, source: SourceType) -> Mention:
    return Mention(
        field_name=field,
        value=value,
        load_id=101,
        source_type=source,
        quote=str(value),
        observed_at=NOW,
        source_confidence=conf,
        normalized=value,
    )


def test_the_url_settles_the_portal_vendor_the_notes_called_other():
    profile = assemble_profile(
        FacilityIdentity(candidate_key="costco1", company_name="Costco Depot"),
        Role.RECEIVER,
        [
            _mention(
                "portal_url",
                "https://appointments.cwtraffic.com/x",
                0.9,
                SourceType.FACILITY_APPOINTMENTS,
            ),
            _mention("portal_vendor", "other", 0.6, SourceType.STOP_NOTE),
            _mention("portal_vendor", "c3", 0.5, SourceType.STOP_NOTE),
        ],
        summary=None,
        run_id="r1",
        model_version="m",
        source_load_ids=[101],
        now=NOW,
        half_life_days=90,
        conflict_support=0.3,
    )
    vendor = profile.fields["portal_vendor"]
    url = profile.fields["portal_url"]
    assert vendor.value == "costco" and not vendor.conflict
    assert vendor.confidence == url.confidence and vendor.evidence == url.evidence[:3]
    assert vendor.candidates[0].value == "costco"
    assert {"other", "c3"} <= {c.value for c in vendor.candidates}  # still shown to reviewers


def test_rules_reach_the_transport_pro_notes():
    profile = assemble_profile(
        FacilityIdentity(candidate_key="m1"),
        Role.SHIPPER,
        [],
        summary="Books by email.",
        run_id="r1",
        model_version="m",
        source_load_ids=[],
        now=NOW,
        half_life_days=90,
        conflict_support=0.3,
    )
    profile.fields["cutoff_time"] = ProfileField(name="cutoff_time", value="14:00")
    profile.fields["max_days_ahead"] = ProfileField(name="max_days_ahead", value=5)
    profile.fields["required_refs"] = ProfileField(name="required_refs", value=["shipment_number"])
    assert to_tpro_write(profile).notes == (
        "Books by email. Requests by 14:00 the business day before. Books at most 5 days ahead. "
        "Needs shipment number."
    )


def _store_with_portals(tmp_path: Path) -> str:
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    with session_scope(session_factory(engine)) as session:
        repo = Repository(session)
        for candidate, name, portal, vendor, origin in (
            ("cw1", "Costco Depot Mira Loma", "https://appointments.cwtraffic.com/a", "other", "v"),
            ("unfi1", "UNFI Hopkins", "https://www.myunfi.com/b", None, "v"),
            ("od1", "Polar Fitzgerald", "https://schedule.opendock.com/c", "opendock", "v"),
            ("cw2", "Costco Depot Tracy", "https://appointments.cwtraffic.com/d", "c3", "human"),
        ):
            key = repo.upsert_facility(
                FacilityIdentity(candidate_key=candidate, company_name=name)
            ).key
            for field, value in (("portal_url", portal), ("portal_vendor", vendor)):
                if value is None:
                    continue
                if origin == "human":
                    repo.set_field_human(
                        key, Role.RECEIVER, field, value, state=FieldState.HUMAN_SET
                    )
                else:
                    repo.upsert_field(
                        key,
                        Role.RECEIVER,
                        ProfileField(name=field, value=value, confidence=0.8, mention_count=1),
                        state=FieldState.VERIFIED,
                        origin=ValueOrigin.VERIFIED,
                        run_id="r1",
                    )
        repo.queue(
            "candidate:cw1",
            Role.RECEIVER,
            ProfileField(name="portal_vendor", value="other", confidence=0.6),
            existing_value=None,
            reason="between thresholds",
            run_id="r1",
        )
    engine.dispose()
    return url


def test_profile_portals_lists_then_files_the_url_vendor_and_keeps_a_persons_choice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    url = _store_with_portals(tmp_path)
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        listed = runner.invoke(app, ["profile", "portals"])
        assert listed.exit_code == 0, listed.output
        assert listed.output.splitlines() == [
            "Costco Depot Mira Loma [receiver] https://appointments.cwtraffic.com/a: other -> costco",
            "Costco Depot Tracy [receiver] https://appointments.cwtraffic.com/d: c3 -> costco  "
            "(kept: a person set it)",
            "UNFI Hopkins [receiver] https://www.myunfi.com/b: None -> unfi",
            "2 to change, 1 kept; run again with --apply --by NAME",
        ]
        assert runner.invoke(app, ["profile", "portals", "--apply"]).exit_code == 2  # needs --by
        applied = runner.invoke(app, ["profile", "portals", "--apply", "--by", "megan"])
        assert "2 portal vendor(s) filed, 1 kept as a person set them" in applied.output
        assert "0 to change, 1 kept" in runner.invoke(app, ["profile", "portals"]).output
    finally:
        get_settings.cache_clear()
    engine = make_engine(url)
    with session_scope(session_factory(engine)) as session:
        repo = Repository(session)
        mira = repo.fields("candidate:cw1", Role.RECEIVER)["portal_vendor"]
        assert unwrap(mira.value) == "costco" and mira.state == FieldState.VERIFIED.value
        unfi = repo.fields("candidate:unfi1", Role.RECEIVER)["portal_vendor"]
        assert unwrap(unfi.value) == "unfi" and unfi.state == FieldState.VERIFIED.value
        tracy = repo.fields("candidate:cw2", Role.RECEIVER)["portal_vendor"]
        assert unwrap(tracy.value) == "c3"  # a person set it; only listed
        review = session.scalars(select(ReviewItem)).one()
        assert review.status == "superseded"
        audit = session.scalars(select(AuditEntry).where(AuditEntry.action == "refine")).all()
        assert {a.reason for a in audit} == {
            "portal URL host appointments.cwtraffic.com is costco",
            "portal URL host myunfi.com is unfi",
        }
    engine.dispose()


# ------------------------------------------------------------------ when a request can go out


def test_cut_off_notice_and_horizon_are_read_in_the_vendor_time_zone(settings, sessions):
    seed_vendor(sessions)
    _rules(sessions, cutoff_time="14:00", notice_period_hours=72, max_days_ahead=1)
    with session_scope(sessions) as session:
        profile = vendor_profile(Repository(session), koch_key())
    assert (profile.cutoff_time, profile.notice_period_hours, profile.max_days_ahead) == (
        "14:00",
        72,
        1,
    )
    monday = datetime(2026, 10, 5, 9, 0, tzinfo=UTC)
    assert business_day_before(monday).weekday() == 4  # a Monday pickup's cut-off is Friday
    cutoff = cutoff_at("2026-10-01 09:00", "America/New_York", "14:00")
    assert cutoff is not None and cutoff.isoformat() == "2026-09-30T14:00:00-04:00"

    no_notice = profile.__class__(**{**profile.__dict__, "notice_period_hours": None})
    assert (
        slot_is_stale("2026-10-01 09:00", "America/New_York", settings, now=NOW, profile=no_notice)
        is None
    )
    assert slot_is_stale(
        "2026-10-01 09:00", "America/New_York", settings, now=WED_3PM, profile=no_notice
    ) == (
        "the desk's cut-off for 2026-10-01 09:00 was Wed 09/30 14:00 (14:00 the business day before)"
    )
    assert slot_is_stale(
        "2026-10-01 09:00", "America/New_York", settings, now=NOW, profile=profile
    ) == (
        "requested slot 2026-10-01 09:00 is inside the desk's 72 h notice; it had to be asked "
        "for by Mon 09/28 09:00"
    )
    assert too_early("2026-10-01 09:00", "America/New_York", profile, now=NOW) == (
        "the desk books at most 1 day ahead; ask from Wed 09/30"
    )
    assert too_early("2026-10-01 09:00", "America/New_York", profile, now=WED_3PM) is None
    assert too_early("2026-10-01 09:00", "America/New_York", None, now=NOW) is None


def test_a_desk_that_needs_the_customers_shipment_number_waits_for_a_person(settings, sessions):
    case_id = _scanned(settings, sessions, required_refs=["shipment_number"])
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and case.status == CaseStatus.UNSCHEDULED.value
        assert open_kinds(case) == ["missing_reference"]
        assert case.open_exceptions[0].description == (
            "the desk needs the customer's shipment number before it books"
        )
        assert stage(case) == "Not requested: reference needed"
        with pytest.raises(ValueError, match="open exceptions"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)

        assert add_reference(session, case, "shipment_number", " 7781234 ", by="megan") == []
        assert open_kinds(case) == []
        assert case.exceptions[0].resolution == "Shipment# 7781234 added"
        mailer = RecordingMailer()
        draft_case(session, case, mailer, settings, now=NOW)
        assert "PO# 226321092660 / Shipment# 7781234 on 10/01 @ 0900" in mailer.drafts[0].body
        with pytest.raises(ValueError, match="reference type must be one of"):
            add_reference(session, case, "widget_number", "1", by="megan")


def test_a_desk_that_books_a_day_ahead_makes_the_request_wait_then_drafts_it(settings, sessions):
    case_id = _scanned(settings, sessions, max_days_ahead=1)
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == []
        assert case.reason == "the desk books at most 1 day ahead; ask from Wed 09/30"
        assert stage(case) == (
            "Not requested yet: the desk books at most 1 day ahead; ask from Wed 09/30"
        )
        ready, waiting = prepare_drafts(session, settings, now=NOW)
        assert ready == [] and [(c.id, why) for c, why in waiting] == [
            (case_id, "the desk books at most 1 day ahead; ask from Wed 09/30")
        ]
        with pytest.raises(ValueError, match="not drafted yet"):
            draft_case(session, case, RecordingMailer(), settings, now=NOW)

        wednesday = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
        ready, waiting = prepare_drafts(session, settings, now=wednesday)
        assert ready == [case] and waiting == [] and case.reason is None


def test_the_timers_raise_a_cut_off_that_passed_before_anyone_sent_the_request(settings, sessions):
    case_id = _scanned(settings, sessions, cutoff_time="14:00")
    with session_scope(sessions) as session:
        case = session.get(BookingCase, case_id)
        assert case is not None and open_kinds(case) == []
        assert sweep(session, now=WED_3PM).raised == []  # without settings: the clocks only

        result = sweep(session, now=WED_3PM, settings=settings)
        assert [(k, d) for _, k, d in result.raised] == [
            (
                "slot_unworkable",
                "the desk's cut-off for 2026-10-01 09:00 was Wed 09/30 14:00 "
                "(14:00 the business day before)",
            )
        ]
        assert case.open_exceptions[0].raised_by == "timer"
        resolve(session, case, [ExceptionType.SLOT_UNWORKABLE], resolution="called", by="megan")
        later = WED_3PM + timedelta(hours=1)
        assert sweep(session, now=later, settings=settings).raised == []  # the call stands

        # A number the desk now requires is raised too, and cleared once the case has it.
        _rules(sessions, required_refs=["sales_order_number"])
        session.expire_all()
        case = session.get(BookingCase, case_id)
        assert case is not None
        result = sweep(session, now=later, settings=settings)
        assert [k for _, k, _ in result.raised] == ["missing_reference"]
        add_reference(session, case, "sales_order_number", "SO259719", by="megan")
        assert open_kinds(case) == []


def test_a_portal_desk_is_named_with_its_system_and_address():
    kind, why = method_exception(
        "web_portal", portal_vendor="opendock", portal_url="https://schedule.opendock.com/w/1"
    )
    assert kind == ExceptionType.METHOD_NOT_SUPPORTED
    assert why == (
        "books on opendock (https://schedule.opendock.com/w/1); the agent only books by email"
    )
    assert method_exception("web_portal", portal_vendor="other")[1] == (
        "books on a web portal; the agent only books by email"
    )
    assert (
        method_exception("web_portal")[1] == "books on a web portal; the agent only books by email"
    )


# ------------------------------------------------------------------ CLI and board


def test_booking_ref_from_the_command_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings
):
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    settings = settings.model_copy(update={"database_url": url, "pilot_terminal_ids": [1089]})
    case_id = _scanned(settings, sessions, required_refs=["shipment_number"])
    engine.dispose()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.setenv("FP_BOOKING_DRAFTS_DIR", str(tmp_path / "drafts"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        bad = runner.invoke(app, ["booking", "ref", str(case_id), "widget", "1", "--by", "m"])
        assert bad.exit_code == 2
        added = runner.invoke(
            app, ["booking", "ref", str(case_id), "shipment_number", "7781234", "--by", "megan"]
        )
        assert added.exit_code == 0 and f"#{case_id} shipment_number = 7781234" in added.output
        shown = runner.invoke(app, ["booking", "show", str(case_id)])
        assert "ref       Shipment# 7781234" in shown.output
    finally:
        get_settings.cache_clear()


def test_the_board_adds_a_reference_and_shows_it(settings, tmp_path: Path):
    url = f"sqlite:///{(tmp_path / 'board.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    sessions = session_factory(engine)
    case_id = _scanned(
        settings.model_copy(update={"database_url": url}),
        sessions,
        required_refs=["sales_order_number"],
    )
    engine.dispose()
    application = create_app(settings.model_copy(update={"database_url": url}))
    with TestClient(application) as client:
        kinds = {k["kind"]: k["label"] for k in client.get("/api/booking/kinds").json()}
        assert kinds["missing_reference"] == "Reference needed"
        refs = {r["kind"]: r for r in client.get("/api/booking/references").json()}
        assert refs["sales_order_number"]["label"] == "SO#"
        base = f"/api/booking/cases/{case_id}"
        todo = client.get(base).json()["open_exceptions"][0]
        assert todo["detail"]["missing"] == ["sales_order_number"]
        refused = client.post(f"{base}/reference", json={"by": "m", "kind": "nope", "value": "1"})
        assert refused.status_code == 422
        detail = client.post(
            f"{base}/reference", json={"by": "megan", "kind": "sales_order_number", "value": "SO1"}
        ).json()
        assert detail["references"] == [
            {"kind": "sales_order_number", "label": "SO#", "value": "SO1"}
        ]
        assert detail["open_exceptions"] == []
        assert any(t["title"] == "Reference added" for t in detail["timeline"])
