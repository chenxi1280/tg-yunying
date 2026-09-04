"""Optional PATCH fields; merged task configs enforce cross-field invariants."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class EngagementSettingsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    engagement_contract_version: Literal["legacy_v0", "unified_engagement_v1"] | None = None
    account_selection_mode: Literal["group"] | None = None
    account_group_ids: list[int] | None = None
    concurrency_limit_per_group: int | None = Field(default=None, ge=1, le=50)
    daily_message_target: int | None = Field(default=None, ge=1, le=100_000)
    daily_target_jitter_bps: int | None = Field(default=None, ge=0, le=3000)
    attention_quiet_after_min_seconds: int | None = Field(default=None, ge=0, le=1800)
    attention_quiet_after_max_seconds: int | None = Field(default=None, ge=0, le=1800)
    initial_historical_post_limit: int | None = Field(default=None, ge=0, le=10)
    source_expectation_mode: Literal[
        "continuous_event_driven", "finite_existing_sources", "promised_daily_sources"
    ] | None = None
    daily_reaction_cap: int | None = Field(default=None, ge=1, le=1_000_000)
    account_ratio_min_bps: int | None = Field(default=None, ge=1, le=10000)
    account_ratio_max_bps: int | None = Field(default=None, ge=1, le=10000)
    rolling_participation_days: int | None = Field(default=None, ge=1, le=30)
    view_exposure_mode: Literal["natural_auto", "explicit_per_source"] | None = None
    per_account_source_degree_min: int | None = Field(default=None, ge=1, le=100)
    per_account_source_degree_max: int | None = Field(default=None, ge=1, le=100)
    every_active_message: bool | None = None
    per_source_exposure_target: int | None = Field(default=None, ge=1, le=10000)
    per_source_exposure_ratio_bps: int | None = Field(default=None, ge=1, le=10000)
