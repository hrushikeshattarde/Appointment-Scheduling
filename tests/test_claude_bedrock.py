"""Claude through Anthropic's SDK, on Bedrock or Anthropic's API, for the agent's model calls.

A stand-in client answers like the Messages API, so nothing here calls AWS or Anthropic.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import anthropic
import pytest

from facility_profiles import claude
from facility_profiles.booking.classify import ClaudeReplyClassifier, ReplyContext
from facility_profiles.booking.inbox import reader_tools
from facility_profiles.booking.schema import ReplyClassification, ReplyStatus
from facility_profiles.booking.writer import ClaudeReplyWriter, ReplySituation, WrittenReply
from facility_profiles.claude import claude_client, claude_model, structured_call
from facility_profiles.config import Settings
from facility_profiles.extraction.llm import ExtractionError, ExtractionRefusedError

CONFIRMED = ReplyClassification(
    status=ReplyStatus.CONFIRMED,
    pickup_date="2026-10-12",
    pickup_time="09:00",
    pickup_number="7704512",
    quotes=["SET! PU# 7704512"],
    confidence=0.95,
)


@dataclass
class FakeClaude:
    """Answers ``messages.create`` with one canned response and keeps every request."""

    text: str = "{}"
    stop_reason: str = "end_turn"
    requests: list[dict[str, Any]] = field(default_factory=list)

    @property
    def messages(self) -> FakeClaude:
        return self

    def create(self, **request: Any) -> SimpleNamespace:
        self.requests.append(request)
        return SimpleNamespace(
            stop_reason=self.stop_reason,
            stop_details=SimpleNamespace(category="cyber")
            if self.stop_reason == "refusal"
            else None,
            content=[
                SimpleNamespace(type="thinking", thinking=""),
                SimpleNamespace(type="text", text=self.text),
            ],
            model=request["model"],
            usage=SimpleNamespace(
                input_tokens=4000,
                output_tokens=600,
                cache_read_input_tokens=None,
                cache_creation_input_tokens=None,
            ),
            _request_id="req_1",
        )


def on(settings: Settings, **changes: Any) -> Settings:
    return settings.model_copy(update=changes)


def context() -> ReplyContext:
    return ReplyContext(
        vendor_name="Lidl Test Facility",
        po_numbers=["999912102601"],
        requested_local="2026-10-12 09:00",
        reply_sent_at=datetime(2026, 10, 8, 17, 49, tzinfo=UTC),
        subject="RE: Pick Up Appointment",
        body="SET! PU# 7704512\n\nPO# 999912102601 is confirmed for pickup on 10/12 @ 0900.",
    )


def test_the_model_is_named_the_way_the_provider_names_it(settings: Settings) -> None:
    bedrock = on(settings, llm_provider="bedrock", llm_model="claude-sonnet-5-5")
    # The current models run on Bedrock through a cross-region inference profile, US by default.
    assert claude_model(bedrock) == "us.anthropic.claude-sonnet-5-5"
    assert claude_model(on(bedrock, bedrock_routing="global")) == (
        "global.anthropic.claude-sonnet-5-5"
    )
    # OpenRouter's dotted spelling, and an ID that already names its provider.
    assert claude_model(on(bedrock, llm_model="claude-sonnet-4.6")) == (
        "us.anthropic.claude-sonnet-4-6"
    )
    profile = "global.anthropic.claude-haiku-5-5"
    assert claude_model(on(bedrock, llm_model=profile)) == profile
    assert claude_model(on(settings, llm_provider="anthropic", llm_model="claude-opus-5-5")) == (
        "claude-opus-5-5"
    )


def test_bedrock_signs_with_this_machines_aws_login_in_its_region(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    made: list[dict[str, Any]] = []
    monkeypatch.setattr(anthropic, "AnthropicBedrock", lambda **kw: made.append(kw) or "bedrock")
    assert (
        claude_client(on(settings, llm_provider="bedrock", bedrock_region="us-west-2")) == "bedrock"
    )
    assert made == [{"aws_region": "us-west-2", "max_retries": 3}]
    # Bedrock needs no OpenRouter key, and takes an effort level for the booking calls.
    assert (
        Settings.model_validate(
            {
                **settings.model_dump(),
                "llm_provider": "bedrock",
                "openrouter_api_key": None,
                "llm_effort": "low",
            }
        ).llm_effort
        == "low"
    )


def test_a_structured_call_asks_for_the_schema_and_returns_the_json() -> None:
    fake = FakeClaude(text='{"answer": 42}')
    reply = structured_call(
        fake,
        model="us.anthropic.claude-sonnet-5-5",
        system="s",
        user="u",
        schema={"type": "object"},
    )
    assert reply.data == {"answer": 42} and reply.request_id == "req_1"
    assert reply.usage.input_tokens == 4000 and reply.usage.output_tokens == 600
    sent = fake.requests[0]
    assert sent["output_config"] == {
        "format": {"type": "json_schema", "schema": {"type": "object"}}
    }
    assert sent["max_tokens"] == claude.MAX_TOKENS and "temperature" not in sent
    structured_call(fake, model="m", system="s", user="u", schema={}, effort="low")
    assert fake.requests[1]["output_config"]["effort"] == "low"


@pytest.mark.parametrize(
    ("fake", "error"),
    [
        (FakeClaude(stop_reason="refusal"), ExtractionRefusedError),
        (FakeClaude(stop_reason="max_tokens"), ExtractionError),
        (FakeClaude(text="not json"), ExtractionError),
        (FakeClaude(text="[1, 2]"), ExtractionError),
    ],
)
def test_a_refusal_a_cut_off_answer_or_no_json_fails_the_call(
    fake: FakeClaude, error: type
) -> None:
    with pytest.raises(error):
        structured_call(fake, model="m", system="s", user="u", schema={})


def test_the_reply_reader_returns_a_validated_reading() -> None:
    fake = FakeClaude(text=CONFIRMED.model_dump_json())
    out = ClaudeReplyClassifier(
        fake, model="us.anthropic.claude-sonnet-5-5", effort="medium"
    ).classify(context())
    assert out.result.status == ReplyStatus.CONFIRMED and out.result.pickup_number == "7704512"
    assert out.model == "us.anthropic.claude-sonnet-5-5" and out.usage.output_tokens == 600
    sent = fake.requests[0]
    assert "PO# 999912102601" in sent["messages"][0]["content"]
    assert sent["output_config"]["effort"] == "medium"
    schema = sent["output_config"]["format"]["schema"]
    assert schema["additionalProperties"] is False and "status" in schema["required"]
    with pytest.raises(ExtractionError, match="classification invalid"):
        ClaudeReplyClassifier(FakeClaude(text='{"status": "nonsense"}'), model="m").classify(
            context()
        )


def test_the_reply_writer_returns_a_validated_draft() -> None:
    written = WrittenReply(body="Yes, 10/12 @ 0900 works. Thank you!")
    fake = FakeClaude(text=written.model_dump_json())
    situation = ReplySituation(intent="thank", decision="booked 10/12 @ 0900")
    assert ClaudeReplyWriter(fake, model="m").write(situation) == written
    with pytest.raises(ExtractionError, match="written reply invalid"):
        ClaudeReplyWriter(FakeClaude(text=json.dumps({"answers": 3})), model="m").write(situation)


def test_bedrock_reads_replies_and_writes_answers_on_the_board(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(claude, "claude_client", lambda _s: FakeClaude())
    bedrock = on(settings, llm_provider="bedrock", llm_model="claude-sonnet-5-5", llm_effort="low")
    reader, writer = reader_tools(bedrock)
    assert isinstance(reader, ClaudeReplyClassifier) and isinstance(writer, ClaudeReplyWriter)
    assert reader.model == writer.model == "us.anthropic.claude-sonnet-5-5"
    # The default provider reads no replies, as before: no key, no model call.
    assert reader_tools(on(settings, llm_provider="anthropic")) == (None, None)


def test_the_profile_extractor_runs_on_bedrock_too(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    from facility_profiles.cli import _extractor
    from facility_profiles.extraction.llm import AnthropicExtractor

    fake = FakeClaude()
    monkeypatch.setattr(claude, "claude_client", lambda _s: fake)
    extractor = _extractor(
        on(settings, llm_provider="bedrock", llm_model="claude-sonnet-5-5"), fake=False
    )
    assert isinstance(extractor, AnthropicExtractor)
    assert extractor.model == "us.anthropic.claude-sonnet-5-5" and extractor._client is fake
