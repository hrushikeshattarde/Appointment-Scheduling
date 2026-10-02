"""The vendor's page behind a click-to-confirm link (``booking/links.py``).

``GET /c/{token}`` shows the times offered, with the one clicked in the email chosen, and a form
to propose another time. It changes nothing: mail scanners open every link in a message.
``POST /c/{token}`` records the vendor's answer, then redirects back to the page, which now says
what was recorded.

This is the only part of the agent a vendor reaches, so it can run on its own:
:func:`create_links_app` serves these pages and ``/health`` and nothing else, no board and no
API (``facility-profiles serve-links``). Every value on the page is escaped; nothing in it comes
from the vendor except what they typed on it.
"""

from __future__ import annotations

import html
from typing import Annotated
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response

from facility_profiles import __version__
from facility_profiles.api.booking import NowDep, SessionDep, SettingsDep
from facility_profiles.booking.links import (
    LinkAnswer,
    LinkError,
    confirm,
    offer_state,
    propose,
    read_token,
    state_says,
)
from facility_profiles.booking.models import SlotOffer
from facility_profiles.booking.timers import fmt_slot
from facility_profiles.config import Settings, get_settings
from facility_profiles.customers import customer_of
from facility_profiles.storage.db import init_db, make_engine, session_factory

MAX_FORM_BYTES = 8192
HEADERS = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
        "frame-ancestors 'none'; base-uri 'none'"
    ),
}
_STYLE = """
:root{--ink:#1d2329;--muted:#5b6670;--line:#d7dde2;--accent:#1a5fb4;--bg:#f6f8fa;--card:#fff;
--ok:#1e7b34;--warn:#9a4b00}
@media (prefers-color-scheme:dark){:root{--ink:#e8edf1;--muted:#a3adb6;--line:#38424b;
--accent:#78aef0;--bg:#14191e;--card:#1c232a;--ok:#6fcf86;--warn:#f0a65a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.45 -apple-system,Segoe UI,Roboto,Arial,sans-serif}
main{max-width:34rem;margin:0 auto;padding:24px 16px 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:20px;
margin:0 0 16px}
h1{font-size:1.3rem;margin:0 0 4px}h2{font-size:1.05rem;margin:0 0 12px}
.muted{color:var(--muted);font-size:.92rem;margin:0}
.slot{display:flex;gap:10px;align-items:center;padding:10px 12px;border:1px solid var(--line);
border-radius:8px;margin:0 0 8px;cursor:pointer}
.slot:has(input:checked){border-color:var(--accent);outline:1px solid var(--accent)}
label.field{display:block;font-size:.92rem;margin:12px 0 4px;color:var(--muted)}
input[type=text],input[type=date],input[type=time]{width:100%;padding:9px 10px;font:inherit;
color:inherit;background:transparent;border:1px solid var(--line);border-radius:6px}
.row{display:flex;gap:10px}.row>div{flex:1}
button{margin-top:16px;width:100%;padding:12px;font:inherit;font-weight:600;border:0;
border-radius:8px;background:var(--accent);color:#fff;cursor:pointer}
button.quiet{background:transparent;color:var(--accent);border:1px solid var(--accent)}
.note{padding:12px 14px;border-radius:8px;margin:0 0 16px;border:1px solid var(--line)}
.note.ok{border-color:var(--ok);color:var(--ok)}.note.warn{border-color:var(--warn);
color:var(--warn)}
"""


