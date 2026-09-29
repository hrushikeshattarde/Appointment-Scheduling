"""Seed the Lidl inbound vendor profiles (POD Megan Goodwin, terminal 1089) from gathered evidence.

Runs `facility-profiles profile set|summary|ask` against the Lidl pod store for the twelve vendor
pickup facilities seen on Lidl - Inbound loads in the 90 days to 2026-09-29. Every value carries
its source: a Transport Pro stop or tracking note (load number quoted) or the lidl@ group mail.
Values are filed as human-set, so the nightly routine never overwrites them; questions go on the
review queue for Megan.

    python scripts/seed_lidl_vendor_profiles.py [--dry-run]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".venv" / "Scripts" / "facility-profiles.exe"
STORE = "sqlite:///./data/facility_profiles_pod-1089-lidl.db"
NOTES = "claude/tpro-notes"
MAIL = "claude/lidl-mail"

FIRM = 'stop note: "Live/Live Appointments are firm ... APPOINTMENT IS FIRM PLEASE ARRIVE 30 Min prior"'
ASK_METHOD = (
    "Megan books Lidl vendor pickups by email (lidl@ group mail, Sep 2026). Confirm this vendor's "
    "channel: email, phone or portal?"
)
ASK_EMAIL = "Appointment desk address for this vendor, needed before an agent can email a request."

# key, display name, evidence load, firm-appointment evidence?, rules for the summary
VENDORS: list[dict[str, object]] = [
    {
        "key": "candidate:b0613ce141ed48b1",
        "name": "Polar - Fitzgerald (220 Frank Rd)",
        "load": 2488942,
        "firm": True,
        "summary": (
            "Live load with a firm appointment; arrive 30 minutes before the slot (a driver who "
            "arrived early on load 2507312 found nobody to load him). Driver needs load bars and "
            "straps and must send the BOL before leaving. Booking channel not yet on record; "
            "Megan books Lidl vendor pickups by email."
        ),
    },
    {
        "key": "candidate:d6e5ad6d54f71f91",
        "name": "Polar - Fitzgerald (245 Frank Rd)",
        "load": 2495968,
        "firm": True,
        "summary": (
            "Second Polar building on Frank Rd. Live load with a firm appointment; arrive 30 "
            "minutes early. Load bars and straps required; BOL before leaving. Booking channel "
            "not yet on record; Megan books Lidl vendor pickups by email."
        ),
    },
    {
        "key": "candidate:fe63d5cd574b32f4",
        "name": "Polar Corporation, Worcester MA",
        "load": 2477890,
        "firm": True,
        "firm_quote": (
            'stop note: "Dry van load live/live appointments are firm ... Driver must send BOL '
            'prior to leaving the shipper"'
        ),
        "summary": (
            "Live load with a firm appointment; check-in time is confirmed in advance. Load bars "
            "and straps required; send the BOL before leaving. Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:aecac731a019b622",
        "name": "DelGrosso Foods, Tyrone PA",
        "load": 2511448,
        "firm": True,
        "summary": (
            "Live load with a firm appointment; arrive 30 minutes early. Load bars and straps "
            "required; BOL before leaving. Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:812464042aa009d3",
        "name": "Ardent Mills - Hanover PA",
        "load": 2495734,
        "firm": True,
        "firm_quote": 'stop note: "Dry van load live/live appointments are firm"',
        "summary": (
            "Live load with a firm appointment. Load bars and straps required; BOL before leaving. "
            "Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:e5b511ec379e6553",
        "name": "RLS Logistics, Lebanon PA",
        "load": 2547311,
        "firm": True,
        "firm_quote": 'stop note: "Reefer Live/Live Appointments are firm ... ARRIVE 30 Min prior"',
        "summary": (
            "Reefer live load with a firm appointment; arrive 30 minutes early. Load bar required; "
            "BOL before leaving. Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:bc484c34efb8fd54",
        "name": "Premium Waters - Allentown PA",
        "load": 2507314,
        "firm": True,
        "firm_quote": 'stop note: "PLEASE ARRIVE 30 MINS EARLY TO APPT"',
        "summary": (
            "Appointment pickup; arrive 30 minutes early. Load bar required. Booking channel not "
            "yet on record."
        ),
    },
    {
        "key": "candidate:21d5ed642fcd683d",
        "name": "Airpack, White Marsh MD",
        "load": 2577859,
        "firm": True,
        "summary": (
            "Live load with a firm appointment; arrive 30 minutes early. Load bar required; BOL "
            "before leaving. Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:e96fde55907d5c53",
        "name": "CG Roxane, Johnstown NY",
        "load": 2504683,
        "firm": False,
        "ask_required": (
            'Notes point to assigned pickup times ("tracking on site OTD for appt time", load '
            "2519133) but no stop note says so. Does this shipper require an appointment?"
        ),
        "summary": (
            "Pickup times appear to be assigned (drivers wait on site for the appointment time). "
            "Load bars and straps required; driver must send pictures of the load and the BOL "
            "before leaving. Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:4d2cf80142f29d6d",
        "name": "Koch Foods, Erlanger KY",
        "load": 2582627,
        "firm": False,
        "ask_required": (
            "Tracking notes reference appointment times but no stop note states the rule. Does "
            "this shipper require an appointment?"
        ),
        "summary": (
            "Appointment times are referenced in tracking notes. Load bar and straps required. "
            "Booking channel not yet on record."
        ),
    },
    {
        "key": "candidate:72e4bfbafdb40ad4",
        "name": "Seneca Foods, Ripon WI",
        "load": 2515377,
        "firm": False,
        "ask_required": "One load in 90 days and no appointment evidence. Does Seneca require one?",
        "summary": (
            "Both POs on the order must be picked up together. Load bars and straps required. "
            "Appointment rule and booking channel not yet on record."
        ),
    },
]

MORGAN = {
    "key": "candidate:a425a687fe4390ac",
    "name": "Morgan Foods, Austin IN",
    "load": 2591069,
    "mail": (
        'lidl@ group mail "Pick Up Appointments", 24-29 Sep 2026, vendor signature: "All '
        'appointments can be made via email @ shipping.appointments@morganfoods.com"'
    ),
    "summary": (
        "Pickup appointments are booked by email with shipping.appointments@morganfoods.com "
        "(shared desk: Natosha Kidd, Vera; direct line 812-794-1152); the desk replies within "
        "minutes in business hours with a date, time and pickup number. Every driver must register "
        "in the Eaigle gate system no earlier than 48 hours before arrival "
        "(driverapp.morganfoods.eaigle.ai) or is refused entry. Load bars and straps required and "
        "pictures of the load and BOL before leaving; on 28 Sep the vendor advised against straps "
        "on that load, so ask about airbags when booking."
    ),
}


def cli(*args: str, dry_run: bool) -> None:
    cmd = [str(CLI), *args]
    print("$", " ".join(f'"{a}"' if " " in a else a for a in cmd[1:]))
    if dry_run:
        return
    env = {**os.environ, "FP_DATABASE_URL": STORE, "FP_LOG_LEVEL": "WARNING"}
    proc = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
    out = (proc.stdout or proc.stderr).strip()
    print("  ", out.splitlines()[-1] if out else "(no output)")
    if proc.returncode != 0:
        sys.exit(f"failed: {' '.join(args)}\n{proc.stdout}\n{proc.stderr}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    for v in VENDORS:
        key, load = str(v["key"]), v["load"]
        print(f"\n## {v['name']} ({key}, load {load})")
        if v["firm"]:
            quote = str(v.get("firm_quote", FIRM))
            cli(
                "profile",
                "set",
                key,
                "shipper",
                "appointment_required",
                "--value",
                "true",
                "--by",
                NOTES,
                "--reason",
                f"load {load} {quote}",
                dry_run=dry_run,
            )
            cli(
                "profile",
                "set",
                key,
                "shipper",
                "time_granularity",
                "--value",
                "exact",
                "--by",
                NOTES,
                "--reason",
                f"load {load} {quote}: firm appointment = exact time",
                dry_run=dry_run,
            )
        else:
            cli(
                "profile",
                "ask",
                key,
                "shipper",
                "appointment_required",
                "--proposed",
                "true",
                "--reason",
                str(v["ask_required"]),
                dry_run=dry_run,
            )
        cli(
            "profile",
            "ask",
            key,
            "shipper",
            "booking_method",
            "--proposed",
            "email",
            "--reason",
            ASK_METHOD,
            dry_run=dry_run,
        )
        cli(
            "profile",
            "ask",
            key,
            "shipper",
            "contact_email",
            "--reason",
            ASK_EMAIL,
            dry_run=dry_run,
        )
        cli(
            "profile",
            "summary",
            key,
            "shipper",
            "--text",
            str(v["summary"]),
            "--by",
            NOTES,
            dry_run=dry_run,
        )

    key, load, mail = str(MORGAN["key"]), MORGAN["load"], str(MORGAN["mail"])
    print(f"\n## {MORGAN['name']} ({key}, load {load})")
    for field, value in (
        ("appointment_required", "true"),
        ("booking_method", "email"),
        ("contact_email", "shipping.appointments@morganfoods.com"),
        ("contact_name", "Morgan Foods appointments desk (Natosha Kidd, Vera)"),
        ("contact_phone", "812-794-1152"),
        ("time_granularity", "exact"),
    ):
        cli(
            "profile",
            "set",
            key,
            "shipper",
            field,
            "--value",
            value,
            "--by",
            MAIL,
            "--reason",
            mail,
            dry_run=dry_run,
        )
    cli(
        "profile",
        "summary",
        key,
        "shipper",
        "--text",
        str(MORGAN["summary"]),
        "--by",
        MAIL,
        dry_run=dry_run,
    )
    print("\ndone" + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
