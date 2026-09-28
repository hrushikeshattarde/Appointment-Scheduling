# facility-profiles

Idea 1 of the Appointment Scheduling Automation programme. A nightly routine that builds a
scored **scheduling profile** for every facility Circle ships to or from, extracted from the stop
notes, dispatch notes, tracking notes and confirmed appointments already held in Transport Pro,
and writes it to a staging store with a review queue and a full audit trail.

PRD: *Appointment Scheduling Automation — Idea 1 PRD* (Claude doc). Requirement IDs in the code
(`FR-1` … `FR-17`) refer to that document.

## What it does

```
 Transport Pro API                      facility-profiles                         People
 ─────────────────      ┌──────────────────────────────────────────────┐      ──────────────
 /load/search      ──►  │ 1 harvest   loads → stops → facility or       │
 /location/{id}    ──►  │             candidate key, links, stop notes   │
 /load/{id}/notes  ──►  │ 2 collect   facility record + recent load,     │
 /tracking/note/.. ──►  │             tracking and dispatch notes        │
                        │ 3 extract   one structured-output LLM call     │
                        │             per facility and role              │
                        │ 4 validate  every quote must exist in a source │
                        │             phones/emails/URLs verbatim        │
                        │ 5 score     agreement across loads, recency,   │
                        │             conflicts                          │
                        │ 6 apply     write / verify / queue / discard   │──►  review queue
                        │             never overwrite a human value      │──►  daily digest
                        │ 7 audit     before, after, sources, model      │──►  CSV export
                        └──────────────────────────────────────────────┘      lookup API
```

Transport Pro has no facility *write* endpoint today, so "written" means authoritative in the
staging store and included in the CSV export for the vendor bulk import. The
`FacilityWriteAdapter` seam in `pipeline/writer.py` is where a real endpoint plugs in.

## Setup

