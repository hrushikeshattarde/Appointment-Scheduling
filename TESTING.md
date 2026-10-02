# Testing the booking agent (Phase 1)

How to check the six Phase 1 steps: statuses and to-dos, timers and the daily summary, desk
rules, desk memory, email templates and reference numbers. There are three layers, quickest
first. Everything here runs on throwaway stores: nothing reaches a vendor, the customer or
Transport Pro.

## Before you start

- **Point every command at a test store.** Without `--db` or `FP_DATABASE_URL`, commands use the
  store named in `.env` or the default `data/facility_profiles.db`, which may be a real pod store.
  The steps below always set one.
- Keep `FP_BOOKING_MODE=draft` (the default): the agent then only writes drafts.
- Run everything from the `facility-profiles` folder, with the `api` extra installed
  (`uv sync --extra api`).
- The commands are for Windows PowerShell. On macOS or Linux use `.venv/bin/` instead of
  `.\.venv\Scripts\`, and `export FP_DATABASE_URL=...` instead of `$env:FP_DATABASE_URL = ...`.
- `data/` and `exports/` are not tracked by git, so test stores and drafts are never committed.

## 1. Automated tests (2 minutes)

```powershell
.\.venv\Scripts\python.exe -m pytest
```

Every test passes and coverage stays above the 80% gate. The booking tests are in
`tests/test_booking*.py`, `tests/test_desk_memory.py` and `tests/test_facility_rules.py`.

## 2. The board with demo data (about 15 minutes)

Build a demo store: 21 invented pickups, one per situation, on `.example` addresses. The seeder
refuses a store that already has cases; delete the file to start again.

```powershell
.\.venv\Scripts\python.exe scripts\seed_booking_demo.py --db sqlite:///./data/phase1-demo.db
```

Start the board. It keeps the terminal busy, so use a second terminal for section 3.

```powershell
.\.venv\Scripts\facility-profiles.exe serve --db sqlite:///./data/phase1-demo.db
```

Open http://127.0.0.1:8000/app/ and type your name in "You" at the top (every decision is
recorded under it). Then check:

| Step | Open | You should see |
|---|---|---|
| 1. Statuses and to-dos | Overview | "Needs action": every case with something open, each to-do with what to do about it |
| 2. Timers | Case 19 Delta Rice Mills | "No reply in 48 h" |
| 2. Timers | Case 18 Keystone Pretzel | "No reply in 24 h" |
| 2. Timers | Case 13 Elm Street Bakery | "Pickup time passed" |
| 2. A different time | Case 20 Harvest Moon Oats | "Confirmed a different time" next to "Approve confirmation"; "Approve slot" clears both |
| 2. Daily summary | "Daily summary" on the Overview | The morning summary, with a Copy button |
| 3. Desk rules | Case 21 Prairie Gold Mills | "Reference needed"; "Add reference…" with any number clears it |
| 4. Desk memory | Case 9 Oak Valley Produce | "Mark booked…" with "Booked with" filled in; a "Booked before" row appears |
| 4. Desk memory | Case 14 Maple Leaf Imports | "Booked before: phone 716-555-0144" |
| 6. Numbers | Case 1 Harbor Beverage | "Numbers": PO, Delivery# and PU# 44710, each with where it came from |
| 6. Search | Appointments, search box | `44710` finds case 1 |

Case numbers follow the seeder's order. The timers' cases are built relative to the time the
store is seeded, so they show the same to-dos whenever it is run.

## 3. Commands on the same demo store

In a second terminal, point the commands at the demo store:

```powershell
$env:FP_DATABASE_URL = "sqlite:///./data/phase1-demo.db"
```

| Command | Checks | You should see |
|---|---|---|
| `booking list --exception any` | Step 1 | One line per case with something open (`!unanswered_48h`, `!missing_reference` ...) |
| `booking timers` | Step 2 | How many cases were checked and what was raised or resolved |
| `booking today` | Step 2 | The daily summary as text |
| `booking template set follow_up --body "Hello,\n\nAny update on PO# {po} for {date}?\n\n{signature}" --by me` | Step 5 | `follow_up template saved for default` |
| `booking template preview 19 --kind follow_up` | Step 5 | Your wording, with case 19's PO and date filled in |
| `booking template fields` | Step 5 | Every fill-in field and what it becomes |
| `booking ref 21 shipment_number TI-123 --by me` | Steps 3 and 6 | The number is added; case 21's "Reference needed" clears |
| `booking find ti-123` | Step 6 | Case 21 (any letter case works) |
| `booking desks` | Step 4 | How each vendor was booked before |
| `booking show 1` | Step 6 | The case, with a `number` line per number and where it came from (a replaced one shows as `was`) |

Prefix each command with `.\.venv\Scripts\facility-profiles.exe`. A template, once saved, also
changes the board's drafts, so remove it again with
`booking template remove follow_up` if you want the pod's own wording back.

## 4. Real data, on a copy

This checks the agent against a pod's own cases without touching them. Make a copy of the Lidl
store; the source is opened read-only:

```powershell
.\.venv\Scripts\python.exe -c "import sqlite3; s=sqlite3.connect('file:data/facility_profiles_pod-1089-lidl.db?mode=ro', uri=True); d=sqlite3.connect('data/lidl-test.db'); s.backup(d); print('copied')"
```

```powershell
$env:FP_DATABASE_URL = "sqlite:///./data/lidl-test.db"
```

Then, prefixed with `.\.venv\Scripts\facility-profiles.exe` as before:

- `booking timers`: a "Pickup time passed" to-do for each pickup that slipped unbooked. On the
  2026-10-01 copy that was six: the five requests drafted on 9/29 and never sent, and the PYE
  store pickup.
- `booking template preview 7`: the Morgan Foods request, identical to the one drafted on 9/29
  (the built-in template is the pod's wording word for word).
- `booking find 20463798`: the Morgan Foods case.
- `booking today`: the daily summary for the Lidl pod.
- The board on the copy, on a second port:
  `serve --db sqlite:///./data/lidl-test.db --port 8001`.

The same works for pod 1160: copy `data/facility_profiles.db` the same way, then
`profile portals` (without `--apply`) lists the portal vendors the URLs correct (31 on the
2026-10-01 store: Costco, UNFI, Ahold, Publix, Bozzuto's and NCR portals filed as "other").

## Do not run while testing

- `booking send`: it emails vendors. It is refused anyway while `FP_BOOKING_MODE` is `draft`.
- `booking scan` and `booking inbox`: they read live Transport Pro and the mailbox, and the inbox
  pays for model calls. Section 4 tests the same logic on a copy.
- `profile portals --apply`, `scripts/seed_lidl_*.py` or any command without a test store: they
  write to the real stores.

## Clean up

Stop the board with Ctrl+C, then:

```powershell
Remove-Item Env:FP_DATABASE_URL
```

```powershell
Remove-Item data\phase1-demo.db*, data\lidl-test.db*
```
