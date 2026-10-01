"""Excel export of the store, including a reviewer-friendly queue sheet (FR-10, FR-11).

The "Review Queue" sheet is designed to be filled in by a CSR: three input columns (Decision,
Corrected value, Reviewer) beside each open item, sorted so the quick, high-volume decisions come
first. :mod:`facility_profiles.review.xlsx_import` reads the answers back.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import select
from sqlalchemy.orm import Session

from facility_profiles import __version__
from facility_profiles.domain.rules import format_hours
from facility_profiles.storage.models import (
    AuditEntry,
    FacilityLoadLink,
    FacilityRecord,
    ProfileFieldRecord,
    ProfileRecord,
    ReviewItem,
    Run,
)
from facility_profiles.storage.repository import Repository, unwrap

FONT = "Arial"
HEADER_FILL = PatternFill("solid", fgColor="1F3864")
INPUT_FILL = PatternFill("solid", fgColor="FFF2CC")
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF")
BODY_FONT = Font(name=FONT)
NOTE_FONT = Font(name=FONT, italic=True, color="595959")
STATE_FILL = {
    "verified": "E2EFDA",
    "extracted": "DDEBF7",
    "queued": "FFF2CC",
    "discarded": "F2F2F2",
    "written": "C6E0B4",
    "human_set": "BDD7EE",
}
REVIEW_SHEET = "Review Queue"
REVIEW_HEADERS = (
    "Item",
    "Facility",
    "TPro location ID",
    "Stops (90 days)",
    "Role",
    "Field",
    "Proposed value",
    "Value on record",
    "Why it is here",
    "Evidence",
    "Other candidates",
    "Decision",
    "Corrected value",
    "Reviewer",
    "Notes",
)
DECISIONS = ("accept", "edit", "reject")
FIELD_ORDER = {
    "appointment_required": 0,
    "booking_method": 1,
    "portal_url": 2,
    "portal_vendor": 3,
    "contact_email": 4,
    "contact_phone": 5,
    "contact_name": 6,
    "receiving_hours": 7,
    "notice_period_hours": 8,
    "cutoff_time": 9,
    "max_days_ahead": 10,
    "required_refs": 11,
    "time_granularity": 12,
}
LEGEND = (
    "HOW TO USE: fill in the three yellow columns for each row you can decide. Decision = accept "
    "(the proposed value is right), edit (type the right value in Corrected value), or reject "
    "(leave the record as it is). Reviewer = your name. Leave a row blank to skip it. Then run: "
    "facility-profiles review import <this file>."
)

Facilities = dict[str, FacilityRecord]


def plain_reason(reason: str | None) -> str:
    """Translate the audit reason into a sentence a reviewer can act on."""
    text = (reason or "").lower()
    if "person entered" in text or "already on the record" in text:
        return "The notes disagree with the facility record. Which is right?"
    if "conflicting values" in text:
        return "Two sources say different things. Pick the right one."
    if "between thresholds" in text:
        return (
            "The evidence is real but not strong enough to file automatically. Confirm or correct."
        )
    if "no supporting mention" in text:
        return "This value has not been seen in any load for months. Is it still right?"
    return reason or ""


def render_value(value: Any) -> Any:
    """Render a stored value for a cell."""
    if isinstance(value, list):
        return format_hours(value) or json.dumps(value)
    if isinstance(value, bool):
        return "yes" if value else "no"
    return value


def _candidates_text(candidates: list[dict[str, Any]], *, skip_first: bool) -> str:
    chosen = candidates[1:4] if skip_first else candidates[:4]
    return "; ".join(
        f"{render_value(c.get('value'))} ({round(c.get('support', 0) * 100)}% of mentions, "
        f"{c.get('distinct_loads', 0)} loads)"
        for c in chosen
    )


@dataclass(frozen=True)
class ExportStats:
    """Row counts per sheet."""

    fields: int
    summaries: int
    queue: int
    facilities: int
    audit: int
    runs: int


def _add_table(
    ws: Worksheet,
    headers: tuple[str, ...] | list[str],
    rows: list[list[Any]],
    *,
    widths: dict[str, int] | None = None,
    note: str | None = None,
) -> int:
    """Write a header row (and optional note above it) plus rows; return the header row number."""
    start = 1
    if note:
        ws.cell(row=1, column=1, value=note).font = NOTE_FONT
        start = 3
    for col, header in enumerate(headers, start=1):
        cell = ws.cell(row=start, column=col, value=header)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for r, row in enumerate(rows, start=start + 1):
        for c, value in enumerate(row, start=1):
            cell = ws.cell(row=r, column=c, value=value)
            cell.font = BODY_FONT
            cell.alignment = Alignment(
                vertical="top", wrap_text=isinstance(value, str) and len(value) > 40
            )
    ws.freeze_panes = ws.cell(row=start + 1, column=1)
    if rows:
        ws.auto_filter.ref = f"A{start}:{get_column_letter(len(headers))}{start + len(rows)}"
    for i, header in enumerate(headers, start=1):
        width = (widths or {}).get(header)
        if width is None:
            sample = [len(str(r[i - 1])) for r in rows[:200] if r[i - 1] is not None]
            width = min(max(len(header), *(sample or [len(header)])) + 2, 60)
        ws.column_dimensions[get_column_letter(i)].width = width
    return start


def _review_rows(repo: Repository, facilities: Facilities) -> list[list[Any]]:
    items = [
        item
        for item in repo.list_review_items(status="open", limit=10_000)
        if item.facility_key in facilities
    ]

    def sort_key(item: ReviewItem) -> tuple[int, int, str]:
        fac = facilities.get(item.facility_key)
        return (FIELD_ORDER.get(item.field_name, 99), -(fac.stop_count if fac else 0), item.role)

    rows: list[list[Any]] = []
    for item in sorted(items, key=sort_key):
        fac = facilities.get(item.facility_key)
        evidence = "; ".join(
            f'"{e.get("quote", "")[:120]}" (load {e.get("load_id") or "record"})'
            for c in (item.candidates or [])[:1]
            for e in (c.get("evidence") or [])[:3]
        )
        rows.append(
            [
                item.id,
                fac.company_name if fac else item.facility_key,
                fac.facility_id if fac else None,
                fac.stop_count if fac else None,
                item.role,
                item.field_name,
                render_value(unwrap(item.proposed)),
                render_value(unwrap(item.existing)),
                plain_reason(item.reason),
                evidence,
                _candidates_text(item.candidates or [], skip_first=False),
                None,
                None,
                None,
                None,
            ]
        )
    return rows


def _review_sheet(ws: Worksheet, repo: Repository, facilities: Facilities) -> tuple[int, int, int]:
    """Fill the reviewer sheet; return (first data row, last data row, row count)."""
    rows = _review_rows(repo, facilities)
    header = _add_table(
        ws,
        REVIEW_HEADERS,
        rows,
        widths={
            "Facility": 32,
            "Proposed value": 34,
            "Value on record": 30,
            "Why it is here": 48,
            "Evidence": 60,
            "Other candidates": 44,
            "Decision": 12,
            "Corrected value": 30,
            "Reviewer": 16,
            "Notes": 30,
        },
        note=LEGEND,
    )
    first, last = header + 1, header + max(len(rows), 1)
    decision_col = get_column_letter(REVIEW_HEADERS.index("Decision") + 1)
    validation = DataValidation(
        type="list", formula1='"accept,edit,reject"', allow_blank=True, showDropDown=False
    )
    validation.error = "Type accept, edit or reject"
    validation.prompt = "accept / edit / reject"
    ws.add_data_validation(validation)
    validation.add(f"{decision_col}{first}:{decision_col}{last}")
    for col_name in ("Decision", "Corrected value", "Reviewer"):
        col = REVIEW_HEADERS.index(col_name) + 1
        for r in range(first, last + 1):
            ws.cell(row=r, column=col).fill = INPUT_FILL
    if rows:
        example = ws.cell(row=first, column=REVIEW_HEADERS.index("Notes") + 1)
        example.value = "Example: put accept in Decision and your name in Reviewer"
        example.font = NOTE_FONT
    return first, last, len(rows)


def _fields_sheet(
    ws: Worksheet, session: Session, facilities: Facilities, run: Run | None
) -> tuple[int, int, int]:
    """All scored fields; return (first data row, last data row, row count)."""
    reasons: dict[tuple[str, str, str], tuple[str, str]] = {}
    if run is not None:
        for entry in session.scalars(
            select(AuditEntry).where(AuditEntry.run_id == run.id).order_by(AuditEntry.id)
        ):
            if entry.field_name:
                reasons[(entry.facility_key, entry.role, entry.field_name)] = (
                    entry.action,
                    entry.reason or "",
                )
    rows: list[list[Any]] = []
    stmt = select(ProfileFieldRecord).order_by(
        ProfileFieldRecord.facility_key, ProfileFieldRecord.role, ProfileFieldRecord.field_name
    )
    for fld in session.scalars(stmt):
        if fld.facility_key not in facilities:
            continue
        value = unwrap(fld.value)
        if value is None and fld.mention_count == 0 and fld.origin != "human":
            continue
        fac = facilities.get(fld.facility_key)
        action, reason = reasons.get((fld.facility_key, fld.role, fld.field_name), ("", ""))
        top = fld.evidence[0] if fld.evidence else {}
        rows.append(
            [
                fac.company_name if fac else fld.facility_key,
                fac.facility_id if fac else None,
                fld.role,
                fld.field_name,
                render_value(value),
                round(fld.confidence, 2),
                fld.state,
                fld.origin,
                action,
                reason,
                fld.distinct_loads,
                fld.mention_count,
                "yes" if fld.conflict else "",
                top.get("source_type", ""),
                (top.get("quote") or "")[:200],
                _candidates_text(fld.candidates or [], skip_first=True),
                (fld.run_id or "")[:8],
            ]
        )
    headers = [
        "Facility",
        "TPro location ID",
        "Role",
        "Field",
        "Value",
        "Confidence",
        "State",
        "Origin",
        "Decision",
        "Reason",
        "Distinct loads",
        "Mentions",
        "Conflict",
        "Top evidence source",
        "Top evidence quote",
        "Other candidates",
        "Run",
    ]
    header = _add_table(
        ws,
        headers,
        rows,
        widths={
            "Facility": 30,
            "Value": 44,
            "Reason": 44,
            "Top evidence quote": 50,
            "Other candidates": 44,
        },
        note=(
            "All scored fields in the store. State: verified = matches the Transport Pro record; "
            "extracted = recommended for writing (recommend mode); queued = needs a person; "
            "discarded = too weak; human_set = decided by a reviewer."
        ),
    )
    state_col = headers.index("State") + 1
    for r in range(header + 1, header + 1 + len(rows)):
        cell = ws.cell(row=r, column=state_col)
        fill = STATE_FILL.get(str(cell.value))
        if fill:
            cell.fill = PatternFill("solid", fgColor=fill)
    return header + 1, header + max(len(rows), 1), len(rows)


def _summaries_sheet(ws: Worksheet, session: Session, facilities: Facilities) -> int:
    rows: list[list[Any]] = []
    for prof in session.scalars(
        select(ProfileRecord).order_by(ProfileRecord.facility_key, ProfileRecord.role)
    ):
        if prof.facility_key not in facilities:
            continue
        fac = facilities.get(prof.facility_key)
        existing = fac.existing if fac else {}
        rows.append(
            [
                fac.company_name if fac else prof.facility_key,
                fac.facility_id if fac else None,
                f"{fac.city}, {fac.state}" if fac else "",
                fac.stop_count if fac else None,
                prof.role,
                prof.scheduling_summary,
                prof.source_count,
                existing.get("method"),
                existing.get("contact"),
                existing.get("email"),
                existing.get("phone"),
                existing.get("portal_url"),
                existing.get("business_hours"),
                (prof.model_version or "")[:40],
            ]
        )
    _add_table(
        ws,
        [
            "Facility",
            "TPro location ID",
            "City",
            "Stops (90 days)",
            "Role",
            "Scheduling summary (model)",
            "Sources read",
            "Record: method",
            "Record: contact",
            "Record: email",
            "Record: phone",
            "Record: portal URL",
            "Record: business hours",
            "Model",
        ],
        rows,
        widths={"Facility": 30, "Scheduling summary (model)": 70, "Record: portal URL": 40},
        note=(
            "One row per facility and role. 'Record:' columns are the Transport Pro facility "
            "record as fetched on the run date."
        ),
    )
    return len(rows)


def _facilities_sheet(ws: Worksheet, session: Session, facilities: Facilities) -> int:
    roles_by_key: dict[str, set[str]] = {}
    for key, role in session.execute(
        select(FacilityLoadLink.facility_key, FacilityLoadLink.role).distinct()
    ):
        roles_by_key.setdefault(key, set()).add(role)
    profiled = {p.facility_key for p in session.scalars(select(ProfileRecord))}
    rows: list[list[Any]] = []
    for fac in sorted(facilities.values(), key=lambda f: -f.stop_count):
        ex = fac.existing or {}
        rows.append(
            [
                fac.company_name,
                fac.facility_id,
                fac.key if fac.facility_id is None else "",
                fac.address,
                fac.city,
                fac.state,
                fac.postal_code,
                fac.stop_count,
                "/".join(sorted(roles_by_key.get(fac.key, []))),
                "yes" if fac.facility_id is not None else "no (candidate)",
                "yes" if fac.key in profiled else "",
                ex.get("method"),
                ex.get("contact"),
                ex.get("email"),
                ex.get("phone"),
                ex.get("portal_url"),
                ex.get("business_hours"),
                fac.last_seen_load_at.strftime("%Y-%m-%d") if fac.last_seen_load_at else "",
            ]
        )
    _add_table(
        ws,
        [
            "Facility",
            "TPro location ID",
            "Candidate key",
            "Address",
            "City",
            "State",
            "Postal code",
            "Stops (90 days)",
            "Roles",
            "Linked to TPro record",
            "Profiled",
            "Record: method",
            "Record: contact",
            "Record: email",
            "Record: phone",
            "Record: portal URL",
            "Record: business hours",
            "Last stop date",
        ],
        rows,
        widths={"Facility": 34, "Address": 30, "Record: portal URL": 36},
        note="Every facility derived from the harvested loads, including unlinked candidates.",
    )
    return len(rows)


def _audit_sheet(ws: Worksheet, session: Session, facilities: Facilities, run: Run | None) -> int:
    rows: list[list[Any]] = []
    if run is not None:
        for entry in session.scalars(
            select(AuditEntry).where(AuditEntry.run_id == run.id).order_by(AuditEntry.id)
        ):
            if entry.facility_key not in facilities:
                continue
            fac = facilities.get(entry.facility_key)
            rows.append(
                [
                    entry.id,
                    entry.created_at.strftime("%Y-%m-%d %H:%M:%S") if entry.created_at else "",
                    fac.company_name if fac else entry.facility_key,
                    entry.role,
                    entry.field_name,
                    entry.action,
                    render_value(unwrap(entry.before)),
                    render_value(unwrap(entry.after)),
                    round(entry.confidence, 2) if entry.confidence is not None else None,
                    len(entry.source_load_ids or []),
                    entry.reason,
                    entry.actor,
                ]
            )
    _add_table(
        ws,
        [
            "Entry",
            "When (UTC)",
            "Facility",
            "Role",
            "Field",
            "Action",
            "Before",
            "After",
            "Confidence",
            "Source loads",
            "Reason",
            "Actor",
        ],
        rows,
        widths={"Facility": 30, "Before": 30, "After": 40, "Reason": 44},
        note="Every decision of the latest run.",
    )
    return len(rows)


def _runs_sheet(ws: Worksheet, session: Session) -> int:
    rows: list[list[Any]] = []
    for item in session.scalars(select(Run).order_by(Run.started_at)):
        st = item.stats or {}
        actions = st.get("actions") or {}
        harvest = st.get("harvest") or {}
        rows.append(
            [
                item.id[:8],
                item.started_at.strftime("%Y-%m-%d %H:%M") if item.started_at else "",
                item.status,
                item.mode,
                item.model_version,
                item.terminal_ids,
                harvest.get("loads"),
                harvest.get("stops"),
                st.get("profiles"),
                actions.get("verify"),
                actions.get("recommend"),
                actions.get("write"),
                actions.get("queue"),
                actions.get("discard"),
                actions.get("withdraw"),
                st.get("validation_issues"),
                st.get("llm_input_tokens"),
                st.get("llm_output_tokens"),
                st.get("llm_cost_usd"),
                item.error,
            ]
        )
    _add_table(
        ws,
        [
            "Run",
            "Started (UTC)",
            "Status",
            "Mode",
            "Model",
            "Terminals",
            "Loads harvested",
            "Stops",
            "Profiles",
            "Verified",
            "Recommended",
            "Written",
            "Queued",
            "Discarded",
            "Withdrawn",
            "Validation drops",
            "Tokens in",
            "Tokens out",
            "Cost USD",
            "Error",
        ],
        rows,
        note="One row per pipeline run.",
    )
    return len(rows)


def _summary_sheet(
    ws: Worksheet,
    *,
    run: Run | None,
    facility_rows: int,
    field_span: tuple[int, int],
    review_span: tuple[int, int],
) -> None:
    ws["A1"] = "Facility scheduling profiles"
    ws["A1"].font = Font(name=FONT, bold=True, size=14)
    ws["A2"] = (
        f"Exported {datetime.now(tz=UTC):%Y-%m-%d %H:%M} UTC by facility-profiles {__version__}"
        + (f"; latest run {run.id[:8]} ({run.mode} mode)." if run else ".")
    )
    ws["A2"].font = NOTE_FONT
    fac_last = 3 + max(facility_rows, 1)
    pf_first, pf_last = field_span
    rq_first, rq_last = review_span
    decision_col = get_column_letter(REVIEW_HEADERS.index("Decision") + 1)
    fields_range = f"'Profile Fields'!G{pf_first}:G{pf_last}"
    metrics = [
        ("Facilities in store", f"=COUNTA(Facilities!A4:A{fac_last})"),
        (
            "Facilities linked to a Transport Pro record",
            f'=COUNTIF(Facilities!J4:J{fac_last},"yes")',
        ),
        ("Facilities profiled", f'=COUNTIF(Facilities!K4:K{fac_last},"yes")'),
        ("Scored fields", f"=COUNTA('Profile Fields'!D{pf_first}:D{pf_last})"),
        ("  verified against the record", f'=COUNTIF({fields_range},"verified")'),
        ("  recommended for writing", f'=COUNTIF({fields_range},"extracted")'),
        ("  decided by a reviewer", f'=COUNTIF({fields_range},"human_set")'),
        ("  queued for a person", f'=COUNTIF({fields_range},"queued")'),
        ("  discarded", f'=COUNTIF({fields_range},"discarded")'),
        ("Open review items", f"=COUNTA('{REVIEW_SHEET}'!A{rq_first}:A{rq_last})"),
        (
            "  of which decided in this file",
            f"=COUNTA('{REVIEW_SHEET}'!{decision_col}{rq_first}:{decision_col}{rq_last})",
        ),
    ]
    ws["A4"], ws["B4"] = "Measure", "Value"
    for cell in (ws["A4"], ws["B4"]):
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for i, (label, formula) in enumerate(metrics, start=5):
        ws.cell(row=i, column=1, value=label).font = BODY_FONT
        ws.cell(row=i, column=2, value=formula).font = BODY_FONT
    ws.column_dimensions["A"].width = 52
    ws.column_dimensions["B"].width = 14
    guide = [
        "Sheets",
        "Review Queue: what needs a person. Fill in Decision, Corrected value and Reviewer, "
        "then import the file.",
        "Profile Fields: every scored field with its state, decision and the quote it rests on.",
        "Scheduling Summaries: the one-sentence summary per facility and role beside the "
        "Transport Pro record.",
        "Facilities: everything harvested from the loads. Audit Log: every decision of the "
        "latest run. Runs: all runs.",
        "Nothing is written to Transport Pro by this tool; the store runs in recommend mode.",
    ]
    for i, line in enumerate(guide, start=len(metrics) + 7):
        ws.cell(row=i, column=1, value=line).font = Font(name=FONT, bold=(i == len(metrics) + 7))


def build_workbook(
    session: Session, *, only: set[str] | None = None
) -> tuple[Workbook, ExportStats]:
    """Build the workbook from the store; ``only`` restricts it to those facility keys."""
    repo = Repository(session)
    run = repo.latest_run()
    facilities: Facilities = {
        f.key: f for f in session.scalars(select(FacilityRecord)) if only is None or f.key in only
    }
    wb = Workbook()

    review_ws = wb.active
    review_ws.title = REVIEW_SHEET
    rq_first, rq_last, queue_count = _review_sheet(review_ws, repo, facilities)
    pf_first, pf_last, field_count = _fields_sheet(
        wb.create_sheet("Profile Fields"), session, facilities, run
    )
    summary_count = _summaries_sheet(wb.create_sheet("Scheduling Summaries"), session, facilities)
    facility_count = _facilities_sheet(wb.create_sheet("Facilities"), session, facilities)
    audit_count = _audit_sheet(wb.create_sheet("Audit Log"), session, facilities, run)
    run_count = _runs_sheet(wb.create_sheet("Runs"), session)
    _summary_sheet(
        wb.create_sheet("Summary", 0),
        run=run,
        facility_rows=facility_count,
        field_span=(pf_first, pf_last),
        review_span=(rq_first, rq_last),
    )
    wb.calculation.fullCalcOnLoad = True
    return wb, ExportStats(
        fields=field_count,
        summaries=summary_count,
        queue=queue_count,
        facilities=facility_count,
        audit=audit_count,
        runs=run_count,
    )


def export_workbook(session: Session, path: Path, *, only: set[str] | None = None) -> ExportStats:
    """Build and save the workbook, optionally for a subset of facilities."""
    wb, stats = build_workbook(session, only=only)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    return stats
