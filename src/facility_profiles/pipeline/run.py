"""Run orchestrator: harvest, then per facility and role collect, extract, score, apply.

Each facility-role pair runs in its own transaction and leaves a checkpoint, so a failed run
can be resumed with the same run ID and skips what already finished (idempotence NFR).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from facility_profiles.config import RunMode, Settings
from facility_profiles.domain.contacts import InternalContacts
from facility_profiles.domain.rules import Thresholds
from facility_profiles.domain.schema import Role
from facility_profiles.domain.scoring import Mention
from facility_profiles.extraction.llm import ExtractionError, Extractor
from facility_profiles.extraction.pricing import Budget, price_for
from facility_profiles.extraction.prompts import render_user_message
from facility_profiles.extraction.validate import validate_and_convert
from facility_profiles.logging import get_logger
from facility_profiles.pipeline.collect import (
    Collector,
    build_bundle,
    existing_values_from_record,
    identity_from_record,
    structured_mentions,
)
from facility_profiles.pipeline.harvest import Harvester, iter_terminal_loads, resolver_from_repo
from facility_profiles.pipeline.profile import assemble_profile
from facility_profiles.pipeline.writer import ApplyStats, FacilityWriteAdapter, ProfileWriter
from facility_profiles.storage.db import session_scope
from facility_profiles.storage.repository import Repository, as_utc
from facility_profiles.tpro.client import TransportProClient

log = get_logger(__name__)

STAGE_APPLIED = "applied"
STAGE_NO_SOURCES = "no_sources"


@dataclass
class RunReport:
    """Summary returned by :meth:`Pipeline.run`."""

    run_id: str
    status: str = "running"
    harvest: dict[str, Any] = field(default_factory=dict)
    profiles: int = 0
    skipped_unchanged: int = 0
    no_sources: int = 0
    extraction_errors: int = 0
    validation_issues: int = 0
    actions: ApplyStats = field(default_factory=ApplyStats)
    llm_input_tokens: int = 0
    llm_output_tokens: int = 0
    llm_cache_read_tokens: int = 0
    llm_cost_usd: float = 0.0
    budget_usd: float | None = None
    budget_stopped: bool = False
    validation_issue_reasons: dict[str, int] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    def as_stats(self) -> dict[str, Any]:
        """Serialisable statistics stored on the run row."""
        return {
            "harvest": self.harvest,
            "profiles": self.profiles,
            "skipped_unchanged": self.skipped_unchanged,
            "no_sources": self.no_sources,
            "extraction_errors": self.extraction_errors,
            "validation_issues": self.validation_issues,
            "actions": dict(self.actions.actions),
            "llm_input_tokens": self.llm_input_tokens,
            "llm_output_tokens": self.llm_output_tokens,
            "llm_cache_read_tokens": self.llm_cache_read_tokens,
            "llm_cost_usd": round(self.llm_cost_usd, 4),
            "budget_usd": self.budget_usd,
            "budget_stopped": self.budget_stopped,
            "validation_issue_reasons": dict(self.validation_issue_reasons),
            "errors": self.errors[:50],
        }


class Pipeline:
    """Wires the stages together for one run."""

    def __init__(
        self,
        settings: Settings,
        sessions: sessionmaker[Session],
        *,
        client: TransportProClient | None,
        extractor: Extractor,
        adapter: FacilityWriteAdapter | None = None,
        now: datetime | None = None,
    ) -> None:
        self._settings = settings
        self._sessions = sessions
        self._client = client
        self._extractor = extractor
        self._adapter = adapter
        self._now = now or datetime.now(tz=UTC)
        self._internal = InternalContacts.build(
            settings.internal_email_domains, settings.internal_phone_numbers
        )
        self._budget: Budget | None = None
        if settings.llm_budget_usd is not None:
            model_name = str(getattr(extractor, "model", settings.llm_model))
            prices = price_for(
                model_name,
                settings.llm_price_input_per_million,
                settings.llm_price_output_per_million,
            )
            self._budget = Budget(settings.llm_budget_usd, prices)

    # ------------------------------------------------------------------ harvest

    def harvest(
        self,
        *,
        terminal_ids: list[int] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, Any]:
        """Pull loads for the terminals and date range and store facilities, links and notes."""
        if self._client is None:
            msg = "harvest needs a Transport Pro client"
            raise RuntimeError(msg)
        end = end or self._now.date()
        start = start or end - timedelta(days=self._settings.lookback_days)
        terminals: list[int | None] = list(terminal_ids or self._settings.pilot_terminal_ids) or [
            None
        ]
        with session_scope(self._sessions) as session:
            repo = Repository(session)
            resolver = resolver_from_repo(
                repo,
                geo_match_meters=self._settings.geo_match_meters,
                name_threshold=self._settings.name_match_threshold,
            )
            harvester = Harvester(repo, resolver)
            loads = iter_terminal_loads(self._client, terminal_ids=terminals, start=start, end=end)
            for index, load in enumerate(loads, start=1):
                harvester.ingest_load(load)
                if index % 200 == 0:
                    session.commit()
                    log.info("harvest.progress", loads=index, stops=harvester.stats.stops)
            stats = harvester.stats.as_dict()
        log.info("harvest.done", **stats)
        return stats

    # ------------------------------------------------------------------ run

    def run(
        self,
        *,
        do_harvest: bool = True,
        refresh_only: bool = False,
        facility_cap: int | None = None,
        terminal_ids: list[int] | None = None,
        run_id: str | None = None,
    ) -> RunReport:
        """Execute a full run; returns the report also stored on the run row."""
        model_version = getattr(self._extractor, "model", None)
        with session_scope(self._sessions) as session:
            repo = Repository(session)
            if run_id and (existing := repo.get_run(run_id)):
                run = existing
                run.status = "running"
            else:
                run = repo.create_run(
                    self._settings.mode.value,
                    terminal_ids or self._settings.pilot_terminal_ids,
                    model_version,
                )
            report = RunReport(run_id=run.id, budget_usd=self._settings.llm_budget_usd)

        try:
            self._learn_internal_phones()
            if do_harvest:
                report.harvest = self.harvest(terminal_ids=terminal_ids)
            self._process_facilities(report, refresh_only=refresh_only, facility_cap=facility_cap)
            report.status = "completed"
        except Exception as exc:
            report.status = "failed"
            report.errors.append(str(exc))
            self._finish(report, error=str(exc))
            raise
        self._finish(report)
        return report

    def _learn_internal_phones(self) -> None:
        """Treat every Circle terminal phone number as internal, when the API is reachable."""
        if self._client is None:
            return
        try:
            terminals = self._client.list_terminals()
        except Exception as exc:
            log.warning("run.terminal_phones_unavailable", error=str(exc))
            return
        phones = [
            str(entry.get("value"))
            for terminal in terminals
            for entry in terminal.phone_numbers
            if isinstance(entry, dict) and entry.get("value")
        ]
        self._internal = self._internal.with_phones(phones)

    def _finish(self, report: RunReport, error: str | None = None) -> None:
        with session_scope(self._sessions) as session:
            Repository(session).finish_run(
                report.run_id, status=report.status, stats=report.as_stats(), error=error
            )

    def _process_facilities(
        self, report: RunReport, *, refresh_only: bool, facility_cap: int | None
    ) -> None:
        cap = facility_cap or self._settings.facility_cap
        with session_scope(self._sessions) as session:
            repo = Repository(session)
            targets = [
                (record.key, role)
                for record in repo.list_facilities(limit=cap)
                for role in repo.roles_for(record.key)
            ]
        log.info("run.targets", count=len(targets), cap=cap)
        for key, role in targets:
            if report.budget_stopped:
                log.warning("run.budget_reached", spent_usd=round(report.llm_cost_usd, 4))
                break
            self._process_one(report, key, role, refresh_only=refresh_only)

    def _process_one(self, report: RunReport, key: str, role: Role, *, refresh_only: bool) -> None:
        with session_scope(self._sessions) as session:
            repo = Repository(session)
            record = repo.get_facility(key)
            if record is None or repo.reached(report.run_id, key, role, STAGE_APPLIED):
                return
            profile_row = repo.profile(key, role)
            if (
                refresh_only
                and profile_row is not None
                and not repo.has_new_sources_since(key, role, as_utc(profile_row.updated_at))
            ):
                report.skipped_unchanged += 1
                return

            collector = Collector(self._client, repo, deep_loads_per_facility=15)
            collect_stats = collector.collect(record, role)
            report.errors.extend(collect_stats.errors)
            session.commit()

            record = repo.get_facility(key)
            assert record is not None
            bundle = build_bundle(
                repo,
                record,
                role,
                max_sources=120,
                max_loads=self._settings.max_loads_per_facility,
            )
            derived = structured_mentions(
                repo,
                record,
                role,
                max_loads=self._settings.max_loads_per_facility,
                internal=self._internal,
                facility_names=[record.company_name, record.city, *bundle.identity.aliases],
            )
            if not bundle.sources and not derived:
                repo.checkpoint(report.run_id, key, role, STAGE_NO_SOURCES)
                report.no_sources += 1
                return

            mentions: list[Mention] = list(derived)
            summary: str | None = None
            model_version: str | None = getattr(self._extractor, "model", None)
            if bundle.sources:
                if self._budget is not None and self._budget.would_exceed(
                    render_user_message(bundle)
                ):
                    report.budget_stopped = True
                    return
                try:
                    output = self._extractor.extract(bundle)
                except ExtractionError as exc:
                    report.extraction_errors += 1
                    report.errors.append(f"{key}/{role.value}: {exc}")
                    repo.audit(
                        run_id=report.run_id,
                        key=key,
                        role=role,
                        field_name=None,
                        action="extraction_error",
                        reason=str(exc),
                        model_version=model_version,
                    )
                    log.warning(
                        "run.extraction_failed", facility=key, role=role.value, error=str(exc)
                    )
                    return
                llm_mentions, issues = validate_and_convert(
                    output.result, bundle, internal=self._internal
                )
                report.validation_issues += len(issues)
                for issue in issues:
                    report.validation_issue_reasons[issue.reason] = (
                        report.validation_issue_reasons.get(issue.reason, 0) + 1
                    )
                repo.save_extraction(
                    run_id=report.run_id,
                    key=key,
                    role=role,
                    model=output.model,
                    prompt_version=output.prompt_version,
                    request_id=output.request_id,
                    input_tokens=output.usage.input_tokens,
                    output_tokens=output.usage.output_tokens,
                    cache_read_tokens=output.usage.cache_read_tokens,
                    source_count=len(bundle.sources),
                    raw=output.result.model_dump(mode="json"),
                    issues=[issue.__dict__ for issue in issues],
                )
                for issue in issues:
                    log.debug("run.validation_issue", facility=key, **issue.__dict__)
                for field_mentions in llm_mentions.values():
                    mentions.extend(field_mentions)
                summary = output.result.scheduling_summary
                model_version = output.model_version
                report.llm_input_tokens += output.usage.input_tokens
                report.llm_output_tokens += output.usage.output_tokens
                report.llm_cache_read_tokens += output.usage.cache_read_tokens
                if self._budget is not None:
                    report.llm_cost_usd = self._budget.add(output.usage)
                    if self._budget.exhausted:
                        report.budget_stopped = True

            identity = identity_from_record(record, bundle.identity.aliases)
            profile = assemble_profile(
                identity,
                role,
                mentions,
                summary=summary,
                run_id=report.run_id,
                model_version=model_version,
                source_load_ids=bundle.load_ids,
                now=self._now,
                half_life_days=float(self._settings.lookback_days),
                conflict_support=self._settings.conflict_support,
            )
            writer = ProfileWriter(
                repo,
                mode=RunMode(self._settings.mode),
                thresholds=Thresholds(
                    write=self._settings.write_threshold, queue=self._settings.queue_threshold
                ),
                stale_after_days=self._settings.stale_after_days,
                run_id=report.run_id,
                model_version=model_version,
                now=self._now,
                adapter=self._adapter,
            )
            stats = writer.apply(profile, existing_values_from_record(record))
            report.actions.merge(stats)
            report.profiles += 1
            repo.checkpoint(report.run_id, key, role, STAGE_APPLIED)
            log.info(
                "run.profile_applied",
                facility=key,
                role=role.value,
                sources=len(bundle.sources),
                actions=stats.actions,
            )
