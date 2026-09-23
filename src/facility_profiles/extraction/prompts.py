"""Prompt text for the extractor.

The system prompt is frozen so it is served from the prompt cache; everything that varies per
facility goes in the user message, rendered by :func:`render_user_message`.
"""

from __future__ import annotations

from facility_profiles.extraction.bundle import SourceBundle

PROMPT_VERSION = "2026-09-22.1"

SYSTEM_PROMPT = f"""You extract how a freight facility (a shipper or receiver) takes \
appointments, using only Circle Logistics' own records about that facility. \
Prompt version {PROMPT_VERSION}.

You receive a bundle of numbered sources, each tagged like [S3]. Return the JSON schema you are \
given and nothing else.

How to fill each field
- For every field, list EVERY distinct candidate value the sources support, each with the quotes \
that support it. Two different values are two candidates; never merge them and never pick one.
- A quote is text copied verbatim from exactly one source (same characters, at most 200 \
characters) and references that source's tag. Do not paraphrase, do not combine sources.
- Leave a field's candidate list empty when the sources say nothing about it. Never guess.
- appointment_required: "true" when the site needs a booked appointment, "false" when it is \
first come first served or explicitly needs none.
- booking_method: one of phone, email, web_portal, fcfs, preset_by_customer. Use \
preset_by_customer when the customer or shipper sets the appointment and Circle only receives \
it ("preset appt", "appointment scheduled by the customer"). Use fcfs for first come first \
served, work-ins and walk-ins. Do not emit "unknown".
- contact_name: a person's name only, as written. Not a company.
- contact_phone: the number exactly as written in the source.
- contact_email: the address exactly as written.
- portal_url: the URL exactly as written.
- portal_vendor: one of opendock, c3, datadocks, one_network, e2open, blue_yonder, retalix, \
other. Infer from the URL or the name of the system.
- notice_period_hours: whole hours of advance notice. "72 HOUR NOTICE" -> "72"; "24 hr" -> "24"; \
"2 days" -> "48"; "same day" -> "0".
- time_granularity: exact when the site gives a single time, window when it gives a range, \
mixed when the sources show both.
- receiving_hours: opening spans in the facility's local time. "FCFS 0700-1430 MON-FRI" -> days \
mon..fri, open "07:00", close "14:30". "24/7" -> all seven days, "00:00" to "23:59". \
"BY APPT M-F 0730-1530" -> by_appointment true. Separate spans for different days.
- scheduling_summary: one plain sentence, at most 200 characters, on how this site books, or \
null when the sources say nothing about scheduling.

Rules
- Only scheduling facts. Ignore trailer, paperwork, dress code, fees, seal, detention, and \
tracking rules.
- The bundle says whether this facility is the shipper or the receiver on these loads. Extract \
the rules for that role. If a note clearly describes the other stop on the load, skip it.
- Sources tagged as the facility's own record fields are one more source, not the truth.
- Old, contradictory or stale notes still count: list them as candidates so reviewers see the \
disagreement.
- Notes may be in Spanish: "cita" means appointment; "cita para cargar" (pickup) and "cita para \
descargar" (delivery) mean an appointment is required.
- Confidence is your belief that the quoted text means the value you assigned, 0 to 1.
"""


def render_user_message(bundle: SourceBundle) -> str:
    """Render the per-facility user message: identity, existing record fields, numbered sources."""
    identity = bundle.identity
    lines: list[str] = [
        f"Facility: {identity.company_name or 'unknown name'}",
        f"Address: {identity.address or ''}, {identity.city or ''}, {identity.state or ''} "
        f"{identity.postal_code or ''}".strip(),
        f"Transport Pro location ID: {identity.facility_id if identity.facility_id else 'none'}",
        f"Role on these loads: {bundle.role.value}",
    ]
    if identity.aliases:
        lines.append("Also appears as: " + "; ".join(identity.aliases[:8]))
    if identity.iana_timezone:
        lines.append(f"Local time zone: {identity.iana_timezone}")

    existing = bundle.existing
    if existing is not None and not existing.is_empty():
        lines.append("")
        lines.append(
            "Appointment fields already on the facility record (treat as one more source):"
        )
        for label, value in (
            ("method", existing.method),
            ("contact", existing.contact),
            ("email", existing.email),
            ("phone", existing.phone),
            ("portal URL", existing.portal_url),
            ("notes", existing.notes),
            ("business hours", existing.business_hours),
        ):
            if value:
                lines.append(f"  {label}: {value}")

    lines.append("")
    lines.append(f"Sources ({len(bundle.sources)}):")
    for doc in bundle.sources:
        when = doc.observed_at.date().isoformat() if doc.observed_at else "undated"
        load = f"load {doc.load_id}" if doc.load_id is not None else "facility record"
        repeats = (
            f", same text on {len(doc.repeat_load_ids)} more load(s)" if doc.repeat_load_ids else ""
        )
        lines.append(f"[{doc.source_id}] {doc.source_type.value} | {load} | {when}{repeats}")
        lines.append(doc.text)
        lines.append("")
    return "\n".join(lines).rstrip()
