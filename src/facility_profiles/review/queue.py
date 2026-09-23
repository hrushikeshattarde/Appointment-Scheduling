"""Reviewer decisions: accept, edit or reject a queued value (FR-11).

A decision becomes a human-set value with the highest weight; the routine never changes it
again, it can only re-queue it with new evidence.
"""

from __future__ import annotations

from typing import Any

from facility_profiles.domain.schema import FieldState, Role
from facility_profiles.extraction.validate import coerce
from facility_profiles.storage.models import ReviewItem
from facility_profiles.storage.repository import Repository, unwrap


class ReviewError(Exception):
    """Invalid review operation."""


class ReviewService:
    """Apply reviewer decisions to the queue, the profile and the audit log."""

    def __init__(self, repo: Repository) -> None:
        self._repo = repo

    def list_open(self, limit: int = 200) -> list[ReviewItem]:
        """Open items, oldest first."""
        return self._repo.list_review_items(status="open", limit=limit)

    def accept(self, item_id: int, *, by: str) -> ReviewItem:
        """Accept the proposed value."""
        item = self._get_open(item_id)
        return self._settle(item, unwrap(item.proposed), status="accepted", by=by)

    def edit(self, item_id: int, value: str, *, by: str) -> ReviewItem:
        """Set a corrected value typed by the reviewer."""
        item = self._get_open(item_id)
        typed: Any = (
            coerce(item.field_name, value) if item.field_name != "receiving_hours" else value
        )
        if typed is None:
            msg = f"'{value}' is not a valid value for {item.field_name}"
            raise ReviewError(msg)
        return self._settle(item, typed, status="edited", by=by)

    def reject(self, item_id: int, *, by: str) -> ReviewItem:
        """Reject the proposal; the field keeps its previous value and is marked rejected."""
        item = self._get_open(item_id)
        self._repo.decide_review_item(item, status="rejected", value=None, decided_by=by)
        role = Role(item.role)
        fields = self._repo.fields(item.facility_key, role)
        previous = fields.get(item.field_name)
        if previous is not None:
            previous.state = FieldState.REJECTED.value
        self._repo.audit(
            run_id=item.run_id,
            key=item.facility_key,
            role=role,
            field_name=item.field_name,
            action="reject",
            before=unwrap(previous.value) if previous else None,
            after=unwrap(previous.value) if previous else None,
            reason=f"rejected by {by}",
            actor=by,
        )
        return item

    def _get_open(self, item_id: int) -> ReviewItem:
        item = self._repo.get_review_item(item_id)
        if item is None:
            msg = f"review item {item_id} not found"
            raise ReviewError(msg)
        if item.status != "open":
            msg = f"review item {item_id} is already {item.status}"
            raise ReviewError(msg)
        return item

    def _settle(self, item: ReviewItem, value: Any, *, status: str, by: str) -> ReviewItem:
        role = Role(item.role)
        previous = self._repo.fields(item.facility_key, role).get(item.field_name)
        before = unwrap(previous.value) if previous else None
        self._repo.decide_review_item(item, status=status, value=value, decided_by=by)
        self._repo.set_field_human(
            item.facility_key, role, item.field_name, value, state=FieldState.HUMAN_SET
        )
        self._repo.audit(
            run_id=item.run_id,
            key=item.facility_key,
            role=role,
            field_name=item.field_name,
            action="human_set",
            before=before,
            after=value,
            confidence=1.0,
            reason=f"{status} by {by}",
            actor=by,
        )
        return item
