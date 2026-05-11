from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api import ApiModel

TaskTypeValue = Literal["group_ai_chat", "group_relay", "channel_view", "channel_like", "channel_comment"]
TaskStatusValue = Literal["draft", "pending", "running", "paused", "completed", "failed"]
ActionStatusValue = Literal["pending", "executing", "success", "failed", "skipped"]
ReviewStatusValue = Literal["pending", "approved", "rejected", "expired"]


class QuietHours(BaseModel):
    model_config = ConfigDict(extra="forbid")

    start: str = "02:00"
    end: str = "08:00"
    timezone: str = "Asia/Shanghai"


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

    group_id: int
    group_name: str = ""
    is_active: bool = True


class GroupAIChatConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_group_id: int
    target_group_name: str = ""
    topic_hint: str | None = None
    chat_history_depth: int = Field(default=50, ge=1, le=200)
    ai_model: str = ""
    system_prompt_override: str | None = None
    tone: Literal["casual", "professional", "mixed", "auto"] = "auto"
    language: str = "zh-CN"
    max_message_length: int | None = Field(default=None, ge=1)
    participation_rate: float = Field(default=0.6, ge=0.01, le=1)
    participation_jitter: float = Field(default=0.5, ge=0, le=1)
    allow_account_repeat: bool = True
    repeat_cooldown_rounds: int = Field(default=2, ge=0)
    messages_per_round: int = Field(default=1, ge=1, le=10)
    history_fetch_account_id: int | None = None


class GroupRelayConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_groups: list[SourceGroup]
    monitor_account_ids: list[int] = Field(default_factory=list)
    filters: RelayFilters = Field(default_factory=RelayFilters)
    target_group_id: int
    send_account_ids: list[int] = Field(default_factory=list)
    content_mode: Literal["raw", "light_rewrite", "ai_rewrite", "summary"] = "light_rewrite"
    rewrite_prompt: str | None = None
    preserve_media: bool = False
    add_source_attribution: bool = False
    dedup_window_minutes: int = Field(default=60, ge=1, le=10080)
    dedup_method: Literal["hash", "semantic", "both"] = "hash"
    require_review: bool = False


class ChannelMessageScopeConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_channel_id: int
    target_channel_name: str = ""
    message_scope: Literal["all", "latest_n", "date_range", "specific"] = "latest_n"
    message_count: int | None = Field(default=10, ge=1, le=500)
    date_from: datetime | None = None
    date_to: datetime | None = None
    message_ids: list[int] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_message_scope(self) -> "ChannelMessageScopeConfig":
        if self.message_scope == "specific" and not self.message_ids:
            raise ValueError("message_scope=specific 时 message_ids 必填")
        if self.message_scope == "date_range" and not (self.date_from or self.date_to):
            raise ValueError("message_scope=date_range 时 date_from/date_to 至少填写一个")
        return self


class ChannelViewConfig(ChannelMessageScopeConfig):
    target_views_per_message: int = Field(default=50, ge=1, le=10000)
    view_count_jitter: float = Field(default=0.2, ge=0, le=1)
    execution_mode: Literal["distribute", "burst"] = "distribute"


class ChannelLikeConfig(ChannelMessageScopeConfig):
    target_likes_per_message: int = Field(default=50, ge=1, le=10000)
    like_count_jitter: float = Field(default=0.3, ge=0, le=1)
    reaction_type: Literal["random", "specific"] = "random"
    allowed_reactions: list[str] = Field(default_factory=lambda: ["👍"])
    max_likes_per_account_per_hour: int = Field(default=10, ge=1, le=1000)


class ChannelCommentConfig(ChannelMessageScopeConfig):
    target_comments_per_message: int = Field(default=10, ge=1, le=1000)
    comment_count_jitter: float = Field(default=0.3, ge=0, le=1)
    ai_model: str = ""
    comment_style: Literal["relevant", "question", "praise", "discussion", "mixed"] = "mixed"
    topic_hint: str | None = None
    system_prompt_override: str | None = None
    language: str = "zh-CN"
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int = Field(default=3, ge=1, le=500)
    require_review: bool = False


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
    tone: Literal["casual", "professional", "mixed", "auto"] | None = None
    language: str | None = None
    max_message_length: int | None = Field(default=None, ge=1)
    participation_rate: float | None = Field(default=None, ge=0.01, le=1)
    participation_jitter: float | None = Field(default=None, ge=0, le=1)
    allow_account_repeat: bool | None = None
    repeat_cooldown_rounds: int | None = Field(default=None, ge=0)
    messages_per_round: int | None = Field(default=None, ge=1, le=10)
    history_fetch_account_id: int | None = None

    monitor_account_ids: list[int] | None = None
    filters: RelayFilters | None = None
    content_mode: Literal["raw", "light_rewrite", "ai_rewrite", "summary"] | None = None
    rewrite_prompt: str | None = None
    preserve_media: bool | None = None
    add_source_attribution: bool | None = None
    dedup_window_minutes: int | None = Field(default=None, ge=1, le=10080)
    dedup_method: Literal["hash", "semantic", "both"] | None = None
    require_review: bool | None = None

    target_views_per_message: int | None = Field(default=None, ge=1, le=10000)
    view_count_jitter: float | None = Field(default=None, ge=0, le=1)
    execution_mode: Literal["distribute", "burst"] | None = None

    target_likes_per_message: int | None = Field(default=None, ge=1, le=10000)
    like_count_jitter: float | None = Field(default=None, ge=0, le=1)
    reaction_type: Literal["random", "specific"] | None = None
    allowed_reactions: list[str] | None = None
    max_likes_per_account_per_hour: int | None = Field(default=None, ge=1, le=1000)

    target_comments_per_message: int | None = Field(default=None, ge=1, le=1000)
    comment_count_jitter: float | None = Field(default=None, ge=0, le=1)
    comment_style: Literal["relevant", "question", "praise", "discussion", "mixed"] | None = None
    max_comment_length: int | None = Field(default=None, ge=1)
    max_comments_per_account_per_hour: int | None = Field(default=None, ge=1, le=500)


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
    message_url: str = ""
    content_preview: str = ""
    stats: dict[str, Any] = Field(default_factory=dict)
    actions: list[ActionOut] = Field(default_factory=list)


class TaskDetailOut(BaseModel):
    task: TaskOut
    actions: list[ActionOut]
    reviews: list[ReviewQueueOut]
    stats: dict[str, Any]
    accounts: list[TaskDetailAccountOut] = Field(default_factory=list)
    message_groups: list[TaskMessageGroupOut] = Field(default_factory=list)


class TaskRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    failed_only: bool = True


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

    task_type: Literal["channel_view", "channel_like"]
    account_config: AccountConfig = Field(default_factory=AccountConfig)
    target_per_message: int = Field(default=1, ge=1, le=10000)
    target_channel_id: int | None = None
    message_scope: Literal["all", "latest_n", "date_range", "specific"] = "latest_n"
    message_count: int | None = Field(default=1, ge=1, le=500)
    message_ids: list[int] = Field(default_factory=list)


class ChannelCapacityCheckOut(BaseModel):
    effective_account_count: int
    target_per_message: int
    max_effective_per_message: int
    will_shortfall: bool
    warning_message: str = ""


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
    "TaskDetailOut",
    "TaskDetailAccountOut",
    "TaskMessageGroupOut",
    "TaskOut",
    "TaskRetryRequest",
    "TaskSettingsUpdate",
    "TaskUpdate",
]
