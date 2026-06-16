from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api import ApiModel
from .operation_plans import OperationPlanTaskLinkOut
from .runtime_summary import TaskRuntimeSummaryOut

TaskTypeValue = Literal["group_ai_chat", "group_relay", "channel_view", "channel_like", "channel_comment"]
TaskStatusValue = Literal["draft", "pending", "running", "paused", "target_reached", "wrapping_up", "completed", "stopped", "failed", "deleted"]
ActionStatusValue = Literal["pending", "executing", "success", "failed", "skipped"]
ReviewStatusValue = Literal["pending", "approved", "rejected", "expired"]
GROUP_AI_HARD_HOURLY_MIN_MESSAGES = 60


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = "02:00"
    end: str = "08:00"
    timezone: str = "Asia/Shanghai"


DEFAULT_HOURLY_ACTIVITY_CURVE = [2, 2, 1, 1, 0, 0, 1, 2, 4, 5, 6, 6, 5, 4, 6, 7, 8, 9, 10, 10, 8, 6, 4, 3]


class OperationProfile(BaseModel):
    model_config = ConfigDict(extra="forbid")

    template_id: str = "natural_full_day"
    source: Literal["built_in_default", "target_recommended", "manual"] = "built_in_default"
    hourly_activity_curve: list[int] = Field(default_factory=lambda: list(DEFAULT_HOURLY_ACTIVITY_CURVE))
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
    curve_type: Literal["front_heavy", "back_heavy", "random_burst", "steady"] | None = None
    curve_duration_hours: int | None = Field(default=None, ge=1, le=168)
    template: Literal["aggressive_1h", "moderate_6h", "gentle_24h", "burst_30min"] | None = "moderate_6h"
    jitter_percent: int = Field(default=30, ge=0, le=100)
    max_actions_per_hour: int | None = Field(default=None, ge=1)
    max_actions_per_day: int | None = Field(default=None, ge=1)
    quiet_hours: QuietHours | None = None

    @model_validator(mode="after")
    def normalize_fixed(self) -> "PacingConfig":
        if self.mode == "fixed":
            self.interval_seconds_min = 60 if self.interval_seconds_min is None else self.interval_seconds_min
            self.interval_seconds_max = self.interval_seconds_min if self.interval_seconds_max is None else self.interval_seconds_max
            if self.interval_seconds_max < self.interval_seconds_min:
                self.interval_seconds_max = self.interval_seconds_min
        if self.mode == "curve" and not self.curve_type:
            self.curve_type = "steady"
        if self.mode == "template" and not self.template:
            self.template = "moderate_6h"
        return self


class FailurePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_retries: int = Field(default=3, ge=0, le=10)
    retry_delay_seconds: int = Field(default=60, ge=0)
    retry_backoff: Literal["none", "linear", "exponential"] = "exponential"
    on_account_banned: Literal["skip_account", "pause_task", "stop_task"] = "skip_account"
    on_api_rate_limit: Literal["wait_and_retry", "skip", "pause"] = "wait_and_retry"
    on_content_rejected: Literal["skip_message", "rewrite_and_retry", "pause"] = "skip_message"
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
        if not self.group_id and not self.operation_target_id and not (self.target_input or "").strip():
            raise ValueError("source group requires group_id, operation_target_id or target_input")
        return self


class GroupAIChatConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_group_id: int | None = None
    target_operation_target_id: int | None = None
    target_type: Literal["group"] = "group"
    target_input: str | None = None
    target_title: str | None = None
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    target_group_name: str = ""
    topic_hint: str | None = None
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
    reply_min_per_round: int = Field(default=0, ge=0)
    hard_hourly_target_enabled: bool = True
    hourly_min_messages: int | None = Field(default=GROUP_AI_HARD_HOURLY_MIN_MESSAGES, ge=1)
    hard_hourly_strategy: Literal["force_planning"] = "force_planning"
    history_fetch_account_id: int | None = None
    auto_join_target: bool = True
    auto_follow_required_channel: bool = True
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
    fact_anchor_required: bool = True
    semantic_repeat_window: int = Field(default=10, ge=1, le=100)
    low_confidence_silence_enabled: bool = True

    @model_validator(mode="after")
    def validate_target_reference(self) -> "GroupAIChatConfig":
        if not self.target_group_id and not self.target_operation_target_id and not (self.target_input or "").strip():
            raise ValueError("target_group_id、target_operation_target_id 或 target_input 至少填写一个")
        if self.reply_min_per_round > self.messages_per_round:
            raise ValueError("reply_min_per_round 不能大于 messages_per_round")
        if not self.hard_hourly_target_enabled:
            raise ValueError("AI 活跃群必须启用每小时硬目标")
        if not self.hourly_min_messages:
            raise ValueError("AI 活跃群必须填写每小时最低发送量")
        if self.hourly_min_messages < GROUP_AI_HARD_HOURLY_MIN_MESSAGES:
            raise ValueError("AI 活跃群每小时最低发送量不能低于 60")
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
    content_mode: Literal["raw", "light_rewrite", "ai_rewrite", "summary"] = "light_rewrite"
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
        if not self.target_group_id and not self.target_group_ids and not self.target_operation_target_id and not self.target_operation_target_ids and not (self.target_input or "").strip():
            raise ValueError("target_group_id、target_group_ids、运营目标或 target_input 至少填写一个")
        if self.target_group_id and self.target_group_id not in self.target_group_ids:
            self.target_group_ids = [self.target_group_id, *self.target_group_ids]
        if self.target_operation_target_id and self.target_operation_target_id not in self.target_operation_target_ids:
            self.target_operation_target_ids = [self.target_operation_target_id, *self.target_operation_target_ids]
        self.require_review = False
        return self


class ChannelMessageScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_channel_id: int | None = None
    target_type: Literal["channel"] = "channel"
    target_input: str | None = None
    target_title: str | None = None
    target_channel_name: str = ""
    message_scope: Literal["all", "latest_n", "date_range", "specific", "dynamic_new"] = "latest_n"
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
            raise ValueError("message_scope=date_range 时 date_from/date_to 至少填写一个")
        return self


class ChannelViewConfig(ChannelMessageScopeConfig):
    message_scope: Literal["all", "latest_n", "date_range", "specific", "dynamic_new"] = "dynamic_new"
    initial_message_scope: Literal["latest_n", "today_new", "date_range", "specific", "new_only"] | None = None
    latest_message_count: int | None = Field(default=None, ge=1, le=500)
    listen_new_messages: bool = True
    per_message_daily_view_target: int | None = Field(default=None, ge=1, le=10000)
    per_message_total_view_target: int | None = Field(default=None, ge=1, le=100000)
    message_active_days: int = Field(default=3, ge=1, le=365)
    task_daily_view_safety_cap: int | None = Field(default=None, ge=1, le=100000)
    max_views_per_account_per_day: int | None = Field(default=None, ge=1, le=10000)
    target_views_per_message: int | None = Field(default=None, ge=1, le=10000)
    view_count_jitter: float = Field(default=0.2, ge=0, le=1)
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
            self.per_message_daily_view_target = legacy_target or 50
        if self.per_message_total_view_target is None:
            self.per_message_total_view_target = legacy_target or 300
        self.target_views_per_message = self.per_message_daily_view_target
        if self.per_message_total_view_target < self.per_message_daily_view_target:
            self.per_message_total_view_target = self.per_message_daily_view_target
        return self


class ChannelLikeConfig(ChannelMessageScopeConfig):
    message_scope: Literal["all", "latest_n", "date_range", "specific", "dynamic_new"] = "dynamic_new"
    target_likes_per_message: int = Field(default=50, ge=1, le=10000)
    like_count_jitter: float = Field(default=0.3, ge=0, le=1)
    reaction_type: Literal["random", "specific"] = "random"
    allowed_reactions: list[str] = Field(default_factory=lambda: ["👍"])
    max_likes_per_account_per_hour: int = Field(default=10, ge=1, le=1000)


class ChannelCommentConfig(ChannelMessageScopeConfig):
    message_scope: Literal["all", "latest_n", "date_range", "specific", "dynamic_new"] = "dynamic_new"
    target_comments_per_message: int = Field(default=10, ge=1, le=1000)
    comment_count_jitter: float = Field(default=0.3, ge=0, le=1)
    comment_mode: Literal["comment", "reply", "mixed"] = "comment"
    reply_to_message_ids: list[int] = Field(default_factory=list)
    reply_min_per_message: int = Field(default=0, ge=0)
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None
    ai_model: str = ""
    comment_style: Literal["relevant", "question", "praise", "discussion", "mixed"] = "mixed"
    topic_hint: str | None = None
    system_prompt_override: str | None = None
    language: str = "zh-CN"
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int = Field(default=3, ge=1, le=500)
    require_review: bool = False

    @model_validator(mode="after")
    def disable_manual_review(self) -> "ChannelCommentConfig":
        if self.comment_mode == "reply" and not self.reply_to_message_ids:
            raise ValueError("comment_mode=reply 时 reply_to_message_ids 必填")
        if self.reply_min_per_message > self.target_comments_per_message:
            raise ValueError("reply_min_per_message 不能大于 target_comments_per_message")
        self.require_review = False
        return self


