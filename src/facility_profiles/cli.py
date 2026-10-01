"""Command-line interface.

facility-profiles init-db
facility-profiles check-tpro
facility-profiles harvest --terminal 1160 --days 90
facility-profiles harvest --terminal 1089 --customer 6680 --customer 7211 --days 90
facility-profiles run --no-harvest --cap 50
facility-profiles lookup 196508
facility-profiles review list | accept 12 --by name | edit 12 --value ... | reject 12
facility-profiles export
facility-profiles digest
"""

from __future__ import annotations

import atexit
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from facility_profiles import __version__
from facility_profiles.booking.models import CaseStatus, ExceptionType
from facility_profiles.config import Settings, get_settings
from facility_profiles.domain.schema import PROFILE_FIELDS, FieldState, Role
from facility_profiles.logging import configure_logging, get_logger
from facility_profiles.pipeline.digest import render_digest
from facility_profiles.pipeline.export import export_profiles_csv
from facility_profiles.pipeline.export_xlsx import export_workbook
from facility_profiles.pipeline.run import Pipeline
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.models import FacilityRecord
from facility_profiles.storage.repository import Repository, unwrap

app = typer.Typer(help="Facility scheduling profiles (Idea 1).", no_args_is_help=True)
review_app = typer.Typer(help="Work the review queue.", no_args_is_help=True)
app.add_typer(review_app, name="review")
profile_app = typer.Typer(
    help="Set profile values by hand, with a reason, or ask a person for one.",
    no_args_is_help=True,
)
app.add_typer(profile_app, name="profile")
booking_app = typer.Typer(
    help="Booking agent prototype (draft mode): scan, draft, inbox, approve.",
    no_args_is_help=True,
)
app.add_typer(booking_app, name="booking")
mail_app = typer.Typer(
    help="Group-mail archive in S3: collect Pick Up Appointment threads, check what is there.",
    no_args_is_help=True,
)
app.add_typer(mail_app, name="mail-archive")

log = get_logger(__name__)


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    return settings


def _sessions(settings: Settings):  # type: ignore[no-untyped-def]  # sessionmaker generic is verbose
    engine = make_engine(settings.database_url)
    init_db(engine)
    atexit.register(engine.dispose)
    return session_factory(engine)


def _client(settings: Settings, *, allow_writes: bool = False):  # type: ignore[no-untyped-def]
    from facility_profiles.tpro.client import TransportProClient

    return TransportProClient.from_settings(settings, allow_writes=allow_writes)


def _extractor(settings: Settings, *, fake: bool):  # type: ignore[no-untyped-def]
    if fake:
        from facility_profiles.extraction.llm import FakeExtractor, empty_result

        return FakeExtractor(empty_result())
    if settings.llm_provider == "openrouter":
        from facility_profiles.extraction.openrouter import OpenRouterExtractor

        assert settings.openrouter_api_key is not None  # enforced by Settings
        return OpenRouterExtractor(
            settings.openrouter_api_key.get_secret_value(),
            model=settings.llm_model,
            max_tokens=settings.llm_max_tokens,
            base_url=settings.openrouter_base_url,
        )
    from facility_profiles.extraction.llm import AnthropicExtractor

    return AnthropicExtractor(settings.llm_model, max_tokens=settings.llm_max_tokens)


@app.command()
def version() -> None:
    """Print the version."""
    typer.echo(__version__)


