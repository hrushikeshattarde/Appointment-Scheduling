"""Command-line interface.

facility-profiles init-db
facility-profiles check-tpro
facility-profiles harvest --terminal 1160 --days 90
facility-profiles harvest --customer lidl --days 90     (or --customer 7211 --terminal 1089)
facility-profiles customers list | show lidl | new acme --name Acme --tpro-customer 1234
facility-profiles access list | grant am@circledelivers.com lidl --act --by name | revoke ...
facility-profiles run --no-harvest --cap 50
facility-profiles lookup 196508
facility-profiles review list | accept 12 --by name | edit 12 --value ... | reject 12
facility-profiles export
facility-profiles digest
"""

from __future__ import annotations

import atexit
import json
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy.orm import Session

from facility_profiles import __version__
from facility_profiles.booking.models import CaseStatus, ExceptionType
from facility_profiles.clock import EASTERN, eastern_to_local, stamp
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
template_app = typer.Typer(
    help="The agent's email wording, per desk or customer, with fill-in fields.",
    no_args_is_help=True,
)
booking_app.add_typer(template_app, name="template")
mail_app = typer.Typer(
    help="Group-mail archive in S3: collect Pick Up Appointment threads, check what is there.",
    no_args_is_help=True,
)
app.add_typer(mail_app, name="mail-archive")
customers_app = typer.Typer(
    help="Customer files: whose loads, which mailbox, their own desk, their numbers.",
    no_args_is_help=True,
)
app.add_typer(customers_app, name="customers")
access_app = typer.Typer(
    help="Who sees which customer on the appointments board (admins: FP_BOARD_ADMINS).",
    no_args_is_help=True,
)
app.add_typer(access_app, name="access")

log = get_logger(__name__)

CustomerScope = Annotated[
    list[str] | None,
    typer.Option(
        "--customer",
        help="A customer file's key (lidl) or a Transport Pro customer ID; repeatable. "
        "Default: FP_CUSTOMERS, else FP_PILOT_CUSTOMER_IDS",
    ),
]


def _settings() -> Settings:
    settings = get_settings()
    configure_logging(settings.log_level, json=settings.log_json)
    return settings


def _sessions(settings: Settings):  # type: ignore[no-untyped-def]  # sessionmaker generic is verbose
    engine = make_engine(settings.database_url)
    init_db(engine)
    atexit.register(engine.dispose)
    return session_factory(engine)


def _scope(
    settings: Settings,
    customer: list[str] | None,
    terminal: list[int] | None,
    *,
    booking: bool = False,
) -> tuple[list[int], list[int]]:
    """Terminals and Transport Pro customer ids from --customer/--terminal and the settings."""
    from facility_profiles.customers import CustomerFileError, scope

    try:
        return scope(settings, customer, terminal, booking=booking)
    except CustomerFileError as exc:
        raise typer.BadParameter(str(exc)) from exc


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


def _serve_scan_scope(
    settings: Settings, customer: list[str] | None
) -> tuple[list[int], list[int]]:
    """Whose loads ``serve --scan-every`` checks.

    It refuses to start without a pod or a customer: that scan would take every load in
    Transport Pro.
    """
    chosen = _scope(settings, customer, None, booking=True)
    if not any(chosen):
        typer.echo(
            "--scan-every needs to know whose loads to check: --scan-customer lidl, or "
            "FP_CUSTOMERS (or FP_PILOT_TERMINAL_IDS) in .env"
        )
        raise typer.Exit(code=2)
    return chosen


def _say_loops(
    settings: Settings,
    timers_every: float,
    autopilot_every: float,
    mail_every: float,
    scan_every: float,
    scan_scope: tuple[list[int], list[int]],
) -> None:
    """Say what the board does on its own besides serving, and refuse what it cannot do."""
    if timers_every > 0:
        typer.echo(f"booking timers run every {timers_every:g} min")
    if autopilot_every > 0:
        typer.echo(f"the agent runs on its own every {autopilot_every:g} min (booking run)")
    if mail_every > 0:
        if not settings.booking_inbox:
            typer.echo("--mail-every needs FP_BOOKING_INBOX (s3://bucket or gmail) to read")
            raise typer.Exit(code=2)
        typer.echo(
            f"the group mail ({settings.booking_inbox}) is read onto the board every "
            f"{mail_every:g} min; nothing is drafted or sent"
        )
    if scan_every > 0:
        terminals, customer_ids = scan_scope
        whose = "; ".join(
            f"{label} {', '.join(map(str, ids))}"
            for label, ids in (("terminals", terminals), ("customers", customer_ids))
            if ids
        )
        typer.echo(
            f"Transport Pro is checked for new pickups every {scan_every:g} min ({whose}); "
            f"pickups up to {settings.booking_days_ahead} days ahead"
        )


@app.command()
def serve(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8000,
    db: Annotated[
        str | None, typer.Option(help="Store to serve, e.g. sqlite:///./data/<pod>.db")
    ] = None,
    timers_every: Annotated[
        float,
        typer.Option(
            help="Run the booking timers every this many minutes (no reply, pickup passed); "
            "0 turns them off"
        ),
    ] = 15,
    autopilot_every: Annotated[
        float,
        typer.Option(
            help="Run the agent on its own every this many minutes, by each customer's rules "
            "(drafts; sends only where a rule and send mode say so); 0, the default, is off"
        ),
    ] = 0,
    scan_every: Annotated[
        float,
        typer.Option(
            help="Check Transport Pro for new and changed pickups every this many minutes "
            "(booking scan; reads only); 0, the default, is off"
        ),
    ] = 0,
    mail_every: Annotated[
        float,
        typer.Option(
            help="Read the customers' group mail onto the board every this many minutes "
            "(FP_BOOKING_INBOX; reads only: nothing is drafted or sent); 0, the default, is off"
        ),
    ] = 0,
    scan_customer: Annotated[
        list[str] | None,
        typer.Option(
            "--scan-customer",
            help="Whose loads --scan-every checks: a customer file's key (lidl) or a Transport "
            "Pro customer ID; repeatable. Default: FP_CUSTOMERS, else FP_PILOT_TERMINAL_IDS and "
            "FP_PILOT_CUSTOMER_IDS",
        ),
    ] = None,
    no_sign_in: Annotated[
        bool,
        typer.Option(
            "--no-sign-in",
            help="Leave Google sign-in off for this run, though .env sets it up (for testing "
            "on this machine only; refused with another --host)",
        ),
    ] = False,
) -> None:
    """Run the appointments board (/app/) and the HTTP API. Needs the api extra."""
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("the board needs the api extra: uv sync --extra api")
        raise typer.Exit(code=2) from exc
    from facility_profiles.access import signin_enabled
    from facility_profiles.api.app import create_app

    settings = _settings()
    if db:
        settings = settings.model_copy(update={"database_url": db})
    local = host in {"127.0.0.1", "localhost", "::1"}
    if no_sign_in:
        if not local:
            typer.echo("--no-sign-in is for this machine only: leave --host at 127.0.0.1")
            raise typer.Exit(code=2)
        settings = settings.model_copy(update={"google_client_id": None})
    if signin_enabled(settings) and not settings.board_admins:
        typer.echo("Google sign-in is on: set FP_BOARD_ADMINS to your email first")
        raise typer.Exit(code=2)
    scan_scope = _serve_scan_scope(settings, scan_customer) if scan_every > 0 else ([], [])
    typer.echo(f"appointments board: http://{host}:{port}/app/  (store {settings.database_url})")
    if no_sign_in:
        typer.echo("sign-in off for this run (--no-sign-in): this machine only, every customer")
    if signin_enabled(settings):
        base = settings.board_public_url or f"http://{'localhost' if local else host}:{port}"
        typer.echo(
            f"Google sign-in on; admins {', '.join(settings.board_admins)}; "
            f"Google sends people back to {base}/auth/callback"
        )
    elif not local:
        typer.echo(
            "warning: no sign-in (FP_GOOGLE_CLIENT_ID and FP_GOOGLE_CLIENT_SECRET unset), so "
            "anyone who can reach this address sees every customer"
        )
    _say_loops(settings, timers_every, autopilot_every, mail_every, scan_every, scan_scope)
    app_ = create_app(
        settings,
        timers_every=timers_every if timers_every > 0 else None,
        autopilot_every=autopilot_every if autopilot_every > 0 else None,
        scan_every=scan_every if scan_every > 0 else None,
        scan_scope=scan_scope,
        mail_every=mail_every if mail_every > 0 else None,
    )
    uvicorn.run(app_, host=host, port=port, log_level="warning")


