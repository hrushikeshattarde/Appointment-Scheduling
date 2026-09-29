"""File the booking channels confirmed in the lidl@circledelivers.com Google Groups archive.

Read on 2026-09-29 through the group's web archive (8,216 conversations back to 2022). Only
business appointment desks are recorded here; no message content is stored. Each value names the
thread subject and date it rests on. Run after ``seed_lidl_vendor_profiles.py``; re-running is
harmless (values are re-filed, already-decided queue items are skipped).

    python scripts/seed_lidl_vendor_mail_findings.py [--dry-run] [--only-rdc]
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".venv" / "Scripts" / "facility-profiles.exe"
STORE = "sqlite:///./data/facility_profiles_pod-1089-lidl.db"
BY = "claude/lidl-groups-archive"

FINDINGS: list[dict[str, object]] = [
    {
        "name": "Polar Beverages Georgia, Fitzgerald GA (220 and 245 Frank Rd, one Opendock site)",
        "keys": ["candidate:b0613ce141ed48b1", "candidate:d6e5ad6d54f71f91"],
        "set": {
            "booking_method": "web_portal",
            "portal_vendor": "opendock",
            "contact_name": "Melissa Tolbert (Polar scheduler)",
            "contact_email": "mtolbert@polarbev.com",
            "contact_phone": "508-453-4273",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 118808052631" and "Pick Up Request" (May 2026): '
            '"Please use OPENDOCK portal to schedule pickups ... Warehouse: 220 - 245 Frank RD, '
            'Dock: 220 Frank Rd"; Opendock Nova notifications "Appointment requested/confirmed at '
            'Polar Beverages Georgia - G6 - 220 - 245 Frank Rd" (May 2026)'
        ),
        "summary": (
            "Book pickups in the Opendock portal: warehouse '220 - 245 Frank Rd', dock '220 Frank "
            "Rd' (220 and 245 Frank Rd are one Opendock warehouse, G6). Opendock emails the "
            "request, confirmation and arrival events to the lidl@ group. PO release and date "
            "questions go by email to the Polar CS team (csrpolarbev@polarbev.com, 508-749-2486); "
            "Polar cancels the appointment when a PO is not ready. Firm appointment, arrive 30 "
            "minutes early, load bars and straps, BOL before leaving."
        ),
    },
    {
        "name": "Polar Corporation, Worcester MA",
        "keys": ["candidate:fe63d5cd574b32f4"],
        "set": {
            "booking_method": "email",
            "contact_name": "Polar CS team (Michelle McDonald)",
            "contact_email": "csrpolarbev@polarbev.com",
            "contact_phone": "508-749-2486",
        },
        "evidence": (
            'group archive: "Lidl Pick Up" / "Lidl Pick Ups" threads with Polar CSR (Apr-Jul 2026): '
            'Circle asks "Can I please schedule the following?", Polar CSR replies "POs okay for '
            'pick up on the dates below"; signature "Polar CS office 508-749-2486, '
            'csrpolarbev@polarbev.com, mmcdonald@polarbev.com"'
        ),
        "summary": (
            "Pickups are requested by email to the Polar CS team (csrpolarbev@polarbev.com, copy "
            "mmcdonald@polarbev.com); they confirm which POs may be picked up on which dates. Firm "
            "live appointments; load bars and straps; BOL before leaving. Beverage tray returns "
            "need no pickup appointment (tell the guard shack)."
        ),
    },
    {
        "name": "DelGrosso Foods, Tyrone PA (Eagle facility)",
        "keys": ["candidate:aecac731a019b622"],
        "set": {
            "booking_method": "email",
            "contact_name": "Gary Weaver (Logistics Manager)",
            "contact_email": "gweaver@delgrossos.com",
            "contact_phone": "814-684-5880",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 104427082660" (Sep 2026), "Pick Up Appointment: '
            '104428072660 & 104428072630" (Jul 2026), "Lidl Pick Up Appointments" (Aug 2026), all '
            "booked by email with Gary Weaver, Logistics Manager, 814-684-5880 x189; the Eagle "
            "facility warehouse coordinator (ext 756) confirms on site"
        ),
        "summary": (
            "Pickup appointments are booked by email with Gary Weaver, Logistics Manager "
            "(814-684-5880 x189); the Eagle facility warehouse coordinator (ext 756) handles "
            "same-day questions. Firm appointment, arrive 30 minutes early, load bars and straps, "
            "BOL before leaving. A missed slot is re-booked next morning by email."
        ),
    },
    {
        "name": "Ardent Mills, Hanover PA (Hanover Logistics warehouse)",
        "keys": ["candidate:812464042aa009d3"],
        "set": {
            "booking_method": "email",
            "contact_name": "Hanover Logistics transportation desk (Peggy Groves)",
            "contact_email": "transportation@hanoverlogistics.com",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 241430072630" (Jul 2026, "Set for 7/29 @ 1600"), '
            '"Pick Up Appointment: 241424082630" (Aug 2026, "Set for 8/24 @ 1700"), "Pick Up '
            'Appointment: 241417092630" (Sep 2026), all with transportation@hanoverlogistics.com'
        ),
        "summary": (
            "The Hanover PA site is run by Hanover Logistics (Hanover Terminal, Inc.). Pickups are "
            "booked by email to transportation@hanoverlogistics.com, which replies with a set time. "
            "Firm appointment; load bars and straps; BOL before leaving."
        ),
    },
    {
        "name": "RLS Logistics, Lebanon PA (Murry's)",
        "keys": ["candidate:e5b511ec379e6553"],
        "set": {
            "booking_method": "email",
            "contact_name": "Lebanon Valley Cold Storage scheduling desk",
            "contact_email": "lebanonvalley@rlslogistics.com",
        },
        "evidence": (
            'group archive: "Lidl Pick Up Appointment" (Aug 2026): Lidl inbound: "RLS Logistics, '
            "2750 Hanford Drive, Lebanon PA 17046. Scheduling contact is "
            "Lebanonvalley@rlslogistics.com\"; the vendor is Murry's, which moved from RLS Allentown "
            "(desk apptsgress@corexpartners.zohodesk.com) in Aug 2026"
        ),
        "summary": (
            "Cold-storage pickup for Murry's. Book by email to lebanonvalley@rlslogistics.com "
            "(replaced the RLS Allentown desk apptsgress@corexpartners.zohodesk.com in Aug 2026). "
            "Firm reefer appointment, arrive 30 minutes early, load bar, BOL before leaving."
        ),
    },
    {
        "name": "Premium Waters, Allentown PA",
        "keys": ["candidate:bc484c34efb8fd54"],
        "set": {
            "booking_method": "email",
            "contact_name": "Wesley Brown (Senior Customer Account Specialist)",
            "contact_email": "wesley.brown@premiumwaters.com",
            "contact_phone": "828-544-6724",
        },
        "evidence": (
            'group archive: "Delivery Appointment: 204515072660" (Jul 2026, "Can we please pick up '
            'at 1500?", reply "Allentown ships Monday-Friday 7am-4pm"), "Pick up appointment - '
            '107204511072360" (Jul 2023), both with wesley.brown@premiumwaters.com'
        ),
        "summary": (
            "Pickups are arranged by email with Wesley Brown (wesley.brown@premiumwaters.com, "
            "828-544-6724). Allentown ships Monday to Friday 07:00-16:00. Arrive 30 minutes early; "
            "load bar required."
        ),
    },
    {
        "name": "CG Roxane, Johnstown NY",
        "keys": ["candidate:e96fde55907d5c53"],
        "set": {
            "appointment_required": "true",
            "time_granularity": "exact",
            "booking_method": "email",
            "contact_name": "Louay Albadawi (shipping clerk)",
            "contact_email": "l.albadawi@cgroxane.com",
            "contact_phone": "518-736-1979",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 288703092601" (Aug 2026), "Pick Up Appointments: '
            '288726082601 & 288727082601" (Aug 2026), "Pick Up Appointment: 288720072603" (Jul '
            "2026), all booked by email with the Johnstown shipping clerk (518-736-1979); shared "
            "desk shippingjohnstown@cgroxane.com in earlier signatures"
        ),
        "summary": (
            "Pickup appointments are booked by email with the Johnstown shipping clerk "
            "(l.albadawi@cgroxane.com; shared desk shippingjohnstown@cgroxane.com; 518-736-1979) "
            "and get a set time. Load bars and straps; pictures of the load and BOL before "
            "leaving; loaders re-wrap pallets on request. Closed on US holidays."
        ),
    },
    {
        "name": "Koch Foods, Erlanger KY (UDF-run desk)",
        "keys": ["candidate:4d2cf80142f29d6d"],
        "set": {
            "appointment_required": "true",
            "time_granularity": "exact",
            "booking_method": "email",
            "contact_name": "CCI desk / Shannon Humphrey (CSR)",
            "contact_email": "cci@udfinc.com",
            "contact_phone": "859-578-1608",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 226321092660" (Sep 2026): request to cci@udfinc.com, '
            "CSR Shannon Humphrey (SHumphre@udfinc.com, 859-578-1608) replies with open slots "
            '("21st and 22nd is full, I have the 23rd from 16:00 to 23:00") and confirms "9/24/26 '
            '11:00 CIRCLE #226321092660 CCI-9389"; also "Pick Up Appointment: 226331082660 & '
            '226304092660" (Aug 2026)'
        ),
        "summary": (
            "Pickup appointments are booked by email to the CCI desk (cci@udfinc.com, copy "
            "SHumphre@udfinc.com, 859-578-1608). The desk offers open slots and confirms with a "
            "time and a CCI pickup number; days fill up, so ask several days ahead. Load bar and "
            "straps required."
        ),
    },
    {
        "name": "Seneca Foods, Ripon WI",
        "keys": ["candidate:72e4bfbafdb40ad4"],
        "set": {
            "appointment_required": "true",
            "booking_method": "email",
            "contact_name": "Ripon distribution desk (Lynn Schepp)",
            "contact_email": "ripondistribution@senecafoods.com",
            "contact_phone": "920-745-3119",
        },
        "evidence": (
            'group archive: "Pick Up Appointment: 109631072601 & 109631072602" (Jul 2026), "Pick up '
            'Appointments: 109617092430 & 109617092432" (Sep 2024) and others, booked by email with '
            "RiponDistribution@senecafoods.com, (920) 745-3119 ext 37200; signature: shipping hours "
            "0600-2100 Monday-Friday"
        ),
        "summary": (
            "Pickup appointments are booked by email to RiponDistribution@senecafoods.com "
            "(920-745-3119 ext 37200). Shipping hours 06:00-21:00 Monday to Friday; closed on US "
            "holidays. Both POs on an order ship together; load bars and straps."
        ),
    },
]

AIRPACK_KEY = "candidate:21d5ed642fcd683d"
AIRPACK_ITEMS = [112, 113]
AIRPACK_SUMMARY = (
    "Not a Lidl vendor: Airpack (White Marsh MD) was used once, on 2026-09-11, as an emergency "
    "cross-dock for a load tendered to the wrong RDC (group thread 118811092632 URGENT). No "
    "appointment process; the firm-appointment stop note was copied from the original load."
)

RDC_CITIES = ("Perryville", "Fredericksburg", "Mebane")
RDC_SET = {"booking_method": "email", "contact_email": "inbound@lidl.us"}
RDC_EVIDENCE = (
    'group archive: "118811092632 URGENT" (Sep 2026: "the new delivery appointment will be: '
    '7/14 7AM - GRM_140926841"), "118826052630 Closed On Monday" and "118826052660 Polar Closed '
    'for Holiday" (May 2026: "Can you please reschedule PO# ..."), "226321092660" (Sep 2026): '
    "Lidl's inbound desk inbound@lidl.us assigns and moves RDC delivery slots by email"
)
RDC_SUMMARY = (
    "Lidl's inbound transport desk (inbound@lidl.us) assigns the delivery slot and delivery "
    "number on the tender; Circle emails the same desk to request or move a slot when the "
    "vendor pickup changes. Goods In may refuse a late arrival and reschedule. Drivers check in "
    "at the Goods In door."
)


def rdc_keys() -> list[str]:
    """Store keys of the Lidl RDC receiver facilities (name starts with Lidl, RDC cities)."""
    db = ROOT / STORE.removeprefix("sqlite:///./")
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "select key from facilities where lower(company_name) like 'lidl%' "
            "and city in (?, ?, ?) order by stop_count desc",
            RDC_CITIES,
        ).fetchall()
    return [r[0] for r in rows]


def cli(*args: str, dry_run: bool, allow_fail: bool = False) -> None:
    cmd = [str(CLI), *args]
    print("$", " ".join(f'"{a}"' if " " in a else a for a in cmd[1:])[:200])
    if dry_run:
        return
    env = {**os.environ, "FP_DATABASE_URL": STORE, "FP_LOG_LEVEL": "WARNING"}
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    out = ((proc.stdout or "") + (proc.stderr or "")).strip()
    print("  ", out.splitlines()[-1] if out else "(no output)")
    if proc.returncode != 0 and not allow_fail:
        sys.exit(f"failed: {' '.join(args)}\n{out}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    only_rdc = "--only-rdc" in sys.argv
    if not only_rdc:
        for f in FINDINGS:
            print(f"\n## {f['name']}")
            for key in f["keys"]:  # type: ignore[union-attr]
                for field, value in f["set"].items():  # type: ignore[union-attr]
                    cli(
                        "profile",
                        "set",
                        key,
                        "shipper",
                        field,
                        "--value",
                        value,
                        "--by",
                        BY,
                        "--reason",
                        str(f["evidence"]),
                        dry_run=dry_run,
                    )
                cli(
                    "profile",
                    "summary",
                    key,
                    "shipper",
                    "--text",
                    str(f["summary"]),
                    "--by",
                    BY,
                    dry_run=dry_run,
                )
        print(f"\n## Airpack ({AIRPACK_KEY})")
        for item in AIRPACK_ITEMS:
            cli("review", "reject", str(item), "--by", BY, dry_run=dry_run, allow_fail=True)
        cli(
            "profile",
            "summary",
            AIRPACK_KEY,
            "shipper",
            "--text",
            AIRPACK_SUMMARY,
            "--by",
            BY,
            dry_run=dry_run,
        )

    print("\n## Lidl RDC receiving desks")
    for key in rdc_keys():
        for field, value in RDC_SET.items():
            cli(
                "profile",
                "set",
                key,
                "receiver",
                field,
                "--value",
                value,
                "--by",
                BY,
                "--reason",
                RDC_EVIDENCE,
                dry_run=dry_run,
            )
        cli(
            "profile",
            "summary",
            key,
            "receiver",
            "--text",
            RDC_SUMMARY,
            "--by",
            BY,
            dry_run=dry_run,
        )
    print("\ndone" + (" (dry run)" if dry_run else ""))


if __name__ == "__main__":
    main()
