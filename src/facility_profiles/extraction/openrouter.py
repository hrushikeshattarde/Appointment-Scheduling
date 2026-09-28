"""OpenRouter extractor: the same structured extraction through OpenRouter's chat API.

OpenRouter exposes Anthropic models behind an OpenAI-compatible endpoint. The request asks for a
strict JSON-schema response so the output is schema-valid by construction, mirroring the
Anthropic extractor; the same quote validation runs afterwards. Anthropic prompt caching is
requested through ``cache_control`` on the system message, which OpenRouter passes through.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import httpx
from pydantic import ValidationError
from tenacity import (
    RetryCallState,
    Retrying,
    retry_if_exception,
    stop_after_attempt,
    wait_exponential_jitter,
)

from facility_profiles import __version__
from facility_profiles.domain.schema import ExtractionResult
from facility_profiles.extraction.bundle import SourceBundle
from facility_profiles.extraction.llm import (
    ExtractionError,
    ExtractionOutput,
    ExtractionRefusedError,
    LLMUsage,
)
from facility_profiles.extraction.prompts import SYSTEM_PROMPT, render_user_message
from facility_profiles.logging import get_logger

log = get_logger(__name__)

DEFAULT_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "anthropic/claude-opus-5"
SCHEMA_NAME = "facility_scheduling_extraction"
# JSON Schema keywords strict structured-output mode rejects; pydantic re-validates the parsed
# result anyway, so dropping them loses nothing.
_UNSUPPORTED_KEYWORDS = (
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "minItems",
    "maxItems",
    "pattern",
    "format",
    "default",
)


def strict_json_schema(model: type[ExtractionResult]) -> dict[str, Any]:
    """Pydantic schema tightened for strict mode: every object closed, every property required."""
    schema = copy.deepcopy(model.model_json_schema())

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            for keyword in _UNSUPPORTED_KEYWORDS:
                node.pop(keyword, None)
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return schema


def qualify_model(model: str) -> str:
    """OpenRouter IDs are ``vendor/name``; a bare Anthropic name gets the ``anthropic/`` prefix."""
    return model if "/" in model else f"anthropic/{model}"


def _retryable(exc: BaseException) -> bool:
    if isinstance(exc, ExtractionError):
        return exc.retryable
    return isinstance(exc, httpx.TransportError)


def _wait(retry_state: RetryCallState) -> float:
    return float(wait_exponential_jitter(initial=1.0, max=30.0)(retry_state))


class OpenRouterExtractor:
    """Structured extraction through OpenRouter's ``/chat/completions`` endpoint."""

    def __init__(
        self,
        api_key: str,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = 16_000,
        base_url: str = DEFAULT_BASE_URL,
        timeout_seconds: float = 300.0,
        attempts: int = 4,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not api_key:
            msg = "OpenRouter API key is required (OPENROUTER_API_KEY)"
            raise ValueError(msg)
        self.model = qualify_model(model)
        self.max_tokens = max_tokens
        self._url = f"{base_url.rstrip('/')}/chat/completions"
        self._attempts = attempts
        self._schema = strict_json_schema(ExtractionResult)
        self._http = httpx.Client(
            timeout=timeout_seconds,
            transport=transport,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://github.com/circle-logistics/facility-profiles",
                "X-Title": f"facility-profiles/{__version__}",
            },
        )

    def close(self) -> None:
        """Close the HTTP connection pool."""
        self._http.close()

    def _body(self, bundle: SourceBundle) -> dict[str, Any]:
        return {
            "model": self.model,
            "max_tokens": self.max_tokens,
            "temperature": 0,
            "messages": [
                {
                    "role": "system",
                    "content": [
                        {
                            "type": "text",
                            "text": SYSTEM_PROMPT,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ],
                },
                {"role": "user", "content": render_user_message(bundle)},
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": self._schema},
            },
            "provider": {"require_parameters": True},
        }

    def extract(self, bundle: SourceBundle) -> ExtractionOutput:
        """Call OpenRouter once (with retries on transient failures) and validate the result."""
        retrying = Retrying(
            retry=retry_if_exception(_retryable),
            wait=_wait,
            stop=stop_after_attempt(self._attempts),
            reraise=True,
        )
        for attempt in retrying:
            with attempt:
                return self._call(bundle)
        raise ExtractionError("unreachable")  # pragma: no cover

    @staticmethod
    def _parse_envelope(response: httpx.Response) -> dict[str, Any]:
        """Turn HTTP status and OpenRouter error envelopes into typed errors."""
        status = response.status_code
        if status in {401, 403}:
            raise ExtractionError(f"OpenRouter rejected the API key (HTTP {status})")
        if status == 429 or status >= 500:
            raise ExtractionError(
                f"OpenRouter HTTP {status}: {response.text[:200]}", retryable=True
            )
        if status >= 400:
            raise ExtractionError(f"OpenRouter HTTP {status}: {response.text[:300]}")
        try:
            data = response.json()
        except ValueError as exc:
            raise ExtractionError("OpenRouter returned a non-JSON body") from exc
        if not isinstance(data, dict):
            raise ExtractionError("OpenRouter returned an unexpected body")
        if error := data.get("error"):
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise ExtractionError(
                f"OpenRouter error {code}: {message}", retryable=code in (429, 502, 503)
            )
        return data

    def _call(self, bundle: SourceBundle) -> ExtractionOutput:
        try:
            response = self._http.post(self._url, json=self._body(bundle))
        except httpx.TransportError as exc:
            raise ExtractionError(f"could not reach OpenRouter: {exc}", retryable=True) from exc

        request_id = response.headers.get("x-request-id")
        data = self._parse_envelope(response)
        choices = data.get("choices") or []
        if not choices:
            raise ExtractionError("OpenRouter returned no choices")
        choice = choices[0]
        finish = choice.get("finish_reason") or choice.get("native_finish_reason")
        if finish in ("length", "max_tokens"):
            raise ExtractionError("response truncated at max_tokens")
        if finish in ("content_filter", "refusal"):
            raise ExtractionRefusedError(f"model refused (finish_reason={finish})")

        content = (choice.get("message") or {}).get("content")
        if isinstance(content, list):  # some providers return content parts
            content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
        if not isinstance(content, str) or not content.strip():
            raise ExtractionError("OpenRouter returned empty content")

        try:
            result = parse_content(content)
        except json.JSONDecodeError as exc:
            raise ExtractionError("structured output was not valid JSON") from exc
        except ValidationError as exc:
            raise ExtractionError(
                f"structured output failed validation: {exc.error_count()} error(s)"
            ) from exc

        usage = data.get("usage") or {}
        details = usage.get("prompt_tokens_details") or {}
        llm_usage = LLMUsage(
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cache_read_tokens=int(details.get("cached_tokens") or 0),
            cache_creation_tokens=int(details.get("cache_write_tokens") or 0),
        )
        log.debug(
            "openrouter.extracted",
            facility=bundle.identity.key,
            role=bundle.role.value,
            model=data.get("model", self.model),
            input_tokens=llm_usage.input_tokens,
            output_tokens=llm_usage.output_tokens,
            cache_read=llm_usage.cache_read_tokens,
        )
        return ExtractionOutput(
            result=result,
            model=str(data.get("model") or self.model),
            usage=llm_usage,
            request_id=request_id or data.get("id"),
        )


def parse_content(content: str) -> ExtractionResult:
    """Validate a raw JSON string against the extraction schema (exposed for tests)."""
    return ExtractionResult.model_validate(json.loads(content))
