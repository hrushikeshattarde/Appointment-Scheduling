"""Token pricing and a spend guard for a run (NFR: cost tracked per run).

Prices are USD per million tokens as published by OpenRouter for Anthropic models on
28 September 2026, matched on the model name; the direct Anthropic API charges the same.
Unknown models fall back to the Opus rate so the guard errs on the safe side. Cache reads are
billed at 10% of the input price.
"""

from __future__ import annotations

from dataclasses import dataclass

from facility_profiles.extraction.llm import LLMUsage

KNOWN_PRICES_PER_MILLION: tuple[tuple[str, float, float], ...] = (
    ("claude-opus-5.5", 4.0, 20.0),
    ("claude-opus-5", 5.0, 25.0),
    ("claude-opus-4", 5.0, 25.0),
    ("claude-sonnet-5", 2.0, 10.0),
    ("claude-sonnet-4", 3.0, 15.0),
    ("claude-haiku-4", 1.0, 5.0),
)
FALLBACK_PRICES = (5.0, 25.0)
CACHE_READ_DISCOUNT = 0.1
CHARS_PER_TOKEN = 4
SYSTEM_PROMPT_TOKENS = 1_500
OUTPUT_TOKENS_ESTIMATE = 2_000


@dataclass(frozen=True)
class Prices:
    """USD per million input and output tokens."""

    input_per_million: float
    output_per_million: float


def price_for(
    model: str,
    input_override: float | None = None,
    output_override: float | None = None,
) -> Prices:
    """Look up prices by model name; explicit overrides win."""
    low = model.lower()
    matched = next(
        ((i, o) for key, i, o in KNOWN_PRICES_PER_MILLION if key in low), FALLBACK_PRICES
    )
    return Prices(
        input_per_million=input_override if input_override is not None else matched[0],
        output_per_million=output_override if output_override is not None else matched[1],
    )


def cost_usd(usage: LLMUsage, prices: Prices) -> float:
    """Cost of one call, with cache reads discounted."""
    uncached = max(usage.input_tokens - usage.cache_read_tokens, 0)
    cached = usage.cache_read_tokens
    return (
        uncached * prices.input_per_million
        + cached * prices.input_per_million * CACHE_READ_DISCOUNT
        + usage.output_tokens * prices.output_per_million
    ) / 1_000_000


def estimate_input_tokens(user_message: str) -> int:
    """Rough token count for a request: message characters over four, plus the system prompt."""
    return len(user_message) // CHARS_PER_TOKEN + SYSTEM_PROMPT_TOKENS


class Budget:
    """Cumulative spend against a USD limit."""

    def __init__(self, limit_usd: float, prices: Prices) -> None:
        if limit_usd <= 0:
            msg = "budget must be positive"
            raise ValueError(msg)
        self.limit_usd = limit_usd
        self.prices = prices
        self.spent_usd = 0.0
        self.calls = 0

    def add(self, usage: LLMUsage) -> float:
        """Record a call's usage; returns the running total."""
        self.spent_usd += cost_usd(usage, self.prices)
        self.calls += 1
        return self.spent_usd

    def would_exceed(self, user_message: str) -> bool:
        """True when the next call, at the estimated size, would pass the limit."""
        estimate = cost_usd(
            LLMUsage(
                input_tokens=estimate_input_tokens(user_message),
                output_tokens=OUTPUT_TOKENS_ESTIMATE,
            ),
            self.prices,
        )
        return self.spent_usd + estimate > self.limit_usd

    @property
    def exhausted(self) -> bool:
        """True once the recorded spend has reached the limit."""
        return self.spent_usd >= self.limit_usd
