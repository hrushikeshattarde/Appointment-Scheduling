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
| `facility-profiles harvest --terminal 1089 --customer 6680 --customer 7211` | Same, restricted to loads billed to the given Transport Pro customer IDs (`customerId` on `/load/search`). Use it for a pod that serves many shippers, such as the Lidl inbound and outbound customers on the Megan Goodwin pod. `run` takes the same `--customer` option; `FP_PILOT_CUSTOMER_IDS` sets the default. |
| `facility-profiles run [--no-harvest] [--refresh] [--cap N] [--terminal ID] [--fake-llm] [--offline] [--resume RUN_ID]` | Full run: collect, extract, score and apply every facility. `--refresh` only re-processes facilities with new sources. `--resume` continues a failed run. |
| `facility-profiles lookup 196508` / `lookup "carolina beverage"` | Shows the stored profile, per role, with confidence and state per field. |
| `facility-profiles review list` / `accept ID --by NAME` / `edit ID --value V --by NAME` / `reject ID --by NAME` | Works the review queue (FR-11). Decisions become human-set values the routine never overwrites. |
| `facility-profiles profile set FACILITY ROLE FIELD --value V --by NAME [--reason TEXT]` | Files a human-set value by hand (a store key, Transport Pro location ID or unique name), closes any open queue item for that field and audits the reason. The routine never overwrites it. |
| `facility-profiles profile summary FACILITY ROLE --text TEXT --by NAME` | Replaces the scheduling summary with a human-written one. |
| `facility-profiles profile ask FACILITY ROLE FIELD --reason TEXT [--proposed V]` | Puts a question on the review queue outside a run, for example the booking address of a vendor. |
| `facility-profiles export [--out file.csv]` | Trusted values in Transport Pro field names for the vendor bulk import (FR-10). |
| `facility-profiles export-xlsx [--out file.xlsx] [--facility F ...]` | Reviewer workbook: a Review Queue sheet with Decision (accept/edit/reject), Corrected value and Reviewer columns, plus Profile Fields, Scheduling Summaries, Facilities, Audit Log and Runs sheets. `--facility` (repeatable) restricts every sheet to those facilities, for a focused hand-off. |
| `facility-profiles review import file.xlsx [--by NAME] [--dry-run]` | Reads the filled-in Review Queue sheet and applies each decision as a human-set value; rows with a blank Decision are skipped and problems are listed per row. |
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

## One store per pod

`run` profiles every facility in the store and `export-xlsx` exports the whole store, so each
pilot pod gets its own SQLite file. Point `FP_DATABASE_URL` at it for every command of that pod:

```powershell
$env:FP_DATABASE_URL = 'sqlite:///./data/facility_profiles_pod-1089-lidl.db'
facility-profiles harvest --terminal 1089 --customer 6680 --customer 7211 --days 90
facility-profiles run --no-harvest --terminal 1089 --customer 6680 --customer 7211 --budget 8
facility-profiles export-xlsx --out exports/facility-profiles-review-pod-1089-lidl-2026-09-29.xlsx
```

Stores so far: `data/facility_profiles.db` (terminal 1160, POD Frankie Saiz, all customers) and
`data/facility_profiles_pod-1089-lidl.db` (terminal 1089, POD Megan Goodwin, Lidl inbound 7211
and Lidl outbound 6680 only).

### Lidl vendor profiles (POD Megan Goodwin)

Lidl store deliveries are tours planned by Lidl (nothing to book), so the bookable facilities on that pod are the twelve vendor pickup sites on Lidl - Inbound loads. `scripts/seed_lidl_vendor_profiles.py` files what the Transport Pro notes and the lidl@ group mail established for them (firm appointments, exact times, driver rules, and for Morgan Foods the email booking desk) as human-set values with their evidence, and queues the two questions Megan has to answer per vendor: booking channel and appointment-desk address. Hand-off workbook: `export-xlsx --facility <vendor> ...`; her answers come back through `review import`. `scripts/seed_lidl_vendor_mail_findings.py` then files the booking channels confirmed in the lidl@ Google Groups archive (Opendock for Polar Fitzgerald, email desks for the rest, inbound@lidl.us for the RDC delivery slots), each with the thread it rests on.

## Booking agent prototype (draft mode)