@app.command("init-db")
def init_db_cmd() -> None:
    """Create the database schema."""
    settings = _settings()
    init_db(make_engine(settings.database_url))
    typer.echo(f"schema ready at {settings.database_url}")


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8000,
    db: Annotated[
        str | None, typer.Option(help="Store to serve, e.g. sqlite:///./data/<pod>.db")
    ] = None,
) -> None:
    """Run the appointments board (/app/) and the HTTP API. Needs the api extra."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("the board needs the api extra: uv sync --extra api")
        raise typer.Exit(code=2) from exc
    from facility_profiles.api.app import create_app

    settings = _settings()
    if db:
        settings = settings.model_copy(update={"database_url": db})
    typer.echo(f"appointments board: http://{host}:{port}/app/  (store {settings.database_url})")
    uvicorn.run(create_app(settings), host=host, port=port, log_level="warning")


@app.command("check-tpro")
def check_tpro() -> None:
    """Authenticate against Transport Pro and read one terminal (read-only smoke test)."""
    settings = _settings()
    with _client(settings) as client:
        terminals = client.list_terminals()
        pods = [t for t in terminals if t.is_pod]
        typer.echo(f"auth ok; {len(terminals)} terminals, {len(pods)} pods")
        page = client.search_loads(
            pickup_date_start=date.today().isoformat(),
            pickup_date_end=date.today().isoformat(),
        )
        typer.echo(
            f"load search ok; {page.pagination.total_records} loads picking up today, "
            f"{page.pagination.total_pages} pages of {page.pagination.per_page}"
        )


@app.command()
def harvest(
    terminal: Annotated[
        list[int] | None, typer.Option(help="Terminal ID(s); default from settings")
    ] = None,
    customer: Annotated[
        list[int] | None,
        typer.Option(help="Customer ID(s) to restrict the loads to; default from settings"),
    ] = None,
    days: Annotated[int | None, typer.Option(help="Look-back window in days")] = None,
) -> None:
    """Pull loads and store facilities, links and stop notes (FR-1, FR-2)."""
    settings = _settings()
    end = datetime.now(tz=UTC).date()
    start = end - timedelta(days=days or settings.lookback_days)
    with _client(settings) as client:
        pipeline = Pipeline(
            settings, _sessions(settings), client=client, extractor=_extractor(settings, fake=True)
        )
        stats = pipeline.harvest(terminal_ids=terminal, customer_ids=customer, start=start, end=end)
    typer.echo(json.dumps(stats, indent=2))


@app.command()
def run(
    harvest_first: Annotated[
        bool, typer.Option("--harvest/--no-harvest", help="Harvest before extracting")
    ] = True,
    refresh: Annotated[
        bool, typer.Option(help="Only facilities with new sources since their last profile")
    ] = False,
    cap: Annotated[int | None, typer.Option(help="Max facilities this run")] = None,
    terminal: Annotated[list[int] | None, typer.Option(help="Terminal ID(s)")] = None,
    customer: Annotated[
        list[int] | None, typer.Option(help="Customer ID(s) to restrict the harvest to")
    ] = None,
    fake_llm: Annotated[bool, typer.Option(help="Use the fake extractor (no model calls)")] = False,
    offline: Annotated[
        bool, typer.Option(help="No Transport Pro calls; use stored sources only")
    ] = False,
    resume: Annotated[str | None, typer.Option(help="Run ID to resume")] = None,
    budget: Annotated[
        float | None,
        typer.Option(help="Stop extracting once estimated LLM spend reaches this many USD"),
    ] = None,
    replay: Annotated[
        bool, typer.Option(help="Reuse the last stored model output instead of calling the model")
    ] = False,
) -> None:
    """Run the pipeline: collect, extract, score and apply every facility."""
    settings = _settings()
    if budget is not None:
        settings = settings.model_copy(update={"llm_budget_usd": budget})
    client = None if offline else _client(settings)
    try:
        sessions = _sessions(settings)
        if replay:
            from facility_profiles.extraction.replay import ReplayExtractor

            extractor = ReplayExtractor(sessions)
        else:
            extractor = _extractor(settings, fake=fake_llm)
        pipeline = Pipeline(settings, sessions, client=client, extractor=extractor)
        report = pipeline.run(
            do_harvest=harvest_first and not offline,
            refresh_only=refresh,
            facility_cap=cap,
            terminal_ids=terminal,
            customer_ids=customer,
            run_id=resume,
        )
    finally:
        if client is not None:
            client.close()
    typer.echo(
        json.dumps(
            {"run_id": report.run_id, "status": report.status, **report.as_stats()}, indent=2
        )
    )


@app.command()
def lookup(
    query: Annotated[
        str, typer.Argument(help="Transport Pro location ID or part of a facility name")
    ],
) -> None:
    """Show the stored profile for a facility."""
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        records = (
            repo.find_facility(facility_id=int(query))
            if query.isdigit()
            else repo.find_facility(name=query)
        )
        if not records:
            typer.echo("no facility found")
            raise typer.Exit(code=1)
        for record in records[:5]:
            typer.echo(
                f"\n{record.company_name} [{record.key}] {record.city}, {record.state} "
                f"— {record.stop_count} stops"
            )
            for role in repo.roles_for(record.key):
                profile = repo.profile(record.key, role)
                typer.echo(
                    f"  role: {role.value}"
                    + (
                        f" — {profile.scheduling_summary}"
                        if profile and profile.scheduling_summary
                        else ""
                    )
                )
                for name, fld in sorted(repo.fields(record.key, role).items()):
                    typer.echo(
                        f"    {name:<22} {json.dumps(unwrap(fld.value)):<40} "
                        f"conf={fld.confidence:.2f} state={fld.state} "
                        f"loads={fld.distinct_loads}{' CONFLICT' if fld.conflict else ''}"
                    )


def resolve_facility(repo: Repository, query: str) -> FacilityRecord:
    """One facility from a store key, a Transport Pro location ID or a unique name."""
    if query.startswith(("tpro:", "candidate:")):
        record = repo.get_facility(query)
        if record is None:
            raise typer.BadParameter(f"no facility with key {query}")
        return record
    records = (
        repo.find_facility(facility_id=int(query))
        if query.isdigit()
        else repo.find_facility(name=query)
    )
    exact = [r for r in records if (r.company_name or "").lower() == query.lower()]
    if len(records) > 1 and len(exact) == 1:
        records = exact
    if len(records) != 1:
        found = "; ".join(f"{r.company_name} ({r.city}) [{r.key}]" for r in records[:8])
        raise typer.BadParameter(
            f"'{query}' matches {len(records)} facilities" + (f": {found}" if found else "")
        )
    return records[0]


@profile_app.command("set")
def profile_set(
    facility: Annotated[str, typer.Argument(help="Store key, location ID or unique name")],
    role: Annotated[Role, typer.Argument(help="shipper or receiver")],
    field: Annotated[str, typer.Argument(help="Profile field name")],
    value: Annotated[str, typer.Option(help="Value to file")],
    by: Annotated[str, typer.Option(help="Who decided")],
    reason: Annotated[str | None, typer.Option(help="Evidence or source")] = None,
) -> None:
    """File a human-set value (never overwritten by the routine) with its evidence."""
    from facility_profiles.extraction.validate import coerce

    if field not in PROFILE_FIELDS:
        raise typer.BadParameter(f"field must be one of {', '.join(PROFILE_FIELDS)}")
    typed = value if field == "receiving_hours" else coerce(field, value)
    if typed is None:
        raise typer.BadParameter(f"'{value}' is not a valid value for {field}")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        record = resolve_facility(repo, facility)
        previous = repo.fields(record.key, role).get(field)
        before = unwrap(previous.value) if previous else None
        closed = repo.close_review_items(record.key, role, field, status="superseded")
        repo.set_field_human(record.key, role, field, typed, state=FieldState.HUMAN_SET)
        repo.audit(
            run_id=None,
            key=record.key,
            role=role,
            field_name=field,
            action="human_set",
            before=before,
            after=typed,
            confidence=1.0,
            reason=f"set by {by}" + (f": {reason}" if reason else ""),
            actor=by,
        )
        name = record.company_name
    typer.echo(
        f"{name} [{role.value}] {field} = {json.dumps(typed)} (was {json.dumps(before)})"
        + (f"; closed {closed} open review item(s)" if closed else "")
    )


@profile_app.command("summary")
def profile_summary(
    facility: Annotated[str, typer.Argument(help="Store key, location ID or unique name")],
    role: Annotated[Role, typer.Argument(help="shipper or receiver")],
    text: Annotated[str, typer.Option(help="One-paragraph scheduling summary")],
    by: Annotated[str, typer.Option(help="Who wrote it")],
) -> None:
    """Replace the scheduling summary with a human-written one."""
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        record = resolve_facility(repo, facility)
        current = repo.profile(record.key, role)
        before = current.scheduling_summary if current else None
        repo.upsert_profile(
            record.key,
            role,
            summary=text,
            run_id="manual",
            model_version=f"human:{by}",
            source_load_ids=list(current.source_load_ids) if current else [],
            source_count=current.source_count if current else 0,
        )
        repo.audit(
            run_id=None,
            key=record.key,
            role=role,
            field_name="scheduling_summary",
            action="human_set",
            before=before,
            after=text,
            confidence=1.0,
            reason=f"summary written by {by}",
            actor=by,
        )
        name = record.company_name
    typer.echo(f"{name} [{role.value}] summary updated")


@profile_app.command("ask")
def profile_ask(
    facility: Annotated[str, typer.Argument(help="Store key, location ID or unique name")],
    role: Annotated[Role, typer.Argument(help="shipper or receiver")],
    field: Annotated[str, typer.Argument(help="Profile field name")],
    reason: Annotated[str, typer.Option(help="What the reviewer should answer")],
    proposed: Annotated[str | None, typer.Option(help="Suggested value, if any")] = None,
) -> None:
    """Put a question about a field on the review queue without a run."""
    if field not in PROFILE_FIELDS:
        raise typer.BadParameter(f"field must be one of {', '.join(PROFILE_FIELDS)}")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        record = resolve_facility(repo, facility)
        item = repo.ask_review(record.key, role, field, reason=reason, proposed=proposed)
        item_id, name = item.id, record.company_name
    typer.echo(f"#{item_id} {name} [{role.value}] {field}: {reason}")


def _booking_case(session, case_id: int):  # type: ignore[no-untyped-def]
    from facility_profiles.booking.models import BookingCase

    case = session.get(BookingCase, case_id)
    if case is None:
        typer.echo(f"case {case_id} not found")
        raise typer.Exit(code=1)
    return case


@booking_app.command("scan")
def booking_scan(
    terminal: Annotated[list[int] | None, typer.Option(help="Terminal ID(s)")] = None,
    customer: Annotated[list[int] | None, typer.Option(help="Customer ID(s)")] = None,
    days_ahead: Annotated[int | None, typer.Option(help="Pickup window in days")] = None,
) -> None:
    """Open a booking case for every pickup stop that still needs an appointment."""
    from facility_profiles.booking.service import scan

    settings = _settings()
    with _client(settings) as client:
        stats = scan(
            client,
            _sessions(settings),
            settings,
            terminal_ids=terminal,
            customer_ids=customer,
            days_ahead=days_ahead,
        )
    typer.echo(json.dumps(stats.__dict__, indent=2))


@booking_app.command("list")
def booking_list(
    status: Annotated[CaseStatus | None, typer.Option(help="Only this status")] = None,
    exception: Annotated[
        str | None,
        typer.Option(help="Only cases with this open exception, or 'any' for every open one"),
    ] = None,
) -> None:
    """List booking cases: status, open exceptions (!kind), vendor, PO, requested slot."""
    from facility_profiles.booking.service import list_cases, summary_line

    kinds = [k.value for k in ExceptionType]
    if exception is not None and exception != "any" and exception not in kinds:
        raise typer.BadParameter(f"exception must be 'any' or one of {', '.join(kinds)}")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        cases = list_cases(session, status.value if status else None, exception=exception)
        if not cases:
            typer.echo("no cases")
            return
        for c in cases:
            typer.echo(summary_line(c))


@booking_app.command("show")
def booking_show(case_id: int) -> None:
    """Show one case with its messages and events."""
    from facility_profiles.booking.service import describe

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        typer.echo(describe(case))
        for e in case.events:
            typer.echo(
                f"  {e.created_at:%Y-%m-%d %H:%M} {e.action} by {e.actor} "
                f"{json.dumps(e.detail)[:160]}"
            )


@booking_app.command("draft")
def booking_draft(
    case_id: Annotated[
        int | None, typer.Argument(help="Case to draft; omit for all new cases")
    ] = None,
) -> None:
    """Compose the request emails as drafts, one per vendor desk (.eml in the drafts folder)."""
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.service import draft_batch, draft_case, ready_to_draft

    settings = _settings()
    mailer = LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender)
    with session_scope(_sessions(settings)) as session:
        if case_id is not None:
            try:
                messages = [draft_case(session, _booking_case(session, case_id), mailer, settings)]
            except ValueError as exc:
                typer.echo(f"#{case_id}: {exc}")
                raise typer.Exit(code=1) from exc
        else:
            messages = draft_batch(session, ready_to_draft(session), mailer, settings)
        for message in messages:
            typer.echo(
                f"#{message.case_id} drafted -> {message.to_addr}: {message.subject}  "
                f"[{message.draft_ref}]"
            )


@booking_app.command("send")
def booking_send(
    case_id: Annotated[
        int | None, typer.Argument(help="Case to send; omit for all new cases, one email per desk")
    ] = None,
    by: Annotated[str, typer.Option(help="Who is sending (recorded on the case)")] = "agent",
) -> None:
    """Send the request through Gmail as the agent's mailbox (FP_BOOKING_MODE=send)."""
    from facility_profiles.booking.mail import GmailSender
    from facility_profiles.booking.outbox import SendRefusedError
    from facility_profiles.booking.service import draft_batch, draft_case, ready_to_draft

    settings = _settings()
    if settings.booking_mode != "send":
        typer.echo("FP_BOOKING_MODE is 'draft'; set it to 'send' to let the agent send")
        raise typer.Exit(code=2)
    if not settings.booking_gmail_key or not settings.booking_gmail_user:
        typer.echo("set FP_BOOKING_GMAIL_KEY and FP_BOOKING_GMAIL_USER (the mailbox to send as)")
        raise typer.Exit(code=2)
    sender = GmailSender(
        Path(settings.booking_gmail_key), settings.booking_gmail_user, settings.booking_gmail_user
    )
    with session_scope(_sessions(settings)) as session:
        try:
            if case_id is not None:
                messages = [
                    draft_case(session, _booking_case(session, case_id), sender, settings, by=by)
                ]
            else:
                messages = draft_batch(session, ready_to_draft(session), sender, settings, by=by)
        except (SendRefusedError, ValueError) as exc:
            typer.echo(f"refused: {exc}")
            raise typer.Exit(code=1) from exc
        for message in messages:
            typer.echo(
                f"#{message.case_id} sent -> {message.to_addr}: {message.subject}  "
                f"[{message.draft_ref}] {message.rfc_message_id}"
            )


