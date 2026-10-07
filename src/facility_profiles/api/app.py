"""FastAPI lookup page and review endpoints (FR-10, FR-11), and the appointments board.

Run with ``facility-profiles serve`` (or ``uvicorn facility_profiles.api.app:create_app
--factory``); the board is at ``/app/`` and its API under ``/api/booking``. ``serve`` also runs
the booking timers every few minutes, so the board shows a vendor's silence or a pickup that
slipped without anyone running a command.

Sign-in: with FP_GOOGLE_CLIENT_ID and FP_GOOGLE_CLIENT_SECRET set, people sign in with their
Google Workspace account and see only the customers an admin gave them (api/auth.py,
api/access.py); without them there is no login, so the board must stay on this machine.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session, sessionmaker
from starlette.types import Scope

from facility_profiles import __version__
from facility_profiles.access import Viewer, signin_enabled
from facility_profiles.access.service import session_secret
from facility_profiles.api import access as access_api
from facility_profiles.api import auth as auth_api
from facility_profiles.api import booking as booking_api
from facility_profiles.api import links as links_api
from facility_profiles.booking.service import ScanStats, scan
from facility_profiles.booking.timers import sweep
from facility_profiles.config import Settings, get_settings

if TYPE_CHECKING:
    from facility_profiles.booking.automation import RunReport
from facility_profiles.logging import get_logger
from facility_profiles.pipeline.collect import identity_from_record
from facility_profiles.pipeline.digest import render_digest
from facility_profiles.pipeline.profile import profile_from_records
from facility_profiles.review.queue import ReviewError, ReviewService
from facility_profiles.storage.db import init_db, make_engine, session_factory, session_scope
from facility_profiles.storage.repository import Repository, unwrap

BOARD = Path(__file__).parent / "static"
WRITES = frozenset({"POST", "PUT", "PATCH", "DELETE"})
log = get_logger(__name__)


class BoardFiles(StaticFiles):
    """The board's page, script and styles, checked with the server on every load.

    Without it a browser keeps an old app.js for a while after an update; with it an unchanged
    file costs a 304 and a changed one reaches everyone at once.
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        """The file, marked to be revalidated before each use."""
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def same_origin(request: Request, settings: Settings) -> bool:
    """True unless the browser says the request comes from another site (a forged form post)."""
    if request.headers.get("sec-fetch-site") == "cross-site":
        return False
    origin = request.headers.get("origin")
    if origin is None:
        return True  # not sent by a browser page, or an old one; the cookie is SameSite=Lax
    allowed = {request.headers.get("host", "")}
    if settings.board_public_url:
        allowed.add(urlsplit(settings.board_public_url).netloc)
    return urlsplit(origin).netloc in allowed


class Decision(BaseModel):
    """Reviewer decision payload."""

    action: str  # accept | edit | reject
    value: str | None = None
    by: str


def run_timers(sessions: sessionmaker[Session], settings: Settings | None = None) -> None:
    """One pass of the booking timers, and the desks' rules given settings, in one transaction."""
    with session_scope(sessions) as session:
        sweep(session, now=datetime.now(tz=UTC), settings=settings)


def run_autopilot(sessions: sessionmaker[Session], settings: Settings) -> None:
    """One pass of the agent on its own (booking/automation.py), drafting into the drafts folder.

    It sends only where a rule says send, FP_BOOKING_MODE is send and a Gmail sender is set. With
    FP_BOOKING_INBOX it also reads the new replies and answers them by the customers' rules.
    """
    from facility_profiles.booking.automation import run_once  # noqa: PLC0415 - optional loop
    from facility_profiles.booking.inbox import inbox_from_settings, reader_tools  # noqa: PLC0415
    from facility_profiles.booking.mail import GmailSender, LocalDraftMailer  # noqa: PLC0415

    mailer = LocalDraftMailer(
        Path(settings.booking_drafts_dir), sender=settings.booking_sender or ""
    )
    sender = None
    if (
        settings.booking_mode == "send"
        and settings.booking_gmail_key
        and settings.booking_gmail_user
    ):
        user = settings.booking_gmail_user
        sender = GmailSender(Path(settings.booking_gmail_key), user, user)
    from facility_profiles.tpro.client import TransportProClient  # noqa: PLC0415

    inbox = inbox_from_settings(settings)
    classifier, writer = reader_tools(settings) if inbox is not None else (None, None)
    with ExitStack() as stack:
        client = None
        if settings.booking_tpro_writeback:
            client = stack.enter_context(
                TransportProClient.from_settings(settings, allow_writes=True)
            )
        # The writer answers from the load as Transport Pro has it now (read only).
        facts = client
        if facts is None and writer is not None:
            facts = stack.enter_context(TransportProClient.from_settings(settings))
        session = stack.enter_context(session_scope(sessions))
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
    log.info("booking.autopilot", **report.counts())