class TaskCreateCommon(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    priority: int = Field(default=3, ge=1, le=5)
    timezone: str = "Asia/Shanghai"
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    max_duration_hours: int | None = Field(default=None, ge=1)
    account_config: AccountConfig = Field(default_factory=AccountConfig)
    pacing_config: PacingConfig = Field(default_factory=PacingConfig)
    failure_policy: FailurePolicy = Field(default_factory=FailurePolicy)


class GroupAIChatTaskCreate(TaskCreateCommon, GroupAIChatConfig):
    pass


class GroupRelayTaskCreate(TaskCreateCommon, GroupRelayConfig):
    pass


class ChannelViewTaskCreate(TaskCreateCommon, ChannelViewConfig):
    pass


class ChannelLikeTaskCreate(TaskCreateCommon, ChannelLikeConfig):
    pass


class ChannelCommentTaskCreate(TaskCreateCommon, ChannelCommentConfig):
    pass


class GroupAIChatTaskConfigUpdate(GroupAIChatConfig):
    pass


class GroupRelayTaskConfigUpdate(GroupRelayConfig):
    pass


class ChannelViewTaskConfigUpdate(ChannelViewConfig):
    pass


class ChannelLikeTaskConfigUpdate(ChannelLikeConfig):
    pass


class ChannelCommentTaskConfigUpdate(ChannelCommentConfig):
    pass


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int | None = Field(default=None, ge=1, le=5)
    timezone: str | None = None
    scheduled_start: datetime | None = None
    scheduled_end: datetime | None = None
    max_duration_hours: int | None = Field(default=None, ge=1)
    account_config: AccountConfig | None = None
    pacing_config: PacingConfig | None = None
    failure_policy: FailurePolicy | None = None


class TaskSettingsUpdate(TaskUpdate):
    model_config = ConfigDict(extra="forbid")

    topic_hint: str | None = None
    chat_history_depth: int | None = Field(default=None, ge=1, le=200)
    ai_model: str | None = None
    system_prompt_override: str | None = None
    slang_prompt_template_id: int | None = None
    slang_terms: dict[str, str] | None = None
    tone: Literal["casual", "professional", "mixed", "auto"] | None = None
    language: str | None = None
    max_message_length: int | None = Field(default=None, ge=1)
    participation_rate: float | None = Field(default=None, ge=0.01, le=1)
    participation_jitter: float | None = Field(default=None, ge=0, le=1)
    allow_account_repeat: bool | None = None
    repeat_cooldown_rounds: int | None = Field(default=None, ge=0)
    account_personas: dict[str, str] | None = None
    account_memory_depth: int | None = Field(default=None, ge=0, le=20)
    messages_per_round_mode: Literal["auto", "manual"] | None = None
    messages_per_round: int | None = Field(default=None, ge=1)
    reply_min_per_round: int | None = Field(default=None, ge=0)
    hard_hourly_target_enabled: bool | None = None
    hourly_min_messages: int | None = Field(default=None, ge=1)
    hard_hourly_strategy: Literal["force_planning"] | None = None
    history_fetch_account_id: int | None = None
    auto_join_target: bool | None = None
    auto_follow_required_channel: bool | None = None
    auto_resolve_verification: bool | None = None
    ai_assisted_verification: bool | None = None
    captcha_failure_policy: Literal["manual"] | None = None
    membership_max_concurrent: int | None = Field(default=None, ge=1, le=50)
    idle_continuation_enabled: bool | None = None
    idle_continuation_seconds: int | None = Field(default=None, ge=30, le=86400)
    context_expire_after_messages: int | None = Field(default=None, ge=0, le=500)
    fact_anchor_required: bool | None = None
    semantic_repeat_window: int | None = Field(default=None, ge=1, le=100)
    low_confidence_silence_enabled: bool | None = None
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None

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
    initial_message_scope: Literal["latest_n", "today_new", "date_range", "specific", "new_only"] | None = None
    latest_message_count: int | None = Field(default=None, ge=1, le=500)
    listen_new_messages: bool | None = None
    per_message_daily_view_target: int | None = Field(default=None, ge=1, le=10000)
    per_message_total_view_target: int | None = Field(default=None, ge=1, le=100000)
    message_active_days: int | None = Field(default=None, ge=1, le=365)
    task_daily_view_safety_cap: int | None = Field(default=None, ge=1, le=100000)
    max_views_per_account_per_day: int | None = Field(default=None, ge=1, le=10000)
    view_count_jitter: float | None = Field(default=None, ge=0, le=1)
    execution_mode: Literal["distribute", "burst"] | None = None

    target_likes_per_message: int | None = Field(default=None, ge=1, le=10000)
    like_count_jitter: float | None = Field(default=None, ge=0, le=1)
    reaction_type: Literal["random", "specific"] | None = None
    allowed_reactions: list[str] | None = None
    max_likes_per_account_per_hour: int | None = Field(default=None, ge=1, le=1000)

    target_comments_per_message: int | None = Field(default=None, ge=1, le=1000)
    comment_count_jitter: float | None = Field(default=None, ge=0, le=1)
    comment_mode: Literal["comment", "reply", "mixed"] | None = None
    reply_to_message_ids: list[int] | None = None
    reply_min_per_message: int | None = Field(default=None, ge=0)
    comment_style: Literal["relevant", "question", "praise", "discussion", "mixed"] | None = None
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int | None = Field(default=None, ge=1, le=500)


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
            raise ValueError("sender_peer_id、sender_username 或 sender_name 至少提供一个")
        if not any([self.source_action_id, self.source_action]):
            raise ValueError("source_action_id 或 source_action 至少提供一个")
        if not self.reason:
            raise ValueError("reason 不能为空")
        return self


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
    stats: dict[str, Any]
    runtime_stage: dict[str, Any] = Field(default_factory=dict)
    target_summary: str = ""
    search_text: str = ""
    created_at: datetime
    updated_at: datetime


class ActionOut(ApiModel):
    id: str
    tenant_id: int
    task_id: str
    task_type: str
    action_type: str
    account_id: int | None
    scheduled_at: datetime
    executed_at: datetime | None
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
    topic_thread: str = ""
    intent: str = ""
    content: str = ""
    reply_to_message_id: int | None = None
    reply_target_label: str = ""
    reply_target_author: str = ""
    reply_target_preview: str = ""
    reply_target_source: str = ""
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
    task_runtime_summary: TaskRuntimeSummaryOut | None = None
    operation_plan_links: list[OperationPlanTaskLinkOut] = Field(default_factory=list)
    accounts: list[TaskDetailAccountOut] = Field(default_factory=list)
    membership_phase: dict[str, Any] = Field(default_factory=dict)
    membership_accounts: list[dict[str, Any]] = Field(default_factory=list)
    message_groups: list[TaskMessageGroupOut] = Field(default_factory=list)
    ai_cycles: list[TaskAICycleOut] = Field(default_factory=list)
    ai_generation_records: list[TaskAIGenerationRecordOut] = Field(default_factory=list)
    ai_account_profiles: list[TaskAIAccountProfileOut] = Field(default_factory=list)
    relay_batches: list[TaskRelayBatchOut] = Field(default_factory=list)
    recent_relay_sources: list[TaskRelaySourceOut] = Field(default_factory=list)
    profile_batch: dict[str, Any] | None = None
    account_security_batch: dict[str, Any] | None = None
    learning_profile_preview: dict[str, Any] = Field(default_factory=dict)


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


class TaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_only: bool = True


class TaskActionReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def normalize_reason(self) -> "TaskActionReasonRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("操作原因不能为空")
        return self


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
    message_scope: Literal["all", "latest_n", "date_range", "specific", "dynamic_new"] = "latest_n"
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
    "ActionOut",
    "ChannelCommentConfig",
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
    "ExecutionAttemptOut",
    "FailurePolicy",
    "GenerateTaskPreviewOut",
    "GroupAIChatConfig",
    "GroupAIChatTaskConfigUpdate",
    "GroupAIChatTaskPreviewRequest",
    "GroupAIChatTaskCreate",
    "GroupRelayConfig",
    "GroupRelayTaskConfigUpdate",
    "GroupRelayTaskCreate",
    "PacingConfig",
    "RecommendTaskAccountsRequest",
    "RecommendedTaskAccountOut",
    "ReviewApproveRequest",
    "ReviewQueueOut",
    "ReviewRejectRequest",
    "TaskCreateCommon",
    "TaskAIAccountProfileOut",
    "TaskAICycleOut",
    "TaskAIGenerationRecordOut",
    "TaskDetailOut",
    "TaskMembershipItemOut",
    "TaskDetailAccountOut",
    "TaskAITurnOut",
    "TaskMessageGroupOut",
    "TaskRelayBatchOut",
    "TaskRelayItemOut",
    "TaskOut",
    "TaskPrecheckOut",
    "TaskPrecheckRequest",
    "TaskRetryRequest",
    "TaskActionReasonRequest",
    "TaskSettingsUpdate",
    "TaskSourceFilterOverrideRequest",
    "TaskUpdate",
]