@app.command("serve-links")
def serve_links(
    host: Annotated[str, typer.Option(help="Interface to listen on")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="Port to listen on")] = 8010,
    db: Annotated[str | None, typer.Option(help="Store the links answer into")] = None,
) -> None:
    """Serve only the vendors' click-to-confirm pages (/c/...), no board and no API.

    This is what goes behind FP_BOOKING_LINK_BASE_URL. Making it reachable from outside is a
    deployment decision; by default it listens on this machine only.
    """
    try:
        import uvicorn
    except ImportError as exc:
        typer.echo("the pages need the api extra: uv sync --extra api")
        raise typer.Exit(code=2) from exc
    from facility_profiles.api.links import create_links_app
    from facility_profiles.booking.links import links_enabled

    settings = _settings()
    if db:
        settings = settings.model_copy(update={"database_url": db})
    if not links_enabled(settings):
        typer.echo("set FP_BOOKING_LINK_BASE_URL and FP_BOOKING_LINK_SECRET first")
        raise typer.Exit(code=2)
    typer.echo(
        f"vendor link pages on http://{host}:{port}/c/...  (links say "
        f"{settings.booking_link_base_url}; store {settings.database_url})"
    )
    uvicorn.run(create_links_app(settings), host=host, port=port, log_level="warning")


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
    customer: CustomerScope = None,
    days: Annotated[int | None, typer.Option(help="Look-back window in days")] = None,
) -> None:
    """Pull loads and store facilities, links and stop notes (FR-1, FR-2)."""
    settings = _settings()
    terminal, customer_ids = _scope(settings, customer, terminal)
    end = datetime.now(tz=UTC).date()
    start = end - timedelta(days=days or settings.lookback_days)
    with _client(settings) as client:
        pipeline = Pipeline(
            settings, _sessions(settings), client=client, extractor=_extractor(settings, fake=True)
        )
        stats = pipeline.harvest(
            terminal_ids=terminal, customer_ids=customer_ids, start=start, end=end
        )
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
    customer: CustomerScope = None,
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
    terminal, customer_ids = _scope(settings, customer, terminal)
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
            customer_ids=customer_ids,
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
        unblocked: list[int] = []
        if role is Role.SHIPPER and field in ("booking_method", "contact_email"):
            from facility_profiles.booking.memory import apply_desk_to_waiting_cases

            unblocked = apply_desk_to_waiting_cases(session, record.key, by=by)
    typer.echo(
        f"{name} [{role.value}] {field} = {json.dumps(typed)} (was {json.dumps(before)})"
        + (f"; closed {closed} open review item(s)" if closed else "")
        + (
            f"; case(s) {', '.join(f'#{i}' for i in unblocked)} now have a desk"
            if unblocked
            else ""
        )
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


@profile_app.command("portals")
def profile_portals(
    apply: Annotated[
        bool, typer.Option(help="File the changes; without it, only list them")
    ] = False,
    by: Annotated[str, typer.Option(help="Who is filing them (kept in the audit log)")] = "",
) -> None:
    """Fix portal vendors their portal URL contradicts ("other" for a Costco or UNFI portal).

    Lists what would change; --apply --by NAME files it. A vendor a person set is never changed.
    """
    from facility_profiles.pipeline.profile import apply_portal_fixes, portal_vendor_fixes

    if apply and not by.strip():
        raise typer.BadParameter("--apply needs --by NAME")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        repo = Repository(session)
        fixes = portal_vendor_fixes(repo)
        for fix in fixes:
            record = repo.get_facility(fix.key)
            name = record.company_name if record else fix.key
            note = "  (kept: a person set it)" if fix.held else ""
            typer.echo(f"{name} [{fix.role.value}] {fix.url}: {fix.before} -> {fix.after}{note}")
        changed = apply_portal_fixes(repo, fixes, by=by.strip()) if apply else 0
    held = sum(1 for f in fixes if f.held)
    if apply:
        typer.echo(f"{changed} portal vendor(s) filed, {held} kept as a person set them")
    else:
        typer.echo(f"{len(fixes) - held} to change, {held} kept; run again with --apply --by NAME")


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
    customer: CustomerScope = None,
    days_ahead: Annotated[int | None, typer.Option(help="Pickup window in days")] = None,
) -> None:
    """Open a booking case for every pickup stop that still needs an appointment."""
    from facility_profiles.booking.service import scan

    settings = _settings()
    terminal, customer_ids = _scope(settings, customer, terminal, booking=True)
    with _client(settings) as client:
        stats = scan(
            client,
            _sessions(settings),
            settings,
            terminal_ids=terminal,
            customer_ids=customer_ids,
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
                f"  {stamp(e.created_at, '%Y-%m-%d %H:%M')} {e.action} by {e.actor} "
                f"{json.dumps(e.detail)[:160]}"
            )