def _page(title: str, body: str, *, status: int = 200) -> Response:
    page = (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{_STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    )
    return Response(page, status_code=status, media_type="text/html", headers=HEADERS)


def _not_ours() -> Response:
    return _page(
        "Link not valid",
        '<div class="card"><h1>This link is not valid</h1>'
        '<p class="muted">Please reply to the email instead.</p></div>',
        status=404,
    )


def _header(offer: SlotOffer, settings: Settings) -> str:
    case = offer.case
    pos = " & ".join(str(p) for p in case.po_numbers) or f"load {case.load_id}"
    where = ", ".join(p for p in (case.vendor_name, case.vendor_city) if p)
    customer = customer_of(case, settings).label(case.customer_name)
    return (
        f'<p class="muted">{html.escape(settings.booking_carrier_name)}</p>'
        f"<h1>Pickup appointment, PO# {html.escape(pos)}</h1>"
        f'<p class="muted">{html.escape(where or "Your facility")} &middot; going to '
        f"{html.escape(customer)}</p>"
    )


def _view(
    offer: SlotOffer, settings: Settings, *, state: str, chosen: int | None, said: LinkAnswer | None
) -> Response:
    case = offer.case
    parts = [f'<div class="card">{_header(offer, settings)}</div>']
    if said is not None:
        kind = "ok" if said.ok else "warn"
        parts.append(f'<p class="note {kind}">{html.escape(said.say)}</p>')
    if state != "open":
        if said is None:
            kind = "ok" if state == "answered" else "warn"
            parts.append(f'<p class="note {kind}">{html.escape(state_says(offer, state))}</p>')
        return _page("Pickup appointment", "".join(parts))
    pick = chosen if chosen is not None and 0 <= chosen < len(offer.slots) else None
    asked = case.requested_local
    slots = "".join(
        f'<label class="slot"><input type="radio" name="slot" value="{i}"'
        f"{' checked' if i == pick else ''} required> {html.escape(fmt_slot(s))}"
        f"{' (the time we asked for)' if s == asked else ''}</label>"
        for i, s in enumerate(offer.slots)
    )
    parts.append(
        '<form class="card" method="post"><h2>Choose the pickup time that works</h2>'
        f"{slots}"
        '<label class="field" for="pu">Pickup or confirmation number (optional)</label>'
        '<input type="text" id="pu" name="pickup_number" maxlength="64" autocomplete="off">'
        '<label class="field" for="nm">Your name (optional)</label>'
        '<input type="text" id="nm" name="name" maxlength="80" autocomplete="name">'
        '<button name="do" value="confirm">Confirm pickup time</button></form>'
    )
    parts.append(
        '<form class="card" method="post" id="propose"><h2>None of these work?</h2>'
        '<p class="muted">Propose a time and we will confirm it by email.</p>'
        '<div class="row"><div><label class="field" for="dt">Date</label>'
        '<input type="date" id="dt" name="date" required></div>'
        '<div><label class="field" for="tm">Time (optional)</label>'
        '<input type="time" id="tm" name="time"></div></div>'
        '<label class="field" for="nt">Note (optional)</label>'
        '<input type="text" id="nt" name="note" maxlength="300">'
        '<label class="field" for="nm2">Your name (optional)</label>'
        '<input type="text" id="nm2" name="name" maxlength="80" autocomplete="name">'
        '<button class="quiet" name="do" value="propose">Propose this time</button></form>'
    )
    parts.append('<p class="muted">Questions? Reply to the email.</p>')
    return _page("Pickup appointment", "".join(parts))


async def _form(request: Request) -> dict[str, str]:
    raw = await request.body()
    if len(raw) > MAX_FORM_BYTES:
        raise HTTPException(status_code=413, detail="form too large")
    parsed = parse_qs(raw.decode("utf-8", "replace"), keep_blank_values=True, max_num_fields=20)
    return {key: values[0] for key, values in parsed.items()}


FormDep = Annotated[dict[str, str], Depends(_form)]
router = APIRouter(tags=["links"])


@router.get("/c/{token}", include_in_schema=False)
def link_page(
    token: str, session: SessionDep, now: NowDep, settings: SettingsDep, s: int | None = None
) -> Response:
    """The times offered; nothing is recorded by opening it."""
    try:
        offer = read_token(session, token, settings)
    except LinkError:
        return _not_ours()
    return _view(offer, settings, state=offer_state(offer, now), chosen=s, said=None)


@router.post("/c/{token}", include_in_schema=False)
def link_answer(
    token: str, form: FormDep, session: SessionDep, now: NowDep, settings: SettingsDep
) -> Response:
    """Record the time the vendor picked or proposed, then show the page again."""
    try:
        offer = read_token(session, token, settings)
    except LinkError:
        return _not_ours()
    if form.get("do") == "propose":
        said = propose(
            session,
            offer,
            settings=settings,
            now=now,
            day=form.get("date", ""),
            clock=form.get("time"),
            note=form.get("note"),
            name=form.get("name"),
        )
    else:
        raw = form.get("slot", "")
        said = confirm(
            session,
            offer,
            int(raw) if raw.isdigit() else -1,
            settings=settings,
            now=now,
            pickup_number=form.get("pickup_number"),
            name=form.get("name"),
        )
    if said.ok:
        session.commit()  # stored before the vendor is told it was
        return RedirectResponse(f"/c/{token}", status_code=303, headers=HEADERS)
    session.rollback()
    return _view(offer, settings, state=offer_state(offer, now), chosen=None, said=said)


@router.get("/robots.txt", include_in_schema=False)
def robots() -> PlainTextResponse:
    """Nothing here is for search engines."""
    return PlainTextResponse("User-agent: *\nDisallow: /\n")


def create_links_app(settings: Settings | None = None) -> FastAPI:
    """The public app: the vendor pages and a health check, nothing else."""
    settings = settings or get_settings()
    engine = make_engine(settings.database_url)
    init_db(engine)
    app = FastAPI(
        title="Pickup appointment links",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.state.sessions = session_factory(engine)
    app.state.settings = settings
    app.include_router(router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app