@booking_app.command("delivery-updated")
def booking_delivery_updated(
    case_id: int,
    ref: Annotated[str, typer.Option(help="New DCT reference, e.g. FRG_200526615")],
    date: Annotated[str, typer.Option(help="New delivery date, YYYY-MM-DD")],
    time: Annotated[str, typer.Option(help="New delivery time, HH:MM local")],
    by: Annotated[str, typer.Option(help="Who rebooked it in DCT")],
    note: Annotated[str | None, typer.Option(help="Why, e.g. We missed the pickup today")] = None,
    tag: Annotated[str, typer.Option(help="Subject tag")] = "MISSED PICK UP",
) -> None:
    """A person rebooked the Lidl delivery in DCT: record it and draft the note to Lidl's desk."""
    from zoneinfo import ZoneInfo

    from facility_profiles.booking.mail import LocalDraftMailer, OutboundDraft
    from facility_profiles.booking.models import BookingEvent, BookingMessage

    settings = _settings()
    if not settings.booking_customer_desk:
        typer.echo("FP_BOOKING_CUSTOMER_DESK is not set")
        raise typer.Exit(code=2)
    mailer = LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender)
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        local = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(
            tzinfo=ZoneInfo(case.vendor_timezone or "America/New_York")
        )
        previous = case.delivery_ref
        case.delivery_ref = ref.upper()
        case.delivery_at_utc = local.astimezone(UTC)
        pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
        body = "\n".join(
            [
                "Hello,",
                "",
                f"{(note or 'We missed the pickup for this today.').strip()} "
                f"I rescheduled the load in DCT for delivery on {local:%m/%d}. "
                "Can you please delete my original appointment?",
                "",
                f"New Appointment: {ref.upper()} {local:%m/%d} @ {local:%H%M}",
                "",
                "Thank you!",
                "",
                settings.booking_signature,
            ]
        )
        draft = OutboundDraft(
            to_addr=settings.booking_customer_desk,
            cc_addr=", ".join(settings.booking_cc),
            subject=f"{pos} {tag}",
            body=body,
        )
        draft_ref = mailer.create_draft(draft)
        case.messages.append(
            BookingMessage(
                case_id=case.id,
                direction="out",
                kind="notify_customer_desk",
                to_addr=draft.to_addr,
                cc_addr=draft.cc_addr,
                subject=draft.subject,
                body=draft.body,
                draft_ref=draft_ref,
            )
        )
        session.add(
            BookingEvent(
                case_id=case.id,
                action="delivery_updated",
                actor=by,
                detail={
                    "previous_ref": previous,
                    "delivery_ref": ref.upper(),
                    "draft_ref": draft_ref,
                },
            )
        )
    typer.echo(
        f"#{case_id} delivery now {ref.upper()} {date} {time}; note to Lidl drafted [{draft_ref}]"
    )


