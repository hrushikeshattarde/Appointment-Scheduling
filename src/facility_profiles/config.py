"""Application settings.

All secrets and environment-specific values come from environment variables or a local
``.env`` file. Transport Pro variables reuse the names used by the Transport Pro MCP server
(``TPRO_BASE_URL``, ``TPRO_USERNAME``, ``TPRO_PASSWORD``, ``TPRO_TIMEOUT_MS``) so one ``.env``
serves both tools. Application settings use the ``FP_`` prefix.
"""

from __future__ import annotations

from enum import StrEnum
from functools import lru_cache
from typing import Annotated, Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class RunMode(StrEnum):
    """Whether the routine may write profiles or only recommend them."""

    RECOMMEND = "recommend"
    WRITE = "write"


class Settings(BaseSettings):
    """Runtime configuration for the facility-profiles routine."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_prefix="FP_",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Transport Pro -------------------------------------------------------------
    tpro_base_url: str = Field(
        validation_alias=AliasChoices("TPRO_BASE_URL"),
        description="Base URL of the Transport Pro Public API, no trailing slash.",
    )
    tpro_username: SecretStr = Field(validation_alias=AliasChoices("TPRO_USERNAME"))
    tpro_password: SecretStr = Field(validation_alias=AliasChoices("TPRO_PASSWORD"))
    tpro_timeout_ms: int = Field(30_000, validation_alias=AliasChoices("TPRO_TIMEOUT_MS"), gt=0)
    tpro_max_requests_per_second: float = Field(
        5.0, validation_alias=AliasChoices("TPRO_MAX_REQUESTS_PER_SECOND"), gt=0
    )

    # --- LLM -----------------------------------------------------------------------------
    llm_provider: Literal["anthropic", "openrouter"] = "anthropic"
    llm_model: str = "claude-opus-5"
    llm_max_tokens: int = Field(16_000, gt=0)
    openrouter_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("OPENROUTER_API_KEY")
    )
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    llm_budget_usd: float | None = Field(default=None, gt=0)
    llm_price_input_per_million: float | None = Field(default=None, ge=0)
    llm_price_output_per_million: float | None = Field(default=None, ge=0)

    # --- Storage -----------------------------------------------------------------------
    database_url: str = "sqlite:///./data/facility_profiles.db"
    export_dir: str = "./exports"

    # --- Run controls -------------------------------------------------------------------
    mode: RunMode = RunMode.RECOMMEND
    pilot_terminal_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    pilot_customer_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    facility_cap: int = Field(500, gt=0)
    lookback_days: int = Field(90, gt=0)
    max_loads_per_facility: int = Field(100, gt=0)
    write_threshold: float = Field(0.8, ge=0, le=1)
    queue_threshold: float = Field(0.5, ge=0, le=1)
    conflict_support: float = Field(0.3, ge=0, le=1)
    stale_after_days: int = Field(180, gt=0)
    geo_match_meters: float = Field(200.0, gt=0)
    name_match_threshold: int = Field(92, ge=0, le=100)
    internal_email_domains: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["circledelivers.com"]
    )
    internal_phone_numbers: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["260-208-4500"]
    )

    # --- Booking agent (prototype, draft mode) --------------------------------------------
    booking_mode: Literal["draft"] = "draft"
    booking_days_ahead: int = Field(7, gt=0)
    booking_default_pickup_time: str = "09:00"
    booking_transit_miles_per_day: int = Field(550, gt=0)
    booking_sender: str = "lidl@circledelivers.com"
    booking_cc: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["lidl@circledelivers.com"]
    )
    booking_signature: str = (
        "Circle Logistics, Inc. | Fort Wayne | 260-208-4500 | lidl@circledelivers.com"
    )
    booking_drafts_dir: str = "./exports/drafts"
    booking_carrier_name: str = "Circle Logistics, Inc."
    booking_customer_desk: str | None = "inbound@lidl.us"
    booking_max_rounds: int = Field(3, gt=0)
    booking_follow_up_hours: int = Field(24, gt=0)
    booking_min_notice_hours: int = Field(4, ge=0)
    booking_avg_mph: float = Field(50.0, gt=0)
    booking_load_hours: float = Field(2.0, ge=0)

    # --- Logging -------------------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    @field_validator("tpro_base_url")
    @classmethod
    def _strip_trailing_slash(cls, value: str) -> str:
        return value.strip().rstrip("/")

    @field_validator("pilot_terminal_ids", "pilot_customer_ids", mode="before")
    @classmethod
    def _split_int_ids(cls, value: object) -> list[int]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [int(part) for part in value.split(",") if part.strip()]
        if isinstance(value, list | tuple):
            return [int(part) for part in value]
        msg = "expected a comma-separated string or a list of integers"
        raise TypeError(msg)

    @field_validator(
        "internal_email_domains", "internal_phone_numbers", "booking_cc", mode="before"
    )
    @classmethod
    def _split_csv(cls, value: object) -> list[str]:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [part.strip() for part in value.split(",") if part.strip()]
        if isinstance(value, list | tuple):
            return [str(part).strip() for part in value if str(part).strip()]
        msg = "expected a comma-separated string or a list"
        raise TypeError(msg)

    @model_validator(mode="after")
    def _openrouter_needs_key(self) -> Settings:
        if self.llm_provider == "openrouter" and self.openrouter_api_key is None:
            msg = "FP_LLM_PROVIDER=openrouter requires OPENROUTER_API_KEY"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _thresholds_are_ordered(self) -> Settings:
        if self.queue_threshold > self.write_threshold:
            msg = "queue_threshold must not exceed write_threshold"
            raise ValueError(msg)
        return self

    @property
    def timeout_seconds(self) -> float:
        """Per-request Transport Pro timeout in seconds."""
        return self.tpro_timeout_ms / 1000


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings, loaded once."""
    return Settings()
