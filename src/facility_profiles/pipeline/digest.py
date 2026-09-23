"""Daily digest for the pod lead (FR-13): what the run did, what needs a decision."""

from __future__ import annotations

from datetime import datetime

from facility_profiles.storage.models import Run
from facility_profiles.storage.repository import Repository, unwrap

MAX_LISTED_ITEMS = 25


def render_digest(repo: Repository, run: Run | None, *, now: datetime) -> str:
    """Render a Markdown digest for the most recent run and the current queue."""
    lines: list[str] = [f"# Facility profiles digest, {now.date().isoformat()}", ""]
    if run is None:
        lines.append("No run has completed yet.")
        return "\n".join(lines)

    stats = run.stats or {}
    lines.extend(
        [
            f"Run `{run.id[:8]}` started {run.started_at:%Y-%m-%d %H:%M} UTC, "
            f"status **{run.status}**, mode **{run.mode}**"
            + (f", terminals {run.terminal_ids}" if run.terminal_ids else "")
            + ".",
            "",
            "## What the run did",
            "",
        ]
    )
    actions = repo.action_counts(run.id)
    harvest = stats.get("harvest") or {}
    lines.append(
        f"- Facilities in store: {repo.count_facilities()} "
        f"({repo.count_facilities(linked_only=True)} linked to a Transport Pro record)"
    )
    if harvest:
        lines.append(
            f"- Loads read: {harvest.get('loads', 0)}, stops: {harvest.get('stops', 0)}, "
            f"new links: {harvest.get('new_links', 0)}"
        )
    lines.append(
        f"- Profiles processed: {stats.get('profiles', 0)}, "
        f"extraction errors: {stats.get('extraction_errors', 0)}"
    )
    for action in ("write", "recommend", "verify", "queue", "discard", "stale"):
        if actions.get(action):
            lines.append(f"- {action.capitalize()}: {actions[action]} fields")
    if stats.get("llm_input_tokens") is not None:
        lines.append(
            f"- LLM tokens: {stats.get('llm_input_tokens', 0)} in "
            f"({stats.get('llm_cache_read_tokens', 0)} cached), "
            f"{stats.get('llm_output_tokens', 0)} out"
        )

    open_items = repo.list_review_items(status="open", limit=MAX_LISTED_ITEMS)
    lines.extend(["", f"## Needs a decision ({repo.count_open_reviews()} open)", ""])
    if not open_items:
        lines.append("Nothing queued.")
    for item in open_items:
        facility = repo.get_facility(item.facility_key)
        name = facility.company_name if facility else item.facility_key
        proposed = unwrap(item.proposed)
        existing = unwrap(item.existing)
        detail = f"proposed `{proposed}`"
        if existing is not None:
            detail += f", existing `{existing}`"
        lines.append(
            f"- #{item.id} {name} ({item.role}) · {item.field_name}: {detail} · {item.reason}"
        )

    states = repo.count_fields_by_state()
    if states:
        lines.extend(["", "## Field states", ""])
        lines.extend(f"- {state}: {count}" for state, count in sorted(states.items()))
    return "\n".join(lines)