@booking_app.command("reschedule")
def booking_reschedule(
    case_id: int,
    date: Annotated[str, typer.Option(help="New pickup date, YYYY-MM-DD")],
    by: Annotated[str, typer.Option(help="Who is asking")],
    time: Annotated[str | None, typer.Option(help="New pickup time, HH:MM local")] = None,
    note: Annotated[
        str | None, typer.Option(help="One line of context, e.g. the driver fell off")
    ] = None,
) -> None:
    """Draft an in-thread request for a new pickup slot (after a missed pickup, for example)."""
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.service import reschedule_case

    settings = _settings()
    mailer = LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender)
    requested = f"{date} {time}" if time else date
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            message = reschedule_case(
                session, case, mailer, settings, requested_local=requested, by=by, note=note
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(
        f"#{case_id} reschedule drafted -> {message.to_addr}: {message.subject} "
        f"[{message.draft_ref}]"
    )


@booking_app.command("sent")
def booking_sent(
    case_id: int,
    by: Annotated[str, typer.Option(help="Who sent it")],
    thread: Annotated[str | None, typer.Option(help="Gmail thread ID, if known")] = None,
    message_id: Annotated[
        str | None,
        typer.Option(help="The sent message's RFC Message-ID, e.g. <...@mail.gmail.com>"),
    ] = None,
) -> None:
    """Record that a person sent the draft, so the reply can be matched.

    Rarely needed now: replies are matched through their In-Reply-To header, and a person's own
    send is recognised in the archive and linked to the case automatically.
    """
    from facility_profiles.booking.service import mark_sent

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            mark_sent(session, case, by=by, thread_id=thread, rfc_message_id=message_id)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(f"#{case_id} marked sent")


@booking_app.command("inbox")
def booking_inbox(
    file: Annotated[Path | None, typer.Option(help="messages.jsonl from the mail pull")] = None,
    key: Annotated[
        Path | None, typer.Option(help="Service-account key for a live Gmail read")
    ] = None,
    subject: Annotated[str | None, typer.Option(help="Mailbox to read as (Gmail)")] = None,
    s3: Annotated[
        str | None,
        typer.Option(help="Read replies from the archive: s3://bucket or s3://bucket/prefix"),
    ] = None,
    days: Annotated[int, typer.Option(help="How far back to read (Gmail or the archive)")] = 7,
    fake: Annotated[
        bool, typer.Option(help="Classify every reply as unrelated (no model)")
    ] = False,
    respond: Annotated[
        bool, typer.Option("--respond/--no-respond", help="Draft answers and counter-offers")
    ] = True,
) -> None:
    """Read replies, match them to cases, classify them, move the cases, draft answers."""
    from facility_profiles.booking.classify import (
        FakeReplyClassifier,
        OpenRouterReplyClassifier,
        ReplyClassifier,
    )
    from facility_profiles.booking.mail import GmailReader, load_messages_jsonl
    from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
    from facility_profiles.booking.service import ingest

    settings = _settings()
    if file is not None:
        messages = load_messages_jsonl(file)
    elif s3 is not None:
        from facility_profiles.mailarchive.reader import S3MailReader
        from facility_profiles.mailarchive.store import Store

        s3_bucket, _, s3_prefix = s3.removeprefix("s3://").strip("/").partition("/")
        messages = S3MailReader(Store(s3_bucket, s3_prefix)).fetch(days=days)
    elif key is not None and subject:
        group = settings.booking_sender
        messages = GmailReader(key, subject).fetch(
            f"(to:{group} OR cc:{group} OR deliveredto:{group}) newer_than:{days}d"
        )
    else:
        typer.echo("give --file messages.jsonl, --s3 s3://bucket, or --key and --subject for Gmail")
        raise typer.Exit(code=2)
    if fake:
        classifier: ReplyClassifier = FakeReplyClassifier(
            lambda _ctx: ReplyClassification(status=ReplyStatus.UNRELATED)
        )
    elif settings.llm_provider == "openrouter" and settings.openrouter_api_key is not None:
        classifier = OpenRouterReplyClassifier(
            settings.openrouter_api_key.get_secret_value(),
            model=settings.llm_model,
            base_url=settings.openrouter_base_url,
        )
    else:
        typer.echo("reply classification needs FP_LLM_PROVIDER=openrouter (or --fake)")
        raise typer.Exit(code=2)
    responder = None
    if respond:
        from facility_profiles.booking.mail import LocalDraftMailer
        from facility_profiles.booking.respond import OpenRouterAnswerComposer, Responder

        composer = None
        if not fake and settings.llm_provider == "openrouter" and settings.openrouter_api_key:
            composer = OpenRouterAnswerComposer(
                settings.openrouter_api_key.get_secret_value(),
                model=settings.llm_model,
                base_url=settings.openrouter_base_url,
            )
        responder = Responder(
            settings,
            LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender),
            composer=composer,
        )
    with session_scope(_sessions(settings)) as session:
        stats = ingest(
            session,
            messages,
            classifier,
            internal_domains=settings.internal_email_domains,
            responder=responder,
            customer_desk=settings.booking_customer_desk,
        )
    typer.echo(json.dumps(stats.__dict__, indent=2))