def run_scan(
    sessions: sessionmaker[Session],
    settings: Settings,
    scope: tuple[list[int], list[int]],
    client: Any = None,
) -> ScanStats:
    """One scan of Transport Pro (booking/service.py ``scan``); it only reads Transport Pro.

    New pickups come onto the board and the board's pickups are kept in step with their loads.
    ``scope`` is the terminals and Transport Pro customer ids, as ``serve --scan-customer`` (or
    FP_CUSTOMERS) chose them.
    """
    from facility_profiles.tpro.client import TransportProClient  # noqa: PLC0415 - optional loop

    terminals, customer_ids = scope
    if client is None:
        with TransportProClient.from_settings(settings) as tpro:
            return run_scan(sessions, settings, scope, tpro)
    stats = scan(client, sessions, settings, terminal_ids=terminals, customer_ids=customer_ids)
    log.info("booking.scan", **{k: v for k, v in stats.__dict__.items() if k != "case_ids"})
    return stats


async def _every(minutes: float, name: str, job: Callable[[], object]) -> None:
    """Run ``job`` now and then every ``minutes``; a failed pass is logged, not fatal."""
    while True:
        try:
            await run_in_threadpool(job)
        except Exception:  # keep the board serving; the next pass retries
            log.exception(f"booking.{name}_failed")
        await asyncio.sleep(minutes * 60)


def run_mail(sessions: sessionmaker[Session], settings: Settings) -> RunReport:
    """One read of the group mail onto the board: kept on its pickups, nothing answered."""
    from facility_profiles.booking.automation import read_mail  # noqa: PLC0415 - optional loop
    from facility_profiles.booking.inbox import inbox_from_settings, reader_tools  # noqa: PLC0415

    inbox = inbox_from_settings(settings)
    if inbox is None:
        msg = "no mail to read: FP_BOOKING_INBOX is unset, or its Gmail key and user are missing"
        raise RuntimeError(msg)
    classifier, _ = reader_tools(settings)
    if classifier is None:
        msg = "no reader for the replies: set FP_LLM_PROVIDER=openrouter and its key"
        raise RuntimeError(msg)
    with session_scope(sessions) as session:
        report = read_mail(session, settings, inbox=inbox, classifier=classifier)
    log.info("booking.mail", **report.counts())
    return report


def _mail_job(
    app: FastAPI, sessions: sessionmaker[Session], settings: Settings, minutes: float
) -> Callable[[], object]:
    """One read of the mail, remembered on ``app.state.mail_check`` for the board."""

    def job() -> None:
        at = datetime.now(tz=UTC).isoformat()
        try:
            report = run_mail(sessions, settings)
        except Exception:
            app.state.mail_check = {"every": minutes, "at": at, "ok": False}
            raise
        app.state.mail_check = {
            "every": minutes,
            "at": at,
            "ok": report.mail_failed == 0,
            "read": report.mail_read,
            "by_person": report.mail_by_person,
        }

    return job


def _scan_job(
    app: FastAPI,
    sessions: sessionmaker[Session],
    settings: Settings,
    minutes: float,
    scope: tuple[list[int], list[int]],
) -> Callable[[], object]:
    """One scan, remembered on ``app.state.scan`` for the board.

    The board shows when Transport Pro was last checked and what came of it, or that the check
    failed; why it failed goes to the log only.
    """

    def job() -> None:
        at = datetime.now(tz=UTC).isoformat()
        try:
            stats = run_scan(sessions, settings, scope)
        except Exception:
            app.state.scan = {"every": minutes, "at": at, "ok": False}
            raise
        app.state.scan = {
            "every": minutes,
            "at": at,
            "ok": True,
            "loads": stats.loads,
            "created": stats.created,
        }

    return job