Requirements: Python 3.12+, [uv](https://docs.astral.sh/uv/), Transport Pro API credentials,
Anthropic access (`ant auth login` or `ANTHROPIC_API_KEY`).

```bash
uv sync --all-extras
cp .env.example .env        # then fill in TPRO_* and adjust FP_* values
uv run facility-profiles init-db
uv run facility-profiles check-tpro
```

`.env` uses the same `TPRO_BASE_URL`, `TPRO_USERNAME`, `TPRO_PASSWORD` names as the Transport Pro
MCP server, so the existing file can be reused. Never commit `.env`.

## Commands

| Command | What it does |
| --- | --- |
| `facility-profiles check-tpro` | Authenticates and reads one terminal list and one load page. Read-only smoke test. |
| `facility-profiles harvest --terminal 1160 --days 90` | Pulls loads by pickup-date windows, resolves stops to facilities, stores links and stop notes (FR-1, FR-2). |
| `facility-profiles run [--no-harvest] [--refresh] [--cap N] [--terminal ID] [--fake-llm] [--offline] [--resume RUN_ID]` | Full run: collect, extract, score and apply every facility. `--refresh` only re-processes facilities with new sources. `--resume` continues a failed run. |
| `facility-profiles lookup 196508` / `lookup "carolina beverage"` | Shows the stored profile, per role, with confidence and state per field. |
| `facility-profiles review list` / `accept ID --by NAME` / `edit ID --value V --by NAME` / `reject ID --by NAME` | Works the review queue (FR-11). Decisions become human-set values the routine never overwrites. |
| `facility-profiles export [--out file.csv]` | Trusted values in Transport Pro field names for the vendor bulk import (FR-10). |
| `facility-profiles digest [--out file.md]` | Markdown digest for the pod lead: what the run did, what needs a decision (FR-13). |
| `uvicorn facility_profiles.api.app:create_app --factory` | Lookup and review HTTP API (`/facilities/{id}`, `/facilities?name=`, `/review`, `/digest`). Needs the `api` extra. |

Run modes: `FP_MODE=recommend` (default; qualifying fields are stored as recommendations and
audited as `recommend`) or `FP_MODE=write`. Switching back to `recommend` is the kill switch
(FR-15); it takes effect on the next run.

## How a value becomes trusted

Every observation of a value is a **mention**: an LLM quote from a note, a structured signal
(stop status, service level, confirmed window, stop contact) or a value already on the facility
record. Mentions are grouped by normalised value and weighted by recency (half-life 90 days) and
source confidence.

```
support    = weight of the winning value / total weight for the field
breadth    = min(1, 0.5 + 0.25 * (distinct loads backing the winner - 1))
confidence = support * breadth * best source confidence for the winner
```

One load can reach at most 0.5, two agreeing loads 0.75, three or more 1.0. Then the policy in
`domain/rules.py` (FR-9):

| Situation | Decision |
| --- | --- |
| a second value has 30% or more of the weight | queue (conflict) |
| confidence below `FP_QUEUE_THRESHOLD` (0.5) | discard |
| record already holds an equal value | verify |
| record holds a different value | queue |
| confidence at or above `FP_WRITE_THRESHOLD` (0.8) and the field is empty | write (or recommend) |
| otherwise | queue |

A field not seen in any source for `FP_STALE_AFTER_DAYS` (180) is marked stale and queued.

## Data model

Tables in `storage/models.py`: `runs`, `facilities`, `facility_aliases`, `facility_loads`,
`source_documents`, `profiles`, `profile_fields`, `review_items`, `audit_log`, `checkpoints`.
SQLite is the pilot default; set `FP_DATABASE_URL=postgresql+psycopg://…` (install the `postgres`
extra) for production. Alembic migrations are to be added before the first Postgres deployment.

Profile fields: `appointment_required`, `booking_method`, `contact_name`, `contact_phone`,
`contact_email`, `portal_url`, `portal_vendor`, `notice_period_hours`, `time_granularity`,
`receiving_hours`, plus a one-line `scheduling_summary`. Mapping to Transport Pro fields is in
`domain/rules.py::to_tpro_write`.

## Transport Pro client

`tpro/client.py` implements the Public API as used by the Transport Pro MCP server: Basic-auth
login and refresh on `POST /auth`, bearer tokens, one re-auth on 401, exponential backoff with
`Retry-After` on 429/5xx, a request-rate limiter, zero-based `page` pagination, and a
`allow_writes` flag that is **off by default** so Phase 1 cannot touch loads (FR-17). Models in
`tpro/models.py` tolerate the API's `false`-for-null and malformed timestamps.

Verified read-only against the live API on 22 September 2026: login and bearer flow, the
zero-based `page` parameter on `/load/search` (page 0 and page 1 of one day's loads had no
overlapping IDs), `/location/{id}`, `/load/{id}/notes`, `/tracking/note/load/{id}` and
`/voiceai/load/{id}` all parse into the models.

## LLM

Two providers, selected with `FP_LLM_PROVIDER`:

- `anthropic` (default): `extraction/llm.py` calls Claude through the Anthropic SDK with
  structured outputs (`messages.parse` + a Pydantic schema) and a cached system prompt.
- `openrouter`: `extraction/openrouter.py` calls the same model through OpenRouter's
  OpenAI-compatible endpoint with a strict JSON-schema `response_format`; set
  `OPENROUTER_API_KEY`. Bare model names such as `claude-opus-5` become
  `anthropic/claude-opus-5`.

Both use `claude-opus-5` by default (`FP_LLM_MODEL`). The model returns candidate values with verbatim quotes that reference
numbered sources; `extraction/validate.py` drops any quote not found in its source and any
phone, email or URL not present verbatim (FR-5, FR-6). `--fake-llm` runs the pipeline without
model calls.

## Development

```bash
make check          # ruff, mypy --strict, pytest with coverage gate (80%)
uv run pytest -q
uv run pre-commit install
```

Tests use synthetic Transport Pro payloads (`tests/conftest.py`), an in-memory SQLite store, a
fake API client and a scripted extractor; the client is tested against mocked HTTP with respx.

## Known gaps and next steps

- Vendor asks from the PRD are still open: a facility write path, a facility search endpoint,
  the allowed `appointments.method` values, the `needsAppointment` filter definition.
- Receiving-hours inference from actual arrival and departure times (`/voiceai/load/{id}`) is
  modelled but not yet wired into collect.
- Digest delivery (email or Slack) and single sign-on in front of the API are deployment
  concerns not included here.
- Alembic migrations before Postgres.