@booking_app.command("draft")
def booking_draft(
    case_id: Annotated[
        int | None, typer.Argument(help="Case to draft; omit for all new cases")
    ] = None,
) -> None:
    """Compose the request emails as drafts, one per vendor desk (.eml in the drafts folder).

    Each desk's rules are checked first: a request past its cut-off or missing a number the
    desk needs becomes a to-do, and one the desk would not book yet waits.
    """
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.service import draft_batch, draft_case, prepare_drafts

    settings = _settings()
    mailer = LocalDraftMailer(
        Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
    )
    now = datetime.now(tz=UTC)
    with session_scope(_sessions(settings)) as session:
        if case_id is not None:
            try:
                messages = [draft_case(session, _booking_case(session, case_id), mailer, settings)]
            except ValueError as exc:
                typer.echo(f"#{case_id}: {exc}")
                raise typer.Exit(code=1) from exc
        else:
            ready, waiting = prepare_drafts(session, settings, now=now)
            for case, why in waiting:
                typer.echo(f"#{case.id} waits: {why}")
            messages = draft_batch(session, ready, mailer, settings, now=now)
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
    from facility_profiles.booking.service import draft_batch, draft_case, prepare_drafts

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
                ready, waiting = prepare_drafts(session, settings, now=datetime.now(tz=UTC))
                for case, why in waiting:
                    typer.echo(f"#{case.id} waits: {why}")
                messages = draft_batch(session, ready, sender, settings, by=by)
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
    ref: Annotated[
        str, typer.Option(help="New delivery reference, e.g. Lidl's DCT ref FRG_200526615")
    ],
    date: Annotated[str, typer.Option(help="New delivery date, YYYY-MM-DD")],
    time: Annotated[str, typer.Option(help="New delivery time, HH:MM Eastern")],
    by: Annotated[str, typer.Option(help="Who rebooked the delivery")],
    note: Annotated[str | None, typer.Option(help="Why, e.g. We missed the pickup today")] = None,
    tag: Annotated[str, typer.Option(help="Subject tag")] = "MISSED PICK UP",
) -> None:
    """A person rebooked the customer's delivery: record it and draft the note to their desk.

    The desk and the booking system's name come from the case's customer file (Lidl: inbound@
    and DCT).
    """
    from facility_profiles.booking.mail import LocalDraftMailer, OutboundDraft
    from facility_profiles.booking.models import BookingEvent, BookingMessage
    from facility_profiles.booking.references import ReferenceSource, record_reference
    from facility_profiles.customers import customer_of
    from facility_profiles.domain.schema import ReferenceType

    settings = _settings()
    mailer = LocalDraftMailer(
        Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
    )
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        customer = customer_of(case, settings)
        if not customer.customer_desk:
            typer.echo(
                f"no customer desk for {customer.label(case.customer_name)}: set "
                f"[customer_desk] email in {customer.source}"
            )
            raise typer.Exit(code=2)
        where = f" in {customer.delivery_system}" if customer.delivery_system else ""
        local = datetime.strptime(f"{date} {time}", "%Y-%m-%d %H:%M").replace(tzinfo=EASTERN)
        previous = case.delivery_ref
        record_reference(
            session,
            case,
            ReferenceType.DELIVERY_NUMBER.value,
            ref,
            source=ReferenceSource.PERSON,
            by=by,
        )
        case.delivery_at_utc = local.astimezone(UTC)
        pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
        body = "\n".join(
            [
                "Hello,",
                "",
                f"{(note or 'We missed the pickup for this today.').strip()} "
                f"I rescheduled the load{where} for delivery on {local:%m/%d}. "
                "Can you please delete my original appointment?",
                "",
                f"New Appointment: {ref.upper()} {local:%m/%d} @ {local:%H%M}",
                "",
                "Thank you!",
                "",
                customer.signature or settings.booking_signature,
            ]
        )
        draft = OutboundDraft(
            to_addr=customer.customer_desk,
            cc_addr=customer.cc_header,
            subject=f"{pos} {tag}",
            body=body,
            from_addr=customer.sender,
            reply_to=customer.group,
        )
        label = customer.label(case.customer_name)
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
        f"#{case_id} delivery now {ref.upper()} {date} {time} ET; note to {label} drafted "
        f"[{draft_ref}]"
    )


