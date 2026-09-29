"""Scoping a harvest to one or more customers on a terminal (pods that serve many shippers)."""

from __future__ import annotations

import re
from datetime import date

import pytest
from typer.testing import CliRunner

from facility_profiles.cli import app
from facility_profiles.config import Settings, get_settings
from facility_profiles.pipeline.harvest import iter_terminal_loads
from facility_profiles.pipeline.run import Pipeline
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository
from tests.conftest import NOW, FakeTPro, sample_facility, sample_loads
from tests.test_pipeline import scripted_extractor

runner = CliRunner()


def test_iter_terminal_loads_queries_each_customer_per_terminal_and_window():
    client = FakeTPro(sample_loads(), {})
    loads = list(
        iter_terminal_loads(
            client,  # type: ignore[arg-type]
            terminal_ids=[1089],
            customer_ids=[6680, 7211],
            start=date(2026, 9, 1),
            end=date(2026, 9, 14),
        )
    )
    # two customers x two 7-day windows
    assert (
        client.calls
        == ["iter_loads ['customer_id', 'pickup_date_end', 'pickup_date_start', 'terminal_id']"] * 4
    )
    assert len(loads) == 2 * len(sample_loads())  # the fake ignores the customer filter


def test_iter_terminal_loads_without_customers_keeps_the_old_filters():
    client = FakeTPro(sample_loads(), {})
    list(
        iter_terminal_loads(
            client,  # type: ignore[arg-type]
            terminal_ids=[1160],
            start=date(2026, 9, 1),
            end=date(2026, 9, 7),
        )
    )
    assert client.calls == ["iter_loads ['pickup_date_end', 'pickup_date_start', 'terminal_id']"]


def test_settings_parse_pilot_customer_ids():
    settings = Settings(
        _env_file=None,  # type: ignore[call-arg]
        TPRO_BASE_URL="https://tpro.test",
        TPRO_USERNAME="u",
        TPRO_PASSWORD="p",
        pilot_customer_ids="6680, 7211",  # type: ignore[arg-type]
    )
    assert settings.pilot_customer_ids == [6680, 7211]
    assert (
        Settings(
            _env_file=None,  # type: ignore[call-arg]
            TPRO_BASE_URL="https://tpro.test",
            TPRO_USERNAME="u",
            TPRO_PASSWORD="p",
        ).pilot_customer_ids
        == []
    )


def test_pipeline_harvest_records_customer_scope(settings, sessions):
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    pipeline = Pipeline(settings, sessions, client=client, extractor=scripted_extractor(), now=NOW)  # type: ignore[arg-type]

    stats = pipeline.harvest(terminal_ids=[1089], customer_ids=[6680, 7211])

    assert stats["terminal_ids"] == [1089]
    assert stats["customer_ids"] == [6680, 7211]
    assert stats["end"] == NOW.date().isoformat()
    assert all("customer_id" in call for call in client.calls if call.startswith("iter_loads"))
    with session_scope(sessions) as session:
        assert Repository(session).find_facility(facility_id=900001)

    # Settings default applies when the caller passes nothing.
    scoped = settings.model_copy(update={"pilot_customer_ids": [7211]})
    client.calls.clear()
    stats = Pipeline(
        scoped, sessions, client=client, extractor=scripted_extractor(), now=NOW
    ).harvest()  # type: ignore[arg-type]
    assert stats["customer_ids"] == [7211]
    assert all("customer_id" in call for call in client.calls if call.startswith("iter_loads"))

    # Run passes the scope through and stores it on the run row's stats.
    report = pipeline.run(terminal_ids=[1089], customer_ids=[6680])
    assert report.harvest["customer_ids"] == [6680]
    with session_scope(sessions) as session:
        run = Repository(session).get_run(report.run_id)
        assert run is not None and run.terminal_ids == "1089"
        assert run.stats["harvest"]["customer_ids"] == [6680]


def test_cli_accepts_customer_option(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{(tmp_path / 'fp.db').as_posix()}")
    monkeypatch.setenv("FP_PILOT_CUSTOMER_IDS", "6680,7211")
    # CI runners report a colour-capable terminal; keep the help text plain and wide.
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    try:
        assert get_settings().pilot_customer_ids == [6680, 7211]
        for command in ("harvest", "run"):
            result = runner.invoke(app, [command, "--help"])
            assert result.exit_code == 0, result.output
            plain = re.sub(r"\[[0-9;]*m", "", result.output)
            assert "--customer" in plain, plain
    finally:
        get_settings.cache_clear()
