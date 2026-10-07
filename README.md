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
| `facility-profiles profile portals [--apply --by NAME]` | Lists the profiles whose portal vendor their portal URL contradicts ("other" for a Costco, UNFI, Ahold, Publix or Bozzuto's portal, or a wrong vendor); `--apply` files the URL's vendor, keeping the field's state and auditing it. A vendor a person set is only listed. |
| `facility-profiles export [--out file.csv]` | Trusted values in Transport Pro field names for the vendor bulk import (FR-10). |
| `facility-profiles export-xlsx [--out file.xlsx] [--facility F ...]` | Reviewer workbook: a Review Queue sheet with Decision (accept/edit/reject), Corrected value and Reviewer columns, plus Profile Fields, Scheduling Summaries, Facilities, Audit Log and Runs sheets. `--facility` (repeatable) restricts every sheet to those facilities, for a focused hand-off. |
| `facility-profiles review import file.xlsx [--by NAME] [--dry-run]` | Reads the filled-in Review Queue sheet and applies each decision as a human-set value; rows with a blank Decision are skipped and problems are listed per row. |
| `facility-profiles digest [--out file.md]` | Markdown digest for the pod lead: what the run did, what needs a decision (FR-13). |
| `facility-profiles serve [--db URL] [--port 8000] [--timers-every 15] [--scan-every 30 --scan-customer lidl]` | The appointments board at `/app/` (see below) plus the lookup and review HTTP API (`/facilities/{id}`, `/facilities?name=`, `/review`, `/digest`) and the board's API under `/api/booking`. Needs the `api` extra. `uvicorn facility_profiles.api.app:create_app --factory` serves the same app. |

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
facility-profiles harvest --customer lidl --days 90     # = --terminal 1089 --customer 6680 --customer 7211
facility-profiles run --no-harvest --customer lidl --budget 8
facility-profiles export-xlsx --out exports/facility-profiles-review-pod-1089-lidl-2026-09-29.xlsx
```

Stores so far: `data/facility_profiles.db` (terminal 1160, POD Frankie Saiz, all customers) and
`data/facility_profiles_pod-1089-lidl.db` (terminal 1089, POD Megan Goodwin, Lidl inbound 7211
and Lidl outbound 6680 only).

### Lidl vendor profiles (POD Megan Goodwin)

Lidl store deliveries are tours planned by Lidl (nothing to book), so the bookable facilities on that pod are the twelve vendor pickup sites on Lidl - Inbound loads. `scripts/seed_lidl_vendor_profiles.py` files what the Transport Pro notes and the lidl@ group mail established for them (firm appointments, exact times, driver rules, and for Morgan Foods the email booking desk) as human-set values with their evidence, and queues the two questions Megan has to answer per vendor: booking channel and appointment-desk address. Hand-off workbook: `export-xlsx --facility <vendor> ...`; her answers come back through `review import`. `scripts/seed_lidl_vendor_mail_findings.py` then files the booking channels confirmed in the lidl@ Google Groups archive (Opendock for Polar Fitzgerald, email desks for the rest, inbound@lidl.us for the RDC delivery slots), each with the thread it rests on.

## Customers

Everything that differs from one customer to the next lives in one file per customer, and no
other code names a customer. Lidl's is
[`src/facility_profiles/customers/lidl.toml`](src/facility_profiles/customers/lidl.toml):

| Section | What the agent does with it | Lidl |
|---|---|---|
| `name` | how emails name the customer ("for Koch Foods going to Lidl?") | `Lidl` |
| `[transport_pro]` | which loads are theirs (`customer_ids`, else `customer_names`); `harvest`/`run --customer lidl` cover these ids on these pods, `booking scan --customer lidl` only `booking_customer_ids` | 7211, 6680; pod 1089; books for 7211 only (outbound loads are Lidl's own tours) |
| `[mail]` | drafts are from `group`, copy `cc` (default: the group), end with `signature` | lidl@circledelivers.com |
| `[customer_desk]` | where "the vendor cannot ship" goes; mail from it moves the delivery slot; `delivery_system` names where the pod rebooks | inbound@lidl.us, DCT |
| `[numbers]` | `po`: subjects made of PO numbers; `po_date`: the date inside a PO (named groups dd, mm, yy); `delivery_ref`: read off the delivery stop and the desk's mail | `\d{12}`, DDMMYY in digits 5-10, `[A-Z]{3}_\d{6,}` |
| `[mail_archive]` | which group mail the S3 archive keeps or drops, on top of the shared rules | "Lidl Pick Ups", DCT, the vendor desks; drops "CIR Capacity" |
| `timezone` | what "today" means in `booking today --customer lidl` | America/New_York |

A load or case belongs to the file that lists its Transport Pro customer id (or name). A load no
file claims is written with the `FP_BOOKING_*` settings, which name no customer: no cc, no
customer desk, a plain Circle signature. A desk that books for two customers gets one email per
customer, each copied to its own group.

Two lists stay pod-wide because they describe vendor desks, not customers:
`FP_BOOKING_SHARED_DESKS` (desks that need the shipper and customer named) and
`FP_BOOKING_PO_DATE_FLOOR_DESKS` (desks that read the customer's PO date as the earliest pickup;
it applies only to customers whose file has a `po_date`).

### Adding a customer

1. Find the customer's Transport Pro customer record(s) and the pod that books them (customer
   search by terminal; a name search can miss, as it did for Lidl).
2. Write the starter file and check it:

   ```powershell
   facility-profiles customers new acme --name "Acme" --tpro-customer 1234 --terminal 1160 --group acme@circledelivers.com --desk inbound@acme.example
   facility-profiles customers show acme --sample "10/6 0700 - ACM-123456"   # what the agent reads
   facility-profiles customers list
   ```

   The file goes into the built-in folder, which the public repository carries. For a customer
   whose desks should stay private, set `FP_CUSTOMERS_DIR` to a folder outside the repository
   first; `customers new` writes there, and a file there replaces a built-in one with the same key.
3. Fill in what you know: the PO and delivery-reference patterns, the customer desk, the mail
   archive rules. Leave out what you do not; `customers show` lists what is not set and what that
   turns off.
4. A new pod gets its own store: set `FP_DATABASE_URL`, then `harvest --customer acme` and
   `run --no-harvest --customer acme`.
5. File the vendor desks with `profile set` (booking method, desk address, `required_refs`,
   `cutoff_time`), the way `scripts/seed_lidl_*.py` did for Lidl.
6. Word it the pod's way if it differs: `booking template set request --customer acme ...`.
7. Book in draft mode: `booking scan --customer acme`, `booking draft`, and read the drafts in
   `exports/drafts` (from, cc and signature are Acme's) before anything is sent.
8. Optional: `FP_CUSTOMERS=acme` makes it the pod's default for harvest, run and scan;
   `mail-archive collect --customer acme` archives its group (the Lambda takes
   `ARCHIVE_CUSTOMER=acme`).

## Booking agent prototype (draft mode)

`facility-profiles booking ...` books vendor pickup appointments by email for customer-tendered
inbound loads (built for the Lidl inbound pod). By default it never sends mail and never
writes to Transport Pro: a person sends each draft and approves each confirmation. Sending
(`FP_BOOKING_MODE=send`) and Transport Pro write-back (`FP_BOOKING_TPRO_WRITEBACK`) are separate
settings, both off. To try it without touching real data, follow [TESTING.md](TESTING.md).

```powershell
$env:FP_DATABASE_URL = 'sqlite:///./data/facility_profiles_pod-1089-lidl.db'
facility-profiles booking scan --customer lidl --days-ahead 7   # = --terminal 1089 --customer 7211
facility-profiles booking list                     # status, !open exceptions, vendor, PO, slot
facility-profiles booking list --exception any     # only what needs a person
facility-profiles booking draft            # one .eml per desk in exports/drafts
facility-profiles booking sent 12 --by megan --thread <gmail thread id>
facility-profiles booking inbox --file data/lidl-mail/messages.jsonl   # or --key/--subject
facility-profiles booking show 12
facility-profiles booking approve 12 --by megan
facility-profiles booking resolve 12 facility_question --by megan --note "answered by phone"
facility-profiles booking booked 12 --by megan --via phone --date 2026-10-05 --time 09:00 --desk 812-794-1152
facility-profiles booking desks             # how each vendor was booked before
facility-profiles booking find PYE_061026919     # the case(s) any number belongs to
facility-profiles booking timers            # no reply in 24 h / 48 h, pickup passed unbooked
facility-profiles booking ref 12 shipment_number 7781234 --by megan   # a number the desk needs
facility-profiles booking today --out exports/today.txt  # the daily summary (runs the timers)
```

### Appointments board

A web page for account managers and the pod to keep track of every pickup appointment, on the
same store the agent writes. Start it with `facility-profiles serve --db <store>` and open
`http://127.0.0.1:8000/app/`.

It is written for account managers, in plain words, with one colour per meaning on every page:
green booked, blue asked the vendor, amber needs a person, red missed, grey not booked yet
(Circle Logistics colours; light and dark follow the computer's setting).

- **Home**: a greeting, a four-step "how booking works" strip (dismissable), totals (needs your
  attention, missed pickups, not asked yet, waiting on vendor, booked), what needs a person with
  what to do about it, the next seven days by day, missed pickups and to-dos by type. Every
  total and bar opens the matching list; the Home tab shows how many pickups need attention.
- **All pickups**: every pickup by time, with a search box (PO, load, vendor, pickup number,
  delivery reference), status tabs with counts, and more filters (to-do, pickup dates, missed
  only). On a phone the rows become cards.
- **Calendar**: the week by day, each pickup coloured by where it stands, with to-do counts.
- **One pickup** (a side panel): what is happening in one sentence, the steps (not asked yet,
  asked the vendor, booked), the to-dos with a hint each, what you can do (approve the vendor's
  time, I booked it myself, cancel), the pickup details, the emails with how the agent read each
  reply, the history, and more details folded away. Each decision is recorded under the
  signed-in name (without sign-in, the name typed in the page).
- **Access** (admins only): who sees which customer; see [Who sees which customer](#who-sees-which-customer).

The board never sends mail and never writes to Transport Pro itself; approving records the
decision as `booking approve` does and queues the slot for write-back. Without Google sign-in
(below) it has no login: keep it on `127.0.0.1`. One server shows one store, so run one
per pod (`--db`) until the stores move to Postgres. To show it without real data,
`python scripts/seed_booking_demo.py --db sqlite:///./data/booking-demo.db` fills a new store with
20 invented cases, one per situation, built through the agent's own code and the timers.

### Who sees which customer

With Google sign-in on, people sign in with their circledelivers.com Google Workspace account and
see only the customers an admin gave them. Admins see every customer and give access on the
board's **Access** tab: one row per person, one column per customer file, and each cell is
**No access**, **View** (watch the bookings: cases, to-dos, emails, timeline) or **View and act**
(also approve, mark booked, add a number, resolve, cancel), with an optional last day. Every
add, grant, change, revoke and removal is kept in the history under the admin who made it.

Turn it on in `.env` (git-ignored; never in the repository, which is public):

```
FP_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
FP_GOOGLE_CLIENT_SECRET=...
FP_BOARD_ADMINS=you@circledelivers.com        # comma-separated; they see everything
# FP_BOARD_DOMAINS=circledelivers.com         # Workspace domains that may sign in (default)
# FP_BOARD_SESSION_HOURS=8                    # how long a sign-in lasts
# FP_BOARD_PUBLIC_URL=https://board.example   # when a proxy in front changes the address
# FP_BOARD_SESSION_SECRET=                    # unset: made on first start, kept in the store
```

In Google Cloud Console the OAuth client must be a **Web application** whose authorized redirect
URIs include `<board address>/auth/callback`: `http://localhost:8000/auth/callback` for a try on
this machine (open the board as `localhost`, not `127.0.0.1`), and the real `https://` address
once it is hosted. `serve` prints the address it expects. A consent screen of type **Internal**
keeps other Google accounts out before they reach the board; the board checks anyway.

How it holds:

- Signing in uses Google's code flow: a one-time state and nonce, the code traded for the ID
  token with the client secret, and the token's issuer, audience, expiry, nonce, verified email
  and Workspace domain (`hd`) checked. A personal Google account made with a work address has no
  `hd` and is turned away.
- The sign-in cookie is signed (HMAC-SHA256), HttpOnly and SameSite=Lax, and lasts
  `FP_BOARD_SESSION_HOURS`. Access is read from the store on every request, so a change holds
  from the person's next page; nobody has to sign out.
- The server does the filtering: every `/api/booking` route answers with the person's customers
  only, another customer's case is "not found", and a decision on a View customer is refused.
  The facility, review and digest pages are for admins. A change (POST) from another site is
  refused.
- A case belongs to the customer file that claims its Transport Pro customer (`customers
  show`); a case no file claims is shown to admins only.
- Someone who signs in before being given a customer sees whom to ask.
- Without `FP_GOOGLE_CLIENT_ID` and `FP_GOOGLE_CLIENT_SECRET` nothing changes: no login,
  everyone at the machine is an admin, decisions take the typed name. The Access tab still works
  then, so access can be set up before sign-in is switched on. With sign-in on and no
  `FP_BOARD_ADMINS`, `serve` refuses to start.
- `serve --no-sign-in` leaves sign-in off for one run even with the Google client in `.env`,
  for testing on this machine (it refuses any `--host` but this machine's).

The same from the command line, for scripts or when the board is not running:

```
facility-profiles access list
facility-profiles access grant am@circledelivers.com lidl --view --by "Your Name"
facility-profiles access grant am@circledelivers.com lidl --act --until 2026-12-31 --by "Your Name"
facility-profiles access revoke am@circledelivers.com lidl --by "Your Name"
facility-profiles access remove am@circledelivers.com --by "Your Name"
facility-profiles access history [--email am@circledelivers.com]
```

A new customer is a new column: `customers new <key>`, then give the account manager access.

### Times are Eastern

Every time a person reads (the board, the CLI, to-dos, the daily summary, Transport Pro notes,
the logs) and every time in an email to a facility is Eastern: EST, or EDT while daylight saving
is on, written "ET". A time a person types (marking a pickup booked, rescheduling, a new delivery
slot) is Eastern too. The store keeps instants in UTC (Transport Pro takes UTC) and pickup slots
on the facility's own clock, the one its hours, cut-offs and weekends are written in;
`facility_profiles/clock.py` converts at the edges. For a facility on Eastern time (every Lidl
vendor but Seneca in Ripon, WI) the clocks are the same and only the label changes.

A facility outside Eastern time sees "ET" after each time ("PO# X on 10/01 @ 0900 ET"). The reply
reader reports times as written and the zone the reply names; a time with no zone is taken as
ours (Eastern), and when that is not the time we asked for, a person checks which clock the desk
meant (`time_zone_unclear`, "Which time zone?"). The model is told when the reply was written on
the facility's own clock, so a "tomorrow" written at 8:30 PM is the next day, not the one after.

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
| `missing_reference` | the desk needs a number the case lacks (the customer's shipment or SO number) | `booking ref` / "Add reference" once the case has them all, a person |
| `method_not_supported` | the vendor books on a portal, by phone, first come first served | a person |
| `slot_unworkable` | the slot passed, is inside the notice window or past the desk's cut-off or notice, or cannot make the delivery | a moved delivery that fixes it, `reschedule`, a person |
| `confirmation_review` | the vendor confirmed a slot (or the agent accepted its offer) | `approve` (the case becomes scheduled) |
| `proposed_time_review` | the vendor offered a different time | the agent accepting it or asking for other days, a person |
| `facility_question` | the vendor asked something | the agent answering it from the case, a person |
| `facility_declined` | the vendor cannot book as asked | the pickup asked for again (`reschedule`, a new delivery slot from the customer desk) |
| `stale_confirmation` | a "confirmation" of a slot already past when the vendor wrote | a person |
| `delivery_moved` | the customer moved the delivery and nothing re-requested the pickup, or a booked pickup can no longer make the new delivery | a person, or the pickup asked for again |
| `handoff` | the agent stopped and nothing more specific was open | a person |
| `confirmed_outside_window` | the vendor confirmed another day, or a time more than 2 h from the one asked | `approve`, a later reply |
| `unanswered_24h` | timer: no answer 24 weekday hours after we wrote | any answer from the vendor, a sent follow-up, 48 h replacing it |
| `unanswered_48h` | timer: still no answer after 48 weekday hours | any answer from the vendor, a person |
| `pickup_expired` | timer: the pickup passed and the case is not booked | `booked`, `approve`, `reschedule`, a later pickup (an offer, a moved delivery), `close` |
| `send_unconfirmed` | Gmail never answered a send, so the email may or may not have gone | the group's copy or a reply to it being read, `booking sent`, any later reply, a person |
| `attachment_unread` | the facility's email carries a file the agent could not read (a scan, a picture, an old .doc) | a later reply about the slot, a person |
| `email_bounced` | the facility's mail server sent our email back | any reply from the facility, `reschedule`, a person |
| `eta_requested` | the facility asks when the driver will arrive, and the agent could not answer from the driver's check call or position | an answer that left nothing open, a later reply, a person |
| `work_in_offered` | after a missed or late arrival, the facility will still take the truck until a time | a later reply about the slot, a person |
| `on_hold` | the facility put the pickup or the order on hold with no day given (the agent stops chasing) | a later reply about the slot, a person |

When the agent hands a reply to a person (money, the round cap, no safe answer) the exception
the reply raised stays open with the agent's reason added to it. A later reply about the slot (a
confirmation, an offer, a deferral, a decline) supersedes what earlier replies left open; a
question only supersedes an earlier question, so "Which door?" after a confirmation leaves the
confirmation waiting for approval. `close` cancels a case and resolves everything on it;
`booked` records a pickup booked outside the agent (phone, portal, a person's own email) and
schedules the case. Stores written before this split are moved onto the new statuses the next
time any command opens them, each parked case getting the exception its old status implied and a
`status_migrated` event.

### New loads on their own

`booking scan` puts the coming pickups on the board once. With `serve --scan-every 30
--scan-customer lidl` the board does it itself: every 30 minutes it reads Transport Pro (only
reads) for the customer's pickups in the next `FP_BOOKING_DAYS_AHEAD` days, adds the new ones and
keeps the others in step with their loads (canceled, booked in Transport Pro, delivery moved).
Without `--scan-customer` it scans the customers in `FP_CUSTOMERS`, else the pods in
`FP_PILOT_TERMINAL_IDS`; with neither it refuses to start, as it would take every load in
Transport Pro. It is off unless asked for.

A load Transport Pro shows **Delivered** (or in transit or completed, should it use those) has
been picked up, so it needs nobody: an open pickup is marked booked, its to-dos are closed, and the
board lists it under **Picked up**, apart from what is still to happen. A load already delivered
when the scan first sees it is not put on the board. "Dispatched" can still be before the pickup,
so it is not taken as picked up. Pickups whose day has passed leave the scan's window, so each
scan also reads back, by load number, the ones still on the board from the last 30 days
(`RECHECK_DAYS`); a delivered or canceled load among them is settled the same way.

Today on the board says when Transport Pro was last checked. When a check fails (Transport Pro
down, the password changed) the page says so in red, the board keeps working on what it has, the
reason goes to the server's log, and the next check tries again. The scan reads everything from
Transport Pro before it writes to the store, so the board is never kept waiting on Transport Pro.

### Timers and the daily summary

Some to-dos come from time passing, not from a reply (`booking/timers.py`). `booking timers`
runs them once; `booking today` runs them and prints the summary; `serve` runs them every 15
minutes (`--timers-every`, 0 turns them off). They only write to-dos on the store: they never
send mail and never write to Transport Pro.

- **No reply.** Once a request (or any later message) has gone to the vendor and nothing came
  back, `unanswered_24h` is raised after 24 hours and `unanswered_48h` replaces it after 48. Only
  Monday-to-Friday hours in the vendor's time zone count, so a Friday-afternoon request is not
  overdue on Monday morning. The clock starts at the first unanswered message; a follow-up does
  not restart it, an out-of-office or unrelated reply does not stop it, and a case waiting on us
  (a confirmation to approve, a question) is not the vendor's silence. `booking follow-up` counts
  the same silence the same way: it nudges after `FP_BOOKING_FOLLOW_UP_HOURS` weekday hours, once
  per silence (the desk's answer starts a new one), and a note to the customer's desk, a
  person's email or an out-of-office does not end it. A follow-up actually sent settles the 24 h.
- **Check back.** "Check back on Monday" gets a check-back that day, and "check back later" with
  no day gets one after `FP_BOOKING_FOLLOW_UP_HOURS` weekday hours, even when the desk was nudged
  before. One pickup gets at most three nudges; after that the to-dos and a person take over. A
  pickup the facility put on hold is not chased.
- **Pickup passed.** `pickup_expired` is raised when the pickup the case is working towards
  (the confirmed slot, else a time the vendor offered that still waits for a person, else the
  slot asked for) has passed and the case is not booked. It replaces the no-reply and
  `slot_unworkable` to-dos, and is not raised on top of a decline, a late confirmation or a moved
  delivery, which already say what to do. Its description says what happened: never requested,
  drafted but never sent, no booking from the vendor, or a confirmation still waiting for approval.
- **Confirmed another time.** When a vendor confirms another day, or a time more than two hours
  from the one asked for, `confirmed_outside_window` is raised next to the confirmation review
  (date-only desks are compared by day). Approving settles both.

Each raise remembers what started its clock (the unanswered message, the slot), so a person's
resolution sticks; when the situation clears the timers resolve their own to-dos, recorded as
`timer`.

`booking today [--customer NAME] [--out FILE]` (also `GET /api/booking/today`, and "Daily
summary" on the board's overview, with a Copy button) is one plain-text page for the morning, to
read or paste into an email or a chat: every
case with something open, listed once under its most urgent to-do (pickup passed, 48 h silence,
confirmations to approve first; a missing desk last) with how long it has been open; the pickups
today and on the next business day and where each stands; the drafts nobody has sent; and what
changed in the last 24 hours. "Today" is in `FP_BOOKING_TIMEZONE` (Fort Wayne by default).

How a case moves: `scan` opens a case for every pickup stop whose appointment is not
confirmed, keyed to the vendor profile (`missing_method` or `method_not_supported` when the
profile has no verified email desk, `scheduled` when the load already carries a vendor pickup
number). A load Transport Pro shows canceled opens no case. A field listing several POs
(`115806102630 & 115806102631`) gives the case all of them.

Each scan also brings the cases it already has up to date with their loads. A case keeps what
Transport Pro said at the last scan (`tpro_seen`) and takes only what changed there since, so a
slot a person or the customer's desk gave it stands:

- load canceled: a case nothing was written for is canceled; one whose request was drafted or
  sent, or that is booked, raises `load_canceled` once (delete the draft, or tell the vendor);
- a vendor pickup number or a confirmed stop entered in Transport Pro: the case is booked
  there (`booked_in_tpro`), with the stop's time; nothing is queued to write back;
- the DCT slot booked after the scan (its reference in the delivery stop's notes, its time):
  kept on the case, so Lidl's "wait for the delivery slot" rule lets the request go; a slot
  that moves once the vendor was asked raises `delivery_moved`;
- a new tender time or new POs before any request: the request asks for them, and the time
  and the desk's rules are checked again.

A case from before scans kept that record takes only what it lacks on its first rescan.
`draft` composes the request in the pod's own wording (`PO# X on MM/DD @ HHMM`, the
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
answered once with the pod's "Thank you!", but only when nothing about it is in doubt: a time we
did not ask for, a number its words do not back, a tie to the request by sender only, or a time
that misses the delivery gets no thank-you (it would tell the facility the pickup is booked).
`approve` records the decision, prints the exact Transport Pro `set_appointment` payload and
queues it for write-back (see "Writing booked pickups to Transport Pro").
Mail on a canceled case is kept and not read. Mail on a booked (scheduled) case is read for one
thing: whether the facility moved, dropped or put off the booked pickup. The agent never moves
a booking itself: a new time, a decline or a "check back" puts the case back to pending (declined
for a decline) and raises `booked_slot_changed` ("Booked pickup changed", with what was booked and
whether Transport Pro still shows it), which stays open until a person approves a time or marks
the pickup booked; that writes the new time to Transport Pro in place of the old one. The same
time again is noted, a question is answered as usual, a time already past when they wrote
("latest is 9pm tonight") is a question for a person, and anything else is kept. A correction
that arrives in the same pass as the confirmation it corrects is caught the same way.
A decline carries its reason (`not_ready`, `no_capacity`, `closed`, `po_not_found`,
`order_canceled`, `other`). Only the first three are about the day, so only they write to the
customer's desk for a new delivery appointment; "we do not have this PO" goes to a person. A
"check back" on or after the pickup day raises `check_back_too_late`.

The conversation policy (`booking/respond.py`) handles what comes back, still as drafts:
a counter-offer is accepted when the offered pickup still makes the customer's delivery
slot (miles at `FP_BOOKING_AVG_MPH` plus `FP_BOOKING_LOAD_HOURS`), otherwise the agent asks for
alternatives inside the workable window; a factual question is answered only from data on
the case (PO numbers, carrier, delivery site and number, load number), by rule first and by
the model second, and every number in the answer must exist on the case; a vendor that
cannot ship gets a drafted note to the customer's inbound desk (its customer file's `[customer_desk]`)
asking for a new delivery slot; `booking follow-up` nudges a quiet desk once per silence after
`FP_BOOKING_FOLLOW_UP_HOURS` weekday hours. Replies that mention rates, fees, detention, claims
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
`booking/timers.py` (no-reply and expiry timers), `booking/today.py` (the daily summary),
`booking/classify.py` (reply reading and validation), `booking/mail.py` (JSONL or Gmail in,
`.eml` or Gmail drafts out).
Real-text regression: `tests/fixtures/lidl_morgan_foods_thread.jsonl` is the pod's September 2026
Morgan Foods thread in the mail pull's format, Outlook cruft included, and
`tests/test_booking_real_thread.py` replays it with the model's recorded readings.

### Three rules from the live threads

A freshly scanned case is checked before any email is written. A desk listed in
`FP_BOOKING_PO_DATE_FLOOR_DESKS` (Morgan Foods by default) reads the date inside the customer's
PO (Lidl's DDMMYY, from `[numbers] po_date` in its customer file) as the earliest pickup, so a request earlier than that day is moved up to it (weekends roll to
Monday); if the floored day can no longer make the delivery the case goes to a person instead.
A requested slot that has already passed, or sits inside `FP_BOOKING_MIN_NOTICE_HOURS`, goes to
a person too, at scan time and again at draft or send time, because a same-day ask is a phone
call. The inbound desk's delivery slots are read in every wording seen so far, including the
two-line "9/30 at 1100" then "PYE_300926723".

### Choosing the pickup time

When the scan opens a case, `booking/recommend.py` chooses the time to ask for and keeps each step:

1. **The day**: the tendered pickup date, else back from the delivery by the transit days. A
   backed-off day that has already passed moves up to the earliest pickup a driver can still make.
2. **The PO-date floor**: a desk in `FP_BOOKING_PO_DATE_FLOOR_DESKS` is never asked for a day
   before the date inside the customer's PO.
3. **The time**: the tendered time, else the time the facility usually gives, else
   `FP_BOOKING_DEFAULT_PICKUP_TIME` (09:00). "Usually" means at least 3 of its confirmed
   appointments, and at least half of them, were at one time. They come from Transport Pro's
   confirmed stops in the harvest and the agent's own bookings. Tendered times are not evidence.
4. **The hours**: with the facility's hours on its profile (`receiving_hours`), a time outside
   them moves to the next opening, or to an hour before closing.
5. **The delivery**: a time that would arrive after the delivery slot (miles at
   `FP_BOOKING_AVG_MPH`, plus `FP_BOOKING_LOAD_HOURS`) moves earlier the same day, to the latest
   that still makes it. It never moves before the facility opens, or, without hours, before
   `FP_BOOKING_EARLIEST_PICKUP_TIME` (05:00).

When no time that day makes the delivery, the case gets **"Cannot make the delivery"**. The to-do
says when the pickup would arrive and the latest pickup that would have made it. It blocks the
request until a person asks the customer to move the delivery, or the vendor for an earlier pickup.
A delivery the customer's desk moves is planned the same way, without the old tender; a new slot
that works clears the to-do.

The board shows **"Why this time"** for each case. A move other than the plain tender is recorded
as a "Pickup time chosen" event (the PO floor keeps its own event). `booking recommend ID` shows
what the agent would ask for now, step by step, and the facility's usual time, without changing
anything. The notice window, the desk's cut-off and how far ahead it books are checked after this,
as before.

On the 2026-10-02 stores: all seven Lidl cases ask for exactly what they did before. Lidl's vendor
stops in Transport Pro carry tendered times only, and no vendor has hours on file. On pod 1160, 62
facilities have confirmed pickup times and 9 have a usual one (Citrojugo 14:00, 16 of 23).

### Writing booked pickups to Transport Pro

When a pickup is booked (approved, picked from a click-to-confirm link, or marked booked with
its time), its slot is queued for Transport Pro: `POST /load/{id}/set_appointment` with
`waypointIndex=SH`, `startDate`, `endDate` and `appointmentStatus=Confirmed`. These are the field
names of the Transport Pro connector's `tpro_load_set_appointment`. `SH` is Transport Pro's name
for the load's shipper stop; the agent used to send the stop's position ("0"). The same slot is
never queued twice; a new time replaces a queued one.

`booking writeback` (and each `booking run` pass) writes the queue only while
`FP_BOOKING_TPRO_WRITEBACK` is on. Until then each booking waits, and the board's "Transport Pro"
row says to enter it there by hand. With it on, each load is read first:

| Transport Pro shows | The writer |
|---|---|
| the same confirmed time | sends nothing ("Already in Transport Pro") |
| a confirmed time this booking put there before (the pickup was moved since) | replaces it with the new time, then reads the load back |
| any other confirmed time | overwrites nothing; raises "Transport Pro has another time" for a person |
| a stop-off, not the load's shipper | writes nothing; a person enters it |
| the tender, or no appointment | writes the booking, then reads the load back to confirm it |

With the appointment, a note goes on the load once per booking (`POST /load/{id}/note`): the
time on the Eastern clock, the facility's pickup number and the conditions it set ("check in at
the guard shack"), which the appointment fields have no room for. A note that fails is retried
with the job, without writing the time again. Transport Pro takes and gives UTC; nothing changes
there.

Every write is recorded on the case ("Written to Transport Pro", with what was there before). A
failed write is retried after an hour and raised as "Automation failed" after three tries. An
appointment whose time has passed, or a case canceled since, is never written.
`booking writeback --dry-run` reads the loads and says what would be written, without writing.

### The agent on its own: rules

`booking run` (or `serve --autopilot-every 15`; off unless asked for) is one pass of the agent
working by itself, by the rules in each customer's file:

```toml
[[rules]]
name = "vendor pickups by email"
when = { methods = ["email"] }      # also desks, vendors (name contains), facilities, customer_ids
do = "draft"                        # draft | send | hold | skip
wait_for = "delivery_slot"          # ask only once the load has its delivery slot and reference
# lead_days = 2                     # ask this many business days before the pickup
# batch_at = "10:00"                # write at this time of day, one email per desk
# pickup_from = "delivery"          # plan the pickup back from the delivery, not the tender
# follow_up = false                 # no automatic nudge to a silent desk
# replies = "send"                  # send the answers to the vendor (default: as `do` says)
# confirm = "auto"                  # book a confirmation of the time asked for, no approval
# customer_notes = "send"           # send the note to the customer's desk (default: draft)
```

The first rule that covers a pickup decides; without one, the agent drafts it as soon as it can.
`skip` opens no case at scan (`skipped_by_rule`). `hold` raises it for a person ("a person books
this", with the rule's `why`) and writes nothing.

Each pass:

1. **Timers**: the timers run first.
   **Replies**: with `FP_BOOKING_INBOX` set, the new replies are read next and answered (see
   "The agent answering on its own" below).
2. **Planning**: every unscheduled case without a request gets a request job (table
   `booking_jobs`). The job is waiting (for the delivery slot), blocked (something on the case
   needs a person), held, or planned for its time.
3. **Writing**: the jobs that are due are written, one email per desk. Each desk's rules are
   checked first, exactly as for `booking draft`; a desk that does not book that far ahead moves
   the job to the day it opens. They are drafts for a person to send, or sent where a rule says
   `send` and `FP_BOOKING_MODE` is send.
4. **Failures**: a batch that fails leaves nothing behind. It is retried an hour later, and after
   three tries raised as "Automation failed".
5. **Follow-ups**: a quiet desk gets a follow-up once per silence, and a check-back on the day it
   named (or a day after "check back later"), up to three on a pickup, unless the rule says not
   to.

A request drafted, a booking made or a case canceled by a person closes the job: the agent never
redoes it. Lidl's rule drafts its email desks once the DCT slot is known; the batch hour and lead
days are left for the pod to set.

### The agent answering on its own

With `FP_BOOKING_INBOX` set, each pass also reads the new replies, the way Bigger Picture does:

- **Where it reads**: `FP_BOOKING_INBOX=gmail` reads the sending mailbox (`FP_BOOKING_GMAIL_KEY`
  for `FP_BOOKING_GMAIL_USER`) for mail to or copying the customers' groups, and mail sent to
  the mailbox itself; `FP_BOOKING_INBOX=s3://bucket/prefix` reads the group-mail archive and,
  when the sending mailbox is set up too, the mail sent straight to it without the group (a
  facility that answered the address the request came from). An email found in both is read
  once, and one source that cannot be read is reported without stopping the other. Each pass
  looks `FP_BOOKING_INBOX_DAYS` back (2). Mail already on a case is skipped by its email ID, so a
  reply is never answered twice, however often the inbox is read.
- **Attached files are read**: the text of a PDF (with the `pdf` extra), Word (.docx), Excel
  (.xlsx), text, CSV, HTML or calendar file goes under the email's own words, marked with the
  file's name, so a confirmation sent only as a PDF is matched by its PO and read like the
  email. A file that cannot be read (a scanned PDF, a picture sent as a file, an old .doc) is
  named in the email and raises "Open the attachment", which also stops the agent booking on
  that reply by itself. Logos in signatures are left alone.
- **Clean text, careful checks**: HTML codes (`&nbsp;`, `&amp;`) are decoded and odd spaces made
  plain, whatever the source. When the reply's words are checked against what the model read,
  the sender's signature backs nothing, a pickup number found only inside a phone, fax or
  extension number is dropped, and a date written out ("11/01", "Oct 1st") must match by month
  as well as day; with no date written out, the day of the month or "tomorrow" still does.
- **Mail no person wrote**: a bounce (the mail server sent our email back) is tied to the email
  it returned by its Message-ID, never read by the model, and raises "Email did not arrive" at
  once with the address and the server's reason. An out-of-office or other automatic reply
  (`Auto-Submitted`, "Automatic reply:") and a delay notice are kept on the pickup and read by
  nobody: the desk still counts as silent, so the no-reply to-dos and the follow-up go on. A
  scheduling portal's own notices are automatic too but carry the booking, so they are read.
- **ETA, late arrivals, holds**: a facility asking when the driver will arrive gets the driver's
  latest word from Transport Pro (a check call about time or place, or the tracking position,
  under a day old, with its time) when there is one; Circle's own tracking notes ("POD indexed")
  are never passed on, and the agent never works out an arrival time itself. Without one, "Driver
  ETA asked" goes to a person, and anything else they asked joins that to-do. A facility that
  will still take the truck late raises "Late arrival offered" with its latest time; one that put
  the pickup on hold raises "Pickup on hold" (on a booked pickup, "Booked pickup changed"), and
  neither is answered by the agent. A hold is never read as a decline, so Lidl is not asked to
  move the delivery for it.
- **Delivery moved under a booked pickup**: when Transport Pro or the customer's desk moves the
  delivery, a booked pickup is checked against it (the booked time plus the drive and loading);
  one that can no longer make it raises "Delivery moved" for a person to ask the facility for an
  earlier pickup. The booking itself is never moved by the agent.
- **No email dropped without a word**: an email that fails to be read (the model unreachable,
  a fault) is tried again every pass; one still failing in the last six hours before it leaves
  the look-back is kept under "Emails we could not match" with the error. From then on it no
  longer turns the mail notice red, and once it can be read it links itself to its pickup.
- **No Message-ID**: the same email read from two mailboxes is known by its sender, subject,
  text and time (within 15 minutes) and read once.
- **Finding the pickup**: by the email IDs the reply answers, its thread, a PO in its words (of a
  pickup being booked or one booked), the desk that wrote, or another address at that desk's
  company when only one pickup is open with it (too weak to book on). Every email the agent
  sends asks for answers to go to the customer's group (`Reply-To`), so a plain Reply reaches the
  group as well as Reply All.
- **One chain per pickup, whoever wrote**: mail a person at Circle sends with the customer's
  group on it (To or Cc) is kept on the pickup it is about, found by the email it answers, its
  thread, a PO or the pickup's delivery number. The request typed by hand, a chaser, a "Thank
  you!", or a note asking the customer's desk for the PO all count. It shows in the pickup's
  Emails as "Sent by a person", from whom and to whom, in time order with the vendor's and the
  agent's mail. It is never read as a vendor's reply, and never counted as the agent's own
  (rounds, the daily cap, the no-reply clock). Mail without the group on it stays private. A
  vendor's confirmation that carries the pickup number already on the load is read as the
  booking itself, not a change, and gives a pickup booked outside the agent its booked time.
- **Reading the mail onto the board without the agent acting**: `serve --mail-every 15` (with
  `FP_BOOKING_INBOX`) reads the group mail every 15 minutes. Every email is kept on its pickup,
  the vendor's are read so the pickup moves and its to-dos are raised, and nothing is drafted or
  sent. Today shows when the mail was last read, and a red notice when it could not be. Mail
  about a load Transport Pro already shows picked up is kept but not read: it is history, not a
  new to-do.
- **Latest updates** on Today lists the newest emails, whoever sent them (the vendor, the agent,
  a person at Circle, the customer's desk), and the Transport Pro changes the scan saw (found,
  delivery or tender moved, booked there, picked up), newest first, over the last two weeks. An
  email that covered several pickups is one row; each row opens its pickup. Drafts are left
  out: "Not asked yet" counts them.
- **Mail no pickup matches** is kept for a person (table `booking_unmatched_mail`) when it is
  about booking (a pickup subject, a known desk or its company, a PO-shaped number): the board's
  Home shows "Emails no pickup matched" to link each to its pickup (the agent then reads it) or
  dismiss it; `booking unmatched`, `booking link-mail MAIL CASE --by`, `booking dismiss-mail
  MAIL --by --note` do the same. A later pass that finds the pickup links it itself. Tour plans,
  tenders and rate requests are never kept.
- **What it does with a reply**: matches it to its pickup, reads it, and answers by the
  conversation policy:
  - accept an offer that still makes the delivery, or ask for other days;
  - answer the facility's questions (see below);
  - thank the vendor for a confirmation;
  - write a note to the customer's desk when the vendor cannot ship.

  Money, claims and the third back-and-forth go to a person.
- **Replies written for the situation** (`booking/writer.py`): the code decides the move, and
  the model writes the words for what the facility actually wrote. One reply states the move and
  answers every question in their email, a confirmation's too ("Confirmed for 9am. What's the
  trailer number?" gets "Thank you!" and the trailer number). It answers from a fact sheet
  (`booking/facts.py`):
  - the pickup itself: POs, load number, customer, pickup and delivery times, delivery number;
  - the load in Transport Pro, read when the reply is written: weight, piece count, equipment,
    commodity, temperature, hazmat, BOL, seal, the stops' notes and addresses;
  - the load's dispatch: the trucking company with its MC, DOT and dispatch phone, and the driver's
    name and cell, truck and trailer.

  Rates, max buy, charges and insurance are never read, so no reply can carry them. Load notes are
  given as notes ("Our load notes call for load bars"), never as promises for the driver.
- **The check on a draft**: before it goes, every date, time, number, phone number and email
  address in the draft must be in the facts or the decision, it must not touch money, it must
  carry the decision's own dates and times, and each answer must cite facts that are known. A
  draft that fails is never sent: the fixed wording goes ("Yes, 10/01 @ 1100 works. Thank you!"),
  or a question with no fixed answer goes to a person.
- **What it cannot answer** (no driver assigned yet, a temperature the load does not hold, "Can
  the driver come at 7am?") is left out: the reply says "I will get back to you on the rest" and
  the question is raised in Needs you. When nothing can be answered the reply only says so, and
  every question waits for a person. Without a model the fixed answers still work: which PO,
  the delivery number, where it delivers, the load number, the customer, and the trucking company
  once the load is dispatched.
- **What goes out**: with `FP_BOOKING_MODE=send` and a sender, an answer is sent where the
  customer's rule says `replies = "send"` (and a note to the customer's desk where it says
  `customer_notes = "send"`). Every send passes the send gate:
  - it goes only to the desk on the profile, to the person at that company who wrote, or to the
    customer desk in the file;
  - the daily cap holds.

  An answer the gate refuses is drafted instead, and raised for a person.
- **Booking without a person**: where the rule says `confirm = "auto"`, the agent books a
  confirmation itself: the case becomes scheduled, the desk is remembered, and the slot is
  queued for Transport Pro, exactly as a person's approval. It does so only when nothing is in
  doubt:
  - the time asked for (the same day, within two hours, not already past);
  - nothing else open on the pickup;
  - every date, time and number backed by the reply's own words;
  - no money or claims;
  - tied to the request by its email ID, thread or PO (not only by the sender);
  - a time that still makes the delivery.

  Otherwise the approval stays for a person, with the reason on it ("not booked automatically:
  ..."). An offer the agent accepted is booked once its "yes" has been sent.
- **Failures**: a reply that fails (the model is down, say) is left for the next pass; an
  unreadable inbox is reported and the rest of the pass goes on.

Lidl's file keeps drafting and approval by a person until the pod switches these on.

`booking run --dry-run` shows what a pass would do and changes nothing. `booking jobs [ID]` lists
the jobs with their rule, status, due time and why. `customers show KEY` lists the rules. The
board shows "Automation" in each case. On a copy of the 2026-10-02 Lidl store a pass closes the
five drafted cases' jobs (already requested) and holds the PYE store pickup (no desk). It writes
nothing.

### Business days, closed days and the checks before a request

- **Business days** are Monday to Friday, less the six freight holidays (New Year's Day,
  Memorial Day, Independence Day, Labor Day, Thanksgiving, Christmas Day; one on a Saturday is
  observed the Friday before, one on a Sunday the Monday after). They count everywhere a day is
  counted: the pickup worked back from a delivery, a desk's cut-off on "the business day
  before", the no-reply clock, link expiry, the days offered to a vendor, `lead_days`.
- **A pickup on a day the facility is closed** (a tendered Saturday or Sunday, a holiday, a
  weekday its hours list no opening) is asked for on the open day before, when a driver can
  still make it, else on the next open day. The recommendation says why ("Sat 10/03 09:00 ET is
  a Saturday; asking for Fri 10/02 09:00 ET"). A facility whose hours say it ships Saturdays
  keeps the Saturday.
- **A request is refused** for a pickup that already has one (`--again` writes it once more;
  a new time is `booking reschedule`), a desk on a Circle address, a desk the profile no longer
  trusts, and a pickup with no date (no tender and no delivery slot). `profile set` refuses a
  Circle address as a facility's desk, and the send gate never sends to one.
- **`booking draft` and `booking send` follow the customer's rule** as the agent does: a pickup
  the rule skips, holds, or keeps waiting for the delivery's appointment waits, and `booking
  send` holds back what the rule only drafts. `--anyway` acts on a named case regardless.
- **Each desk's email stands on its own:** one the send gate refuses is reported and the rest
  go on; what was already sent stays recorded. The daily cap counts emails, so a batched
  request for four POs is one.
- **A batched email follows each pickup's own facility:** a desk that books for several plants
  gets one email, but each pickup is checked against its own plant's profile (the desk it
  trusts, its cut-off, the numbers it needs) and its PO line carries that plant's numbers, with
  a date only where that plant books by the day.
- **Gmail failures are reported, not crashed on:** a login refused, no connection, or Gmail
  saying no ends `booking send` with "not sent: ..." and nothing recorded. When Gmail took the
  email and never answered (a timeout, a dropped connection, its own error), the email may have
  gone: it is recorded with the Message-ID it carries and the to-do "Check the email went out", and
  never sent again on its own. It clears when the group's copy or the facility's reply to it is
  read, or when a person marks it sent (`booking sent`); if it is not in the Sent folder,
  resolve the to-do and send it again with `--again`.

### Desk rules

A vendor profile also carries the rules its booking desk stated, filed by a person (`profile
set`, the review workbook) because the extractor does not look for them yet:

| Field | Value | The agent |
|---|---|---|
| `notice_period_hours` | hours before the pickup the desk wants the request | goes to a person past it (`slot_unworkable`) |
| `cutoff_time` | `HH:MM` local, on the business day before the pickup ("2 PM" is accepted) | goes to a person past it (`slot_unworkable`) |
| `max_days_ahead` | how far ahead the desk books at all | holds the request until then; the case says from which day |
| `required_refs` | numbers needed besides the PO: `shipment_number`, `sales_order_number`, `bol_number`, `load_number`, `delivery_number` ("TI shipment number", "SO#" are accepted) | raises `missing_reference` until a person adds them, then writes them into the request line: `PO# X / Shipment# 7781234 on 10/01 @ 0900` |

They are checked when a load is scanned, by `booking draft` and `booking send` (which print the
cases that wait, and why), and by the timers for requests not sent yet, so a cut-off that passed
overnight becomes a to-do. A number is added with `booking ref CASE KIND VALUE --by NAME` or "Add
reference" on the board; the to-do clears once the case has every number its desk needs.
`scripts/seed_lidl_desk_rules.py` files the two the lidl@ threads stated: Morgan Foods wants
Lidl's TI shipment number, RLS Lebanon wants Lidl's SO number. A portal desk's to-do names its
system and address ("books on opendock (https://...)").

### Desk memory

When a pickup is booked, the way it was booked is remembered for the vendor (`booking/memory.py`,
table `booking_desk_memory`: method, desk, how many times, when last, by whom). Approving a
vendor's email confirmation counts the desk the request went to. Marking a pickup booked counts
`--via` phone, portal or email with `--desk`, the address or number used ("Booked with" on the
board; an email booking without one counts the desk on file). Updating a booking does not count
it twice.

The profile learns from it where it has nothing trusted yet: the booking method, and the email
desk, phone number or portal address (with the portal's system) are filed as a person's values,
audited as `learned` with the case they came from. A value already trusted is never overwritten;
the memory still shows what else worked ("Booked before" on the board, `booking desks`). Once the
profile has an email desk, the vendor's other cases waiting for one take it and the agent can
request them; the "No booking desk" to-do is resolved with where the desk came from. The same
happens when a desk is filed with `profile set`, at once, and through the timers.

### Reference numbers

Every number on a pickup is kept in one place (`booking/references.py`, table
`booking_references`): PO numbers and the delivery (DCT) reference from the load, a vendor
pickup number from the load or the vendor's reply, a new delivery reference from the customer's
desk, and what a person adds (the customer's shipment or SO number, a vendor confirmation number,
a portal appointment id: `booking ref CASE KIND VALUE`, "Add reference" on the board, or a pickup
number given with `booking booked`). Each row says its kind, where it came from (load, vendor,
customer_desk, person), who, when, and the email it came from. A newer value of a kind replaces
the older one, which stays as history; a case keeps several POs side by side. The case's own
`pickup_number`, `delivery_ref` and `reference_numbers` follow the current values, so nothing
that reads them changes. Stores from before are filled once from those columns when opened.

Any number finds its case, a replaced pickup number too: `booking find NUMBER` (any kind, any
letter case, or a load number), the board's search. `booking show` and the board list the
current numbers with their source, then the earlier ones.

### Email templates

The agent's wording comes from templates (`booking/templates.py`, table `booking_templates`):
`request` (one pickup), `batch_request` (several pickups for one desk in one email), `reschedule`,
`follow_up` (after a day of silence) and `check_back` (on the day a vendor said to ask again).
A template is a subject (requests only; the rest answer in the thread) and a body with fields in
braces: `{lines}` (the PO lines), `{ask}`, `{po}`, `{load}`, `{date}`, `{time}`, `{vendor}`,
`{customer}`, `{desk_name}`, `{delivery_ref}`, `{delivery_date}`, `{refs}`, `{carrier}`,
`{signature}`, and for a reschedule `{line}`, `{previous}`, `{note}`. A field left empty leaves no
blank gap.

The template used is the most specific saved: for the desk's address, else for the customer
(its key `lidl`, its name, or a Transport Pro name such as `Lidl - Inbound`), else the pod's default, else the built-in one, which is the pod's own wording
word for word. Templates are checked when saved: an unknown field, an unmatched brace, a request
without `{lines}` or a reschedule without `{line}` is refused. The "Request drafted" step on the
board says which wording was used.

```powershell
facility-profiles booking template fields
facility-profiles booking template set request --customer lidl --subject "Pick Up Appointment: {po}" --body-file lidl-request.txt --by megan
facility-profiles booking template set follow_up --desk cci@udfinc.com --body "Hello,\n\nChecking on {po} for {date}.\n\n{signature}" --by megan
facility-profiles booking template preview 12            # the email the agent would write now
facility-profiles booking template show request --desk cci@udfinc.com
facility-profiles booking template list
facility-profiles booking template remove follow_up --desk cci@udfinc.com
```

### Click-to-confirm links

A request can carry the pickup times as one-click links, so a vendor clicks a time instead of
writing a reply. It is off until two settings are set:

```powershell
$env:FP_BOOKING_LINK_BASE_URL = 'https://book.example.com'    # where the vendor pages are served
$env:FP_BOOKING_LINK_SECRET = '<a long random string>'         # signs the links; keep it secret
facility-profiles serve-links --port 8010                      # the vendor pages, and nothing else
```

With both set, each request (single, batched per desk, or a reschedule) offers the time asked for
and the hours around it on the same day (`FP_BOOKING_LINK_OFFSETS_MINUTES`, default -60, 0, +60,
+120). A desk given dates only is offered the day asked for and the next two weekdays. Only times
that still make the delivery and pass the desk's rules are offered. The text gets one line after
the PO lines ("Or confirm a time with one click: <link>", one per PO in a batch). The HTML part
shows each time as a button. A saved template without `{links}` gets the line after its PO lines;
with links off, every email reads exactly as before.

The link opens a page with the times, the vendor's optional pickup number and name, and "None of
these work? Propose a time". Opening the page changes nothing, because mail scanners open every
link. The vendor's second click, a POST, is what counts:

- **A time picked** is recorded as the vendor's answer: an inbound "link" message, the
  confirmation, and the pickup number as a vendor reference, with no model call. It is booked
  straight away (`FP_BOOKING_LINK_AUTO_SCHEDULE`, on by default; off, it waits for "Approve" like
  an emailed confirmation). The time is checked again on the click, so one too close to dispatch
  a driver is refused.
- **A time proposed** becomes the usual "vendor offered another time" to-do, with whether it
  still makes the delivery.
- A link is signed (HMAC, never guessable), expires after `FP_BOOKING_LINK_VALID_HOURS` (72)
  weekday hours (weekends do not count, so a Friday-morning request's links last into
  Wednesday) or the last time offered, whichever comes first, and stops working once answered,
  once a later request replaces it, or once the pickup is booked or canceled another way.
- The buttons and the page show times in Eastern; a time the vendor proposes is read as Eastern.
- A draft nobody marked as sent is marked sent by the click: the vendor holds the link.

`booking links ID` lists a case's offers, their links and what the vendor did; `booking template
preview ID` shows where the link goes. The board shows "Offered by link" in the case and the
click in the timeline. `serve-links` serves only `/c/...` and `/health`: no board, no API, no docs.
Its responses are not cached, not indexed and not framed. `serve` also mounts `/c/...` for trying
links on this machine.

Going live needs the open decisions from the plan: a public HTTPS address for `serve-links` in
front of the same store the agent uses (a shared database rather than a laptop's SQLite file), and
the shared mailbox the requests go out from.

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

Mail whose subject and people say nothing is still kept when it answers a kept email by its
Message-ID (a reply under a subject of its own, remembered in `state/kept-ids.json` for 180
days), or when its text names one of the customer's PO numbers, a pickup or confirmation number,
or a pickup appointment (a new desk writing "Order ready"). Its text is fetched once; a message
found not to be about booking is remembered in `state/body-checked.json` for the days the pass
looks back. The agent applies the same text rules to mail no pickup matched, so such mail lands
in "Emails no pickup matched" for a person instead of being dropped. Each attachment's manifest
entry says whether it sat in the body (`inline`, a logo) or was attached.

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
`contact_email`, `portal_url`, `portal_vendor`, `notice_period_hours`, `cutoff_time`,
`max_days_ahead`, `required_refs`, `time_granularity`, `receiving_hours`, plus a one-line
`scheduling_summary`. Mapping to Transport Pro fields is in `domain/rules.py::to_tpro_write`
(the desk rules go into the appointment notes).

`portal_vendor` is one of opendock, c3, datadocks, one_network, e2open, blue_yonder, retalix
(also NCR Power Traffic), costco, unfi, ahold, publix, bozzutos or other. A portal URL names its
system with certainty, so a profile's vendor follows its URL's host (`domain/normalize.py`,
`PORTAL_HOSTS`; `appointments.cwtraffic.com` is Costco) over the notes' reading.

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

[TESTING.md](TESTING.md) walks through testing the booking agent by hand: the automated tests,
the board on a demo store, the commands, and a pod's real cases on a copy of its store.

## Known gaps and next steps

- The agent learns of new mail only when a pass runs (`serve --autopilot-every N`); there is no
  push (Gmail watch, or SES inbound to a function), and nothing runs while the machine is off.
  Hosting it (HTTPS, a shared Postgres store, the sending mailbox) is still to do.

- Vendor asks from the PRD are still open: a facility write path, a facility search endpoint,
  the allowed `appointments.method` values, the `needsAppointment` filter definition.
- Receiving-hours inference from actual arrival and departure times (`/voiceai/load/{id}`) is
  modelled but not yet wired into collect.
- Digest delivery (email or Slack) is a deployment concern not included here. Google sign-in
  is built in; hosting the board (HTTPS, a shared Postgres store) is still to do.
- Alembic migrations before Postgres.
- Customers: the board's Customer filter and `GET /api/booking/today?customer=` still take the
  Transport Pro customer name, not the customer key (who sees what goes by key); the S3 collector's deploy script still deploys Lidl's function only
  (`circle-lidl-mail-collector`), so a second customer's collector needs its own function name,
  bucket or prefix and `ARCHIVE_CUSTOMER`. The vendor-desk lists (`FP_BOOKING_SHARED_DESKS`,
  `FP_BOOKING_PO_DATE_FLOOR_DESKS`) belong on the vendor profiles as desk rules.