@booking_app.command("reschedule")
def booking_reschedule(
    case_id: int,
    date: Annotated[str, typer.Option(help="New pickup date, YYYY-MM-DD")],
    by: Annotated[str, typer.Option(help="Who is asking")],
    time: Annotated[str | None, typer.Option(help="New pickup time, HH:MM Eastern")] = None,
    note: Annotated[
        str | None, typer.Option(help="One line of context, e.g. the driver fell off")
    ] = None,
) -> None:
    """Draft an in-thread request for a new pickup slot (after a missed pickup, for example).

    The date and time are Eastern, like every time the agent shows and writes.
    """
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.service import reschedule_case

    settings = _settings()
    mailer = LocalDraftMailer(
        Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
    )
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        requested = eastern_to_local(f"{date} {time}" if time else date, case.vendor_timezone)
        assert requested is not None
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
        from facility_profiles.booking.inbox import group_query

        query = group_query(settings, days)
        if query is None:
            typer.echo("no group to read: set [mail] group in a customer file")
            raise typer.Exit(code=2)
        messages = GmailReader(key, subject).fetch(query)
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
    with ExitStack() as stack:
        if respond:
            from facility_profiles.booking.mail import LocalDraftMailer
            from facility_profiles.booking.respond import Responder
            from facility_profiles.booking.writer import OpenRouterReplyWriter

            writer = facts = None
            if not fake and settings.llm_provider == "openrouter" and settings.openrouter_api_key:
                writer = OpenRouterReplyWriter(
                    settings.openrouter_api_key.get_secret_value(),
                    model=settings.llm_model,
                    base_url=settings.openrouter_base_url,
                )
                facts = stack.enter_context(_client(settings))  # the load's facts, read only
            responder = Responder(
                settings,
                LocalDraftMailer(
                    Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
                ),
                writer=writer,
                facts=facts,
            )
        session = stack.enter_context(session_scope(_sessions(settings)))
        stats = ingest(
            session,
            messages,
            classifier,
            internal_domains=settings.internal_email_domains,
            responder=responder,
            settings=settings,
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
        LocalDraftMailer(Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""),
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

    The slot is queued for Transport Pro; ``booking writeback`` writes it while
    FP_BOOKING_TPRO_WRITEBACK is on.
    """
    from facility_profiles.booking.service import approve

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            payload, _written = approve(session, case, by=by)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    state = (
        "queued; booking writeback sends it"
        if settings.booking_tpro_writeback
        else "write-back is off: enter it in Transport Pro by hand"
    )
    typer.echo(f"#{case_id} approved; Transport Pro appointment {json.dumps(payload)} ({state})")


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
    time: Annotated[str | None, typer.Option(help="Pickup time, HH:MM Eastern")] = None,
    pickup_number: Annotated[str | None, typer.Option(help="Vendor pickup number")] = None,
    note: Annotated[str | None, typer.Option(help="Anything worth keeping")] = None,
    desk: Annotated[
        str | None,
        typer.Option(help="The email, phone or portal address it was booked with (remembered)"),
    ] = None,
) -> None:
    """Record a pickup booked outside the agent: the case becomes scheduled.

    The facility remembers how it was booked; a desk its profile lacked is filed there, and its
    other cases waiting for a desk take it.
    """
    from facility_profiles.booking.service import mark_booked

    if time and not date:
        raise typer.BadParameter("--time needs --date")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        local = eastern_to_local(f"{date} {time}" if date and time else date, case.vendor_timezone)
        try:
            learned = mark_booked(
                session,
                case,
                by=by,
                via=via,
                local=local,
                pickup_number=pickup_number,
                note=note,
                desk=desk,
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(f"#{case_id} scheduled (booked by {via})")
    if learned.filled:
        typer.echo(f"  the vendor profile learned its {', '.join(learned.filled)}")
    if learned.unblocked:
        typer.echo(f"  case(s) {', '.join(f'#{i}' for i in learned.unblocked)} now have a desk")


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


@booking_app.command("ref")
def booking_ref(
    case_id: int,
    kind: Annotated[
        str,
        typer.Argument(
            help="shipment_number, sales_order_number, bol_number, delivery_number, ..."
        ),
    ],
    value: Annotated[str, typer.Argument(help="The number, e.g. Lidl's TI shipment number")],
    by: Annotated[str, typer.Option(help="Who added it")],
) -> None:
    """Add a number the vendor's desk needs to a case; the request is drafted with it."""
    from facility_profiles.booking.rules import REFERENCE_NAMES
    from facility_profiles.booking.service import add_reference

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        try:
            missing = add_reference(session, case, kind, value, by=by)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    still = f"; still missing: {', '.join(REFERENCE_NAMES[m] for m in missing)}" if missing else ""
    typer.echo(f"#{case_id} {kind} = {value.strip()}{still}")


DeskOption = Annotated[str | None, typer.Option(help="For this booking desk's address only")]
CustomerOption = Annotated[
    str | None,
    typer.Option(
        help="For this customer only: its key (lidl), or a Transport Pro name ('Lidl - Inbound')"
    ),
]


def _template_kind(kind: str):  # type: ignore[no-untyped-def]
    from facility_profiles.booking.templates import TemplateKind

    try:
        return TemplateKind(kind)
    except ValueError as exc:
        kinds = ", ".join(k.value for k in TemplateKind)
        raise typer.BadParameter(f"kind must be one of {kinds}") from exc


@template_app.command("fields")
def template_fields_cmd() -> None:
    """Every fill-in field, what it becomes, and which emails can use it."""
    from facility_profiles.booking.templates import FIELDS, KIND_FIELDS

    for name, meaning in FIELDS.items():
        kinds = [k.value for k, allowed in KIND_FIELDS.items() if name in allowed]
        typer.echo(f"{{{name}}}  {meaning}  [{', '.join(kinds)}]")


@template_app.command("list")
def template_list() -> None:
    """The saved templates; every other email uses the built-in wording."""
    from facility_profiles.booking.templates import saved_templates

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        rows = saved_templates(session)
        if not rows:
            typer.echo("no saved templates: the agent writes with the built-in wording")
            return
        for row in rows:
            scope = row.scope if row.scope == "default" else f"{row.scope} {row.match}"
            when = f"{row.updated_at:%Y-%m-%d}" if row.updated_at else ""
            typer.echo(f"{row.kind:<14} {scope:<48} by {row.updated_by or '?'} {when}")


@template_app.command("show")
def template_show(
    kind: Annotated[
        str, typer.Argument(help="request, batch_request, reschedule, follow_up, check_back")
    ],
    desk: DeskOption = None,
    customer: CustomerOption = None,
) -> None:
    """The template that applies to a desk or customer (and where it comes from)."""
    from facility_profiles.booking.templates import pick

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        template = pick(session, _template_kind(kind), desk=desk, customer=customer)
    typer.echo(f"# {template.kind.value} ({template.source})")
    if template.subject:
        typer.echo(f"Subject: {template.subject}")
    typer.echo("")
    typer.echo(template.body)


@template_app.command("set")
def template_set(
    kind: Annotated[
        str, typer.Argument(help="request, batch_request, reschedule, follow_up, check_back")
    ],
    by: Annotated[str, typer.Option(help="Who wrote it")],
    body: Annotated[str | None, typer.Option(help=r"The body; write \n for a line break")] = None,
    body_file: Annotated[Path | None, typer.Option(help="Read the body from this file")] = None,
    subject: Annotated[str | None, typer.Option(help="The subject (requests only)")] = None,
    desk: DeskOption = None,
    customer: CustomerOption = None,
) -> None:
    """Save the wording for one kind of email, for a desk, a customer or (neither) the whole pod.

    Fields in braces are filled in: {po}, {date}, {lines}... (see `booking template fields`).
    """
    from facility_profiles.booking.templates import save_template

    if (body is None) == (body_file is None):
        raise typer.BadParameter("give the body with --body or --body-file")
    text = body_file.read_text(encoding="utf-8") if body_file else (body or "").replace("\\n", "\n")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        try:
            template = save_template(
                session,
                _template_kind(kind),
                body=text,
                subject=subject,
                desk=desk,
                customer=customer,
                by=by,
            )
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"{template.kind.value} template saved for {template.source}")


@template_app.command("remove")
def template_remove(
    kind: Annotated[
        str, typer.Argument(help="request, batch_request, reschedule, follow_up, check_back")
    ],
    desk: DeskOption = None,
    customer: CustomerOption = None,
) -> None:
    """Remove a saved template; the next one down (customer, default, built-in) applies again."""
    from facility_profiles.booking.templates import remove_template

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        removed = remove_template(session, _template_kind(kind), desk=desk, customer=customer)
    if not removed:
        typer.echo("no such saved template")
        raise typer.Exit(code=1)
    typer.echo(f"{kind} template removed")


@template_app.command("preview")
def template_preview(
    case_id: int,
    kind: Annotated[
        str, typer.Option(help="request, reschedule, follow_up or check_back")
    ] = "request",
) -> None:
    """Show the email the agent would write for a case now; nothing is drafted or sent."""
    from facility_profiles.booking.links import link_lines, links_enabled, offered_slots
    from facility_profiles.booking.rules import vendor_profile
    from facility_profiles.booking.templates import (
        TemplateKind,
        case_values,
        pick,
        render,
        request_values,
        reschedule_values,
        with_links,
    )
    from facility_profiles.booking.timers import fmt_slot
    from facility_profiles.customers import customer_of

    which = _template_kind(kind)
    if which == TemplateKind.BATCH_REQUEST:
        raise typer.BadParameter("a batch is previewed one case at a time: use request")
    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        names = customer_of(case, settings).template_matches(case.customer_name)
        template = pick(session, which, desk=case.contact_email, customer=names)
        links = ""
        if links_enabled(settings) and which in (TemplateKind.REQUEST, TemplateKind.RESCHEDULE):
            slots = offered_slots(case, settings, profile, now=datetime.now(tz=UTC))
            if slots:
                url = f"{settings.booking_link_base_url}/c/(made when drafted)"
                links = link_lines([case], {case.id: url})
                links += "\n(offers " + ", ".join(fmt_slot(slot) for slot in slots) + ")"
                template = with_links(template)
        if which == TemplateKind.REQUEST:
            values = request_values([case], settings, profile, links=links)
        elif which == TemplateKind.RESCHEDULE:
            current = case.confirmed_local or case.requested_local
            values = reschedule_values(
                case, settings, profile, previous=current, note=None, links=links
            )
        else:
            values = case_values([case], settings, profile)
        subject, text = render(template, values)
        to = case.contact_email or "(no desk)"
    typer.echo(f"# {which.value} for case #{case_id} ({template.source} template)")
    typer.echo(f"To: {to}")
    if subject:
        typer.echo(f"Subject: {subject}")
    typer.echo("")
    typer.echo(text)