def create_app(
    settings: Settings | None = None,
    *,
    timers_every: float | None = None,
    autopilot_every: float | None = None,
    scan_every: float | None = None,
    scan_scope: tuple[list[int], list[int]] = ([], []),
    mail_every: float | None = None,
) -> FastAPI:
    """Build the application.

    With ``timers_every`` (minutes) it also runs the booking timers; with ``autopilot_every``,
    the agent's own pass by each customer's rules; with ``scan_every``, a scan of Transport Pro
    for new pickups over ``scan_scope`` (terminals, customer ids); with ``mail_every``, a read
    of the customers' group mail onto the board that answers nothing. The last three are off
    unless asked for.
    """
    settings = settings or get_settings()
    if signin_enabled(settings) and not settings.board_admins:
        msg = (
            "Google sign-in is on but FP_BOARD_ADMINS names nobody, so no one could give "
            "access; set FP_BOARD_ADMINS to your email"
        )
        raise ValueError(msg)
    engine = make_engine(settings.database_url)
    init_db(engine)
    sessions = session_factory(engine)
    with session_scope(sessions) as session:
        secret = session_secret(session, settings) if signin_enabled(settings) else b""

    @asynccontextmanager
    async def lifespan(app_: FastAPI) -> AsyncIterator[None]:
        loops: list[tuple[float | None, str, Callable[[], object]]] = [
            (timers_every, "timers", lambda: run_timers(sessions, settings)),
            (autopilot_every, "autopilot", lambda: run_autopilot(sessions, settings)),
        ]
        if mail_every:  # the mail after the loads: a reply can name a pickup the scan just found
            loops.insert(0, (mail_every, "mail", _mail_job(app_, sessions, settings, mail_every)))
        if scan_every:
            scanning = _scan_job(app_, sessions, settings, scan_every, scan_scope)
            loops.insert(0, (scan_every, "scan", scanning))
        tasks = [
            asyncio.create_task(_every(every, name, job)) for every, name, job in loops if every
        ]
        yield
        for task in tasks:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

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

    app = FastAPI(title="Facility scheduling profiles", version=__version__, lifespan=lifespan)
    app.state.sessions = sessions
    app.state.settings = settings
    app.state.session_secret = secret
    # The scan's last pass, for the board (None: this server does not scan Transport Pro).
    app.state.scan = {"every": scan_every, "at": None, "ok": None} if scan_every else None
    # The mail's last read, for the board (None: this server does not read the mail).
    app.state.mail_check = {"every": mail_every, "at": None, "ok": None} if mail_every else None
    if signin_enabled(settings):

        @app.middleware("http")
        async def board_writes_from_the_board(request: Request, call_next: Any) -> Response:
            """A change (approve, give access...) must come from the board's own pages."""
            if (
                request.method in WRITES
                and not request.url.path.startswith("/c/")
                and not same_origin(request, settings)
            ):
                detail = "changes can only be made from the board itself"
                return JSONResponse({"detail": detail}, status_code=403)
            response: Response = await call_next(request)
            return response

    app.include_router(auth_api.router)
    app.include_router(access_api.router)
    app.include_router(booking_api.router)
    # The vendor pages too, for trying links on this machine; in public they run on their own
    # (links_api.create_links_app), never with the board.
    app.include_router(links_api.router)
    app.mount("/app", BoardFiles(directory=BOARD, html=True), name="board")

    @app.get("/", include_in_schema=False)
    def home() -> RedirectResponse:
        return RedirectResponse("/app/")

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    admin = Depends(auth_api.require_admin)

    @app.get("/facilities")
    def search(
        name: str, session: Session = Depends(get_session), _admin: Viewer = admin
    ) -> list[dict[str, Any]]:
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
    def facility(
        facility_id: int, session: Session = Depends(get_session), _admin: Viewer = admin
    ) -> dict[str, Any]:
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
    def review(
        session: Session = Depends(get_session), _admin: Viewer = admin
    ) -> list[dict[str, Any]]:
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
        item_id: int,
        body: Decision,
        session: Session = Depends(get_session),
        viewer: Viewer = admin,
    ) -> dict[str, Any]:
        service = ReviewService(Repository(session))
        by = auth_api.decided_by(viewer, body.by)
        try:
            if body.action == "accept":
                item = service.accept(item_id, by=by)
            elif body.action == "edit":
                item = service.edit(item_id, body.value or "", by=by)
            elif body.action == "reject":
                item = service.reject(item_id, by=by)
            else:
                raise HTTPException(status_code=400, detail="action must be accept, edit or reject")
        except ReviewError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": item.id, "status": item.status}

    @app.get("/digest")
    def digest(session: Session = Depends(get_session), _admin: Viewer = admin) -> dict[str, str]:
        repo = Repository(session)
        return {"markdown": render_digest(repo, repo.latest_run(), now=datetime.now(tz=UTC))}

    return app
