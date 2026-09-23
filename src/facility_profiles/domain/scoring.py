"""Confidence scoring and conflict detection across mentions (FR-7, FR-8).

A *mention* is one observation of a value for one field: a quote in a note on a load, a
structured signal such as a confirmed appointment window, or a value already on the facility
record. Mentions are grouped by normalised value, weighted by recency and source confidence,
and the winner's confidence combines its share of the weight (support) with how many distinct
loads back it (breadth).

    confidence = support * breadth * best_source_confidence

with ``breadth = min(1, 0.5 + 0.25 * (distinct_loads - 1))`` so a single load can reach at most
0.5 (queue), two agreeing loads 0.75 (queue), and three or more agreeing loads 1.0 (write).
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from facility_profiles.domain.schema import (
    CandidateValue,
    Evidence,
    ProfileField,
    SourceType,
)

DEFAULT_HALF_LIFE_DAYS = 90.0
FACILITY_SOURCE_LOAD_ID = -1  # sentinel load ID for facility-record mentions


@dataclass
class Mention:
    """One observation of a candidate value for a field."""

    field_name: str
    value: Any
    load_id: int | None
    source_type: SourceType
    quote: str
    observed_at: datetime | None = None
    source_confidence: float = 1.0
    normalized: Any = field(default=None)

    def key(self) -> Any:
        """Grouping key: the normalised value when set, else the value itself."""
        return self.normalized if self.normalized is not None else self.value


def recency_weight(observed_at: datetime | None, now: datetime, half_life_days: float) -> float:
    """Exponential decay: 1.0 today, 0.5 after ``half_life_days``. Undated mentions count 0.7."""
    if observed_at is None:
        return 0.7
    age_days = max((now - observed_at).total_seconds() / 86_400, 0.0)
    return math.pow(0.5, age_days / half_life_days)


def breadth(distinct_loads: int) -> float:
    """Corroboration factor from the number of distinct loads backing a value."""
    if distinct_loads <= 0:
        return 0.0
    return min(1.0, 0.5 + 0.25 * (distinct_loads - 1))


def score_field(
    field_name: str,
    mentions: list[Mention],
    *,
    now: datetime | None = None,
    half_life_days: float = DEFAULT_HALF_LIFE_DAYS,
    conflict_support: float = 0.3,
) -> ProfileField:
    """Score a field from its mentions. Returns an empty field when there are none."""
    current = now or datetime.now(tz=UTC)
    if not mentions:
        return ProfileField(name=field_name)

    weight_by_value: dict[Any, float] = defaultdict(float)
    loads_by_value: dict[Any, set[int]] = defaultdict(set)
    conf_by_value: dict[Any, list[float]] = defaultdict(list)
    evidence_by_value: dict[Any, list[Evidence]] = defaultdict(list)
    canonical: dict[Any, Any] = {}
    latest_by_value: dict[Any, datetime | None] = {}

    for mention in mentions:
        key = mention.key()
        weight = recency_weight(mention.observed_at, current, half_life_days) * max(
            min(mention.source_confidence, 1.0), 0.0
        )
        weight_by_value[key] += weight
        if mention.load_id is not None:
            loads_by_value[key].add(mention.load_id)
        conf_by_value[key].append(mention.source_confidence)
        canonical.setdefault(key, mention.value)
        evidence_by_value[key].append(
            Evidence(
                load_id=None if mention.load_id == FACILITY_SOURCE_LOAD_ID else mention.load_id,
                source_type=mention.source_type,
                quote=mention.quote,
                observed_at=mention.observed_at,
            )
        )
        prior = latest_by_value.get(key)
        if mention.observed_at and (prior is None or mention.observed_at > prior):
            latest_by_value[key] = mention.observed_at
        latest_by_value.setdefault(key, None)

    total_weight = sum(weight_by_value.values()) or 1.0
    ranked = sorted(weight_by_value.items(), key=lambda kv: kv[1], reverse=True)
    top_key, top_weight = ranked[0]

    support = top_weight / total_weight
    distinct = len(loads_by_value[top_key])
    best_conf = max(conf_by_value[top_key])
    confidence = round(support * breadth(distinct) * best_conf, 4)

    candidates = [
        CandidateValue(
            value=canonical[key],
            support=round(weight / total_weight, 4),
            distinct_loads=len(loads_by_value[key]),
            evidence=evidence_by_value[key],
        )
        for key, weight in ranked
    ]
    conflict = any(c.support >= conflict_support for c in candidates[1:])

    return ProfileField(
        name=field_name,
        value=canonical[top_key],
        confidence=confidence,
        support_count=len(evidence_by_value[top_key]),
        mention_count=len(mentions),
        distinct_loads=distinct,
        conflict=conflict,
        evidence=evidence_by_value[top_key],
        candidates=candidates,
        last_seen=latest_by_value.get(top_key),
    )