@booking_app.command("run")
def booking_run(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Show what the pass would do; change and write nothing"),
    ] = False,
) -> None:
    """One pass of the agent on its own, by each customer's rules.

    With FP_BOOKING_INBOX the new replies are read first and answered by the customer's rules
    (sent where ``replies = "send"``, booked where ``confirm = "auto"``). Then every open request
    is planned and the due ones are written: drafts, sent only where a rule and send mode say so.
    A dry run reads and answers too, then rolls everything back: nothing is sent or kept.
    """
    from facility_profiles.booking.automation import run_once
    from facility_profiles.booking.inbox import inbox_from_settings, reader_tools
    from facility_profiles.booking.mail import GmailSender, LocalDraftMailer, RecordingMailer

    settings = _settings()
    mailer = (
        RecordingMailer()
        if dry_run
        else LocalDraftMailer(
            Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
        )
    )
    sender = None
    if (
        not dry_run
        and settings.booking_mode == "send"
        and settings.booking_gmail_key
        and settings.booking_gmail_user
    ):
        user = settings.booking_gmail_user
        sender = GmailSender(Path(settings.booking_gmail_key), user, user)
    with ExitStack() as stack:
        session = stack.enter_context(session_scope(_sessions(settings)))
        client = None
        if settings.booking_tpro_writeback and not dry_run:
            client = stack.enter_context(_client(settings, allow_writes=True))
        inbox = inbox_from_settings(settings)
        classifier, writer = reader_tools(settings) if inbox is not None else (None, None)
        facts = client
        if facts is None and writer is not None:  # the load's facts for the answers, read only
            facts = stack.enter_context(_client(settings))
        report = run_once(
            session,
            settings,
            now=datetime.now(tz=UTC),
            mailer=mailer,
            sender=sender,
            client=client,
            inbox=inbox,
            classifier=classifier,
            writer=writer,
            facts=facts,
        )
        for line in report.lines:
            typer.echo(line)
        typer.echo(json.dumps(report.counts()))
        if dry_run:
            session.rollback()
            typer.echo("dry run: nothing was changed and no draft was written")


@booking_app.command("writeback")
def booking_writeback(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Read the loads and say what would be written; write nothing"
        ),
    ] = False,
) -> None:
    """Write booked pickups to Transport Pro (FP_BOOKING_TPRO_WRITEBACK must be on).

    Each load is read first: the same time already there is not sent again, a different
    confirmed time is never overwritten (a to-do instead), and every write is read back.
    """
    from facility_profiles.booking.writeback import write_appointments

    settings = _settings()
    live = settings.booking_tpro_writeback and not dry_run
    if not settings.booking_tpro_writeback and not dry_run:
        typer.echo("FP_BOOKING_TPRO_WRITEBACK is off: nothing is written (--dry-run reads only)")
    with session_scope(_sessions(settings)) as session:
        if settings.booking_tpro_writeback or dry_run:
            with _client(settings, allow_writes=live) as client:
                report = write_appointments(
                    session, settings, client, now=datetime.now(tz=UTC), dry_run=dry_run
                )
        else:
            report = write_appointments(session, settings, None, now=datetime.now(tz=UTC))
        for line in report.lines:
            typer.echo(line)
        typer.echo(json.dumps(report.counts()))
        if dry_run:
            session.rollback()
            typer.echo("dry run: nothing was written to Transport Pro or changed here")


@booking_app.command("jobs")
def booking_jobs(
    case_id: Annotated[int | None, typer.Argument(help="One case only")] = None,
    status: Annotated[str | None, typer.Option(help="Only jobs in this status")] = None,
) -> None:
    """What the agent planned and did on its own: each job, its rule, status and why."""
    from sqlalchemy import select

    from facility_profiles.booking.models import AutomationJob

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        stmt = select(AutomationJob).order_by(AutomationJob.case_id, AutomationJob.id)
        if case_id is not None:
            stmt = stmt.where(AutomationJob.case_id == case_id)
        if status:
            stmt = stmt.where(AutomationJob.status == status)
        jobs = list(session.scalars(stmt))
        if not jobs:
            typer.echo("no jobs")
            return
        for job in jobs:
            due = stamp(job.due_at) if job.due_at else "-"
            tries = f" tries {job.attempts}" if job.attempts else ""
            typer.echo(
                f"#{job.case_id:<4} {job.kind:<9} {job.status:<9} {job.action:<5} "
                f"{job.rule[:28]:<28} due {due}{tries}  {job.reason or ''}"
            )


@booking_app.command("recommend")
def booking_recommend(case_id: int) -> None:
    """Which pickup time the agent would ask for now, and why (nothing is changed)."""
    from facility_profiles.booking.recommend import facility_history, recommend_time, usual_time
    from facility_profiles.booking.rules import vendor_profile
    from facility_profiles.booking.timers import fmt_slot

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        profile = (
            vendor_profile(Repository(session), case.facility_key) if case.facility_key else None
        )
        history = facility_history(session, case.facility_key)
        rec = recommend_time(case, settings, profile, now=datetime.now(tz=UTC), history=history)
        usual = usual_time(history)
        tz = case.vendor_timezone
        typer.echo(f"#{case_id} asks for {fmt_slot(case.requested_local, tz)} now")
        typer.echo(f"recommended: {fmt_slot(rec.local, tz)}  ({rec.verdict})")
        for step in rec.steps:
            typer.echo(f"  {'*' if step.moved else '-'} {step.rule:<8} {step.note}")
        if rec.latest:
            typer.echo(f"  latest pickup that makes the delivery: {fmt_slot(rec.latest, tz)}")
        shown = f"{usual.clock} ({usual.count} of {usual.total})" if usual else "none yet"
        typer.echo(f"facility history: {len(history)} confirmed time(s); usual {shown}")
        session.rollback()


