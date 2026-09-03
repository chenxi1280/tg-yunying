from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.ai_gateway import canonical_ai_model_identity
from app.search_keywords import normalized_keyword_hash, strict_keyword_materials
from app.security import encrypt_secret

from .api import ApiModel
from .operation_plans import OperationPlanTaskLinkOut
from .runtime_summary import TaskRuntimeSummaryOut

TaskTypeValue = Literal[
    "group_ai_chat",
    "group_relay",
    "group_membership_admission",
    "channel_view",
    "channel_like",
    "channel_comment",
    "search_click",
    "search_join_group",
    "group_clone",
]
TaskStatusValue = Literal[
    "draft",
    "pending",
    "running",
    "paused",
    "target_reached",
    "wrapping_up",
    "completed",
    "stopped",
    "failed",
    "deleted",
]
ActionStatusValue = Literal["pending", "executing", "success", "failed", "skipped"]
ReviewStatusValue = Literal["pending", "approved", "rejected", "expired"]
GROUP_AI_HARD_HOURLY_MIN_MESSAGES = 10
CHANNEL_COUNT_JITTER_DEFAULT = 0.2
DEFAULT_CHANNEL_LIKE_ALLOWED_REACTIONS = [
    "👍",
    "❤️",
    "🔥",
    "👏",
    "🎉",
    "🤩",
    "👌",
    "💯",
    "🙌",
    "✨",
]
MAX_TOTAL_COMMENT_JITTER = 0.3
DEFAULT_CHANNEL_COMMENT_BUSINESS_MAX_PER_MESSAGE = 80
DEFAULT_CHANNEL_COMMENT_PLANNED_FALLBACK_MAX_BPS = 2000
MAX_SEARCH_JOIN_SAFE_NAVIGATION = 3
MAX_SEARCH_JOIN_PAGES = 70
DEFAULT_SEARCH_JOIN_MAX_ACTIONS_PER_DAY = 100
DEFAULT_SEARCH_JOIN_DAILY_ACCOUNT_LIMIT = 1
DEFAULT_SEARCH_JOIN_KEYWORD_ACCOUNT_DAILY_LIMIT = 2
DEFAULT_SEARCH_JOIN_ACTION_SKIP_PROBABILITY = 0.1
DEFAULT_SEARCH_JOIN_HOURLY_JITTER_PERCENT = 30
DEFAULT_SEARCH_JOIN_DAILY_JITTER_PERCENT = 20
DEFAULT_SEARCH_JOIN_CURVE = [
    1,
    1,
    0,
    0,
    0,
    0,
    1,
    2,
    2,
    3,
    3,
    3,
    2,
    2,
    3,
    4,
    4,
    5,
    5,
    5,
    4,
    3,
    2,
    1,
]
KEYWORD_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
TELEGRAM_PUBLIC_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{1,64}$")
MAX_GROUP_AI_PREJOIN_CHANNELS = 3


