"""FastAPI lookup page and review endpoints (FR-10, FR-11), and the appointments board.

Run with ``facility-profiles serve`` (or ``uvicorn facility_profiles.api.app:create_app
--factory``); the board is at ``/app/`` and its API under ``/api/booking``. Authentication is
left to the reverse proxy / single sign-on in front of this service (see the NFR section of the
PRD).
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from facility_profiles import __version__
from facility_profiles.api import booking as booking_api
from facility_profiles.config import Settings, get_settings
from facility_profiles.pipeline.collect import identity_from_record
from facility_profiles.pipeline.digest import render_digest
from facility_profiles.pipeline.profile import profile_from_records
from facility_profiles.review.queue import ReviewError, ReviewService
from facility_profiles.storage.db import init_db, make_engine, session_factory
from facility_profiles.storage.repository import Repository, unwrap

BOARD = Path(__file__).parent / "static"


class Decision(BaseModel):
    """Reviewer decision payload."""

    action: str  # accept | edit | reject
    value: str | None = None
    by: str


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)

    def get_session() -> Iterator[Session]:
        session = sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app = FastAPI(title="Facility scheduling profiles", version=__version__)
    app.state.sessions = sessions
    app.include_router(booking_api.router)
    app.mount("/app", StaticFiles(directory=BOARD, html=True), name="board")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/app/")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    @app.get("/facilities")
    def search(name: str, session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        repo = Repository(session)
        return [
            {
                "key": r.key,
                "facility_id": r.facility_id,
                "company_name": r.company_name,
                "city": r.city,
                "state": r.state,
                "stop_count": r.stop_count,
            }
            for r in repo.find_facility(name=name)
        ]

    @app.get("/facilities/{facility_id}")
    def facility(facility_id: int, session: Session = Depends(get_session)) -> dict[str, Any]:
        repo = Repository(session)
        records = repo.find_facility(facility_id=facility_id)
        if not records:
            raise HTTPException(status_code=404, detail="facility not found")
        record = records[0]
        identity = identity_from_record(
            record, [a.company_name or "" for a in repo.aliases(record.key)]
        )
        profiles = {}
        for role in repo.roles_for(record.key):
            profile = profile_from_records(
                identity, role, repo.fields(record.key, role), repo.profile(record.key, role)
            )
            profiles[role.value] = profile.model_dump(mode="json")
        return {
            "facility": identity.model_dump(mode="json"),
            "stop_count": record.stop_count,
            "profiles": profiles,
        }

    @app.get("/review")
    def review(session: Session = Depends(get_session)) -> list[dict[str, Any]]:
        repo = Repository(session)
        return [
            {
                "id": i.id,
                "facility_key": i.facility_key,
                "role": i.role,
                "field_name": i.field_name,
                "proposed": unwrap(i.proposed),
                "existing": unwrap(i.existing),
                "candidates": i.candidates,
                "reason": i.reason,
                "created_at": i.created_at.isoformat(),
            }
            for i in repo.list_review_items(status="open")
        ]

    @app.post("/review/{item_id}")
    def decide(
        item_id: int, body: Decision, session: Session = Depends(get_session)
    ) -> dict[str, Any]:
        service = ReviewService(Repository(session))
        try:
            if body.action == "accept":
                item = service.accept(item_id, by=body.by)
            elif body.action == "edit":
                item = service.edit(item_id, body.value or "", by=body.by)
            elif body.action == "reject":
                item = service.reject(item_id, by=body.by)
            else:
                raise HTTPException(status_code=400, detail="action must be accept, edit or reject")
        except ReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": item.id, "status": item.status}

    @app.get("/digest")
    def digest(session: Session = Depends(get_session)) -> dict[str, str]:
        repo = Repository(session)
        return {"markdown": render_digest(repo, repo.latest_run(), now=datetime.now(tz=UTC))}

    return app
