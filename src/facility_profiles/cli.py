"""Command-line interface.

facility-profiles init-db
facility-profiles check-tpro
facility-profiles harvest --terminal 1160 --days 90
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
from facility_profiles.config import Settings, get_settings
from facility_profiles.logging import configure_logging, get_logger
from facility_profiles.pipeline.digest import render_digest
from facility_profiles.pipeline.export import export_profiles_csv
from facility_profiles.pipeline.run import Pipeline
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository, unwrap

app = typer.Typer(help="Facility scheduling profiles (Idea 1).", no_args_is_help=True)
review_app = typer.Typer(help="Work the review queue.", no_args_is_help=True)
app.add_typer(review_app, name="review")

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
        stats = pipeline.harvest(terminal_ids=terminal, start=start, end=end)
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
    fake_llm: Annotated[bool, typer.Option(help="Use the fake extractor (no model calls)")] = False,
    offline: Annotated[
        bool, typer.Option(help="No Transport Pro calls; use stored sources only")
    ] = False,
    resume: Annotated[str | None, typer.Option(help="Run ID to resume")] = None,
) -> None:
    """Run the pipeline: collect, extract, score and apply every facility."""
    settings = _settings()
    client = None if offline else _client(settings)
    try:
        pipeline = Pipeline(
            settings,
            _sessions(settings),
            client=client,
            extractor=_extractor(settings, fake=fake_llm),
        )
        report = pipeline.run(
            do_harvest=harvest_first and not offline,
            refresh_only=refresh,
            facility_cap=cap,
            terminal_ids=terminal,
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
