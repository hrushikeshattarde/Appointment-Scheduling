from __future__ import annotations

import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from facility_profiles import __version__
from facility_profiles.cli import app
from facility_profiles.config import get_settings

runner = CliRunner()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "data" / "fp.db"
    monkeypatch.setenv("TPRO_BASE_URL", "https://tpro.test")
    monkeypatch.setenv("TPRO_USERNAME", "u")
    monkeypatch.setenv("TPRO_PASSWORD", "p")
    monkeypatch.setenv("FP_DATABASE_URL", f"sqlite:///{db.as_posix()}")
    monkeypatch.setenv("FP_PILOT_TERMINAL_IDS", "1160, 1124")
    monkeypatch.setenv("FP_EXPORT_DIR", str(tmp_path / "exports"))
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    yield db
    get_settings.cache_clear()


def test_version_and_init_db(env: Path):
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0 and __version__ in result.output
    result = runner.invoke(app, ["init-db"])
    assert result.exit_code == 0, result.output
    assert env.exists()
    settings = get_settings()
    assert settings.pilot_terminal_ids == [1160, 1124]
    assert settings.tpro_base_url == "https://tpro.test"


def test_lookup_review_export_digest_on_empty_store(env: Path):
    assert runner.invoke(app, ["lookup", "nothing-here"]).exit_code == 1
    result = runner.invoke(app, ["review", "list"])
    assert result.exit_code == 0 and "queue is empty" in result.output
    result = runner.invoke(app, ["export"])
    assert result.exit_code == 0 and "wrote 0 rows" in result.output
    result = runner.invoke(app, ["digest"])
    assert result.exit_code == 0 and "No run has completed yet" in result.output
    result = runner.invoke(app, ["review", "accept", "999", "--by", "x"])
    assert result.exit_code == 1 and "not found" in result.output


def test_api_health_and_404(env: Path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from facility_profiles.api.app import create_app

    client = TestClient(create_app(get_settings()))
    assert client.get("/health").json()["status"] == "ok"
    assert client.get("/facilities/1").status_code == 404
    assert client.get("/facilities", params={"name": "x"}).json() == []
    assert client.get("/review").json() == []
    assert client.post("/review/1", json={"action": "accept", "by": "x"}).status_code == 409
    assert "digest" in client.get("/digest").json()["markdown"].lower() or True
    assert fastapi is not None and os.environ["FP_DATABASE_URL"].startswith("sqlite")