def _strict_topic_participation_rate(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise ValueError("任务话题占比上限必须是数值")
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("任务话题占比上限必须是数值") from exc
    if not normalized.is_finite() or normalized < 0 or normalized > Decimal("0.30"):
        raise ValueError("任务话题占比上限必须在 0 到 0.30 之间")
    if normalized.as_tuple().exponent < -2:
        raise ValueError("任务话题占比上限最多两位小数")
    return float(normalized)


def _normalize_public_telegram_channel_ref(value: Any) -> str:
    text = str(value or "").strip()
    if text.startswith("@"):
        text = text[1:]
    elif "://" in text:
        parsed = urlparse(text)
        if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in {
            "t.me",
            "www.t.me",
            "telegram.me",
            "www.telegram.me",
        }:
            raise ValueError("预关注频道必须填写公开 Telegram 频道地址或 username")
        parts = [part for part in parsed.path.split("/") if part]
        if len(parts) != 1:
            raise ValueError("预关注频道只支持公开频道地址，不支持邀请链接或消息地址")
        text = parts[0]
    if not TELEGRAM_PUBLIC_USERNAME_RE.fullmatch(text) or text.startswith("+"):
        raise ValueError("预关注频道必须填写公开 Telegram 频道地址或 username")
    return text


def _normalize_group_ai_prejoin_channels(value: Any) -> list[str]:
    if value in (None, ""):
        return []
    if not isinstance(value, list):
        raise ValueError("预关注频道必须是地址列表")
    normalized = list(
        dict.fromkeys(_normalize_public_telegram_channel_ref(item) for item in value)
    )
    if len(normalized) > MAX_GROUP_AI_PREJOIN_CHANNELS:
        raise ValueError("预关注频道最多配置 3 个")
    return normalized


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = "02:00"
    end: str = "08:00"
    timezone: str = "Asia/Shanghai"

    @field_validator("start", "end")
    @classmethod
    def validate_time(cls, value: str, info) -> str:
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError(f"quiet_hours.{info.field_name} 必须是 HH:MM")
        return value

    @model_validator(mode="after")
    def validate_window(self) -> "QuietHours":
        if self.start == self.end:
            raise ValueError("quiet_hours.start 与 quiet_hours.end 不能相同")
        return self


DEFAULT_HOURLY_ACTIVITY_CURVE = [
    2,
    2,
    1,
    1,
    0,
    0,
    1,
    2,
    4,
    5,
    6,
    6,
    5,
    4,
    6,
    7,
    8,
    9,
    10,
    10,
    8,
    6,
    4,
    3,
]


class OperationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = "natural_full_day"
    source: Literal["built_in_default", "target_recommended", "manual"] = (
        "built_in_default"
    )
    hourly_activity_curve: list[int] = Field(
        default_factory=lambda: list(DEFAULT_HOURLY_ACTIVITY_CURVE)
    )
    quiet_threshold: int = Field(default=2, ge=0, le=60)
    peak_threshold: int = Field(default=8, ge=0, le=60)
    manual_override: bool = False

    @model_validator(mode="after")
    def validate_curve(self) -> "OperationProfile":
        if len(self.hourly_activity_curve) != 24:
            raise ValueError("hourly_activity_curve 必须包含 24 个每小时轮数点")
        normalized = [int(item) for item in self.hourly_activity_curve]
        if any(item < 0 or item > 60 for item in normalized):
            raise ValueError("hourly_activity_curve 每小时轮数必须在 0-60 之间")
        if not any(item > 0 for item in normalized):
            raise ValueError("hourly_activity_curve 不能全为 0")
        self.hourly_activity_curve = normalized
        if self.peak_threshold < self.quiet_threshold:
            self.peak_threshold = self.quiet_threshold
        return self


class AccountConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_mode: Literal["all", "group", "manual"] = "all"
    account_group_id: int | None = None
    account_ids: list[int] = Field(default_factory=list)
    max_concurrent: int = Field(default=20, ge=1, le=500)
    cooldown_per_account_minutes: int = Field(default=5, ge=0, le=1440)
    ban_policy: Literal["skip", "pause_task", "alert"] = "skip"

    @model_validator(mode="after")
    def validate_selection(self) -> "AccountConfig":
        if self.selection_mode == "group" and not self.account_group_id:
            raise ValueError("selection_mode=group 时 account_group_id 必填")
        if self.selection_mode == "manual" and not self.account_ids:
            raise ValueError("selection_mode=manual 时 account_ids 必填")
        return self


class PacingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["fixed", "curve", "template"] = "template"
    operation_profile: OperationProfile = Field(default_factory=OperationProfile)
    interval_seconds_min: int | None = Field(default=None, ge=0)
    interval_seconds_max: int | None = Field(default=None, ge=0)
    curve_type: (
        Literal["front_heavy", "back_heavy", "random_burst", "steady"] | None
    ) = None
    curve_duration_hours: int | None = Field(default=None, ge=1, le=168)
    template: (
        Literal["aggressive_1h", "moderate_6h", "gentle_24h", "burst_30min"] | None
    ) = "moderate_6h"
    jitter_percent: int = Field(default=30, ge=0, le=100)
    max_actions_per_hour: int | None = Field(default=None, ge=1)
    max_actions_per_day: int | None = Field(default=None, ge=1)
    quiet_hours: QuietHours | None = None
    source_capacity_v2_enabled: bool = False
    source_capacity_policy_version_id: str = ""

    @model_validator(mode="after")
    def normalize_fixed(self) -> "PacingConfig":
        if (
            self.source_capacity_v2_enabled
            and not self.source_capacity_policy_version_id
        ):
            raise ValueError("启用来源容量 v2 时必须绑定策略版本")
        if (
            not self.source_capacity_v2_enabled
            and self.source_capacity_policy_version_id
        ):
            raise ValueError("来源容量策略版本只能在 v2 启用时配置")
        if self.mode == "fixed":
            self.interval_seconds_min = (
                60 if self.interval_seconds_min is None else self.interval_seconds_min
            )
            self.interval_seconds_max = (
                self.interval_seconds_min
                if self.interval_seconds_max is None
                else self.interval_seconds_max
            )
            if self.interval_seconds_max < self.interval_seconds_min:
                self.interval_seconds_max = self.interval_seconds_min
        if self.mode == "curve" and not self.curve_type:
            self.curve_type = "steady"
        if self.mode == "template" and not self.template:
            self.template = "moderate_6h"
        return self


class SearchJoinPacingConfig(PacingConfig):
    max_actions_per_hour: int | None = Field(default=None, ge=0, le=500)
    per_account_total_action_limit: int = Field(default=0, ge=0, le=100000)
    per_account_daily_action_limit: int = Field(
        default=DEFAULT_SEARCH_JOIN_DAILY_ACCOUNT_LIMIT, ge=0, le=1000
    )
    per_account_hourly_action_limit: int = Field(default=0, ge=0, le=500)
    per_account_cooldown_days: int = Field(default=0, ge=0, le=365)
    per_keyword_account_daily_limit: int = Field(
        default=DEFAULT_SEARCH_JOIN_KEYWORD_ACCOUNT_DAILY_LIMIT, ge=0, le=1000
    )
    captcha_trigger_rate: float = Field(default=0, ge=0, le=1)
    max_actions_per_day: int | None = Field(
        default=DEFAULT_SEARCH_JOIN_MAX_ACTIONS_PER_DAY, ge=0
    )
    hourly_skip_probability: float = Field(default=0, ge=0, le=1)
    daily_skip_probability: float = Field(default=0, ge=0, le=1)
    skip_probability_per_action: float = Field(
        default=DEFAULT_SEARCH_JOIN_ACTION_SKIP_PROBABILITY, ge=0, le=1
    )
    hourly_jitter_percent: int = Field(
        default=DEFAULT_SEARCH_JOIN_HOURLY_JITTER_PERCENT, ge=0, le=100
    )
    daily_jitter_percent: int = Field(
        default=DEFAULT_SEARCH_JOIN_DAILY_JITTER_PERCENT, ge=0, le=100
    )

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_jitter(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        next_data = dict(data)
        legacy = next_data.get("jitter_percent")
        hourly = next_data.get("hourly_jitter_percent")
        if legacy is not None and hourly is not None and int(legacy) != int(hourly):
            raise ValueError("jitter_percent 与 hourly_jitter_percent 冲突")
        if legacy is not None and hourly is None:
            next_data["hourly_jitter_percent"] = int(legacy)
        return next_data


class SearchClickPacingConfig(PacingConfig):
    daily_jitter_percent: int = Field(default=30, ge=0, le=100)
    hourly_jitter_percent: int = Field(default=30, ge=0, le=100)


class SearchRankDeboostPacingConfig(PacingConfig):
    max_actions_per_day: int | None = Field(default=None, ge=1)
    hourly_jitter_percent: int = Field(default=0, ge=0, le=100)
    daily_jitter_percent: int = Field(default=0, ge=0, le=100)


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay_seconds: int = Field(default=60, ge=0)
    retry_backoff: Literal["none", "linear", "exponential"] = "exponential"
    on_account_banned: Literal["skip_account", "pause_task", "stop_task"] = (
        "skip_account"
    )
    on_api_rate_limit: Literal["wait_and_retry", "skip", "pause"] = "wait_and_retry"
    on_content_rejected: Literal["skip_message", "rewrite_and_retry", "pause"] = (
        "skip_message"
    )
    alert_on_failure: bool = False
    alert_webhook: str | None = None


class RelayFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keyword_whitelist: list[str] = Field(default_factory=list)
    keyword_blacklist: list[str] = Field(default_factory=list)
    min_message_length: int | None = Field(default=None, ge=0)
    max_message_length: int | None = Field(default=None, ge=1)
    allowed_media_types: list[str] = Field(default_factory=list)
    blocked_user_ids: list[str] = Field(default_factory=list)
    only_with_media: bool = False
    only_text: bool = False
    language_filter: str | None = None


class SourceGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    group_id: int | None = None
    operation_target_id: int | None = None
    target_input: str | None = None
    target_title: str | None = None
    group_name: str = ""
    is_active: bool = True

    @model_validator(mode="after")
    def validate_group_reference(self) -> "SourceGroup":
        if (
            not self.group_id
            and not self.operation_target_id
            and not (self.target_input or "").strip()
        ):
            raise ValueError(
                "source group requires group_id, operation_target_id or target_input"
            )
        return self


class GroupAITopicDirection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    weight: float = Field(default=1, ge=0.01, le=100)


class GroupAITeacherTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    priority: int = Field(default=1, ge=1, le=100)


def _plain_lines(value: str) -> list[str]:
    return [line.strip() for line in value.splitlines() if line.strip()]


def _topic_directions_from_lines(value: str) -> list[dict[str, Any]]:
    lines = _plain_lines(value)
    total = len(lines)
    return [
        {"title": line, "weight": float(total - index)}
        for index, line in enumerate(lines)
    ]


def _teacher_targets_from_lines(value: str) -> list[dict[str, Any]]:
    lines = _plain_lines(value)
    total = len(lines)
    return [
        {"name": line, "priority": total - index} for index, line in enumerate(lines)
    ]


def _normalize_topic_directions(value: Any) -> Any:
    if isinstance(value, str):
        return _topic_directions_from_lines(value)
    return value


def _normalize_teacher_targets(value: Any) -> Any:
    if isinstance(value, str):
        return _teacher_targets_from_lines(value)
    return value


class GroupAIChatConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_group_id: int | None = None
    target_operation_target_id: int | None = None
    target_reference_revision: int | None = Field(default=None, ge=1, exclude=True)
    target_type: Literal["group"] = "group"
    target_input: str | None = None
    target_title: str | None = None
    group_ai_prejoin_channel_ids: list[str] = Field(default_factory=list, exclude=True)
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    target_group_name: str = ""
    topic_directions: list[GroupAITopicDirection] = Field(default_factory=list)
    topic_participation_rate: float | None = Field(
        default=None,
        ge=0,
        le=0.30,
        description="任务 topic_directions 的普通正文占比上限；非目标值，不限制词库主题或讨论老师",
    )
    topic_participation_rate_next: float | None = Field(
        default=None, ge=0, le=0.30, exclude=True
    )
    topic_participation_rate_effective_date: date | None = Field(
        default=None, exclude=True
    )
    teacher_targets: list[GroupAITeacherTarget] = Field(default_factory=list)
    chat_history_depth: int = Field(default=50, ge=1, le=200)
    ai_model: str = ""
    system_prompt_override: str | None = None
    slang_prompt_template_id: int | None = None
    slang_terms: dict[str, str] = Field(default_factory=dict)
    tone: Literal["casual", "professional", "mixed", "auto"] = "auto"
    language: str = "zh-CN"
    max_message_length: int | None = Field(default=None, ge=1)
    participation_rate: float = Field(default=0.6, ge=0.01, le=1)
    participation_jitter: float = Field(default=0.5, ge=0, le=1)
    allow_account_repeat: bool = True
    repeat_cooldown_rounds: int = Field(default=2, ge=0)
    account_personas: dict[str, str] = Field(default_factory=dict)
    account_memory_depth: int = Field(default=3, ge=0, le=20)
    messages_per_round_mode: Literal["auto", "manual"] = "auto"
    messages_per_round: int = Field(default=1, ge=1)
    reply_min_per_round: int = Field(default=1, ge=0)
    daily_message_target: int = Field(default=1, ge=1, le=100_000)
    hard_hourly_target_enabled: bool | None = Field(default=None, exclude=True)
    hourly_min_messages: int | None = Field(default=None, ge=1, exclude=True)
    hard_hourly_strategy: str | None = Field(default=None, exclude=True)
    ai_provider_id: int | None = Field(default=None, exclude=True)
    context_freshness_window_hours: int | None = Field(default=None, exclude=True)
    account_coverage_mode: Literal["all_accounts_daily"] = "all_accounts_daily"

    coverage_window_hours: Literal[24] = 24
    history_fetch_account_id: int | None = None
    auto_join_target: bool = True
    group_bot_admission_required: bool = True
    auto_resolve_verification: bool = True
    ai_assisted_verification: bool = True
    captcha_failure_policy: Literal["manual"] = "manual"
    membership_max_concurrent: int = Field(default=5, ge=1, le=50)
    idle_continuation_enabled: bool = True
    idle_continuation_seconds: int = Field(default=300, ge=30, le=86400)
    silent_mode_enabled: bool = True
    silent_start: str = "23:00"
    silent_end: str = "08:00"
    silent_max_accounts: int = Field(default=5, ge=1, le=50)
    silent_messages_per_round: int = Field(default=1, ge=1)
    ramp_up_minutes: int = Field(default=60, ge=0, le=1440)
    ramp_start_ratio: float = Field(default=0.3, ge=0.01, le=1)
    context_expire_after_messages: int = Field(default=10, ge=0, le=500)
    due_catch_up_pipeline_depth: int = Field(default=1, ge=1, le=4)
    fact_anchor_required: bool = True
    semantic_repeat_window: int = Field(default=10, ge=1, le=100)
    low_confidence_silence_enabled: bool = True
    ai_two_stage_enabled: bool = False
    ai_semantic_reviewer_model: str = ""
    adult_prompt_enabled: bool | None = None
    content_route: (
        Literal[
            "general",
            "adult_visual",
            "adult_product",
            "adult_service_inquiry",
            "adult_service_sensory",
            "adult_service",
        ]
        | None
    ) = None
    ai_content_route_v2_enabled: bool = False
    ai_content_policy_version_id: str = ""
    ai_content_allowed_routes: list[str] = Field(default_factory=list)
    ai_content_attestation_ids: list[str] = Field(default_factory=list)
    ai_content_policy_manifest_id: str = ""
    ai_content_sampling_manifest_hash: str = ""
    ai_content_max_cost_per_slot: float = Field(default=0, ge=0)
    ai_content_daily_budget: float = Field(default=0, ge=0)
    ai_dialogue_chain_enabled: bool = False

    @field_validator("topic_directions", mode="before")
    @classmethod
    def normalize_topic_directions(cls, value: Any) -> Any:
        return _normalize_topic_directions(value)

    @field_validator(
        "topic_participation_rate", "topic_participation_rate_next", mode="before"
    )
    @classmethod
    def validate_topic_participation_rate(cls, value: Any) -> Any:
        return _strict_topic_participation_rate(value)

    @field_validator("teacher_targets", mode="before")
    @classmethod
    def normalize_teacher_targets(cls, value: Any) -> Any:
        return _normalize_teacher_targets(value)

    @field_validator("group_ai_prejoin_channel_ids", mode="before")
    @classmethod
    def normalize_prejoin_channels(cls, value: Any) -> list[str]:
        return _normalize_group_ai_prejoin_channels(value)

    @model_validator(mode="after")
    def validate_target_reference(self) -> "GroupAIChatConfig":
        if (
            not self.target_group_id
            and not self.target_operation_target_id
            and not (self.target_input or "").strip()
        ):
            raise ValueError(
                "target_group_id、target_operation_target_id 或 target_input 至少填写一个"
            )
        if self.reply_min_per_round > self.messages_per_round:
            raise ValueError("reply_min_per_round 不能大于 messages_per_round")
        if not self.group_bot_admission_required:
            raise ValueError("AI 活跃群必须启用群管机器人准入")
        _validate_semantic_reviewer(self)
        _validate_ai_content_route_config(self)
        return self


class GroupRelayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_groups: list[SourceGroup]
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    monitor_account_ids: list[int] = Field(default_factory=list)
    filters: RelayFilters = Field(default_factory=RelayFilters)
    target_group_id: int | None = None
    target_operation_target_id: int | None = None
    target_type: Literal["group"] = "group"
    target_input: str | None = None
    target_title: str | None = None
    target_group_ids: list[int] = Field(default_factory=list)
    target_operation_target_ids: list[int] = Field(default_factory=list)
    send_account_ids: list[int] = Field(default_factory=list)
    content_mode: Literal["raw", "light_rewrite", "ai_rewrite", "summary"] = (
        "light_rewrite"
    )
    rewrite_prompt: str | None = None
    preserve_media: bool = False
    add_source_attribution: bool = False
    filter_bot_messages: bool = True
    filter_admin_messages: bool = False
    excluded_sender_peer_ids: list[str] = Field(default_factory=list)
    excluded_sender_usernames: list[str] = Field(default_factory=list)
    excluded_sender_names: list[str] = Field(default_factory=list)
    dedup_window_minutes: int = Field(default=60, ge=1, le=10080)
    dedup_method: Literal["hash", "semantic", "both"] = "hash"
    require_review: bool = False

    @model_validator(mode="after")
    def validate_relay_targets(self) -> "GroupRelayConfig":
        if (
            not self.target_group_id
            and not self.target_group_ids
            and not self.target_operation_target_id
            and not self.target_operation_target_ids
            and not (self.target_input or "").strip()
        ):
            raise ValueError(
                "target_group_id、target_group_ids、运营目标或 target_input 至少填写一个"
            )
        if self.target_group_id and self.target_group_id not in self.target_group_ids:
            self.target_group_ids = [self.target_group_id, *self.target_group_ids]
        if (
            self.target_operation_target_id
            and self.target_operation_target_id not in self.target_operation_target_ids
        ):
            self.target_operation_target_ids = [
                self.target_operation_target_id,
                *self.target_operation_target_ids,
            ]
        self.require_review = False
        return self


class GroupCloneSourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_group_id: int = Field(gt=0)
    operation_target_id: int = Field(gt=0)
    peer_type: Literal["channel"] = "channel"
    peer_id: str = Field(min_length=1, max_length=120)
    listener_account_id: int = Field(gt=0)
    authorization_id: int = Field(gt=0)
    authorization_mode: Literal["public", "owned", "admin_authorized"]

    @model_validator(mode="after")
    def validate_source(self) -> "GroupCloneSourceConfig":
        if not self.peer_id.strip():
            raise ValueError("source.peer_id 必填")
        return self


class GroupCloneTargetConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    internal_group_id: int = Field(gt=0)
    operation_target_id: int = Field(gt=0)
    peer_type: Literal["channel"] = "channel"
    peer_id: str = Field(min_length=1, max_length=120)
    control_account_id: int = Field(gt=0)
    control_authorization_id: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_target(self) -> "GroupCloneTargetConfig":
        if not self.peer_id.strip():
            raise ValueError("target.peer_id 必填")
        return self


class GroupCloneSenderPoolConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_ids: list[int] = Field(min_length=1)
    active_minutes: int = Field(default=30, ge=1, le=1440)
    guarded_minutes: int = Field(default=120, ge=1, le=10080)
    eligible_release_minutes: int = Field(default=720, ge=1, le=43200)
    minimum_tenure_minutes: int = Field(default=60, ge=1, le=10080)

    @model_validator(mode="after")
    def validate_pool(self) -> "GroupCloneSenderPoolConfig":
        if len(set(self.account_ids)) != len(self.account_ids):
            raise ValueError("sender_pool.account_ids 不得重复")
        if (
            not self.active_minutes
            < self.guarded_minutes
            < self.eligible_release_minutes
        ):
            raise ValueError(
                "sender binding 生命周期必须满足 active < guarded < eligible_release"
            )
        if self.minimum_tenure_minutes > self.eligible_release_minutes:
            raise ValueError("minimum_tenure_minutes 不能大于 eligible_release_minutes")
        return self


class GroupClonePacingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    min_delay_ms: int = Field(default=1000, ge=0, le=300000)
    max_delay_ms: int = Field(default=6000, ge=0, le=300000)
    strict_target_order: Literal[True] = True

    @model_validator(mode="after")
    def validate_delay(self) -> "GroupClonePacingConfig":
        if self.min_delay_ms > self.max_delay_ms:
            raise ValueError("pacing.min_delay_ms 不能大于 pacing.max_delay_ms")
        return self


class GroupCloneContentConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rule_set_id: int = Field(gt=0)
    rule_set_version: int = Field(gt=0)
    orphan_reply_policy: Literal[
        "quote_fallback", "drop_subtree", "block_for_review"
    ] = "quote_fallback"
    incomplete_album_policy: Literal["drop_incomplete", "send_partial_degraded"] = (
        "drop_incomplete"
    )
    unsupported_media_policy: Literal["block", "manual_review"] = "block"


class GroupCloneLifecycleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_mode: Literal["start_from_now"] = "start_from_now"
    failure_order_policy: Literal["fail_stop", "continue_with_visible_gap"] = (
        "fail_stop"
    )
    unknown_deadline_seconds: int = Field(default=900, ge=60, le=86400)


class GroupCloneRetentionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_event_days: int = Field(default=30, ge=1, le=365)
    media_cache_ttl_seconds: int = Field(default=86400, ge=60, le=604800)


class GroupCloneConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: GroupCloneSourceConfig
    target: GroupCloneTargetConfig
    sender_pool: GroupCloneSenderPoolConfig
    pacing: GroupClonePacingConfig = Field(default_factory=GroupClonePacingConfig)
    content: GroupCloneContentConfig
    lifecycle: GroupCloneLifecycleConfig = Field(
        default_factory=GroupCloneLifecycleConfig
    )
    retention: GroupCloneRetentionConfig = Field(
        default_factory=GroupCloneRetentionConfig
    )


class ChannelMessageScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_channel_id: int | None = None
    target_type: Literal["channel"] = "channel"
    target_input: str | None = None
    target_title: str | None = None
    target_channel_name: str = ""
    message_scope: Literal[
        "all", "latest_n", "date_range", "specific", "dynamic_new"
    ] = "latest_n"
    message_count: int | None = Field(default=10, ge=1, le=500)
    date_from: datetime | None = None
    date_to: datetime | None = None
    message_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message_scope(self) -> "ChannelMessageScopeConfig":
        if not self.target_channel_id and not (self.target_input or "").strip():
            raise ValueError("target_channel_id 或 target_input 至少填写一个")
        if self.message_scope == "specific" and not self.message_ids:
            raise ValueError("message_scope=specific 时 message_ids 必填")
        if self.message_scope == "date_range" and not (self.date_from or self.date_to):
            raise ValueError(
                "message_scope=date_range 时 date_from/date_to 至少填写一个"
            )
        return self


class ChannelViewConfig(ChannelMessageScopeConfig):
    message_scope: Literal[
        "all", "latest_n", "date_range", "specific", "dynamic_new"
    ] = "dynamic_new"
    initial_message_scope: (
        Literal["latest_n", "today_new", "date_range", "specific", "new_only"] | None
    ) = None
    latest_message_count: int | None = Field(default=None, ge=1, le=500)
    listen_new_messages: bool = True
    account_coverage_mode: Literal["all_accounts_daily"] = "all_accounts_daily"
    per_message_daily_view_target: int | None = Field(default=None, ge=1, le=10000)
    per_message_total_view_target: int | None = Field(default=0, ge=0, le=100000)
    message_active_days: int = Field(default=7, ge=1, le=365)
    task_daily_view_safety_cap: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    max_views_per_account_per_day: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    target_views_per_message: int | None = Field(default=None, ge=1, le=10000)
    view_count_jitter: float = Field(default=CHANNEL_COUNT_JITTER_DEFAULT, ge=0, le=1)
    execution_mode: Literal["distribute", "burst"] = "distribute"

    @model_validator(mode="after")
    def normalize_post_level_view_targets(self) -> "ChannelViewConfig":
        legacy_target = self.target_views_per_message
        if self.initial_message_scope:
            scope_map = {
                "latest_n": "latest_n",
                "today_new": "date_range",
                "date_range": "date_range",
                "specific": "specific",
                "new_only": "dynamic_new",
            }
            self.message_scope = scope_map[self.initial_message_scope]
        if self.latest_message_count is not None:
            self.message_count = self.latest_message_count
        if self.per_message_daily_view_target is None:
            self.per_message_daily_view_target = legacy_target
        if self.per_message_total_view_target is None:
            self.per_message_total_view_target = 0
        self.target_views_per_message = self.per_message_daily_view_target
        return self


class ChannelLikeConfig(ChannelMessageScopeConfig):
    message_scope: Literal[
        "all", "latest_n", "date_range", "specific", "dynamic_new"
    ] = "dynamic_new"
    target_likes_per_message: int = Field(default=50, ge=1, le=10000)
    like_count_jitter: float = Field(default=CHANNEL_COUNT_JITTER_DEFAULT, ge=0, le=1)
    reaction_type: Literal["random", "specific"] = "random"
    reaction_scope: Literal["configured", "all_available"] = "all_available"
    allowed_reactions: list[str] = Field(
        default_factory=lambda: list(DEFAULT_CHANNEL_LIKE_ALLOWED_REACTIONS)
    )
    max_likes_per_account_per_hour: int = Field(default=1_000_000, ge=1, le=1_000_000)
    message_active_days: int = Field(default=7, ge=1, le=365)
    rolling_window_days: int | None = Field(default=None, ge=1, le=365)


class ChannelCommentConfig(ChannelMessageScopeConfig):
    message_scope: Literal[
        "all", "latest_n", "date_range", "specific", "dynamic_new"
    ] = "dynamic_new"
    target_comments_per_message: int = Field(default=10, ge=1, le=1000)
    business_max_comments_per_message: int = Field(
        default=DEFAULT_CHANNEL_COMMENT_BUSINESS_MAX_PER_MESSAGE,
        ge=1,
        le=1000,
    )
    planned_fallback_max_bps: int = Field(
        default=DEFAULT_CHANNEL_COMMENT_PLANNED_FALLBACK_MAX_BPS,
        ge=0,
        le=10000,
    )
    comment_count_jitter: float = Field(default=0.05, ge=0, le=1)
    max_total_comments: int = Field(default=1_000_000, ge=1, le=1_000_000)
    max_total_comments_jitter: float = Field(
        default=0, ge=0, le=MAX_TOTAL_COMMENT_JITTER
    )
    daily_comment_cap: int = Field(default=0, ge=0)
    rolling_window_days: int = Field(default=3, ge=1, le=30)
    allow_returning_accounts: bool = False
    comment_mode: Literal["comment", "reply", "mixed"] = "mixed"
    reply_to_message_ids: list[int] = Field(default_factory=list)
    reply_min_per_message: int = Field(default=1, ge=0)
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    ai_model: str = ""
    comment_style: Literal["relevant", "question", "praise", "discussion", "mixed"] = (
        "mixed"
    )
    topic_hint: str | None = None
    system_prompt_override: str | None = None
    language: str = "zh-CN"
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    require_review: bool = False
    ai_two_stage_enabled: bool = False
    ai_semantic_reviewer_model: str = ""
    ai_content_route_v2_enabled: bool = False
    ai_content_policy_version_id: str = ""
    ai_content_allowed_routes: list[str] = Field(default_factory=list)
    ai_content_attestation_ids: list[str] = Field(default_factory=list)
    channel_comment_grounding_v1_enabled: bool = False
    auto_join_discussion_enabled: bool = False
    discussion_join_account_ids: list[int] = Field(default_factory=list)
    discussion_join_budget: int = Field(default=0, ge=0)
    discussion_join_pacing_policy_version: str = ""
    discussion_join_pacing_policy: dict[str, int] = Field(default_factory=dict)
    unicode_emoji_enabled: bool = True
    image_meme_enabled: bool = False
    image_meme_material_group_id: int | None = Field(default=None, gt=0)
    unicode_emoji_weight_bps: int = Field(default=10000, ge=0, le=10000)
    image_meme_weight_bps: int = Field(default=0, ge=0, le=10000)
    allow_image_reselection_before_gateway: bool = True
    allow_cross_kind_fallback_to_unicode: bool = True

    @model_validator(mode="after")
    def disable_manual_review(self) -> "ChannelCommentConfig":
        if self.comment_mode == "reply" and not self.reply_to_message_ids:
            raise ValueError("comment_mode=reply 时 reply_to_message_ids 必填")
        if self.reply_min_per_message > self.target_comments_per_message:
            raise ValueError(
                "reply_min_per_message 不能大于 target_comments_per_message"
            )
        _validate_semantic_reviewer(self)
        _validate_ai_content_route_config(self)
        self._validate_comment_fallback_policy()
        self._validate_discussion_join_policy()
        self.require_review = False
        return self

    def _validate_discussion_join_policy(self) -> None:
        if not self.auto_join_discussion_enabled:
            return
        if not self.discussion_join_account_ids:
            raise ValueError("discussion_join_authorized_scope_required")
        if self.discussion_join_budget <= 0:
            raise ValueError("discussion_join_budget_required")
        if not self.discussion_join_pacing_policy_version.strip():
            raise ValueError("discussion_join_pacing_policy_version_required")
        if int(self.discussion_join_pacing_policy.get("interval_seconds") or 0) <= 0:
            raise ValueError("discussion_join_pacing_policy_required")

    def _validate_comment_fallback_policy(self) -> None:
        if not self.channel_comment_grounding_v1_enabled:
            return
        if not self.ai_two_stage_enabled or not self.ai_content_route_v2_enabled:
            raise ValueError("channel_comment_grounding_activation_incomplete")
        if self.rolling_window_days != 3:
            raise ValueError("channel_comment_rolling_window_must_be_3_days")
        if self.daily_comment_cap <= 0:
            raise ValueError("channel_comment_daily_cap_required")
        if not self.unicode_emoji_enabled and not self.image_meme_enabled:
            raise ValueError("comment_fallback_type_required")
        if self.unicode_emoji_weight_bps + self.image_meme_weight_bps != 10000:
            raise ValueError("comment_fallback_weights_must_total_10000")
        if self.unicode_emoji_weight_bps and not self.unicode_emoji_enabled:
            raise ValueError("unicode_emoji_weight_requires_enabled_type")
        if self.image_meme_weight_bps and not self.image_meme_enabled:
            raise ValueError("image_meme_weight_requires_enabled_type")
        if self.image_meme_weight_bps > 0 and not self.image_meme_material_group_id:
            raise ValueError("image_meme_material_group_required")


def _validate_semantic_reviewer(
    config: GroupAIChatConfig | ChannelCommentConfig,
) -> None:
    if not config.ai_two_stage_enabled:
        return
    generator_model = config.ai_model.strip()
    reviewer_model = config.ai_semantic_reviewer_model.strip()
    if not generator_model:
        raise ValueError("启用两阶段生成时必须显式配置生成模型")
    if not reviewer_model:
        raise ValueError("启用两阶段生成时必须配置独立语义评审模型")
    if _ai_model_identity(reviewer_model) == _ai_model_identity(generator_model):
        raise ValueError("语义评审模型必须与生成模型不同")


def _validate_ai_content_route_config(
    config: GroupAIChatConfig | ChannelCommentConfig,
) -> None:
    valid_routes = {
        "general",
        "adult_visual",
        "adult_product",
        "adult_service_inquiry",
        "adult_service_sensory",
    }
    enabled = config.ai_content_route_v2_enabled
    configured = bool(
        config.ai_content_policy_version_id
        or config.ai_content_allowed_routes
        or config.ai_content_attestation_ids
    )
    if not enabled and configured:
        raise ValueError("AI 内容路由配置只能在 v2 启用时提交")
    if not enabled:
        return
    if not config.ai_two_stage_enabled:
        raise ValueError("AI 内容路由 v2 必须启用两阶段生成")
    if not config.ai_content_policy_version_id:
        raise ValueError("AI 内容路由 v2 必须绑定已激活策略版本")
    if not config.ai_content_allowed_routes:
        raise ValueError("AI 内容路由 v2 至少配置一个允许路由")
    if not set(config.ai_content_allowed_routes) <= valid_routes:
        raise ValueError("AI 内容路由 v2 包含未知路由")


def _ai_model_identity(model_name: str) -> str:
    return canonical_ai_model_identity(model_name)


class GroupMembershipAdmissionPacingConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["spread"] = "spread"
    max_concurrent: int = Field(default=5, ge=1, le=50)
    per_minute: int = Field(default=10, ge=1, le=200)


class GroupMembershipAdmissionTestMessageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["ai_random"] = "ai_random"
    min_chars: int = Field(default=3, ge=1, le=80)
    max_chars: int = Field(default=12, ge=1, le=120)
    delete_after_send: bool = False

    @model_validator(mode="after")
    def validate_length_window(self) -> "GroupMembershipAdmissionTestMessageConfig":
        if self.max_chars < self.min_chars:
            raise ValueError("test_message.max_chars 必须大于等于 min_chars")
        return self


class GroupMembershipAdmissionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_operation_target_id: int = Field(gt=0)
    account_group_ids: list[int] = Field(default_factory=list)
    admission_pacing: GroupMembershipAdmissionPacingConfig = Field(
        default_factory=GroupMembershipAdmissionPacingConfig
    )
    test_message: GroupMembershipAdmissionTestMessageConfig = Field(
        default_factory=GroupMembershipAdmissionTestMessageConfig
    )

    @model_validator(mode="after")
    def validate_account_groups(self) -> "GroupMembershipAdmissionConfig":
        group_ids = list(
            dict.fromkeys(int(item) for item in self.account_group_ids if int(item) > 0)
        )
        if not group_ids:
            raise ValueError("account_group_ids 至少选择一个账号分组")
        self.account_group_ids = group_ids
        return self


class SearchJoinBotConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=80)
    display_name: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def normalize_username(self) -> "SearchJoinBotConfig":
        self.username = self.username.strip().lstrip("@")
        if not self.username:
            raise ValueError("search bot username 不能为空")
        return self


class SearchJoinVisibilityAttribution(BaseModel):
    model_config = ConfigDict(extra="forbid")

    organic_search_join: bool = True
    linked_ai_activity: bool = True
    jisou_ecosystem: Literal["bot_joined", "flow_alliance", "unknown"] = "unknown"
    paid_keyword_ad: Literal["none", "active", "expired", "unknown"] = "unknown"
    content_health: Literal["healthy", "weak", "blocked", "unknown"] = "unknown"


class SearchJoinGroupConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_operation_target_id: int | None = Field(default=None, gt=0)
    target_group_id: int | None = Field(default=None, gt=0)
    target_input: str | None = Field(default=None, max_length=300)
    target_title: str | None = Field(default=None, max_length=180)
    target_link: str | None = Field(default=None, max_length=300)
    execution_mode: Literal["mtproto_userbot"] = "mtproto_userbot"
    search_bots: list[SearchJoinBotConfig] = Field(default_factory=list)
    max_pages: int = Field(
        default=MAX_SEARCH_JOIN_PAGES, ge=1, le=MAX_SEARCH_JOIN_PAGES
    )
    keywords: list[str] = Field(default_factory=list, exclude=True)
    keyword_hashes: list[str] = Field(default_factory=list)
    keyword_text_ciphertexts: list[str] = Field(default_factory=list)
    business_region: str = Field(default="", max_length=80)
    account_locale: str = Field(default="zh-CN", max_length=20)
    proxy_country: str = Field(default="", max_length=20)
    pre_join_decoy_click_min: int = Field(
        default=0, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    pre_join_decoy_click_max: int = Field(
        default=2, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    post_join_safe_navigation_min: int = Field(
        default=0, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    post_join_safe_navigation_max: int = Field(
        default=0, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    decoy_join_enabled: bool = False
    hourly_round_curve: list[int] = Field(
        default_factory=lambda: list(DEFAULT_SEARCH_JOIN_CURVE)
    )
    actions_per_round: int = Field(default=1, ge=1, le=20)
    max_actions_per_hour: int = Field(default=20, ge=1, le=500)
    hourly_min_successful_joins: int = Field(default=1, ge=1, le=500)
    target_count: int | None = Field(default=None, ge=1)
    daily_click_target_count: int | None = Field(default=None, ge=1)
    daily_target_count: int | None = Field(default=None, ge=1)
    allow_same_account_repeat_application: bool = False
    strict_daily_target: bool = False
    target_relevance_score: int | None = Field(default=None, ge=0, le=100)
    target_content_health: Literal["healthy", "weak", "blocked", "unknown"] = "unknown"
    jisou_ecosystem_status: Literal["bot_joined", "flow_alliance", "unknown"] = (
        "unknown"
    )
    paid_keyword_ad_status: Literal["none", "active", "expired", "unknown"] = "unknown"
    search_visibility_attribution: SearchJoinVisibilityAttribution = Field(
        default_factory=SearchJoinVisibilityAttribution
    )
    post_join_policy: Literal["stay_joined"] = "stay_joined"
    post_join_task_links: list[dict[str, Any]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_search_join_config(self) -> "SearchJoinGroupConfig":
        if self.target_count is not None and (
            self.daily_click_target_count is not None
            or self.daily_target_count is not None
        ):
            raise ValueError("target_count 与每日目标不能同时填写")
        if (
            not self.target_group_id
            and not self.target_operation_target_id
            and not (self.target_input or "").strip()
        ):
            raise ValueError(
                "target_group_id、target_operation_target_id 或 target_input 至少填写一个"
            )
        if not self.search_bots:
            raise ValueError("search_bots 至少选择一个搜索机器人")
        if len(self.hourly_round_curve) != 24:
            raise ValueError("hourly_round_curve 必须包含 24 个小时点")
        if self.pre_join_decoy_click_min > self.pre_join_decoy_click_max:
            raise ValueError(
                "pre_join_decoy_click_min 不能大于 pre_join_decoy_click_max"
            )
        if self.post_join_safe_navigation_min or self.post_join_safe_navigation_max:
            raise ValueError("post_join_safe_navigation 本期不支持")
        if self.pre_join_decoy_click_max > MAX_SEARCH_JOIN_SAFE_NAVIGATION:
            raise ValueError("非目标安全浏览总量不能超过 3")
        if self.decoy_join_enabled:
            raise ValueError("不得加入非目标群")
        self.keyword_hashes, self.keyword_text_ciphertexts = _keyword_materials(
            self.keywords,
            self.keyword_hashes,
            self.keyword_text_ciphertexts,
        )
        if not self.keyword_hashes:
            raise ValueError("keywords 或 keyword_hashes 至少提供一个")
        if any(not KEYWORD_HASH_RE.fullmatch(item) for item in self.keyword_hashes):
            raise ValueError("keyword_hashes 必须是 64 位小写 hex")
        return self


class SearchClickConfig(BaseModel):
    """Pure search-click contract; membership fields are intentionally absent."""

    model_config = ConfigDict(extra="forbid")

    search_execution_mode: Literal["click_only"] = "click_only"
    target_operation_target_id: int = Field(gt=0)
    target_input: str = Field(min_length=1, max_length=300)
    target_title: str = Field(min_length=1, max_length=180)
    target_link: str = Field(min_length=1, max_length=300)
    daily_click_target_count: int = Field(ge=1)
    search_bots: list[SearchJoinBotConfig] = Field(min_length=1)
    keyword_hashes: list[str] = Field(min_length=1)
    keyword_text_ciphertexts: list[str] = Field(min_length=1)
    execution_mode: Literal["mtproto_userbot"] = "mtproto_userbot"
    max_pages: int = Field(
        default=MAX_SEARCH_JOIN_PAGES, ge=1, le=MAX_SEARCH_JOIN_PAGES
    )

    @model_validator(mode="after")
    def validate_keyword_materials(self) -> "SearchClickConfig":
        _existing_keyword_materials(
            self.keyword_hashes,
            self.keyword_text_ciphertexts,
        )
        if any(not KEYWORD_HASH_RE.fullmatch(item) for item in self.keyword_hashes):
            raise ValueError("keyword_hashes 必须是 64 位小写 hex")
        return self


def _keyword_materials(
    keywords: list[str],
    existing_hashes: list[str],
    existing_ciphertexts: list[str],
) -> tuple[list[str], list[str]]:
    pairs = _existing_keyword_materials(existing_hashes, existing_ciphertexts)
    known_hashes = {item[0] for item in pairs}
    for keyword in keywords:
        text = keyword.strip()
        if not text:
            continue
        keyword_hash = normalized_keyword_hash(text)
        if keyword_hash not in known_hashes:
            pairs.append((keyword_hash, encrypt_secret(text)))
            known_hashes.add(keyword_hash)
    return [item[0] for item in pairs], [item[1] for item in pairs]


def _existing_keyword_materials(
    existing_hashes: list[str],
    existing_ciphertexts: list[str],
) -> list[tuple[str, str]]:
    return strict_keyword_materials(existing_hashes, existing_ciphertexts)


class TaskCreateCommon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    client_request_id: str | None = Field(default=None, min_length=8, max_length=120)
    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=3, ge=1, le=5)
    timezone: str = "Asia/Shanghai"
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    max_duration_hours: int | None = Field(default=None, ge=1)
    account_config: AccountConfig = Field(default_factory=AccountConfig)
    pacing_config: PacingConfig = Field(default_factory=PacingConfig)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)

    @model_validator(mode="after")
    def validate_schedule_window(self) -> "TaskCreateCommon":
        if (
            self.scheduled_start
            and self.scheduled_end
            and self.scheduled_end <= self.scheduled_start
        ):
            raise ValueError("scheduled_end 必须晚于 scheduled_start")
        return self


class SearchClickSimpleTaskCreate(BaseModel):
    """搜索点击新建页的业务目标与运营执行范围输入。"""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str | None = Field(default=None, min_length=8, max_length=120)
    target_title: str = Field(min_length=1, max_length=180)
    target_link: str = Field(min_length=1, max_length=300)
    keywords: list[str] = Field(min_length=1)
    account_group_id: int = Field(gt=0)
    max_actions_per_day: int = Field(ge=1)
    scheduled_end: datetime
    daily_jitter_percent: int = Field(ge=0, le=100)
    hourly_jitter_percent: int = Field(ge=0, le=100)
    quiet_hours: QuietHours | None = None

    @field_validator("target_title", "target_link")
    @classmethod
    def normalize_target_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("目标群名称和链接不能为空")
        return normalized

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        unique: list[str] = []
        seen_hashes: set[str] = set()
        for item in values:
            keyword = item.strip()
            if not keyword:
                continue
            keyword_hash = normalized_keyword_hash(keyword)
            if keyword_hash in seen_hashes:
                continue
            seen_hashes.add(keyword_hash)
            unique.append(keyword)
        if not unique:
            raise ValueError("keywords 至少提供一个非空关键词")
        return unique


class SearchJoinGroupSimpleTaskCreate(SearchClickSimpleTaskCreate):
    daily_click_target_count: int | None = Field(default=None, ge=1)
    daily_target_count: int = Field(ge=1)
    allow_same_account_repeat_application: bool = False
    per_account_daily_action_limit: int = Field(
        default=DEFAULT_SEARCH_JOIN_DAILY_ACCOUNT_LIMIT, ge=0, le=1000
    )

    @model_validator(mode="after")
    def validate_daily_action_budget(self) -> "SearchJoinGroupSimpleTaskCreate":
        source_target = self.daily_click_target_count or self.daily_target_count
        if self.max_actions_per_day < source_target:
            raise ValueError("max_actions_per_day 不能小于每日点击目标")
        return self


class SearchClickTaskCreate(BaseModel):
    """Dedicated operator input for a pure search-click task."""

    model_config = ConfigDict(extra="forbid")

    client_request_id: str | None = Field(default=None, min_length=8, max_length=120)
    search_execution_mode: Literal["click_only"] = "click_only"
    target_title: str = Field(min_length=1, max_length=180)
    target_link: str = Field(min_length=1, max_length=300)
    keywords: list[str] = Field(min_length=1)
    daily_click_target_count: int = Field(ge=1)
    account_group_id: int = Field(gt=0)
    scheduled_end: datetime
    daily_jitter_percent: int = Field(default=20, ge=0, le=100)
    hourly_jitter_percent: int = Field(default=30, ge=0, le=100)
    quiet_hours: QuietHours | None = None

    @field_validator("target_title", "target_link")
    @classmethod
    def normalize_target_text(cls, value: str) -> str:
        return SearchClickSimpleTaskCreate.normalize_target_text(value)

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        return SearchClickSimpleTaskCreate.normalize_keywords(values)


class SearchRankDeboostSimpleTaskCreate(SearchClickSimpleTaskCreate):
    target_count: int = Field(ge=1)


class GroupAIChatTaskCreate(TaskCreateCommon, GroupAIChatConfig):
    topic_participation_rate: float = Field(
        ge=0,
        le=0.30,
        description="任务 topic_directions 的普通正文占比上限；非目标值，不限制词库主题或讨论老师",
    )


class GroupRelayTaskCreate(TaskCreateCommon, GroupRelayConfig):
    pass


class ChannelViewTaskCreate(TaskCreateCommon, ChannelViewConfig):
    pass


class ChannelLikeTaskCreate(TaskCreateCommon, ChannelLikeConfig):
    pass


class ChannelCommentTaskCreate(TaskCreateCommon, ChannelCommentConfig):
    pass


class GroupMembershipAdmissionTaskCreate(
    TaskCreateCommon, GroupMembershipAdmissionConfig
):
    pass


class SearchJoinGroupTaskCreate(TaskCreateCommon, SearchJoinGroupConfig):
    pacing_config: SearchJoinPacingConfig = Field(
        default_factory=SearchJoinPacingConfig
    )

    @model_validator(mode="after")
    def validate_daily_action_budget(self) -> "SearchJoinGroupTaskCreate":
        source_target = self.daily_click_target_count or self.daily_target_count
        if source_target is None:
            return self
        max_actions_per_day = self.pacing_config.max_actions_per_day
        if max_actions_per_day is None or max_actions_per_day < source_target:
            raise ValueError("max_actions_per_day 不能小于每日点击目标")
        return self


class SearchClickInternalTaskCreate(TaskCreateCommon, SearchClickConfig):
    pacing_config: SearchClickPacingConfig = Field(
        default_factory=SearchClickPacingConfig
    )


class SearchClickTaskConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_title: str | None = Field(default=None, min_length=1, max_length=180)
    target_link: str | None = Field(default=None, min_length=1, max_length=300)
    keywords: list[str] | None = Field(default=None, min_length=1, max_length=50)
    daily_click_target_count: int | None = Field(default=None, ge=1)
    account_group_id: int | None = Field(default=None, gt=0)
    scheduled_end: datetime | None = None
    daily_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    hourly_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    quiet_hours: QuietHours | None = None

    @model_validator(mode="after")
    def validate_target_pair(self) -> "SearchClickTaskConfigUpdate":
        if (self.target_title is None) != (self.target_link is None):
            raise ValueError("target_title 与 target_link 必须同时修改")
        return self


class GroupAIChatTaskConfigUpdate(GroupAIChatConfig):
    topic_participation_rate: float = Field(
        ge=0,
        le=0.30,
        description="任务 topic_directions 的普通正文占比上限；下一任务日的新 allocation plan 生效",
    )


class GroupRelayTaskConfigUpdate(GroupRelayConfig):
    pass


class ChannelViewTaskConfigUpdate(ChannelViewConfig):
    pass


class GroupCloneTaskCreate(TaskCreateCommon, GroupCloneConfig):
    pass


class GroupCloneTaskConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_pool: GroupCloneSenderPoolConfig
    pacing: GroupClonePacingConfig
    content: GroupCloneContentConfig
    lifecycle: GroupCloneLifecycleConfig
    retention: GroupCloneRetentionConfig


class GroupClonePrecheckResponse(BaseModel):
    passed: bool
    precheck_fingerprint: str = ""
    authority_version: int = 0
    hard_blocks: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source_info: dict = Field(default_factory=dict)
    target_info: dict = Field(default_factory=dict)
    sender_pool_info: dict = Field(default_factory=dict)


class GroupCloneSequencerHeadDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_case_revision: int = 1
    decision: Literal["accept_visible_gap", "retry_same_mutation", "keep_blocked"]
    reason: str = Field(..., min_length=1, max_length=255)
    client_request_id: str = Field(..., min_length=8, max_length=100)


class GroupCloneManualReviewDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_review_revision: int = Field(ge=1)
    decision: Literal["release", "drop", "keep_blocked"]
    reason: str = Field(..., min_length=1, max_length=255)
    client_request_id: str = Field(..., min_length=8, max_length=100)


class GroupCloneSenderBindingChangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_binding_version: int = Field(ge=1)
    replacement_account_id: int | None = Field(default=None, ge=1)
    reason: str = Field(..., min_length=1, max_length=255)
    client_request_id: str = Field(..., min_length=8, max_length=100)


class GroupCloneCutoverRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_token: str
    legacy_task_id: str
    expected_legacy_revision: int
    route_manifest_hash: str
    expected_authority_version: int
    open_action_fingerprint: str
    client_request_id: str = Field(..., min_length=8, max_length=100)
    reason: str = Field(..., min_length=1, max_length=255)
    clone_config: GroupCloneTaskCreate


class GroupCloneRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_token: str
    clone_task_id: str
    expected_authority_version: int
    open_action_fingerprint: str
    client_request_id: str = Field(..., min_length=8, max_length=100)
    reason: str = Field(..., min_length=1, max_length=255)


class ChannelLikeTaskConfigUpdate(ChannelLikeConfig):
    pass


class ChannelCommentTaskConfigUpdate(ChannelCommentConfig):
    pass


class GroupMembershipAdmissionTaskConfigUpdate(GroupMembershipAdmissionConfig):
    pass


class SearchJoinGroupTaskConfigUpdate(BaseModel):
    """搜索点击任务的局部配置更新。

    关键词明文不会从任务详情回传；未提交关键词字段时，服务层必须保留既有
    hash/ciphertext 配对，而不是要求前端重新提供不可见的加密材料。
    """

    model_config = ConfigDict(extra="forbid")

    target_operation_target_id: int | None = Field(default=None, gt=0)
    target_count: int | None = Field(default=None, ge=1)
    daily_click_target_count: int | None = Field(default=None, ge=1)
    daily_target_count: int | None = Field(default=None, ge=1)
    allow_same_account_repeat_application: bool | None = None
    target_group_id: int | None = Field(default=None, gt=0)
    target_input: str | None = Field(default=None, max_length=300)
    target_title: str | None = Field(default=None, max_length=180)
    target_link: str | None = Field(default=None, max_length=300)
    execution_mode: Literal["mtproto_userbot"] | None = None
    search_bots: list[SearchJoinBotConfig] | None = None
    max_pages: int | None = Field(default=None, ge=1, le=MAX_SEARCH_JOIN_PAGES)
    keywords: list[str] | None = None
    keyword_hashes: list[str] | None = None
    keyword_text_ciphertexts: list[str] | None = None
    business_region: str | None = Field(default=None, max_length=80)
    account_locale: str | None = Field(default=None, max_length=20)
    proxy_country: str | None = Field(default=None, max_length=20)
    pre_join_decoy_click_min: int | None = Field(
        default=None, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    pre_join_decoy_click_max: int | None = Field(
        default=None, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    post_join_safe_navigation_min: int | None = Field(
        default=None, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    post_join_safe_navigation_max: int | None = Field(
        default=None, ge=0, le=MAX_SEARCH_JOIN_SAFE_NAVIGATION
    )
    decoy_join_enabled: bool | None = None
    hourly_round_curve: list[int] | None = None
    actions_per_round: int | None = Field(default=None, ge=1, le=20)
    max_actions_per_hour: int | None = Field(default=None, ge=1, le=500)
    hourly_min_successful_joins: int | None = Field(default=None, ge=1, le=500)
    target_relevance_score: int | None = Field(default=None, ge=0, le=100)
    target_content_health: Literal["healthy", "weak", "blocked", "unknown"] | None = (
        None
    )
    jisou_ecosystem_status: Literal["bot_joined", "flow_alliance", "unknown"] | None = (
        None
    )
    paid_keyword_ad_status: Literal["none", "active", "expired", "unknown"] | None = (
        None
    )
    search_visibility_attribution: SearchJoinVisibilityAttribution | None = None
    post_join_policy: Literal["stay_joined"] | None = None
    post_join_task_links: list[dict[str, Any]] | None = None
    pacing_config: SearchJoinPacingConfig | None = None
    account_group_id: int | None = Field(default=None, gt=0)
    max_actions_per_day: int | None = Field(default=None, ge=1)
    per_account_daily_action_limit: int | None = Field(default=None, ge=0, le=1000)
    enable_strict_daily_target: Literal[True] | None = None
    scheduled_end: datetime | None = None
    daily_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    hourly_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    quiet_hours: QuietHours | None = None

    @field_validator("target_title", "target_link")
    @classmethod
    def normalize_target_text(cls, value: str | None) -> str | None:
        return (
            SearchClickSimpleTaskCreate.normalize_target_text(value)
            if value is not None
            else None
        )

    @model_validator(mode="after")
    def validate_keyword_material_patch(self) -> "SearchJoinGroupTaskConfigUpdate":
        fields = self.model_fields_set
        if "target_count" in fields and {
            "daily_click_target_count",
            "daily_target_count",
        }.intersection(fields):
            raise ValueError("target_count 与每日目标不能同时填写")
        target_title_supplied = "target_title" in fields
        target_link_supplied = "target_link" in fields
        if target_title_supplied != target_link_supplied:
            raise ValueError("目标群名称和公开链接必须同时填写")
        if target_title_supplied and (not self.target_title or not self.target_link):
            raise ValueError("目标群名称和公开链接不能为空")
        hashes_supplied = "keyword_hashes" in fields
        ciphertexts_supplied = "keyword_text_ciphertexts" in fields
        if hashes_supplied != ciphertexts_supplied:
            raise ValueError("keyword_hashes 与 keyword_text_ciphertexts 必须一一对应")
        if hashes_supplied:
            hashes = self.keyword_hashes or []
            ciphertexts = self.keyword_text_ciphertexts or []
            _existing_keyword_materials(hashes, ciphertexts)
        return self


class SearchRankDeboostTaskCreate(BaseModel):
    """搜索排名降权任务创建 schema。"""

    name: str
    search_bots: list[str] = Field(default=["jisou"], description="首版仅支持 jisou")
    keywords: list[dict] = Field(default_factory=list, description="关键词列表")
    target_group_ids: list[int] = Field(
        default_factory=list, description="我方目标群 ID 列表，用于实时排名判定与白名单"
    )
    account_pool_id: int | None = Field(
        default=None,
        description="兼容字段：账号分组 ID，必须为 pool_purpose=rank_deboost 的分组",
    )
    proxy_airport_node_id: int | None = Field(
        default=None, description="兼容字段：分组级绑定的 Clash 节点 ID"
    )
    timezone: str = "Asia/Shanghai"
    scheduled_end: datetime | None = None
    account_config: AccountConfig = Field(default_factory=AccountConfig)
    pacing_config: SearchRankDeboostPacingConfig = Field(
        default_factory=SearchRankDeboostPacingConfig
    )
    config: dict = Field(
        default_factory=dict, description="任务配置，含节奏、停留时长、限流"
    )
    notes: str = ""


class SearchRankDeboostTaskConfigUpdate(BaseModel):
    """搜索排名观察任务的业务目标与运营执行范围更新。"""

    model_config = ConfigDict(extra="forbid")

    target_operation_target_id: int | None = Field(default=None, gt=0)
    target_title: str | None = Field(default=None, min_length=1, max_length=180)
    target_link: str | None = Field(default=None, min_length=1, max_length=300)
    keywords: list[str] | None = None
    target_count: int | None = Field(default=None, ge=1)
    account_group_id: int | None = Field(default=None, gt=0)
    max_actions_per_day: int | None = Field(default=None, ge=1)
    scheduled_end: datetime | None = None
    daily_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    hourly_jitter_percent: int | None = Field(default=None, ge=0, le=100)
    quiet_hours: QuietHours | None = None

    @field_validator("target_title", "target_link")
    @classmethod
    def normalize_target_text(cls, value: str | None) -> str | None:
        return (
            SearchClickSimpleTaskCreate.normalize_target_text(value)
            if value is not None
            else None
        )

    @model_validator(mode="after")
    def validate_target_patch(self) -> "SearchRankDeboostTaskConfigUpdate":
        fields = self.model_fields_set
        if ("target_title" in fields) != ("target_link" in fields):
            raise ValueError("目标群名称和公开链接必须同时填写")
        if "target_title" in fields and (not self.target_title or not self.target_link):
            raise ValueError("目标群名称和公开链接不能为空")
        return self

    @field_validator("keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        return SearchClickSimpleTaskCreate.normalize_keywords(values)


class SearchRankDeboostExemptGroupResponse(BaseModel):
    """随机豁免群响应 schema。"""

    task_id: str
    exempt_group_username: str
    exempt_group_peer_id: str
    exempt_group_title: str
    exempt_group_match_strategy: str
    selected_at: datetime
    selected_by: str


class AccountGroupProxyBindingOut(ApiModel):
    id: int
    tenant_id: int
    account_pool_id: int
    proxy_airport_node_id: int
    runtime_proxy_id: int | None = None
    binding_generation: int
    status: str
    observed_exit_ip: str = ""
    observed_exit_country: str = ""
    observed_exit_asn: str = ""
    observed_exit_isp: str = ""
    last_probe_at: datetime | None = None
    last_probe_error: str = ""
    reference_count: int = 0


class RankDeboostProxyBindingRequest(BaseModel):
    proxy_airport_node_id: int = Field(gt=0)
    reason: str = Field(default="", max_length=255)


class RankDeboostProxyBindingDeleteRequest(BaseModel):
    reason: str = Field(default="", max_length=255)


class SearchRankDeboostClickReservationOut(ApiModel):
    id: str
    tenant_id: int
    task_id: str
    action_id: str
    account_id: int
    account_pool_id: int
    keyword_hash: str
    local_date: date
    hour_bucket: datetime
    reserved_count: int
    consumed_count: int
    status: str
    expires_at: datetime


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int | None = Field(default=None, ge=1, le=5)
    timezone: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    max_duration_hours: int | None = Field(default=None, ge=1)
    account_config: AccountConfig | None = None
    pacing_config: PacingConfig | SearchJoinPacingConfig | None = None
    failure_policy: FailurePolicy | None = None


class TaskSettingsUpdate(TaskUpdate):
    model_config = ConfigDict(extra="forbid")

    topic_hint: str | None = None
    topic_directions: list[GroupAITopicDirection] | None = None
    topic_participation_rate: float | None = Field(default=None, ge=0, le=0.30)
    teacher_targets: list[GroupAITeacherTarget] | None = None
    chat_history_depth: int | None = Field(default=None, ge=1, le=200)
    ai_model: str | None = None
    system_prompt_override: str | None = None
    slang_prompt_template_id: int | None = None
    slang_terms: dict[str, str] | None = None
    tone: Literal["casual", "professional", "mixed", "auto"] | None = None
    language: str | None = None
    max_message_length: int | None = Field(default=None, ge=1)

    @field_validator("topic_directions", mode="before")
    @classmethod
    def normalize_topic_directions(cls, value: Any) -> Any:
        return _normalize_topic_directions(value)

    @field_validator("topic_participation_rate", mode="before")
    @classmethod
    def validate_topic_participation_rate(cls, value: Any) -> Any:
        return _strict_topic_participation_rate(value)

    @field_validator("teacher_targets", mode="before")
    @classmethod
    def normalize_teacher_targets(cls, value: Any) -> Any:
        return _normalize_teacher_targets(value)

    participation_rate: float | None = Field(default=None, ge=0.01, le=1)
    participation_jitter: float | None = Field(default=None, ge=0, le=1)
    allow_account_repeat: bool | None = None
    repeat_cooldown_rounds: int | None = Field(default=None, ge=0)
    account_personas: dict[str, str] | None = None
    account_memory_depth: int | None = Field(default=None, ge=0, le=20)
    messages_per_round_mode: Literal["auto", "manual"] | None = None
    messages_per_round: int | None = Field(default=None, ge=1)
    reply_min_per_round: int | None = Field(default=None, ge=0)
    group_ai_prejoin_channel_ids: list[str] | None = Field(default=None, exclude=True)
    account_coverage_mode: Literal["all_accounts_daily"] | None = None
    coverage_window_hours: Literal[24] | None = None
    history_fetch_account_id: int | None = None
    auto_join_target: bool | None = None
    group_bot_admission_required: bool | None = None
    auto_resolve_verification: bool | None = None
    ai_assisted_verification: bool | None = None
    captcha_failure_policy: Literal["manual"] | None = None
    membership_max_concurrent: int | None = Field(default=None, ge=1, le=50)
    idle_continuation_enabled: bool | None = None
    idle_continuation_seconds: int | None = Field(default=None, ge=30, le=86400)
    context_expire_after_messages: int | None = Field(default=None, ge=0, le=500)
    due_catch_up_pipeline_depth: int | None = Field(default=None, ge=1, le=4)
    fact_anchor_required: bool | None = None
    semantic_repeat_window: int | None = Field(default=None, ge=1, le=100)
    low_confidence_silence_enabled: bool | None = None
    ai_two_stage_enabled: bool | None = None
    ai_semantic_reviewer_model: str | None = None
    ai_content_route_v2_enabled: bool | None = None
    ai_content_policy_version_id: str | None = None
    ai_content_allowed_routes: list[str] | None = None
    ai_content_attestation_ids: list[str] | None = None
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None

    @field_validator("group_ai_prejoin_channel_ids", mode="before")
    @classmethod
    def normalize_prejoin_channels(cls, value: Any) -> list[str]:
        return _normalize_group_ai_prejoin_channels(value)

    source_groups: list[SourceGroup] | None = None
    target_group_id: int | None = None
    target_operation_target_id: int | None = None
    target_group_name: str | None = None
    target_group_ids: list[int] | None = None
    target_operation_target_ids: list[int] | None = None
    monitor_account_ids: list[int] | None = None
    filters: RelayFilters | None = None
    content_mode: Literal["raw", "light_rewrite", "ai_rewrite", "summary"] | None = None
    rewrite_prompt: str | None = None
    preserve_media: bool | None = None
    add_source_attribution: bool | None = None
    filter_bot_messages: bool | None = None
    filter_admin_messages: bool | None = None
    excluded_sender_peer_ids: list[str] | None = None
    excluded_sender_usernames: list[str] | None = None
    excluded_sender_names: list[str] | None = None
    dedup_window_minutes: int | None = Field(default=None, ge=1, le=10080)
    dedup_method: Literal["hash", "semantic", "both"] | None = None
    require_review: bool | None = None

    target_views_per_message: int | None = Field(default=None, ge=1, le=10000)
    initial_message_scope: (
        Literal["latest_n", "today_new", "date_range", "specific", "new_only"] | None
    ) = None
    latest_message_count: int | None = Field(default=None, ge=1, le=500)
    listen_new_messages: bool | None = None
    per_message_daily_view_target: int | None = Field(default=None, ge=1, le=10000)
    per_message_total_view_target: int | None = Field(default=None, ge=0, le=100000)
    message_active_days: int | None = Field(default=None, ge=1, le=365)
    task_daily_view_safety_cap: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    max_views_per_account_per_day: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    view_count_jitter: float | None = Field(default=None, ge=0, le=1)
    execution_mode: Literal["distribute", "burst"] | None = None

    target_likes_per_message: int | None = Field(default=None, ge=1, le=10000)
    like_count_jitter: float | None = Field(default=None, ge=0, le=1)
    reaction_type: Literal["random", "specific"] | None = None
    reaction_scope: Literal["configured", "all_available"] | None = None
    allowed_reactions: list[str] | None = None
    max_likes_per_account_per_hour: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )

    target_comments_per_message: int | None = Field(default=None, ge=1, le=1000)
    business_max_comments_per_message: int | None = Field(default=None, ge=1, le=1000)
    planned_fallback_max_bps: int | None = Field(default=None, ge=0, le=10000)
    comment_count_jitter: float | None = Field(default=None, ge=0, le=1)
    max_total_comments: int | None = Field(default=1_000_000, ge=1, le=1_000_000)
    max_total_comments_jitter: float | None = Field(
        default=None, ge=0, le=MAX_TOTAL_COMMENT_JITTER
    )
    daily_comment_cap: int | None = Field(default=None, ge=0)
    rolling_window_days: int | None = Field(default=None, ge=1, le=365)
    comment_mode: Literal["comment", "reply", "mixed"] | None = None
    reply_to_message_ids: list[int] | None = None
    reply_min_per_message: int | None = Field(default=None, ge=0)
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    ai_model: str | None = None
    comment_style: (
        Literal["relevant", "question", "praise", "discussion", "mixed"] | None
    ) = None
    topic_hint: str | None = None
    system_prompt_override: str | None = None
    language: str | None = None
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int | None = Field(
        default=1_000_000, ge=1, le=1_000_000
    )
    channel_comment_grounding_v1_enabled: bool | None = None
    auto_join_discussion_enabled: bool | None = None
    discussion_join_account_ids: list[int] | None = None
    discussion_join_budget: int | None = Field(default=None, ge=0)
    discussion_join_pacing_policy_version: str | None = None
    discussion_join_pacing_policy: dict[str, int] | None = None
    unicode_emoji_enabled: bool | None = None
    image_meme_enabled: bool | None = None
    image_meme_material_group_id: int | None = Field(default=None, gt=0)
    unicode_emoji_weight_bps: int | None = Field(default=None, ge=0, le=10000)
    image_meme_weight_bps: int | None = Field(default=None, ge=0, le=10000)
    allow_image_reselection_before_gateway: bool | None = None
    allow_cross_kind_fallback_to_unicode: bool | None = None


class TaskSourceFilterOverrideRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_peer_id: str = ""
    sender_username: str = ""
    sender_name: str = ""
    source_action_id: str | None = None
    source_action: str = ""
    reason: str = Field(..., min_length=1, max_length=500)

    @model_validator(mode="after")
    def normalize_identity(self) -> "TaskSourceFilterOverrideRequest":
        self.sender_peer_id = self.sender_peer_id.strip()
        self.sender_username = self.sender_username.strip().lstrip("@")
        self.sender_name = self.sender_name.strip()
        self.source_action_id = (self.source_action_id or "").strip() or None
        self.source_action = self.source_action.strip()
        self.reason = self.reason.strip()
        if not any([self.sender_peer_id, self.sender_username, self.sender_name]):
            raise ValueError(
                "sender_peer_id、sender_username 或 sender_name 至少提供一个"
            )
        if not any([self.source_action_id, self.source_action]):
            raise ValueError("source_action_id 或 source_action 至少提供一个")
        if not self.reason:
            raise ValueError("reason 不能为空")
        return self


class TaskListItemOut(ApiModel):
    id: str
    tenant_id: int
    source_kind: str
    name: str
    type: str
    status: str
    priority: int
    next_run_at: datetime | None
    last_error: str
    stats: dict[str, Any] = Field(default_factory=dict)
    runtime_stage: dict[str, Any] = Field(default_factory=dict)
    target_summary: str = ""
    account_scope_summary: str = ""
    target_group_label: str = ""
    associated_channel_label: str = ""
    group_key: str = ""
    created_at: datetime
    updated_at: datetime


class TaskListSummaryOut(ApiModel):
    total: int = 0
    running: int = 0
    failed: int = 0


class TaskListGroupOut(ApiModel):
    key: str
    target_group_label: str
    associated_channel_label: str
    task_count: int
    running_count: int
    failed_count: int


class TaskListPageOut(ApiModel):
    items: list[TaskListItemOut]
    total: int
    page: int
    page_size: int
    summary: TaskListSummaryOut
    groups: list[TaskListGroupOut]


class TaskOut(ApiModel):
    id: str
    tenant_id: int
    name: str
    type: str
    status: str
    priority: int
    timezone: str
    scheduled_start: datetime | None
    scheduled_end: datetime | None
    max_duration_hours: int | None
    next_run_at: datetime | None
    last_error: str
    account_config: dict[str, Any]
    pacing_config: dict[str, Any]
    failure_policy: dict[str, Any]
    type_config: dict[str, Any]
    config_revision: int = 1
    task_lifecycle_epoch: int = 1
    topic_policy_state: str = "not_applicable"
    topic_policy_inventory: dict[str, Any] = Field(default_factory=dict)
    content_policy_effective_scopes: dict[str, Any] = Field(default_factory=dict)
    group_ai_prejoin_channel_ids: list[str] = Field(default_factory=list)
    stats: dict[str, Any]
    runtime_stage: dict[str, Any] = Field(default_factory=dict)
    target_summary: str = ""
    search_text: str = ""
    create_status: str = "existing"
    start_status: str = "not_requested"
    start_failure_code: str = ""
    runtime_state: str = "runnable"
    runtime_blocker_codes: list[str] = Field(default_factory=list)
    start_operation_id: str | None = None
    start_operation_version: int | None = None
    start_operation_legacy_untracked: bool = False
    created_at: datetime
    updated_at: datetime

    @model_validator(mode="after")
    def project_topic_policy_state(self) -> "TaskOut":
        if self.type != "group_ai_chat":
            self.topic_policy_state = "not_applicable"
        elif self.type_config.get("topic_participation_rate") is None:
            self.topic_policy_state = "legacy_unconfirmed"
        else:
            self.topic_policy_state = "confirmed"
        if self.type == "group_ai_chat":
            expected = int(self.type_config.get("daily_message_target") or 0)
            self.topic_policy_inventory = {
                "task_id": self.id,
                "task_status": self.status,
                "config_revision": self.config_revision,
                "topic_direction_count": len(
                    self.type_config.get("topic_directions") or []
                ),
                "expected_normal_count": expected,
                "projected_topic_max_counts": {
                    "0.00": 0,
                    "0.10": expected // 10,
                    "0.20": expected // 5,
                    "0.30": expected * 3 // 10,
                },
                "confirmation_state": self.topic_policy_state,
            }
            pending_rate = self.type_config.get("topic_participation_rate_next")
            effective_date = self.type_config.get(
                "topic_participation_rate_effective_date"
            )
            policy_meta = dict(
                self.type_config.get("_ai_group_content_policy_meta") or {}
            )
            rate_meta = dict(policy_meta.get("topic_participation_rate") or {})
            rate_is_pending = pending_rate is not None
            self.content_policy_effective_scopes = {
                "topic_participation_rate": {
                    "effective_scope": "next_task_day"
                    if rate_is_pending
                    else "current_task_day",
                    "effective_revision": _policy_meta_value(
                        rate_meta,
                        "next_revision" if rate_is_pending else "current_revision",
                        self.config_revision,
                    ),
                    "effective_at": _policy_meta_value(
                        rate_meta,
                        "next_effective_at"
                        if rate_is_pending
                        else "current_effective_at",
                        effective_date or self.updated_at,
                    ),
                    "current_value": self.type_config.get("topic_participation_rate"),
                    "next_value": pending_rate,
                },
                "topic_directions": _new_intent_effective_scope(
                    policy_meta,
                    "topic_directions",
                    self.config_revision,
                    self.updated_at,
                ),
                "teacher_targets": _new_intent_effective_scope(
                    policy_meta,
                    "teacher_targets",
                    self.config_revision,
                    self.updated_at,
                ),
            }
        return self


def _policy_meta_value(meta: dict[str, Any], key: str, fallback: Any) -> Any:
    value = meta.get(key)
    return fallback if value in (None, "") else value


def _new_intent_effective_scope(
    policy_meta: dict[str, Any],
    field: str,
    fallback_revision: int,
    fallback_at: datetime,
) -> dict[str, Any]:
    meta = dict(policy_meta.get(field) or {})
    return {
        "effective_scope": "new_content_intent",
        "effective_revision": _policy_meta_value(meta, "revision", fallback_revision),
        "effective_at": _policy_meta_value(meta, "effective_at", fallback_at),
    }


class ActionOut(ApiModel):
    id: str
    tenant_id: int
    task_id: str
    task_type: str
    action_type: str
    account_id: int | None
    account_display_name: str = ""
    account_username: str | None = ""
    scheduled_at: datetime
    executed_at: datetime | None
    pacing_due_at: datetime | None = None
    release_not_before_at: datetime | None = None
    effective_claim_at: datetime | None = None
    pacing_slot_key: str = ""
    status: str
    payload: dict[str, Any]
    result: dict[str, Any]
    retry_count: int
    failure_type: str = ""
    failure_reason: str = ""
    failure_diagnosis: dict[str, Any] = Field(default_factory=dict)
    raw_error: str = ""
    trace_id: str = ""
    operation_issue_id: str = ""
    operation_issue_status: str = ""
    operation_issue_rolled_up: bool = False
    created_at: datetime

    @field_validator("pacing_slot_key", mode="before")
    @classmethod
    def _none_pacing_slot_key_to_empty(cls, value: Any) -> Any:
        # legacy / 未冻结节奏槽位的 Action 该列为 NULL；读模型统一投影为空串。
        return "" if value is None else value


class ExecutionAttemptOut(ApiModel):
    id: str
    tenant_id: int
    action_id: str
    worker_id: str
    account_id: int | None
    attempt_no: int
    status: str
    before_call_at: datetime | None
    gateway_call_started_at: datetime | None
    after_call_at: datetime | None
    remote_message_id: str
    failure_type: str
    failure_detail: str
    result_snapshot: dict[str, Any]
    created_at: datetime


class ReviewQueueOut(ApiModel):
    id: str
    tenant_id: int
    task_id: str
    action_id: str
    content_preview: str
    source_info: str
    status: str
    reviewed_by: str
    reviewed_at: datetime | None
    reject_reason: str
    expires_at: datetime | None
    created_at: datetime


class TaskDetailAccountOut(BaseModel):
    id: int
    display_name: str
    username: str | None = None
    status: str


class TaskMessageGroupOut(BaseModel):
    channel_target_id: int | None = None
    channel_title: str = ""
    channel_username: str = ""
    message_id: int | None = None
    action_type: str = ""
    action_label: str = ""
    message_url: str = ""
    content_preview: str = ""
    target_count: int = 0
    completed_count: int = 0
    failed_count: int = 0
    running_count: int = 0
    skipped_count: int = 0
    duplicate_count: int = 0
    capacity_shortfall: int = 0
    subtask_status: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    actions: list[ActionOut] = Field(default_factory=list)


class TaskAITurnOut(BaseModel):
    action_id: str
    turn_index: int
    account_id: int | None = None
    account_role: str = ""
    account_memory: str = ""
    account_profile: str = ""
    account_voice_profile_version: int = 0
    account_voice_profile_summary: str = ""
    account_voice_profile_match_score: int = 0
    account_voice_profile_match_reason: str = ""
    account_mask_version: int = 0
    account_mask_summary: str = ""
    account_mask_match_score: int = 0
    account_mask_match_reason: str = ""
    stance_summary: str = ""
    topic_thread: str = ""
    topic_plan: str = ""
    intent: str = ""
    act_type: str = ""
    generation_source: str = ""
    quality_decision: str = ""
    quality_fallback: str = ""
    ai_message_memory_id: str = ""
    semantic_cluster: str = ""
    content: str = ""
    reply_to_message_id: int | None = None
    reply_target_label: str = ""
    reply_target_author: str = ""
    reply_target_preview: str = ""
    reply_target_source: str = ""
    material_intent: str = ""
    material_matched_tags: list[str] = Field(default_factory=list)
    material_candidate_count: int = 0
    material_id: int | None = None
    material_failure_reason: str = ""
    status: str
    scheduled_at: datetime
    executed_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class TaskAICycleOut(BaseModel):
    cycle_id: str
    context_message_ids: list[int] = Field(default_factory=list)
    stats: dict[str, Any] = Field(default_factory=dict)
    turns: list[TaskAITurnOut] = Field(default_factory=list)


class TaskAIGenerationRecordOut(BaseModel):
    generation_id: str
    cycle_id: str
    status: str = ""
    generation_source: str = ""
    generated_count: int = 0
    token_count: int = 0
    context_message_count: int = 0
    account_memory_count: int = 0
    profile_scene: str = ""
    profile_version: int = 0
    profile_hit_summary: str = ""
    profile_unavailable_reason: str = ""
    anchor_message_ids: list[int] = Field(default_factory=list)
    quality_risks: list[str] = Field(default_factory=list)
    skip_reason: str = ""
    scheduled_at: datetime | None = None
    created_at: datetime | None = None


class TaskAIAccountProfileOut(BaseModel):
    account_id: int
    display_name: str = ""
    username: str | None = None
    status: str = ""
    total_success_count: int = 0
    current_task_success_count: int = 0
    cross_task_success_count: int = 0
    profile_summary: str = ""


class TaskRelayItemOut(BaseModel):
    action_id: str
    relay_event_id: str = ""
    source_event_key: str = ""
    source_group_id: int | None = None
    source_operation_target_id: int | None = None
    operation_target_id: int | None = None
    source_info: str = ""
    source_group_title: str = ""
    source_sender_name: str = ""
    source_sender_peer_id: str = ""
    source_sender_username: str = ""
    source_sender_role: str = ""
    source_is_bot: bool = False
    source_filter_reason: str = ""
    source_remote_message_id: str = ""
    source_message_type: str = ""
    source_sent_at: datetime | None = None
    target_display: str = ""
    original_text: str = ""
    transformed_text: str = ""
    material_fingerprint: str = ""
    rule_set_id: int | None = None
    rule_set_name: str = ""
    rule_set_version_id: int | None = None
    resolved_rule_set_version_id: int | None = None
    rule_set_version: int | None = None
    rule_binding_mode: str = ""
    rule_trace: dict[str, Any] = Field(default_factory=dict)
    account_id: int | None = None
    status: str
    retry_count: int = 0
    scheduled_at: datetime
    executed_at: datetime | None = None
    result: dict[str, Any] = Field(default_factory=dict)


class TaskRelayBatchOut(BaseModel):
    relay_batch_id: str
    stats: dict[str, Any] = Field(default_factory=dict)
    source_event_count: int = 0
    material_count: int = 0
    rule_version_count: int = 0
    items: list[TaskRelayItemOut] = Field(default_factory=list)


class TaskRelaySourceOut(BaseModel):
    source_group_id: int | None = None
    source_group_title: str = ""
    listener_account_id: int | None = None
    sender_peer_id: str = ""
    sender_name: str = ""
    sender_username: str = ""
    sender_role: str = ""
    is_bot: bool = False
    source_filter_reason: str = ""
    content: str = ""
    message_type: str = ""
    remote_message_id: str = ""
    sent_at: datetime | None = None


class TaskDetailOut(BaseModel):
    task: TaskOut
    actions: list[ActionOut]
    stats: dict[str, Any]
    pacing_summary: dict[str, Any] = Field(default_factory=dict)
    rank_deboost_exempt_group: SearchRankDeboostExemptGroupResponse | None = None
    task_runtime_summary: TaskRuntimeSummaryOut | None = None
    operation_plan_links: list[OperationPlanTaskLinkOut] = Field(default_factory=list)
    accounts: list[TaskDetailAccountOut] = Field(default_factory=list)
    membership_phase: dict[str, Any] = Field(default_factory=dict)
    membership_accounts: list[dict[str, Any]] = Field(default_factory=list)
    membership_admission_phase: dict[str, Any] = Field(default_factory=dict)
    membership_admission_items: list[dict[str, Any]] = Field(default_factory=list)
    account_coverage_items: list[TaskAccountCoverageItemOut] = Field(
        default_factory=list
    )
    message_groups: list[TaskMessageGroupOut] = Field(default_factory=list)
    ai_cycles: list[TaskAICycleOut] = Field(default_factory=list)
    ai_generation_records: list[TaskAIGenerationRecordOut] = Field(default_factory=list)
    ai_account_profiles: list[TaskAIAccountProfileOut] = Field(default_factory=list)
    ai_quality_funnel: dict[str, Any] = Field(default_factory=dict)
    account_online_summary: dict[str, Any] = Field(default_factory=dict)
    relay_batches: list[TaskRelayBatchOut] = Field(default_factory=list)
    recent_relay_sources: list[TaskRelaySourceOut] = Field(default_factory=list)
    profile_batch: dict[str, Any] | None = None
    account_security_batch: dict[str, Any] | None = None
    learning_profile_preview: dict[str, Any] = Field(default_factory=dict)
    ai_group_content_allocation: dict[str, Any] = Field(default_factory=dict)
    channel_comment_discussion: dict[str, Any] = Field(default_factory=dict)
    channel_comment_grounding: dict[str, Any] = Field(default_factory=dict)


class TaskMembershipItemOut(BaseModel):
    item_id: str
    latest_action_id: str
    account_id: int
    display_name: str = ""
    username: str = ""
    status: str
    phase: str
    can_send: bool = False
    target_id: int | None = None
    target_type: str = ""
    target_display: str = ""
    scheduled_at: datetime | None = None
    completed_at: datetime | None = None
    failure_type: str = ""
    failure_detail: str = ""
    manual_required: bool = False
    verification_task_id: int | None = None
    verification_status: str = ""
    verification_action: str = ""
    can_auto_resolve: bool = False
    challenge_question: str = ""
    recovery_bucket: str = ""
    recovery_label: str = ""
    recovery_action: str = ""
    operator_required: bool = False
    auto_retryable: bool = False
    account_replace_required: bool = False


class TaskAccountCoverageItemOut(BaseModel):
    id: str
    account_id: int
    display_name: str = ""
    username: str = ""
    coverage_date: date
    target_count: int
    confirmed_count: int
    state: str
    blocker_code: str = ""
    blocker_stage: str = ""
    blocker_detail: str = ""
    reserved_action_id: str | None = None
    last_action_id: str | None = None
    last_success_action_id: str | None = None
    last_remote_message_id: str = ""
    next_eligible_at: datetime | None = None
    next_decision_at: datetime | None = None
    recovery_path: str = ""
    targeted_at: datetime
    completed_at: datetime | None = None


class TaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_only: bool = True


class TaskStartRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start_operation_id: str = Field(min_length=8, max_length=120)
    replaces_start_operation_id: str | None = Field(
        default=None, min_length=8, max_length=120
    )
    replaces_start_operation_version: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def validate_replace_tuple(self) -> "TaskStartRequest":
        values = (
            self.replaces_start_operation_id,
            self.replaces_start_operation_version,
        )
        if (values[0] is None) != (values[1] is None):
            raise ValueError("replace_start_operation_tuple_incomplete")
        return self


class TaskActionReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def normalize_reason(self) -> "TaskActionReasonRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("操作原因不能为空")
        return self


class ChannelCommentGroundingEnrollmentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_config_revision: int = Field(ge=1)
    expected_lifecycle_epoch: int = Field(ge=1)
    group_binding_id: str = Field(min_length=1, max_length=36)
    enabled_at: datetime
    approval_reference: str = Field(min_length=1, max_length=160)


class ChannelCommentGroundingEnrollmentCloseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enrollment_id: str = Field(min_length=1, max_length=36)
    expected_config_revision: int = Field(ge=1)
    expected_lifecycle_epoch: int = Field(ge=1)
    closed_at: datetime
    approval_reference: str = Field(min_length=1, max_length=160)


class ChannelCommentGroundingEnrollmentOut(ApiModel):
    id: str
    task_id: str
    task_config_revision: int
    task_lifecycle_epoch: int
    enrollment_revision: int
    enabled_at: datetime
    contract_version: str
    group_binding_id: str
    group_binding_revision: int
    activation_hash: str
    enrollment_state: str
    closed_at: datetime | None = None


class GroupAIChatTaskPreviewRequest(GroupAIChatConfig):
    model_config = ConfigDict(extra="forbid")

    count: int = Field(default=3, ge=1, le=20)


class ChannelCommentTaskPreviewRequest(ChannelCommentConfig):
    model_config = ConfigDict(extra="forbid")

    message_content: str = ""
    count: int = Field(default=3, ge=1, le=20)


class GenerateTaskPreviewOut(BaseModel):
    previews: list[str]


class ChannelCapacityCheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: Literal["channel_view", "channel_like", "channel_comment"]
    account_config: AccountConfig = Field(default_factory=AccountConfig)
    target_per_message: int = Field(default=1, ge=1, le=10000)
    target_channel_id: int | None = None
    target_channel_name: str = ""
    message_scope: Literal[
        "all", "latest_n", "date_range", "specific", "dynamic_new"
    ] = "latest_n"
    message_count: int | None = Field(default=1, ge=1, le=500)
    date_from: datetime | None = None
    date_to: datetime | None = None
    message_ids: list[int] = Field(default_factory=list)


class ChannelCapacityCheckOut(BaseModel):
    effective_account_count: int
    target_per_message: int
    max_effective_per_message: int
    will_shortfall: bool
    warning_message: str = ""
    membership_summary: dict[str, Any] = Field(default_factory=dict)


class TaskPrecheckRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_type: TaskTypeValue
    payload: dict[str, Any] = Field(default_factory=dict)


class TaskPrecheckOut(ApiModel):
    task_type: str
    decision: Literal["allow", "warn", "block"]
    available_account_count: int
    candidate_account_count: int
    limited_account_count: int
    blocked_account_count: int
    target_ability: list[dict[str, Any]] = Field(default_factory=list)
    target_resolution: dict[str, Any] = Field(default_factory=dict)
    membership_summary: dict[str, Any] = Field(default_factory=dict)
    voice_profile_summary: dict[str, Any] = Field(default_factory=dict)
    ready_account_count: int = 0
    preparable_account_count: int = 0
    estimated_membership_actions: int = 0
    membership_warnings: list[str] = Field(default_factory=list)
    membership_subtask_preview: dict[str, Any] = Field(default_factory=dict)
    learning_profile_preview: dict[str, Any] = Field(default_factory=dict)
    hourly_round_curve: list[int] = Field(default_factory=list)
    current_hour_rounds: int = 0
    messages_per_round: int = 0
    max_actions_per_hour: int = 0
    estimated_hourly_capacity: int = 0
    round_capacity_explanation: str = ""
    hard_hourly_target: dict[str, Any] = Field(default_factory=dict)
    estimated_actions: int
    capacity_shortfall: int
    capacity_summary: dict[str, Any] = Field(default_factory=dict)
    rule_version: dict[str, Any] | None = None
    risk_hits: list[str] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    suggested_actions: list[str] = Field(default_factory=list)
    trace_id: str = ""


class RecommendTaskAccountsRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    selection_mode: Literal["all", "group", "manual"] = "all"
    account_group_id: int | None = None
    account_ids: list[int] = Field(default_factory=list)
    target_group_id: int | None = None
    limit: int = Field(default=50, ge=1, le=200)


class RecommendedTaskAccountOut(BaseModel):
    id: int
    display_name: str
    username: str = ""
    status: str
    reason: str = ""


class ReviewApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edited_content: str | None = None


class ReviewRejectRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = ""


__all__ = [
    "AccountConfig",
    "AccountGroupProxyBindingOut",
    "ActionOut",
    "ChannelCommentConfig",
    "ChannelCommentGroundingEnrollmentCloseRequest",
    "ChannelCommentGroundingEnrollmentOut",
    "ChannelCommentGroundingEnrollmentRequest",
    "ChannelCapacityCheckOut",
    "ChannelCapacityCheckRequest",
    "ChannelCommentTaskConfigUpdate",
    "ChannelCommentTaskPreviewRequest",
    "ChannelCommentTaskCreate",
    "ChannelLikeConfig",
    "ChannelLikeTaskConfigUpdate",
    "ChannelLikeTaskCreate",
    "ChannelViewConfig",
    "ChannelViewTaskConfigUpdate",
    "ChannelViewTaskCreate",
    "DEFAULT_CHANNEL_LIKE_ALLOWED_REACTIONS",
    "ExecutionAttemptOut",
    "FailurePolicy",
    "GenerateTaskPreviewOut",
    "GroupAIChatConfig",
    "GroupAIChatTaskConfigUpdate",
    "GroupAIChatTaskPreviewRequest",
    "GroupAIChatTaskCreate",
    "GroupMembershipAdmissionConfig",
    "GroupMembershipAdmissionPacingConfig",
    "GroupMembershipAdmissionTaskConfigUpdate",
    "GroupMembershipAdmissionTaskCreate",
    "GroupMembershipAdmissionTestMessageConfig",
    "GroupRelayConfig",
    "GroupRelayTaskConfigUpdate",
    "GroupRelayTaskCreate",
    "PacingConfig",
    "RecommendTaskAccountsRequest",
    "RecommendedTaskAccountOut",
    "RankDeboostProxyBindingDeleteRequest",
    "RankDeboostProxyBindingRequest",
    "ReviewApproveRequest",
    "ReviewQueueOut",
    "ReviewRejectRequest",
    "SearchJoinBotConfig",
    "SearchJoinGroupConfig",
    "SearchJoinGroupSimpleTaskCreate",
    "SearchJoinGroupTaskConfigUpdate",
    "SearchJoinGroupTaskCreate",
    "SearchJoinVisibilityAttribution",
    "SearchRankDeboostExemptGroupResponse",
    "SearchRankDeboostClickReservationOut",
    "SearchRankDeboostSimpleTaskCreate",
    "SearchRankDeboostTaskConfigUpdate",
    "SearchRankDeboostTaskCreate",
    "SearchClickSimpleTaskCreate",
    "SearchClickTaskConfigUpdate",
    "SearchClickTaskCreate",
    "TaskCreateCommon",
    "TaskAIAccountProfileOut",
    "TaskAICycleOut",
    "TaskAIGenerationRecordOut",
    "TaskAccountCoverageItemOut",
    "TaskDetailOut",
    "TaskMembershipItemOut",
    "TaskDetailAccountOut",
    "TaskAITurnOut",
    "TaskMessageGroupOut",
    "TaskRelayBatchOut",
    "TaskRelayItemOut",
    "TaskListGroupOut",
    "TaskListItemOut",
    "TaskListPageOut",
    "TaskListSummaryOut",
    "TaskOut",
    "TaskPrecheckOut",
    "TaskPrecheckRequest",
    "TaskRetryRequest",
    "TaskStartRequest",
    "TaskActionReasonRequest",
    "TaskSettingsUpdate",
    "TaskSourceFilterOverrideRequest",
    "TaskUpdate",
]