@booking_app.command("links")
def booking_links(case_id: int) -> None:
    """The times a case's requests offered by link, the links, and what the vendor did."""
    from facility_profiles.booking.links import LinkError, offer_state, offer_url
    from facility_profiles.booking.timers import fmt_slot

    settings = _settings()
    now = datetime.now(tz=UTC)
    with session_scope(_sessions(settings)) as session:
        case = _booking_case(session, case_id)
        if not case.offers:
            typer.echo(f"#{case_id} has offered no times by link")
            return
        for offer in case.offers:
            state = offer_state(offer, now)
            answer = f" -> {offer.answer}" if offer.answer else ""
            typer.echo(
                f"offer {offer.id} ({state}{answer}), expires {stamp(offer.expires_at)}: "
                + ", ".join(fmt_slot(str(slot), case.vendor_timezone) for slot in offer.slots)
            )
            try:
                typer.echo(f"  {offer_url(offer, settings)}")
            except LinkError as exc:
                typer.echo(f"  (no link: {exc})")


@booking_app.command("unmatched")
def booking_unmatched(
    show_all: Annotated[bool, typer.Option("--all", help="Linked and dismissed ones too")] = False,
) -> None:
    """Booking mail no pickup matched: kept for a person to link to its pickup or dismiss."""
    from sqlalchemy import select

    from facility_profiles.booking.models import UnmatchedMail
    from facility_profiles.booking.unmatched import open_unmatched

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        items = (
            list(session.scalars(select(UnmatchedMail).order_by(UnmatchedMail.sent_at.desc())))
            if show_all
            else open_unmatched(session)
        )
        if not items:
            typer.echo("no unmatched mail")
            return
        for item in items:
            first = next((ln.strip() for ln in (item.body or "").splitlines() if ln.strip()), "")
            where = f" -> case #{item.case_id}" if item.case_id else ""
            typer.echo(
                f"mail {item.id:<4} {item.status:<9} {stamp(item.sent_at)}  {item.from_addr}  "
                f"{item.subject!r}  [{item.reason}]{where}"
            )
            if first:
                typer.echo(f"           {first[:120]}")


@booking_app.command("link-mail")
def booking_link_mail(
    mail_id: int,
    case_id: int,
    by: Annotated[str, typer.Option(help="Who links it")],
) -> None:
    """Tie an unmatched email to its pickup; the agent reads it as that pickup's reply.

    Answers are drafted only: a person sends them.
    """
    from facility_profiles.booking.inbox import reader_tools
    from facility_profiles.booking.mail import LocalDraftMailer
    from facility_profiles.booking.models import UnmatchedMail
    from facility_profiles.booking.respond import Responder
    from facility_profiles.booking.unmatched import link_unmatched

    settings = _settings()
    classifier, writer = reader_tools(settings)
    with ExitStack() as stack:
        responder = Responder(
            settings,
            LocalDraftMailer(
                Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
            ),
            writer=writer,
            facts=stack.enter_context(_client(settings)) if writer is not None else None,
        )
        session = stack.enter_context(session_scope(_sessions(settings)))
        item = session.get(UnmatchedMail, mail_id)
        if item is None:
            typer.echo(f"no mail {mail_id}")
            raise typer.Exit(code=1)
        case = _booking_case(session, case_id)
        try:
            stats = link_unmatched(
                session,
                item,
                case,
                by=by,
                classifier=classifier,
                settings=settings,
                responder=responder,
            )
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
        read = "read" if stats.classified else "kept unread (no reader configured)"
        typer.echo(f"mail {mail_id} linked to case #{case_id}; {read}")


@booking_app.command("dismiss-mail")
def booking_dismiss_mail(
    mail_id: int,
    by: Annotated[str, typer.Option(help="Who dismisses it")],
    note: Annotated[str, typer.Option(help="Why it needs nothing")],
) -> None:
    """Say an unmatched email needs nothing from the agent."""
    from facility_profiles.booking.models import UnmatchedMail
    from facility_profiles.booking.unmatched import dismiss_unmatched

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        item = session.get(UnmatchedMail, mail_id)
        if item is None:
            typer.echo(f"no mail {mail_id}")
            raise typer.Exit(code=1)
        try:
            dismiss_unmatched(session, item, by=by, note=note)
        except ValueError as exc:
            typer.echo(str(exc))
            raise typer.Exit(code=1) from exc
    typer.echo(f"mail {mail_id} dismissed")


@booking_app.command("find")
def booking_find(
    number: Annotated[
        str,
        typer.Argument(help="Any number: PO, load, pickup#, DCT ref, shipment, SO, portal id"),
    ],
) -> None:
    """Find the cases a number belongs to, now or before (a replaced pickup number too)."""
    from facility_profiles.booking.references import find_cases
    from facility_profiles.booking.service import summary_line

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        cases = find_cases(session, number)
        if not cases:
            typer.echo(f"no case carries {number}")
            raise typer.Exit(code=1)
        for case in cases:
            typer.echo(summary_line(case))


@booking_app.command("desks")
def booking_desks() -> None:
    """How each facility was booked before: method, desk, how often and when last."""
    from sqlalchemy import select

    from facility_profiles.booking.models import DeskMemory

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        rows = list(
            session.scalars(
                select(DeskMemory).order_by(DeskMemory.facility_key, DeskMemory.worked_count.desc())
            )
        )
        if not rows:
            typer.echo("nothing booked yet")
            return
        repo = Repository(session)
        for row in rows:
            record = repo.get_facility(row.facility_key)
            name = record.company_name if record and record.company_name else row.facility_key
            typer.echo(
                f"{name[:36]:<36} {row.method:<10} {row.desk or '-':<40} x{row.worked_count:<3} "
                f"last {row.last_worked_at:%Y-%m-%d} (case #{row.last_case_id})"
            )


@booking_app.command("timers")
def booking_timers() -> None:
    """Raise what time alone brings: no reply in 24 h or 48 h, a pickup that passed unbooked.

    Writes to-dos on the store only; nothing is sent and nothing goes to Transport Pro. Each
    one clears itself when the vendor answers, the pickup moves later or the case is booked.
    """
    from facility_profiles.booking.timers import sweep

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        result = sweep(session, now=datetime.now(tz=UTC), settings=settings)
    for case_id, kind, description in result.raised:
        typer.echo(f"#{case_id:<4} raised   {kind:<16} {description}")
    for case_id, kind, why in result.resolved:
        typer.echo(f"#{case_id:<4} resolved {kind:<16} {why}")
    typer.echo(
        f"{result.cases} case(s) checked: {len(result.raised)} raised, "
        f"{len(result.resolved)} resolved"
    )


