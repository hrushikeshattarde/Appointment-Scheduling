"""CSV export of trusted profile values in Transport Pro field names (FR-10)."""

from __future__ import annotations

import csv
from pathlib import Path

from facility_profiles.domain.rules import to_tpro_write
from facility_profiles.domain.schema import Role
from facility_profiles.pipeline.collect import identity_from_record
from facility_profiles.pipeline.profile import displayable_fields, profile_from_records
from facility_profiles.storage.repository import Repository

EXPORT_COLUMNS = (
    "facility_id",
    "company_name",
    "city",
    "state",
    "role",
    "appointments.method",
    "appointments.contact",
    "appointments.email",
    "appointments.phone",
    "appointments.portalURL",
    "appointments.notes",
    "businessHours",
    "min_confidence",
    "field_states",
    "source_load_ids",
    "run_id",
)


def export_profiles_csv(repo: Repository, path: Path, *, linked_only: bool = True) -> int:
    """Write one row per facility and role that has at least one trusted field."""
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = 0
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        for record in repo.list_facilities(linked_only=linked_only):
            for role in repo.roles_for(record.key):
                fields = repo.fields(record.key, role)
                if not fields:
                    continue
                identity = identity_from_record(record, [])
                profile = profile_from_records(
                    identity, role, fields, repo.profile(record.key, role)
                )
                trusted = displayable_fields(profile)
                if not trusted:
                    continue
                trusted_profile = profile.model_copy(update={"fields": trusted})
                tpro = to_tpro_write(trusted_profile).as_dict()
                writer.writerow(
                    {
                        "facility_id": record.facility_id or "",
                        "company_name": record.company_name or "",
                        "city": record.city or "",
                        "state": record.state or "",
                        "role": role.value,
                        **{k: (v or "") for k, v in tpro.items()},
                        "min_confidence": f"{min(f.confidence for f in trusted.values()):.2f}",
                        "field_states": ";".join(
                            f"{n}={f.state.value}" for n, f in trusted.items()
                        ),
                        "source_load_ids": ";".join(str(i) for i in profile.source_load_ids[:50]),
                        "run_id": profile.run_id or "",
                    }
                )
                rows += 1
    return rows


def roles_of(repo: Repository, key: str) -> list[Role]:
    """Roles a facility has been seen in (thin wrapper for callers outside the pipeline)."""
    return repo.roles_for(key)
