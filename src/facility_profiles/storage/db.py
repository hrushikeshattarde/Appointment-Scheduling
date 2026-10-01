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

from sqlalchemy import Column, DefaultClause, Engine, create_engine, event, inspect, text
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
    """Create missing tables (the booking agent's too), add new columns, upgrade old rows."""
    from facility_profiles.booking import models as _booking_models  # noqa: F401, PLC0415
    from facility_profiles.booking.worklist import migrate_legacy_statuses  # noqa: PLC0415

    Base.metadata.create_all(engine)
    ensure_columns(engine)
    with Session(engine) as session, session.begin():
        migrate_legacy_statuses(session)


def ensure_columns(engine: Engine) -> list[str]:
    """Add columns the models gained since a store was created; return what was added.

    ``create_all`` never alters an existing table, and the pilot stores are SQLite files that
    predate some columns. Adding a nullable column (with the model's server default, so older
    rows read as that value) is the one schema change SQLite does in place, so it is done here;
    anything else waits for Alembic.
    """
    inspector = inspect(engine)
    existing_tables = set(inspector.get_table_names())
    added: list[str] = []
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in existing_tables:
                continue
            present = {c["name"] for c in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in present:
                    continue
                ddl = column.type.compile(dialect=engine.dialect) + _literal_default(column)
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))
                added.append(f"{table.name}.{column.name}")
                present.add(column.name)
            # An index the model declares on a column that was just added, or that an older
            # store never had: create it so lookups by Message-ID stay cheap.
            have_indexes = {i["name"] for i in inspector.get_indexes(table.name)}
            for index in table.indexes:
                if index.name in have_indexes or not all(c.name in present for c in index.columns):
                    continue
                index.create(bind=conn)
                added.append(f"{table.name}.{index.name}")
    return added


def _literal_default(column: Column[Any]) -> str:
    """The DEFAULT clause for a column added in place: its string server default, quoted."""
    default = column.server_default
    if isinstance(default, DefaultClause) and isinstance(default.arg, str):
        return " DEFAULT '" + default.arg.replace("'", "''") + "'"
    return ""


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