@booking_app.command("today")
def booking_today(
    customer: Annotated[
        str | None,
        typer.Option(help="Only this customer: its key (lidl) or a Transport Pro customer name"),
    ] = None,
    out: Annotated[Path | None, typer.Option(help="Also write the summary to this file")] = None,
    timers: Annotated[
        bool, typer.Option("--timers/--no-timers", help="Run the timers first, so it is current")
    ] = True,
) -> None:
    """The daily summary: what needs a person, today's pickups, drafts waiting to be sent."""
    from facility_profiles.booking.service import list_cases
    from facility_profiles.booking.timers import sweep
    from facility_profiles.booking.today import render_today, today_summary
    from facility_profiles.customers import customers

    settings = _settings()
    now = datetime.now(tz=UTC)
    known = customers(settings)
    wanted = (customer or "").strip().lower()
    key = wanted if wanted in {c.key for c in known.files} else None
    timezone = (known.get(key).timezone if key else None) or settings.booking_timezone
    with session_scope(_sessions(settings)) as session:
        if timers:
            sweep(session, now=now, settings=settings)
        cases = list_cases(session)
        if key:
            cases = [c for c in cases if known.for_case(c).key == key]
        data = today_summary(cases, now=now, timezone=timezone, customer=None if key else customer)
    if key:
        data["customer"] = known.get(key).name
    text = render_today(data)
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text, encoding="utf-8")
    typer.echo(text)


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
    customer: Annotated[
        str | None,
        typer.Option(help="Whose group and rules (a customer file's key); default: the only one"),
    ] = None,
    group: Annotated[
        str | None, typer.Option(help="The group address; default: the customer file's")
    ] = None,
    days: Annotated[int, typer.Option(help="How many days back to list")] = 3,
    max_messages: Annotated[int, typer.Option("--max", help="Per-pass cap")] = 300,
    desk: Annotated[
        list[str] | None, typer.Option(help="Extra appointment-desk address (repeatable)")
    ] = None,
    verbose: Annotated[bool, typer.Option(help="Print every stored message")] = False,
) -> None:
    """One collection pass from this machine: the same code the Lambda runs every 15 minutes."""
    from facility_profiles.customers import CustomerFileError, customers
    from facility_profiles.mailarchive import filters
    from facility_profiles.mailarchive.collector import run
    from facility_profiles.mailarchive.gmail import Delegated, load_service_account
    from facility_profiles.mailarchive.store import Store

    settings = _settings()
    try:
        chosen = customers(settings).only_or(
            customer or (settings.customers[0] if len(settings.customers) == 1 else None)
        )
    except CustomerFileError as exc:
        raise typer.BadParameter(str(exc)) from exc
    rules = filters.rules_for(chosen).with_desks(desk or [])
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
        rules=rules,
        group=group,
        days=days,
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


# ------------------------------------------------------------------ customers


@customers_app.command("list")
def customers_list() -> None:
    """Every customer file: key, name, Transport Pro customers, pods, mailbox and desk."""
    from facility_profiles.customers import CustomerFileError, customers

    settings = _settings()
    try:
        known = customers(settings)
    except CustomerFileError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    for c in known.files:
        default = " (default)" if c.key in settings.customers else ""
        ids = ", ".join(str(i) for i in c.tpro_customer_ids) or "-"
        pods = ", ".join(str(t) for t in c.terminal_ids) or "-"
        typer.echo(
            f"{c.key:<12} {c.name}{default}: customers {ids}; pods {pods}; "
            f"group {c.group or '-'}; desk {c.customer_desk or '-'}"
        )
        typer.echo(f"{'':<12} {c.source}")
    fb = known.fallback
    typer.echo(
        f"{'(no file)':<12} any other customer: group {fb.group or '-'}; "
        f"desk {fb.customer_desk or '-'} (FP_BOOKING_* settings)"
    )


@customers_app.command("show")
def customers_show(
    key: Annotated[str, typer.Argument(help="The customer file's key, e.g. lidl")],
    sample: Annotated[
        list[str] | None,
        typer.Option(
            help="Text to try the customer's number patterns on, e.g. a desk's email line "
            "(repeatable)"
        ),
    ] = None,
) -> None:
    """Check one customer file and show what the agent will do with it."""
    from facility_profiles.customers import CustomerFileError, customers
    from facility_profiles.mailarchive import filters

    settings = _settings()
    try:
        c = customers(settings).get(key)
    except CustomerFileError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    rules = filters.rules_for(c)

    def line(label: str, value: object) -> None:
        typer.echo(f"  {label:<18} {value if value not in (None, '', ()) else '-'}")

    typer.echo(f"{c.key}: {c.name}  ({c.source})")
    if c.description:
        typer.echo(f"  {c.description}")
    line("TPro customers", ", ".join(str(i) for i in c.tpro_customer_ids))
    line("books pickups for", ", ".join(str(i) for i in c.booking_customer_ids))
    line("TPro names", ", ".join(c.tpro_customer_names))
    line("pods (terminals)", ", ".join(str(t) for t in c.terminal_ids))
    line("time zone", c.timezone or f"{settings.booking_timezone} (FP_BOOKING_TIMEZONE)")
    line("group", c.group)
    line("drafts from", c.sender)
    line("cc", c.cc_header)
    line("signature", c.signature or f"{settings.booking_signature} (FP_BOOKING_SIGNATURE)")
    line("customer desk", c.customer_desk)
    line("delivery booked in", c.delivery_system)
    line("PO pattern", c.po.pattern if c.po else None)
    line("PO date pattern", c.po_date.pattern if c.po_date else None)
    line("delivery ref", c.delivery_ref.pattern if c.delivery_ref else None)
    line("archive subjects", ", ".join(name for name, _ in rules.keep))
    line(
        "archive desks",
        f"{len(rules.desks)} address(es), domains {', '.join(sorted(rules.desk_domains))}",
    )
    for i, rule in enumerate(c.rules, 1):
        line(f"rule {i}", rule.describe())
    if not c.rules:
        line("rules", "none: the agent drafts every pickup when it runs on its own")
    gaps = [
        what
        for what, missing in (
            ("[mail] group: drafts carry no From and copy no one", not c.group),
            ("[customer_desk] email: 'vendor cannot ship' goes to a person", not c.customer_desk),
            ("[numbers] delivery_ref: no delivery reference is read", not c.delivery_ref),
            (
                "[transport_pro] customer_ids: `--customer` cannot scope a scan",
                not c.tpro_customer_ids,
            ),
        )
        if missing
    ]
    for gap in gaps:
        typer.echo(f"  not set: {gap}")
    for text in sample or []:
        typer.echo(f"sample: {text!r}")
        found = filters.identifiers(text, rules=rules)
        line("PO numbers", ", ".join(found["po_numbers"]))
        for po in found["po_numbers"]:
            embedded = c.po_embedded_date(po, near=date.today())
            if embedded:
                line(f"  date in {po}", f"{embedded:%a %m/%d/%Y}")
        line("delivery refs", ", ".join(found["delivery_refs"]))
        line("pickup numbers", ", ".join(found["pickup_numbers"]))
        slot = next((m for p in c.slot_patterns() if (m := p.search(text))), None)
        if slot:
            when = (
                f"{slot.group('m')}/{slot.group('d')} {slot.group('h')}"
                f"{slot.group('min') or ''}{slot.group('ampm') or ''}"
            )
            line("delivery slot", f"{when} -> {slot.group('ref').upper()}")
        subject = filters.match_reason(text, set(), rules)
        line("as a subject", subject or "not kept")


