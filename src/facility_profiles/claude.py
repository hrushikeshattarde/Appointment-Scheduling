"""Claude, called directly (``FP_LLM_PROVIDER=anthropic``) or on Amazon Bedrock (``bedrock``).

One client and one call shape for the reply reader, the reply writer and the profile extractor.
On Bedrock the SDK's Bedrock client signs each request with this machine's AWS login: the server's
IAM role in production, ``AWS_PROFILE`` on a laptop. The current Claude models run on Bedrock
only through a cross-region inference profile, so ``FP_LLM_MODEL=claude-sonnet-5-5`` is sent as
``us.anthropic.claude-sonnet-5-5`` (``FP_BEDROCK_ROUTING``: ``us``, or ``global`` where the
account allows it). Checked on 10/08/2026 in account 988836287275: the ``us`` profile answers,
``global`` is refused by the account's permissions, and Bedrock's newer Mantle endpoint does not
know these models, so the runtime API is used.

Every call asks for JSON that fits a schema (structured outputs), so a reply is valid by
construction; the caller still validates it with pydantic. A model that declines, a response cut
off at ``max_tokens`` or an API failure raises :class:`ExtractionError`, which the callers already
handle: the email is tried again on the next pass, then kept for a person.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Literal

import anthropic
from anthropic.types import OutputConfigParam

from facility_profiles.config import Settings
from facility_profiles.extraction.llm import ExtractionError, ExtractionRefusedError, LLMUsage

# The output a structured call may use, thinking included; only what is used is billed.
MAX_TOKENS = 16_000

Client = anthropic.Anthropic | anthropic.AnthropicBedrock
Effort = Literal["low", "medium", "high", "xhigh", "max"]


def claude_client(settings: Settings) -> Client:
    """The client for ``FP_LLM_PROVIDER``: Bedrock in FP_BEDROCK_REGION, else Anthropic's API."""
    if settings.llm_provider == "bedrock":
        return anthropic.AnthropicBedrock(aws_region=settings.bedrock_region, max_retries=3)
    return anthropic.Anthropic(max_retries=3)


def claude_model(settings: Settings) -> str:
    """``FP_LLM_MODEL`` as the provider names it: ``us.anthropic.claude-sonnet-5-5`` on Bedrock.

    A version written with a dot (``claude-sonnet-4.6``, as OpenRouter spells it) is written with
    a dash, and an ID that already names its provider (``global.anthropic.claude-…``) is kept.
    """
    model = re.sub(r"(\d)\.(\d)", r"\1-\2", settings.llm_model.strip())
    if settings.llm_provider == "bedrock" and "anthropic." not in model:
        return f"{settings.bedrock_routing}.anthropic.{model}"
    return model


@dataclass(frozen=True)
class StructuredReply:
    """The JSON the model returned, and what the call cost."""

    data: dict[str, Any]
    model: str
    usage: LLMUsage
    request_id: str | None = None


def structured_call(
    client: Client,
    *,
    model: str,
    system: str,
    user: str,
    schema: dict[str, Any],
    effort: Effort | None = None,
    max_tokens: int = MAX_TOKENS,
) -> StructuredReply:
    """One call whose answer must fit ``schema``; the parsed JSON object comes back.

    ``effort`` (low to max) is sent when set; without it the model's own default holds.
    """
    output_config: OutputConfigParam = {"format": {"type": "json_schema", "schema": schema}}
    if effort:
        output_config["effort"] = effort
    try:
        response = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config=output_config,
        )
    except anthropic.BadRequestError as exc:
        raise ExtractionError(f"bad request: {exc.message}") from exc
    except anthropic.AuthenticationError as exc:
        raise ExtractionError(f"Claude authentication failed: {exc.message}") from exc
    except anthropic.PermissionDeniedError as exc:
        raise ExtractionError(f"Claude access denied: {exc.message}") from exc
    except anthropic.NotFoundError as exc:
        raise ExtractionError(f"model {model} not found: {exc.message}") from exc
    except anthropic.RateLimitError as exc:
        raise ExtractionError("rate limited by Claude", retryable=True) from exc
    except anthropic.APIStatusError as exc:
        retryable = exc.status_code >= 500
        raise ExtractionError(f"Claude API error {exc.status_code}", retryable=retryable) from exc
    except anthropic.APIConnectionError as exc:
        raise ExtractionError("could not reach Claude", retryable=True) from exc

    if response.stop_reason == "refusal":
        details = getattr(response, "stop_details", None)
        category = getattr(details, "category", None) if details else None
        raise ExtractionRefusedError(f"model refused (category={category})")
    if response.stop_reason == "max_tokens":
        raise ExtractionError("response cut off at max_tokens")
    text = next((b.text for b in response.content if b.type == "text"), "")
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"the model's answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ExtractionError("the model's answer is not a JSON object")
    usage = response.usage
    return StructuredReply(
        data=data,
        model=response.model or model,
        usage=LLMUsage(
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_input_tokens or 0,
            cache_creation_tokens=usage.cache_creation_input_tokens or 0,
        ),
        request_id=getattr(response, "_request_id", None),
    )