@booking_app.command("follow-up")
def booking_follow_up() -> None:
    """Draft one nudge for every sent request with no reply for the configured time."""
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.respond import Responder
    from facility_profiles.booking.service import list_cases

    settings = _settings()
    responder = Responder(
        settings,
        LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender),
    )
    count = 0
    with session_scope(_sessions(settings)) as session:
        for case in list_cases(session, CaseStatus.PENDING.value):
            message = responder.follow_up(session, case)
            if message is not None:
                count += 1
                typer.echo(
                    f"#{case.id} follow-up drafted -> {message.to_addr} [{message.draft_ref}]"
                )
    typer.echo(f"{count} follow-up(s) drafted")


@booking_app.command("approve")
def booking_approve(
    case_id: int,
    by: Annotated[str, typer.Option(help="Who approved")],
) -> None:
    """Approve the vendor's confirmation: the case becomes scheduled.

    Written to Transport Pro only when writes are enabled.
    """
    from facility_profiles.booking.service import approve

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            payload, written = approve(session, case, by=by, client=None)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(
        f"#{case_id} approved; Transport Pro appointment payload: {json.dumps(payload)} "
        + ("(written)" if written else "(not written: draft mode)")
    )


@booking_app.command("close")
def booking_close(
    case_id: int,
    by: Annotated[str, typer.Option(help="Who closed it")],
    reason: Annotated[str, typer.Option(help="Why")],
) -> None:
    """Cancel a case that is no longer needed. For a pickup booked another way, use `booked`."""
    from facility_profiles.booking.service import close_case

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        close_case(session, _booking_case(session, case_id), by=by, reason=reason)
    typer.echo(f"#{case_id} canceled")


