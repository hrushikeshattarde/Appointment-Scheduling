"""File the booking-desk rules the lidl@ threads stated, for the vendor profiles on pod 1089.

Two desks will not book a Lidl pickup on the PO alone:

- Morgan Foods (shipping.appointments@morganfoods.com) wants Lidl's TI shipment number: "Lidl is
  usually scheduled through TI", and it asks for the shipment number "given to you by TI".
- RLS Logistics, Lebanon PA (Murry's) wants Lidl's SO number per PO: "PLEASE PROVIDE SO NUMBER,
  this PO is not showing in our system" (Aug 2026), and confirms with it ("SO259719").

Filed as ``required_refs``: the booking agent then holds the request, raises "Reference needed"
on the case, and writes the number into the request line once a person adds it (``booking
ref``). No cut-off or booking horizon was stated by any Lidl vendor, so none is filed.

Morgan Foods also refuses a driver not registered in its Eaigle gate system, which opens 48 hours
before arrival (the link is in the desk's signature). Filed as ``carrier_steps``: a booked Morgan
pickup raises "Tell the carrier" with the time registration opens. Run after
``seed_lidl_vendor_profiles.py``; re-running is harmless.

    python scripts/seed_lidl_desk_rules.py [--dry-run]
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / ".venv" / "Scripts" / "facility-profiles.exe"
STORE = "sqlite:///./data/facility_profiles_pod-1089-lidl.db"
BY = "claude/lidl-groups-archive"

RULES: list[dict[str, str]] = [
    {
        "name": "Morgan Foods, Austin IN",
        "key": "candidate:a425a687fe4390ac",
        "required_refs": "shipment_number",
        "evidence": (
            'lidl@ group archive, "Pick Up Appointment" threads (2026): Morgan Foods asks for the '
            'shipment number "given to you by TI", not the PO; "Lidl is usually scheduled through '
            'TI"'
        ),
        "carrier_steps": (
            "48h before: Register the driver in Morgan Foods' Eaigle gate system "
            "(driverapp.morganfoods.eaigle.ai); a driver not registered is refused entry"
        ),
        "carrier_evidence": (
            'lidl@ group mail "Pick Up Appointments", 24-29 Sep 2026: every driver must register '
            "in the Eaigle gate system no earlier than 48 hours before arrival or is refused entry"
        ),
    },
    {
        "name": "RLS Logistics, Lebanon PA (Murry's)",
        "key": "candidate:e5b511ec379e6553",
        "required_refs": "sales_order_number",
        "evidence": (
            'lidl@ group archive, "Pick Up Appointments 200527082601 & 288726082601" (Aug 2026): '
            '"PLEASE PROVIDE SO NUMBER, this PO is not showing in our system"; confirmations '
            'carry the SO number ("SO259719")'
        ),
    },
]


def cli(*args: str, dry_run: bool) -> None:
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
    if proc.returncode != 0:
        sys.exit(f"failed: {' '.join(args)}\n{out}")


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    for rule in RULES:
        print(f"\n## {rule['name']}")
        filed = [("required_refs", "evidence")]
        if "carrier_steps" in rule:
            filed.append(("carrier_steps", "carrier_evidence"))
        for field, evidence in filed:
            cli(
                "profile",
                "set",
                rule["key"],
                "shipper",
                field,
                "--value",
                rule[field],
                "--by",
                BY,
                "--reason",
                rule[evidence],
                dry_run=dry_run,
            )


if __name__ == "__main__":
    main()