@customers_app.command("new")
def customers_new(
    key: Annotated[str, typer.Argument(help="Short key, lower-case: acme, costco-west")],
    name: Annotated[str, typer.Option(help="How emails name the customer, e.g. Acme")],
    tpro_customer: Annotated[
        list[int] | None, typer.Option(help="Transport Pro customer ID (repeatable)")
    ] = None,
    tpro_name: Annotated[
        list[str] | None, typer.Option(help="Transport Pro customer name (repeatable)")
    ] = None,
    terminal: Annotated[list[int] | None, typer.Option(help="Pod terminal ID (repeatable)")] = None,
    group: Annotated[str | None, typer.Option(help="The group the threads run through")] = None,
    desk: Annotated[str | None, typer.Option(help="The customer's own inbound desk")] = None,
    folder: Annotated[
        Path | None,
        typer.Option(help="Where to write it; default FP_CUSTOMERS_DIR, else the built-in folder"),
    ] = None,
) -> None:
    """Write a starter customer file with what you know, then check it."""
    from facility_profiles.customers import (
        BUILT_IN_DIR,
        CustomerFileError,
        load_customer,
        reload,
    )
    from facility_profiles.customers.starter import starter_text

    settings = _settings()
    key = key.strip().lower()
    target_dir = folder or (
        Path(settings.customers_dir) if settings.customers_dir else BUILT_IN_DIR
    )
    path = target_dir / f"{key}.toml"
    if path.exists():
        typer.echo(f"{path} already exists; edit it, or pick another key")
        raise typer.Exit(code=1)
    target_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(
        starter_text(
            key,
            name=name,
            customer_ids=tpro_customer or [],
            customer_names=tpro_name or [],
            terminal_ids=terminal or [],
            group=group,
            desk=desk,
        ),
        encoding="utf-8",
        newline="\n",
    )
    reload()
    typer.echo(f"wrote {path}")
    if target_dir == BUILT_IN_DIR:
        typer.echo("  it is in the package folder, which the public repository carries")
    elif folder is not None and settings.customers_dir != str(folder):
        typer.echo(f"  set FP_CUSTOMERS_DIR={folder} so the agent reads it")
    try:
        load_customer(path)
    except CustomerFileError as exc:
        typer.echo(f"still to fill in:\n{exc}")
        return
    typer.echo(f"it reads cleanly; next: facility-profiles customers show {key}")


# ------------------------------------------------------------------ access


def _access_change(action: str, run: Callable[[Session, Settings], str | None]) -> None:
    """Run one access change in a transaction, saying what happened or why it could not be."""
    from facility_profiles.access import AccessError

    settings = _settings()
    try:
        with session_scope(_sessions(settings)) as session:
            said = run(session, settings)
    except AccessError as exc:
        typer.echo(str(exc))
        raise typer.Exit(code=1) from exc
    typer.echo(said or f"nothing to {action}")


@access_app.command("list")
def access_list() -> None:
    """Admins, and everyone on the board with what they see for each customer."""
    from facility_profiles.access import access_grid

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        grid = access_grid(session, settings, now=datetime.now(tz=UTC))
    for email in grid["admins"]:
        typer.echo(f"{email:<40} admin: every customer")
    names = {c["key"]: c["name"] for c in grid["customers"]}
    for person in grid["people"]:
        held = [
            f"{names.get(k, k)} {a['level']}"
            + (f" until {a['until']}" if a["until"] else "")
            + (" (ended)" if a["ended"] else "")
            for k, a in person["access"].items()
        ]
        typer.echo(f"{person['email']:<40} {', '.join(held) or 'no customer yet'}")
    if not grid["admins"] and not grid["people"]:
        typer.echo("nobody yet: set FP_BOARD_ADMINS, then grant access")


@access_app.command("grant")
def access_grant(
    email: Annotated[str, typer.Argument(help="Their Google Workspace email")],
    customer: Annotated[str, typer.Argument(help="The customer file's key, e.g. lidl")],
    by: Annotated[str, typer.Option(help="Who is giving it (kept in the history)")],
    act: Annotated[
        bool, typer.Option("--act/--view", help="Also approve, book and cancel, or only view")
    ] = False,
    until: Annotated[
        str | None, typer.Option(help="Last day it holds, YYYY-MM-DD (default: no end)")
    ] = None,
    note: Annotated[str | None, typer.Option(help="Why, for the history")] = None,
) -> None:
    """Give someone a customer (adding them to the board), or change what they have."""
    from facility_profiles.access import AccessLevel, set_access

    end = date.fromisoformat(until) if until else None
    level = AccessLevel.ACT if act else AccessLevel.VIEW

    def run(session: Session, settings: Settings) -> str | None:
        done = set_access(
            session,
            settings,
            email,
            customer,
            level,
            by=by,
            until=end,
            note=note,
            now=datetime.now(tz=UTC),
        )
        return f"{done}: {email.strip().lower()} {level.value} on {customer}" if done else None

    _access_change("change", run)


@access_app.command("revoke")
def access_revoke(
    email: Annotated[str, typer.Argument(help="Their email")],
    customer: Annotated[str, typer.Argument(help="The customer file's key, e.g. lidl")],
    by: Annotated[str, typer.Option(help="Who is taking it back (kept in the history)")],
) -> None:
    """Take back one customer from someone."""
    from facility_profiles.access import set_access

    def run(session: Session, settings: Settings) -> str | None:
        done = set_access(session, settings, email, customer, None, by=by, now=datetime.now(tz=UTC))
        return f"revoked: {email.strip().lower()} on {customer}" if done else None

    _access_change("revoke", run)


@access_app.command("remove")
def access_remove(
    email: Annotated[str, typer.Argument(help="Their email")],
    by: Annotated[str, typer.Option(help="Who is removing them (kept in the history)")],
) -> None:
    """Take someone off the board, with every customer they had."""
    from facility_profiles.access import remove_person

    def run(session: Session, _settings: Settings) -> str | None:
        n = remove_person(session, email, by=by, now=datetime.now(tz=UTC))
        return f"removed: {email.strip().lower()} ({n} customer{'' if n == 1 else 's'} taken back)"

    _access_change("remove", run)


@access_app.command("history")
def access_history(
    email: Annotated[str | None, typer.Option(help="Only this person's")] = None,
    limit: Annotated[int, typer.Option(help="How many, latest first")] = 50,
) -> None:
    """Who gave, changed or took back what, latest first."""
    from facility_profiles.access import history

    settings = _settings()
    with session_scope(_sessions(settings)) as session:
        rows = history(session, limit=limit, email=email)
        for c in rows:
            parts = [c.customer_key, c.level, f"until {c.until.isoformat()}" if c.until else None]
            what = " ".join(part for part in parts if part)
            when = c.at.strftime("%Y-%m-%d %H:%M")
            typer.echo(f"{when}  {c.by}: {c.action} {c.email} {what}".rstrip())
    if not rows:
        typer.echo("no changes yet")


if __name__ == "__main__":  # pragma: no cover
    app()
