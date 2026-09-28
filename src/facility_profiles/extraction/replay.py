"""Replay extractor: reuse the stored raw model output instead of calling the model.

Lets scoring and write-policy changes be re-applied to the last extraction for free. The
replayed output is marked with ``request_id="replay"`` and zero usage so cost is not counted
twice.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.domain.schema import ExtractionResult
from facility_profiles.extraction.bundle import SourceBundle
from facility_profiles.extraction.llm import ExtractionError, ExtractionOutput, LLMUsage
from facility_profiles.extraction.prompts import PROMPT_VERSION
from facility_profiles.storage.models import ExtractionRecord


class ReplayExtractor:
    """Return the most recent stored extraction for the bundle's facility and role."""

    model = "replay"

    def __init__(self, sessions: sessionmaker[Session]) -> None:
        self._sessions = sessions

    def extract(self, bundle: SourceBundle) -> ExtractionOutput:
        """Load the last stored result; raise when the facility was never extracted."""
        with self._sessions() as session:
            row = session.scalars(
                select(ExtractionRecord)
                .where(
                    ExtractionRecord.facility_key == bundle.identity.key,
                    ExtractionRecord.role == bundle.role.value,
                    ExtractionRecord.request_id != "replay",
                )
                .order_by(ExtractionRecord.created_at.desc(), ExtractionRecord.id.desc())
                .limit(1)
            ).first()
            if row is None:
                msg = f"no stored extraction for {bundle.identity.key}/{bundle.role.value}"
                raise ExtractionError(msg)
            result = ExtractionResult.model_validate(row.raw)
            return ExtractionOutput(
                result=result,
                model=row.model or "unknown",
                prompt_version=row.prompt_version or PROMPT_VERSION,
                usage=LLMUsage(),
                request_id="replay",
            )