`facility-profiles booking ...` books vendor pickup appointments by email for customer-tendered
inbound loads (built for the Lidl inbound pod). It never sends mail and never writes to
Transport Pro: a person sends each draft and approves each confirmation.

```powershell
$env:FP_DATABASE_URL = 'sqlite:///./data/facility_profiles_pod-1089-lidl.db'
facility-profiles booking scan --terminal 1089 --customer 7211 --days-ahead 7
facility-profiles booking list                     # status, !open exceptions, vendor, PO, slot
facility-profiles booking list --exception any     # only what needs a person
facility-profiles booking draft            # one .eml per desk in exports/drafts
facility-profiles booking sent 12 --by megan --thread <gmail thread id>
facility-profiles booking inbox --file data/lidl-mail/messages.jsonl   # or --key/--subject
facility-profiles booking show 12
facility-profiles booking approve 12 --by megan
facility-profiles booking resolve 12 facility_question --by megan --note "answered by phone"
facility-profiles booking booked 12 --by megan --via phone --date 2026-10-05 --time 09:00
```

### Status and exceptions

A case's status says only where the pickup appointment stands: `unscheduled` (nothing asked of
the vendor yet; a draft may be waiting to be sent), `pending` (a request went out), `scheduled`
(approved by a person, booked another way, or already carrying a vendor pickup number),
`declined` (the vendor cannot book as asked) or `canceled`. Whatever a person has to do or
decide is an exception on the case, raised with a one-line description and resolved by the
agent when the situation clears or by a person with a note (`booking resolve`):

| Exception | Raised when | Cleared by |
|---|---|---|
| `missing_method` | the profile has no verified email booking desk | a person (`resolve`, `booked`, `close`) |
| `method_not_supported` | the vendor books on a portal, by phone, first come first served | a person |
| `slot_unworkable` | the slot passed, is inside the notice window, or cannot make the delivery | a moved delivery that fixes it, `reschedule`, a person |
| `confirmation_review` | the vendor confirmed a slot (or the agent accepted its offer) | `approve` (the case becomes scheduled) |
| `proposed_time_review` | the vendor offered a different time | the agent accepting it or asking for other days, a person |
| `facility_question` | the vendor asked something | the agent answering it from the case, a person |
| `facility_declined` | the vendor cannot book as asked | the pickup asked for again (`reschedule`, a new delivery slot from the customer desk) |
| `stale_confirmation` | a "confirmation" of a slot already past when the vendor wrote | a person |
| `delivery_moved` | the customer moved the delivery and nothing re-requested the pickup | a person, or the pickup asked for again |
| `handoff` | the agent stopped and nothing more specific was open | a person |

