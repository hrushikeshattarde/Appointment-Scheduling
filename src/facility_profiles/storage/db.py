"""Engine and session helpers.

SQLite is the pilot default; the URL is the only thing that changes for Postgres. Schema
creation uses ``create_all`` for the pilot; Alembic migrations are introduced before the first
Postgres deployment.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from facility_profiles.storage.models import Base


def make_engine(database_url: str, *, echo: bool = False) -> Engine:
    """Create an engine, preparing the SQLite directory and pragmas when needed."""
    connect_args: dict[str, Any] = {}
    kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        path = database_url.split("///", 1)[-1] if "///" in database_url else ""
        if path and path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
        else:
            kwargs["poolclass"] = StaticPool  # one shared in-memory database per engine
        connect_args["check_same_thread"] = False
    engine = create_engine(
        database_url, echo=echo, connect_args=connect_args, future=True, **kwargs
    )
    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _sqlite_pragmas(dbapi_connection: Any, _record: Any) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    return engine


def init_db(engine: Engine) -> None:
    """Create all tables that do not exist yet (including the booking agent's)."""
    from facility_profiles.booking import models as _booking_models  # noqa: F401, PLC0415

    Base.metadata.create_all(engine)


def session_factory(engine: Engine) -> sessionmaker[Session]:
    """Return a configured session factory."""
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


@contextmanager
def session_scope(factory: sessionmaker[Session]) -> Iterator[Session]:
    """Transactional scope: commit on success, roll back on error."""
    session = factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