@booking_app.command("booked")
def booking_booked(
    case_id: int,
    by: Annotated[str, typer.Option(help="Who booked it")],
    via: Annotated[str, typer.Option(help="How: phone, portal, email")],
    date: Annotated[str | None, typer.Option(help="Pickup date, YYYY-MM-DD")] = None,
    time: Annotated[str | None, typer.Option(help="Pickup time, HH:MM local")] = None,
    pickup_number: Annotated[str | None, typer.Option(help="Vendor pickup number")] = None,
    note: Annotated[str | None, typer.Option(help="Anything worth keeping")] = None,
) -> None:
    """Record a pickup booked outside the agent: the case becomes scheduled."""
    from facility_profiles.booking.service import mark_booked

    if time and not date:
        raise typer.BadParameter("--time needs --date")
    local = f"{date} {time}" if date and time else date
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            mark_booked(
                session,
                case,
                by=by,
                via=via,
                local=local,
                pickup_number=pickup_number,
                note=note,
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(f"#{case_id} scheduled (booked by {via})")


@booking_app.command("resolve")
def booking_resolve(
    case_id: int,
    kind: Annotated[ExceptionType, typer.Argument(help="The open exception to resolve")],
    by: Annotated[str, typer.Option(help="Who resolved it")],
    note: Annotated[str, typer.Option(help="How it was resolved")],
) -> None:
    """Resolve an open exception on a case by hand, with a note."""
    from facility_profiles.booking.worklist import resolve

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        if not resolve(session, case, [kind], resolution=note, by=by):
            typer.echo(f"#{case_id} has no open {kind.value}")
            raise typer.Exit(code=1)
    typer.echo(f"#{case_id} {kind.value} resolved")


@mail_app.command("collect")
def mail_archive_collect(
    key: Annotated[Path, typer.Option(help="Service-account JSON key (gmail.readonly delegation)")],
    subject: Annotated[
        str | None,
        typer.Option(help="Group member whose mailbox is read (FP_MAIL_ARCHIVE_GMAIL_USER)"),
    ] = None,
    bucket: Annotated[
        str | None, typer.Option(help="Archive bucket (FP_MAIL_ARCHIVE_BUCKET)")
    ] = None,
    prefix: Annotated[str | None, typer.Option(help="Key prefix (FP_MAIL_ARCHIVE_PREFIX)")] = None,
    group: Annotated[str, typer.Option(help="The group address")] = "lidl@circledelivers.com",
    days: Annotated[int, typer.Option(help="How many days back to list")] = 3,
    max_messages: Annotated[int, typer.Option("--max", help="Per-pass cap")] = 300,
    desk: Annotated[
        list[str] | None, typer.Option(help="Extra appointment-desk address (repeatable)")
    ] = None,
    verbose: Annotated[bool, typer.Option(help="Print every stored message")] = False,
) -> None:
    """One collection pass from this machine: the same code the Lambda runs every 15 minutes."""
    from facility_profiles.mailarchive import filters
    from facility_profiles.mailarchive.collector import run
    from facility_profiles.mailarchive.gmail import Delegated, load_service_account
    from facility_profiles.mailarchive.store import Store

    settings = _settings()
    bucket = bucket or settings.mail_archive_bucket
    subject = subject or settings.mail_archive_gmail_user
    if not bucket or not subject:
        typer.echo(
            "give --bucket and --subject, or set FP_MAIL_ARCHIVE_BUCKET and "
            "FP_MAIL_ARCHIVE_GMAIL_USER"
        )
        raise typer.Exit(code=2)
    store = Store(bucket, prefix if prefix is not None else settings.mail_archive_prefix)
    ok, why = store.writable()
    if not ok:
        typer.echo(why)
        raise typer.Exit(code=1)
    gmail = Delegated(load_service_account(key), subject=subject)
    stats = run(
        gmail,
        store,
        mailbox=subject,
        group=group,
        days=days,
        desks=filters.DEFAULT_DESKS | {d.strip().lower() for d in desk or [] if d.strip()},
        max_messages=max_messages,
        verbose=verbose,
    )
    typer.echo(stats.line())
    if stats.error:
        raise typer.Exit(code=1)


@mail_app.command("status")
def mail_archive_status(
    bucket: Annotated[
        str | None, typer.Option(help="Archive bucket (FP_MAIL_ARCHIVE_BUCKET)")
    ] = None,
    prefix: Annotated[str | None, typer.Option(help="Key prefix (FP_MAIL_ARCHIVE_PREFIX)")] = None,
    days: Annotated[int, typer.Option(help="Days to count messages for")] = 7,
) -> None:
    """What the archive holds: the last pass, the kept threads, messages per day."""
    from facility_profiles.mailarchive.reader import day_prefixes
    from facility_profiles.mailarchive.store import LAST_RUN_KEY, THREADS_KEY, Store

    settings = _settings()
    bucket = bucket or settings.mail_archive_bucket
    if not bucket:
        typer.echo("give --bucket or set FP_MAIL_ARCHIVE_BUCKET")
        raise typer.Exit(code=2)
    store = Store(bucket, prefix if prefix is not None else settings.mail_archive_prefix)
    last = store.get_json(LAST_RUN_KEY)
    typer.echo(
        f"last pass: {last.get('at')} as {last.get('mailbox')}" if last else "no pass recorded yet"
    )
    if last:
        typer.echo(f"  {last.get('line')}")
    threads = store.get_json(THREADS_KEY) or {}
    typer.echo(f"{len(threads)} thread(s) kept")
    for day_prefix in day_prefixes(days=days):
        count = sum(1 for k in store.list_keys(day_prefix) if k.endswith(".json"))
        typer.echo(f"  {day_prefix} {count}")


@app.command("repair-links")
def repair_links() -> None:
    """Merge duplicate stop links left by re-resolution and recount stops per facility."""
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        removed, recounted = Repository(session).repair_links()
    typer.echo(f"removed {removed} duplicate links; recounted {recounted} facilities")


@review_app.command("list")
def review_list(limit: int = 50) -> None:
    """List open review items."""
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        items = repo.list_review_items(status="open", limit=limit)
        if not items:
            typer.echo("queue is empty")
            return
        for item in items:
            facility = repo.get_facility(item.facility_key)
            name = (facility.company_name if facility else None) or item.facility_key
            typer.echo(
                f"#{item.id:<5} {name[:40]:<40} {item.role:<8} {item.field_name:<20} "
                f"proposed={json.dumps(unwrap(item.proposed))} "
                f"existing={json.dumps(unwrap(item.existing))} · {item.reason}"
            )


def _review_action(item_id: int, action: str, by: str, value: str | None = None) -> None:
    from facility_profiles.review.queue import ReviewError, ReviewService

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        service = ReviewService(Repository(session))
        try:
            if action == "accept":
                item = service.accept(item_id, by=by)
            elif action == "edit":
                item = service.edit(item_id, value or "", by=by)
            else:
                item = service.reject(item_id, by=by)
        except ReviewError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
        typer.echo(f"#{item.id} {item.field_name} -> {item.status}")


@review_app.command("accept")
def review_accept(item_id: int, by: Annotated[str, typer.Option(help="Reviewer name")]) -> None:
    """Accept the proposed value."""
    _review_action(item_id, "accept", by)


@review_app.command("edit")
def review_edit(
    item_id: int,
    value: Annotated[str, typer.Option(help="Corrected value")],
    by: Annotated[str, typer.Option(help="Reviewer name")],
) -> None:
    """Set a corrected value."""
    _review_action(item_id, "edit", by, value)


@review_app.command("reject")
def review_reject(item_id: int, by: Annotated[str, typer.Option(help="Reviewer name")]) -> None:
    """Reject the proposal."""
    _review_action(item_id, "reject", by)


@app.command()
def export(out: Annotated[Path | None, typer.Option(help="CSV path")] = None) -> None:
    """Export trusted profile values as CSV in Transport Pro field names."""
    settings = _settings()
    path = out or Path(settings.export_dir) / f"facility-profiles-{date.today().isoformat()}.csv"
    with session_scope(_sessions(settings)) as session:
        rows = export_profiles_csv(Repository(session), path)
    typer.echo(f"wrote {rows} rows to {path}")


@app.command("export-xlsx")
def export_xlsx(
    out: Annotated[Path | None, typer.Option(help="Workbook path")] = None,
    facility: Annotated[
        list[str] | None,
        typer.Option(help="Only these facilities (key, location ID or unique name); repeatable"),
    ] = None,
) -> None:
    """Export the store to Excel, with a Review Queue sheet reviewers can fill in."""
    settings = _settings()
    path = (
        out
        or Path(settings.export_dir) / f"facility-profiles-review-{date.today().isoformat()}.xlsx"
    )
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        only = {resolve_facility(repo, q).key for q in facility} if facility else None
        stats = export_workbook(session, path, only=only)
    typer.echo(
        f"wrote {path}: {stats.queue} queue items, {stats.fields} fields, "
        f"{stats.facilities} facilities, {stats.audit} audit rows"
    )


@review_app.command("import")
def review_import(
    file: Annotated[Path, typer.Argument(help="Filled-in workbook from export-xlsx")],
    by: Annotated[str | None, typer.Option(help="Reviewer name for rows without one")] = None,
    dry_run: Annotated[bool, typer.Option(help="Show what would be applied")] = False,
) -> None:
    """Apply the decisions typed into the Review Queue sheet."""
    from facility_profiles.review.xlsx_import import apply_decisions, read_decisions

    settings = _settings()
    decisions, errors = read_decisions(file)
    for err in errors:
        typer.echo(f"skipped: {err}")
    if not decisions:
        typer.echo("no decisions found in the sheet")
        raise typer.Exit(code=1 if errors else 0)
    with session_scope(_sessions(settings)) as session:
        result = apply_decisions(
            Repository(session), decisions, default_reviewer=by, dry_run=dry_run
        )
    for err in result.errors:
        typer.echo(f"skipped: {err}")
    label = "would apply" if dry_run else "applied"
    typer.echo(f"{label} {result.total_applied} decision(s): {result.applied}")


@app.command()
def digest(
    out: Annotated[Path | None, typer.Option(help="Write the digest to this file too")] = None,
) -> None:
    """Render the daily digest for the pod lead."""
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        text = render_digest(repo, repo.latest_run(), now=datetime.now(tz=UTC))
    if out:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    typer.echo(text)


if __name__ == "__main__":  # pragma: no cover
    app()
