import json

import httpx
import pytest
import respx

from facility_profiles.domain.schema import ExtractionResult, FacilityIdentity, Role, SourceType
from facility_profiles.extraction.bundle import BundleBuilder
from facility_profiles.extraction.llm import ExtractionError, ExtractionRefusedError, empty_result
from facility_profiles.extraction.openrouter import (
    OpenRouterExtractor,
    qualify_model,
    strict_json_schema,
)

URL = "https://openrouter.ai/api/v1/chat/completions"


def bundle():
    builder = BundleBuilder(FacilityIdentity(facility_id=1, company_name="X"), Role.SHIPPER)
    builder.add(SourceType.STOP_NOTE, "FCFS 0700-1430 MON-FRI", load_id=10)
    return builder.build()


def ok_response(content: str, **extra):
    body = {
        "id": "gen-123",
        "model": "anthropic/claude-opus-5",
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        "usage": {
            "prompt_tokens": 1200,
            "completion_tokens": 300,
            "prompt_tokens_details": {"cached_tokens": 900},
        },
    }
    body.update(extra)
    return httpx.Response(200, json=body)


def make(**kwargs) -> OpenRouterExtractor:
    return OpenRouterExtractor("sk-or-test", attempts=3, **kwargs)


def test_strict_schema_closes_every_object_and_drops_unsupported_keywords():
    schema = strict_json_schema(ExtractionResult)
    text = json.dumps(schema)
    assert '"minimum"' not in text and '"maximum"' not in text
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == set(schema["properties"].keys())
    candidate = schema["$defs"]["Candidate"]
    assert candidate["additionalProperties"] is False
    assert candidate["required"] == ["value", "quotes", "confidence"]
    assert qualify_model("claude-opus-5") == "anthropic/claude-opus-5"
    assert qualify_model("openai/gpt-5") == "openai/gpt-5"


@respx.mock
def test_successful_call_sends_strict_schema_and_parses_usage():
    route = respx.post(URL).mock(return_value=ok_response(empty_result().model_dump_json()))
    extractor = make(model="claude-sonnet-5")
    out = extractor.extract(bundle())
    assert isinstance(out.result, ExtractionResult)
    assert out.model == "anthropic/claude-opus-5"  # as reported by the response
    assert out.usage.input_tokens == 1200 and out.usage.cache_read_tokens == 900
    assert out.request_id == "gen-123"
    sent = json.loads(route.calls[0].request.content)
    assert sent["model"] == "anthropic/claude-sonnet-5"
    assert sent["response_format"]["json_schema"]["strict"] is True
    assert sent["messages"][0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    assert "Sources (1)" in sent["messages"][1]["content"]
    assert route.calls[0].request.headers["authorization"] == "Bearer sk-or-test"
    extractor.close()


@respx.mock
def test_retries_rate_limits_then_succeeds():
    route = respx.post(URL).mock(
        side_effect=[
            httpx.Response(429, text="slow down"),
            ok_response(empty_result().model_dump_json()),
        ]
    )
    out = make().extract(bundle())
    assert out.usage.output_tokens == 300
    assert route.call_count == 2


@respx.mock
def test_bad_key_is_not_retried():
    route = respx.post(URL).mock(
        return_value=httpx.Response(401, json={"error": {"message": "bad key"}})
    )
    with pytest.raises(ExtractionError, match="rejected the API key"):
        make().extract(bundle())
    assert route.call_count == 1


@respx.mock
def test_truncation_refusal_and_invalid_json_are_reported():
    respx.post(URL).mock(
        side_effect=[
            ok_response("{}", choices=[{"finish_reason": "length", "message": {"content": "{"}}]),
            ok_response("{}", choices=[{"finish_reason": "refusal", "message": {"content": ""}}]),
            ok_response("not json"),
            ok_response(json.dumps({"appointment_required": {"candidates": []}})),
            httpx.Response(200, json={"error": {"code": 400, "message": "schema rejected"}}),
        ]
    )
    extractor = make()
    with pytest.raises(ExtractionError, match="truncated"):
        extractor.extract(bundle())
    with pytest.raises(ExtractionRefusedError):
        extractor.extract(bundle())
    with pytest.raises(ExtractionError, match="not valid JSON"):
        extractor.extract(bundle())
    with pytest.raises(ExtractionError, match="failed validation"):
        extractor.extract(bundle())
    with pytest.raises(ExtractionError, match="schema rejected"):
        extractor.extract(bundle())


def test_requires_api_key():
    with pytest.raises(ValueError, match="OPENROUTER_API_KEY"):
        OpenRouterExtractor("")