When the agent hands a reply to a person (money, the round cap, no safe answer) the exception
the reply raised stays open with the agent's reason added to it. A later reply about the slot (a
confirmation, an offer, a deferral, a decline) supersedes what earlier replies left open; a
question only supersedes an earlier question, so "Which door?" after a confirmation leaves the
confirmation waiting for approval. `close` cancels a case and resolves everything on it;
`booked` records a pickup booked outside the agent (phone, portal, a person's own email) and
schedules the case. Stores written before this split are moved onto the new statuses the next
time any command opens them, each parked case getting the exception its old status implied and a
`status_migrated` event.

How a case moves: `scan` opens a case for every pickup stop whose appointment is not
confirmed, keyed to the vendor profile (`missing_method` or `method_not_supported` when the
profile has no verified email desk, `scheduled` when the load already carries a vendor pickup
number). `draft` composes the request in the pod's own wording (`PO# X on MM/DD @ HHMM`, the
requested time from the tender or backed off the Lidl delivery slot, and nothing else, exactly
as the pod writes it) and saves it as a draft. `sent` records that a person sent it. `inbox`
matches replies to cases by thread, PO number or sender, classifies each reply with a
strict-schema model call (confirmed, counter-offer, question, rejected, unrelated), drops any
date, time or pickup number the reply's own words do not back (link cruft such as
`115802102660<tel:(580)%20210-2660>` is stripped first, and a quote from the quoted history
under the reply only ever backs a counter-offer), and moves the case: a confirmation sets the
slot in UTC and raises `confirmation_review` (a bare "SET" or a pickup number alone means the
requested slot; a time alone means the requested date), deferred ("check back Monday") keeps
waiting, a counter-offer, a question or a decline raises its exception. A "confirmation" of a
slot that had already passed when the vendor wrote ("latest is 9pm tonight" after a missed
pickup) is raised as `stale_confirmation`, a work-in note for a person. A confirmation is
answered once with the pod's "Thank you!". `approve` records the decision and prints the exact
Transport Pro `set_appointment` payload; the write itself stays behind the client's write flag.
Mail that arrives on a scheduled or canceled case (driver ETAs, securement, "did this get
resolved?") is kept on the case and never read as a new answer.

The conversation policy (`booking/respond.py`) handles what comes back, still as drafts:
a counter-offer is accepted when the offered pickup still makes the customer's delivery
slot (miles at `FP_BOOKING_AVG_MPH` plus `FP_BOOKING_LOAD_HOURS`), otherwise the agent asks for
alternatives inside the workable window; a factual question is answered only from data on
the case (PO numbers, carrier, delivery site and number, load number), by rule first and by
the model second, and every number in the answer must exist on the case; a vendor that
cannot ship gets a drafted note to the customer's inbound desk (`FP_BOOKING_CUSTOMER_DESK`)
asking for a new delivery slot; `booking follow-up` nudges once after
`FP_BOOKING_FOLLOW_UP_HOURS` of silence. Replies that mention rates, fees, detention, claims
or damage, and threads past `FP_BOOKING_MAX_ROUNDS`, go to a person untouched.

Also from the threads: a desk that serves several shippers (`FP_BOOKING_SHARED_DESKS`, the CCI
desk by default) is asked "for Koch Foods going to Lidl"; a first-come-first-served shipper
(appointment not required, or window granularity on the profile) gets a date with no time; a
vendor's "check back on Monday" is honoured, with the nudge sent that day as "Checking in on
this!"; and `booking reschedule ID --date --time --by [--note]` drafts the in-thread request
for a new slot after a Circle-side miss, the most common event in the archive.

Third pass through the archive added: the quoted history under a reply is shown to the classifier
and can back a counter-offer (vendors edit times inside it); `booking draft` batches the cases ready to draft into
one email per desk with one line per PO, like the pod; mail from the customer's inbound desk is
never treated as a vendor reply but is read for a new delivery slot ("8/20 7AM - GRM_200826926"
or "FRG_200526615 05/20 @ 1100"), which moves the pickup request in the vendor thread; and
`booking delivery-updated` records a DCT rebooking a person made and drafts the pod's note to the
desk. Lidl delivery slots themselves live in Lidl's DCT dock portal (AMB, CHL and FRZ tabs), which
Circle books directly; the desk only helps when no slot is free.

Tables: `booking_cases`, `booking_messages`, `booking_events`, `booking_exceptions` (created by
`init-db`, which also adds new columns to older stores and moves old statuses over).
Code: `booking/service.py` (cases), `booking/worklist.py` (exceptions and the status migration),
`booking/classify.py` (reply reading and validation), `booking/mail.py` (JSONL or Gmail in,
`.eml` or Gmail drafts out).
Real-text regression: `tests/fixtures/lidl_morgan_foods_thread.jsonl` is the pod's September 2026
Morgan Foods thread in the mail pull's format, Outlook cruft included, and
`tests/test_booking_real_thread.py` replays it with the model's recorded readings.

### Three rules from the live threads

A freshly scanned case is checked before any email is written. A desk listed in
`FP_BOOKING_PO_DATE_FLOOR_DESKS` (Morgan Foods by default) reads the DDMMYY date inside a Lidl PO
as the earliest pickup, so a request earlier than that day is moved up to it (weekends roll to
Monday); if the floored day can no longer make the delivery the case goes to a person instead.
A requested slot that has already passed, or sits inside `FP_BOOKING_MIN_NOTICE_HOURS`, goes to
a person too, at scan time and again at draft or send time, because a same-day ask is a phone
call. The inbound desk's delivery slots are read in every wording seen so far, including the
two-line "9/30 at 1100" then "PYE_300926723".

### One reply, several POs

A batched request covers several cases with one email, and Morgan Foods answers it line by
line ("A & B-9/28 @ 9am pickup# 20463264" then "C & D-10/2 @ 9am pickup# 20463798, we cannot
schedule early pickups"). The classifier returns one entry per PO line (`items`), each checked
against the text like the whole reply and dropped when its POs are nowhere in the message; a reply
is matched to every case it belongs to (by Message-ID, thread, or PO numbers) and applied line by
line, a case whose POs the reply never names is left where it was, and at most one message goes
back for one reply: the conversation policy's answer if there is one, else a single "Thank you!".

### Sending as the agent, and tying replies to requests

Every outbound message now carries its own RFC `Message-ID`, and every stored message keeps
its `In-Reply-To` and `References`. A vendor's reply is matched to its case through those ids
first, so the match holds whatever mailbox the reply is read from; the Gmail thread, a PO number
in the reply's own words and a lone open case for the sender remain as fallbacks. A person's own
send of a drafted request is recognised in the archive (same recipient desk, same subject or the
case's POs) and links the case automatically, so `booking sent` is rarely needed.

`FP_BOOKING_MODE=send` with `FP_BOOKING_GMAIL_KEY` and `FP_BOOKING_GMAIL_USER` lets
`booking send [CASE]` deliver requests through the Gmail API as that mailbox (a Google Group
cannot send; the agent writes from a member mailbox and copies the group). A send passes a
deterministic gate first: send mode on, the recipient equal to the profile's trusted desk, and
fewer than `FP_BOOKING_SEND_DAILY_CAP` sends in the last 24 hours. The case moves straight to
`pending` with the Gmail id, thread and Message-ID recorded. Replies drafted by the conversation
policy answer the vendor's Message-ID and go through the same outbox, so with a sending outbox
they are sent too; the CLI keeps them as drafts until the policy gate for unattended replies
lands. The service-account key needs `gmail.send` under domain-wide delegation.

### Group-mail archive in S3

`facility-profiles mail-archive ...` keeps the lidl@ group's pickup-appointment threads in S3, on
the doc-intake bot's pattern. A Google Group has no mailbox, so the collector reads the group's
traffic through a member's mailbox (Gmail API, domain-wide delegation, read-only), keeps the
threads that are about booking a pickup (`mailarchive/filters.py`: the pod's "Pick Up
Appointment" subjects, the inbound desk's PO-number and RESCHEDULE subjects, the portals'
appointment notices, and any message a known appointment desk took part in; tour planning and
Emerge notices are dropped), and writes each message as `mail/yyyy/mm/dd/<key>.eml` (raw RFC822)
plus `<key>.json` (headers including Message-ID, the reply's own words, the quoted history, PO and
pickup numbers, DCT references, why it was kept) with every attachment once under
`attachments/<sha256>`. `<key>` is derived from the RFC Message-ID, so the same message read from
two members' mailboxes is one object and a backfill from a long-standing member merges cleanly. A
thread is kept as a whole once any message in it qualifies, and the earlier messages of a newly
matched thread are collected with it.

```powershell
facility-profiles mail-archive collect --key <service-account.json> --subject <member> --bucket <bucket> --days 10
facility-profiles mail-archive status --bucket <bucket>
facility-profiles booking inbox --s3 s3://<bucket> --days 7      # replies straight from the archive
python scripts/deploy_lidl_mail_archive.py bucket | deploy | invoke | status | schedule on|off
```

In AWS the same code runs as Lambda `circle-lidl-mail-collector` every 15 minutes (EventBridge
Scheduler `circle-lidl-mail-every-15-min`), reading the service-account key from Secrets Manager
and listing the last three days each pass; keys are content-derived, so a pass may stop anywhere
and the next one stores only what is missing. Settings: `FP_MAIL_ARCHIVE_BUCKET`,
`FP_MAIL_ARCHIVE_PREFIX`, `FP_MAIL_ARCHIVE_GMAIL_USER`; the `aws` extra adds boto3.

## Review flow for CSRs

1. `facility-profiles export-xlsx` and send the workbook to the pod.
2. Reviewers fill in the three yellow columns on the Review Queue sheet: Decision, Corrected value (only for edit) and Reviewer. Blank rows are skipped.
3. `facility-profiles review import <file> --dry-run` to see what would be applied, then without `--dry-run`.
4. Decisions become human-set values the routine never overwrites; the audit log records who decided what. `run --replay` then re-scores everything else at no model cost.

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
