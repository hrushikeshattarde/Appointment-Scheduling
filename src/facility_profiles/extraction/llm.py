"""LLM extractors: the Anthropic implementation and a fake for tests.

The Anthropic extractor uses structured outputs (``client.messages.parse`` with a Pydantic
``output_format``) so the response is schema-valid by construction (FR-4). The frozen system
prompt carries a cache breakpoint so repeated calls pay for it once.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import anthropic

from facility_profiles.domain.schema import ExtractionResult, FieldCandidates
from facility_profiles.extraction.bundle import SourceBundle
from facility_profiles.extraction.prompts import PROMPT_VERSION, SYSTEM_PROMPT, render_user_message
from facility_profiles.logging import get_logger

log = get_logger(__name__)


class ExtractionError(Exception):
    """The extractor could not produce a result."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class ExtractionRefusedError(ExtractionError):
    """The model declined the request."""


@dataclass(frozen=True)
class LLMUsage:
    """Token accounting for one call."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0


@dataclass(frozen=True)
class ExtractionOutput:
    """A validated result with its provenance."""

    result: ExtractionResult
    model: str
    prompt_version: str = PROMPT_VERSION
    usage: LLMUsage = LLMUsage()
    request_id: str | None = None

    @property
    def model_version(self) -> str:
        """``model@prompt-version`` for the audit log."""
        return f"{self.model}@{self.prompt_version}"


class Extractor(Protocol):
    """Anything that turns a bundle into an :class:`ExtractionResult`."""

    def extract(self, bundle: SourceBundle) -> ExtractionOutput:
        """Extract scheduling fields for one facility and role."""
        ...


class AnthropicExtractor:
    """Structured-output extraction with the Anthropic SDK."""

    def __init__(
        self,
        model: str = "claude-opus-5",
        *,
        max_tokens: int = 16_000,
        client: anthropic.Anthropic | None = None,
    ) -> None:
        self.model = model
        self.max_tokens = max_tokens
        self._client = client or anthropic.Anthropic(max_retries=3)

    def extract(self, bundle: SourceBundle) -> ExtractionOutput:
        """Call the model once and return the parsed, schema-valid result."""
        user_message = render_user_message(bundle)
        try:
            response = self._client.messages.parse(
                model=self.model,
                max_tokens=self.max_tokens,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_message}],
                output_format=ExtractionResult,
            )
        except anthropic.BadRequestError as exc:
            raise ExtractionError(f"bad request: {exc.message}") from exc
        except anthropic.AuthenticationError as exc:
            raise ExtractionError("Anthropic authentication failed") from exc
        except anthropic.RateLimitError as exc:
            raise ExtractionError("rate limited by Anthropic", retryable=True) from exc
        except anthropic.APIStatusError as exc:
            retryable = exc.status_code >= 500
            raise ExtractionError(
                f"Anthropic API error {exc.status_code}", retryable=retryable
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise ExtractionError("could not reach Anthropic", retryable=True) from exc

        if response.stop_reason == "refusal":
            details = getattr(response, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise ExtractionRefusedError(f"model refused (category={category})")
        if response.stop_reason == "max_tokens":
            raise ExtractionError("response truncated at max_tokens", retryable=False)
        parsed = response.parsed_output
        if parsed is None:
            raise ExtractionError("no structured output in response")

        usage = response.usage
        llm_usage = LLMUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_tokens=usage.cache_creation_input_tokens or 0,
        )
        log.debug(
            "llm.extracted",
            facility=bundle.identity.key,
            role=bundle.role.value,
            input_tokens=llm_usage.input_tokens,
            output_tokens=llm_usage.output_tokens,
            cache_read=llm_usage.cache_read_tokens,
        )
        return ExtractionOutput(
            result=parsed,
            model=self.model,
            usage=llm_usage,
            request_id=response._request_id,  # documented public property despite the underscore
        )


class FakeExtractor:
    """Deterministic extractor for tests and dry runs."""

    def __init__(
        self,
        result: ExtractionResult | Callable[[SourceBundle], ExtractionResult],
        *,
        model: str = "fake-model",
    ) -> None:
        self._result = result
        self.model = model
        self.calls: list[SourceBundle] = []

    def extract(self, bundle: SourceBundle) -> ExtractionOutput:
        """Return the configured result."""
        self.calls.append(bundle)
        result = self._result(bundle) if callable(self._result) else self._result
        return ExtractionOutput(result=result, model=self.model)


def empty_result() -> ExtractionResult:
    """An extraction result with no candidates, useful as a fake default."""
    return ExtractionResult(
        appointment_required=FieldCandidates(candidates=[]),
        booking_method=FieldCandidates(candidates=[]),
        contact_name=FieldCandidates(candidates=[]),
        contact_phone=FieldCandidates(candidates=[]),
        contact_email=FieldCandidates(candidates=[]),
        portal_url=FieldCandidates(candidates=[]),
        portal_vendor=FieldCandidates(candidates=[]),
        notice_period_hours=FieldCandidates(candidates=[]),
        time_granularity=FieldCandidates(candidates=[]),
        receiving_hours=[],
        scheduling_summary=None,
    )
