import pytest

from facility_profiles.extraction.llm import LLMUsage
from facility_profiles.extraction.pricing import Budget, Prices, cost_usd, price_for
from facility_profiles.pipeline.run import Pipeline
from tests.conftest import NOW, FakeTPro, sample_facility, sample_loads
from tests.test_pipeline import scripted_extractor


def test_price_lookup_prefers_longest_match_and_overrides():
    assert price_for("anthropic/claude-sonnet-4.6") == Prices(3.0, 15.0)
    assert price_for("anthropic/claude-opus-5.5") == Prices(4.0, 20.0)
    assert price_for("claude-opus-5") == Prices(5.0, 25.0)
    assert price_for("claude-haiku-4.5") == Prices(1.0, 5.0)
    assert price_for("mystery-model") == Prices(5.0, 25.0)  # conservative fallback
    assert price_for("claude-sonnet-5", input_override=1.0) == Prices(1.0, 10.0)


def test_cost_discounts_cache_reads():
    prices = Prices(3.0, 15.0)
    plain = cost_usd(LLMUsage(input_tokens=1_000_000, output_tokens=100_000), prices)
    assert plain == pytest.approx(3.0 + 1.5)
    cached = cost_usd(
        LLMUsage(input_tokens=1_000_000, output_tokens=0, cache_read_tokens=1_000_000), prices
    )
    assert cached == pytest.approx(0.3)


def test_budget_tracks_spend_and_predicts_overrun():
    budget = Budget(1.0, Prices(3.0, 15.0))
    assert not budget.would_exceed("x" * 4_000)  # ~2.5k tokens in, 2k out: about 4 cents
    budget.add(LLMUsage(input_tokens=200_000, output_tokens=20_000))  # 0.60 + 0.30
    assert budget.spent_usd == pytest.approx(0.9)
    assert not budget.exhausted
    assert budget.would_exceed("x" * 400_000)  # ~100k tokens in: 0.30 + 0.03 > remaining 0.10
    budget.add(LLMUsage(input_tokens=100_000, output_tokens=0))
    assert budget.exhausted
    with pytest.raises(ValueError, match="positive"):
        Budget(0, Prices(1.0, 1.0))


def test_pipeline_stops_when_budget_is_exhausted(settings, sessions):
    settings = settings.model_copy(update={"llm_budget_usd": 3.0, "llm_model": "claude-opus-5"})
    client = FakeTPro(sample_loads(), {900001: sample_facility()})
    expensive = scripted_extractor()
    expensive._usage = LLMUsage(input_tokens=1_000_000, output_tokens=0)  # $5 per call at Opus
    pipeline = Pipeline(settings, sessions, client=client, extractor=expensive, now=NOW)  # type: ignore[arg-type]
    report = pipeline.run()
    assert report.status == "completed"
    assert report.profiles == 1  # the first call already passed the cap
    assert report.budget_stopped is True
    assert report.budget_usd == 3.0
    assert report.llm_cost_usd == pytest.approx(5.0)
    assert report.as_stats()["budget_stopped"] is True
