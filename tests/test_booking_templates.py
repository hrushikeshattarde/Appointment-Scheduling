"""Email templates: the agent's wording per desk or customer, with fill-in fields."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select
from typer.testing import CliRunner

from facility_profiles.booking.mail import RecordingMailer
from facility_profiles.booking.models import BookingCase, BookingEvent
from facility_profiles.booking.respond import Responder
from facility_profiles.booking.service import (
    draft_batch,
    draft_case,
    list_cases,
    mark_sent,
    reschedule_case,
    scan,
)
from facility_profiles.booking.templates import (
    BUILT_IN,
    TemplateKind,
    check_template,
    pick,
    remove_template,
    save_template,
)
from facility_profiles.cli import app
from facility_profiles.config import get_settings
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from tests.conftest import FakeTPro
from tests.test_booking import NOW, lidl_load, seed_vendor

SIGNATURE = "Circle Logistics, Inc. | Fort Wayne | 260-208-4500 | lidl@circledelivers.com"


def _cases(settings, sessions, *pos: str) -> list[int]:  # type: ignore[no-untyped-def]
    """Koch Foods pickups (the CCI desk), one per PO, scanned at NOW."""
    seed_vendor(sessions)
    loads = [lidl_load(7301 + i, po=po) for i, po in enumerate(pos)]
    scan(FakeTPro(loads, {}), sessions, settings, days_ahead=7, now=NOW)  # type: ignore[arg-type]
    with session_scope(sessions) as session:
        return sorted(c.id for c in list_cases(session))


def _event(session, case_id: int, action: str) -> BookingEvent:  # type: ignore[no-untyped-def]
    return session.scalars(
        select(BookingEvent).where(BookingEvent.case_id == case_id, BookingEvent.action == action)
    ).one()


def test_a_template_that_would_write_a_broken_email_is_refused():
    assert check_template(TemplateKind.REQUEST, "Pickup {po}", "Hi,\n{lines}\n{signature}") == []
    problems = check_template(TemplateKind.REQUEST, "Pickup {po", "Hi {name},\n{signature}")
    assert problems[0].startswith("the subject has an unmatched brace")
    assert problems[1].startswith("the body uses {name}, which a request cannot fill; use {ask}")
    assert problems[2] == (
        "a request needs {lines}: the PO lines asked for, one per line: PO# X on MM/DD @ HHMM "
        "(Eastern), with any number the desk needs"
    )
    assert check_template(TemplateKind.FOLLOW_UP, "Following up", "Hello,\n{lines}") == [
        "the body uses {lines}, which a follow_up cannot fill; use {carrier}, {customer}, "
        "{date}, {delivery_date}, {delivery_ref}, {desk_name}, {load}, {po}, {refs}, "
        "{signature}, {time}, {vendor}",
        "a follow_up answers in the thread and keeps its subject",
    ]
    assert check_template(TemplateKind.RESCHEDULE, None, "  ") == [
        "the body is empty",
        "a reschedule needs {line}: the PO line asked for again (reschedule)",
    ]
    assert check_template(TemplateKind.REQUEST, "Two\nlines", "{lines}") == [
        "the subject must be one line of text"
    ]


def test_the_desk_beats_the_customer_beats_the_pod_default_beats_the_built_in(session):
    def source() -> str:
        return pick(
            session, TemplateKind.REQUEST, desk="CCI@udfinc.com", customer="Lidl - Inbound"
        ).source

    assert source() == "built-in"
    save_template(session, TemplateKind.REQUEST, body="Pod:\n{lines}", by="megan")
    assert source() == "default"
    save_template(
        session, TemplateKind.REQUEST, body="Lidl:\n{lines}", customer="Lidl - Inbound", by="m"
    )
    assert source() == "customer Lidl - Inbound"
    saved = save_template(
        session, TemplateKind.REQUEST, body="CCI:\n{lines}", desk=" cci@UDFINC.com ", by="m"
    )
    assert saved.source == "desk cci@udfinc.com" and source() == "desk cci@udfinc.com"
    assert saved.subject == BUILT_IN[TemplateKind.REQUEST].subject  # no subject: the built-in one
    assert pick(session, TemplateKind.REQUEST, desk=None, customer=None).source == "default"
    assert remove_template(session, TemplateKind.REQUEST, desk="cci@udfinc.com")
    assert not remove_template(session, TemplateKind.REQUEST, desk="cci@udfinc.com")
    assert source() == "customer Lidl - Inbound"
    with pytest.raises(ValueError, match="one desk or one customer"):
        save_template(session, TemplateKind.REQUEST, body="{lines}", desk="a", customer="b", by="m")
    with pytest.raises(ValueError, match="needs \\{lines\\}"):
        save_template(session, TemplateKind.REQUEST, body="Hello", by="m")


def test_a_customer_template_writes_the_request_with_its_fields_filled(settings, sessions):
    (case_id,) = _cases(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        save_template(
            session,
            TemplateKind.REQUEST,
            subject="{customer} pickup {po} - {vendor}",
            body=(
                "Hi {desk_name},\n\nPlease book load {load}:\n{lines}\n\n{refs}\n\n"
                "It delivers {delivery_ref} on {delivery_date}.\n\nThanks!\n{signature}"
            ),
            customer="Lidl - Inbound",
            by="megan",
        )
        case = session.get(BookingCase, case_id)
        assert case is not None
        mailer = RecordingMailer()
        message = draft_case(session, case, mailer, settings, now=NOW)
        assert message.subject == "Lidl pickup 226321092660 - Koch Foods"
        assert message.body == (
            "Hi CCI desk,\n\nPlease book load 7301:\nPO# 226321092660 on 10/01 @ 0900\n\n"
            "It delivers PYE_021026123 on 10/02.\n\nThanks!\n" + SIGNATURE
        )  # the empty {refs} leaves no gap
        assert _event(session, case_id, "drafted").detail["template"] == "customer Lidl - Inbound"


def test_a_desk_template_writes_the_batch_for_that_desk(settings, sessions):
    first, second = _cases(settings, sessions, "226321092660", "226322092660")
    with session_scope(sessions) as session:
        save_template(
            session,
            TemplateKind.BATCH_REQUEST,
            subject="Lidl pickups for {vendor}: {po}",
            body="Good afternoon,\n\n{ask}\n\n{lines}\n\nThank you!\n\n{signature}",
            desk="cci@udfinc.com",
            by="megan",
        )
        cases = [session.get(BookingCase, i) for i in (first, second)]
        messages = draft_batch(session, cases, RecordingMailer(), settings, now=NOW)  # type: ignore[arg-type]
        assert {m.subject for m in messages} == {
            "Lidl pickups for Koch Foods: 226321092660 & 226322092660"
        }
        assert messages[0].body == (
            "Good afternoon,\n\nCan I please schedule the following for Koch Foods going to "
            "Lidl?\n\nPO# 226321092660 on 10/01 @ 0900\nPO# 226322092660 on 10/01 @ 0900\n\n"
            "Thank you!\n\n" + SIGNATURE
        )
        assert _event(session, second, "drafted").detail["template"] == "desk cci@udfinc.com"


def test_the_reschedule_and_follow_up_wording_comes_from_templates_too(settings, sessions):
    (case_id,) = _cases(settings, sessions, "226321092660")
    with session_scope(sessions) as session:
        save_template(
            session,
            TemplateKind.RESCHEDULE,
            body="Hello,\n\n{note}\n\nWe had {previous}. Can we move {line}?\n\n{signature}",
            by="megan",
        )
        save_template(
            session,
            TemplateKind.FOLLOW_UP,
            body="Hello {desk_name},\n\nAny update on PO# {po} for {date}?\n\n{signature}",
            by="megan",
        )
        case = session.get(BookingCase, case_id)
        assert case is not None
        draft_case(session, case, RecordingMailer(), settings, now=NOW)
        mark_sent(session, case, by="megan", thread_id="t1", sent_at=NOW)

        later = NOW + timedelta(hours=25)
        nudge = Responder(settings, RecordingMailer(), now=later).follow_up(session, case)
        assert nudge is not None
        assert nudge.body == (
            "Hello CCI desk,\n\nAny update on PO# 226321092660 for 10/01?\n\n" + SIGNATURE
        )  # signed once: the template carries the signature
        assert _event(session, case_id, "follow_up").detail["reason"] == (
            "no reply for 24 weekday hours (default wording)"
        )

        message = reschedule_case(
            session,
            case,
            RecordingMailer(),
            settings,
            requested_local="2026-10-02 10:00",
            by="megan",
        )
        assert message.body == (
            "Hello,\n\nWe had 10/01 @ 0900. Can we move PO# 226321092660 on 10/02 @ 1000?\n\n"
            + SIGNATURE
        )  # the empty {note} leaves no gap


def test_the_template_commands(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, settings):
    url = f"sqlite:///{(tmp_path / 'fp.db').as_posix()}"
    engine = make_engine(url)
    init_db(engine)
    (case_id,) = _cases(settings, session_factory(engine), "226321092660")
    engine.dispose()
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", url)
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    runner = CliRunner()
    try:
        fields = runner.invoke(app, ["booking", "template", "fields"]).output
        assert "{lines}  the PO lines asked for" in fields and "[request, batch_request]" in fields
        assert "no saved templates" in runner.invoke(app, ["booking", "template", "list"]).output

        bad = runner.invoke(
            app, ["booking", "template", "set", "request", "--by", "m", "--body", "Hi {name}"]
        )
        assert bad.exit_code == 2
        set_request = ["booking", "template", "set", "request", "--by", "megan"]
        scope = ["--customer", "Lidl - Inbound", "--subject", "Lidl {po}"]
        body = ["--body", "Hi,\\n\\n{lines}\\n\\n{signature}"]  # \n as typed in a shell
        saved = runner.invoke(app, [*set_request, *scope, *body])
        assert saved.exit_code == 0, saved.output
        assert "request template saved for customer Lidl - Inbound" in saved.output
        body_file = tmp_path / "follow.txt"
        body_file.write_text("Hello,\n\nAny news on {po}?\n\n{signature}", encoding="utf-8")
        set_follow_up = ["booking", "template", "set", "follow_up", "--by", "megan"]
        result = runner.invoke(app, [*set_follow_up, "--body-file", str(body_file)])
        assert "follow_up template saved for default" in result.output

        listed = runner.invoke(app, ["booking", "template", "list"]).output.splitlines()
        assert listed[0].startswith("follow_up      default") and "by megan" in listed[0]
        assert listed[1].startswith("request        customer Lidl - Inbound")
        shown = runner.invoke(
            app, ["booking", "template", "show", "request", "--customer", "Lidl - Inbound"]
        ).output
        assert shown.startswith("# request (customer Lidl - Inbound)\nSubject: Lidl {po}\n")

        preview = runner.invoke(app, ["booking", "template", "preview", str(case_id)])
        assert preview.exit_code == 0, preview.output
        assert preview.output == (
            f"# request for case #{case_id} (customer Lidl - Inbound template)\n"
            "To: cci@udfinc.com\nSubject: Lidl 226321092660\n\n"
            "Hi,\n\nPO# 226321092660 on 10/01 @ 0900\n\n" + SIGNATURE + "\n"
        )
        nudge = runner.invoke(
            app, ["booking", "template", "preview", str(case_id), "--kind", "follow_up"]
        )
        assert "Any news on 226321092660?" in nudge.output
        assert runner.invoke(app, ["booking", "show", str(case_id)]).output.count("->") == 0
        bad_kind = runner.invoke(app, ["booking", "template", "show", "fax"])
        assert bad_kind.exit_code == 2

        removed = runner.invoke(
            app, ["booking", "template", "remove", "request", "--customer", "Lidl - Inbound"]
        )
        assert "request template removed" in removed.output
        again = runner.invoke(
            app, ["booking", "template", "remove", "request", "--customer", "Lidl - Inbound"]
        )
        assert again.exit_code == 1
    finally:
        get_settings.cache_clear()
