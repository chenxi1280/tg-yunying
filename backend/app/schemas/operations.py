from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class OperationTargetCreate(BaseModel):
    tenant_id: int = 1
    target_type: str = Field(pattern="^(group|channel)$")
    tg_peer_id: str = Field(min_length=1, max_length=120)
    title: str = Field(min_length=1, max_length=180)
    username: str = ""
    member_count: int = Field(default=0, ge=0)
    can_send: bool = True
    auth_status: str = "未确认"


class OperationTargetUpdate(BaseModel):
    target_type: str | None = Field(default=None, pattern="^(group|channel)$")
    tg_peer_id: str | None = Field(default=None, min_length=1, max_length=120)
    title: str | None = Field(default=None, min_length=1, max_length=180)
    username: str | None = None
    member_count: int | None = Field(default=None, ge=0)
    can_send: bool | None = None
    auth_status: str | None = None
    active_window: str | None = Field(default=None, max_length=80)
    daily_limit: int | None = Field(default=None, ge=0)
    account_cooldown_seconds: int | None = Field(default=None, ge=0)
    group_cooldown_seconds: int | None = Field(default=None, ge=0)
    banned_words: str | None = None
    link_whitelist: str | None = None
    require_review: bool | None = None


class OperationTargetOut(ApiModel):
    id: int
    tenant_id: int
    target_type: str
    tg_peer_id: str
    title: str
    username: str
    member_count: int
    can_send: bool
    auth_status: str
    linked_group_id: int | None = None
    can_listen: bool = False
    can_archive: bool = False
    can_task: bool = False
    task_capabilities: list[str] = Field(default_factory=list)
    available_send_account_count: int = 0
    listener_account_count: int = 0
    last_sync_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ChannelMessageCreate(BaseModel):
    tenant_id: int = 1
    channel_target_id: int
    message_id: int = Field(ge=1)
    message_url: str = ""
    content_preview: str = ""
    published_at: datetime | None = None


class ChannelMessageOut(ApiModel):
    id: int
    tenant_id: int
    channel_target_id: int
    message_id: int
    message_url: str
    content_preview: str
    published_at: datetime | None
    created_at: datetime


class ChannelMessageCommentOut(ApiModel):
    id: int
    tenant_id: int
    channel_target_id: int
    channel_message_id: int
    comment_message_id: int
    parent_comment_message_id: int | None
    author_peer_id: str
    author_name: str
    content_preview: str
    reply_count: int
    published_at: datetime | None
    created_at: datetime


class ChannelMessageCommentSyncOut(BaseModel):
    inserted: int = 0
    comments: list[ChannelMessageCommentOut] = Field(default_factory=list)
    sync_error: str = ""


class OperationTargetAccountOut(BaseModel):
    id: int
    display_name: str
    username: str | None = None
    status: str
    health_score: float
    permission_label: str = ""
    can_send: bool = False
    is_listener: bool = False
    last_sent_at: datetime | None = None


class OperationTargetAccountUpdate(BaseModel):
    permission_label: str | None = Field(default=None, max_length=80)
    can_send: bool | None = None
    is_listener: bool | None = None


class OperationTargetGroupMessageOut(BaseModel):
    id: int
    listener_account_id: int
    sender_name: str
    content: str
    message_type: str
    sent_at: datetime | None = None
    used_for_ai: bool = False


class OperationTargetLinkedGroupOut(BaseModel):
    id: int
    title: str
    group_type: str
    member_count: int
    auth_status: str
    can_send: bool
    active_window: str = ""
    daily_limit: int = 0
    account_cooldown_seconds: int = 0
    group_cooldown_seconds: int = 0
    banned_words: str = ""
    link_whitelist: str = ""
    require_review: bool = True
    listener_enabled: bool
    listener_context_limit: int
    listener_last_error: str = ""


class OperationTargetTaskHistoryOut(BaseModel):
    id: str
    name: str
    type: str
    status: str
    success_count: int = 0
    failure_count: int = 0
    updated_at: datetime


class OperationTargetSendRecordOut(BaseModel):
    id: int
    content: str
    status: str
    account_id: int | None = None
    failure_detail: str = ""
    sent_at: datetime | None = None
    created_at: datetime


class OperationTargetArchiveRecordOut(BaseModel):
    id: int
    title: str
    status: str
    message_count: int = 0
    member_count: int = 0
    failure_detail: str = ""
    created_at: datetime


class OperationTargetRiskOut(BaseModel):
    level: str = "正常"
    messages: list[str] = Field(default_factory=list)


class OperationTargetDetailOut(BaseModel):
    target: OperationTargetOut
    linked_group: OperationTargetLinkedGroupOut | None = None
    accounts: list[OperationTargetAccountOut] = []
    group_messages: list[OperationTargetGroupMessageOut] = []
    channel_messages: list[ChannelMessageOut] = []
    channel_comments: list[ChannelMessageCommentOut] = []
    task_history: list[OperationTargetTaskHistoryOut] = Field(default_factory=list)
    send_records: list[OperationTargetSendRecordOut] = Field(default_factory=list)
    archive_records: list[OperationTargetArchiveRecordOut] = Field(default_factory=list)
    risk: OperationTargetRiskOut = Field(default_factory=OperationTargetRiskOut)
    sync_error: str = ""
    stats: dict[str, Any] = {}


class OperationTargetMessageSyncOut(BaseModel):
    inserted: int = 0
    detail: OperationTargetDetailOut


class OperationTargetsSyncFailureOut(BaseModel):
    account_id: int
    display_name: str
    error: str


class OperationTargetsSyncOut(BaseModel):
    synced_accounts: int = 0
    failed_accounts: list[OperationTargetsSyncFailureOut] = Field(default_factory=list)
    target_count: int = 0
    targets: list[OperationTargetOut] = Field(default_factory=list)


class OperationTaskCreate(BaseModel):
    tenant_id: int = 1
    task_type: str = Field(pattern="^(MESSAGE_SEND|CHANNEL_VIEW|CHANNEL_REACTION|CHANNEL_REPLY)$")
    target_id: int | None = None
    channel_message_id: int | None = None
    title: str = ""
    content: str = ""
    reaction: str = ""
    account_ids: list[int] = Field(default_factory=list)
    quantity: int = Field(default=1, ge=1, le=500)
    quantity_jitter_ratio: float = Field(default=0.15, ge=0, le=1)
    content_mode: str = Field(default="literal", pattern="^(literal|ai)$")
    interval_seconds: int = Field(default=0, ge=0)


class OperationTaskOut(ApiModel):
    id: int
    tenant_id: int
    task_type: str
    target_id: int | None
    channel_message_id: int | None
    title: str
    content: str
    reaction: str
    account_ids: str
    quantity: int
    actual_quantity: int
    quantity_jitter_ratio: int
    content_mode: str
    completed_count: int
    interval_seconds: int
    status: str
    failure_type: str
    failure_detail: str
    scheduled_at: datetime
    executed_at: datetime | None
    created_at: datetime


class OperationTaskAttemptOut(ApiModel):
    id: int
    tenant_id: int
    task_id: int
    account_id: int | None
    action_type: str
    content: str
    reaction: str
    status: str
    failure_type: str
    failure_detail: str
    remote_message_id: str
    idempotency_key: str
    planned_delay_seconds: int
    scheduled_at: datetime
    executed_at: datetime | None


class ManualSendRequest(BaseModel):
    target_id: int
    content: str = Field(min_length=1)


class ManualOperationRecordOut(ApiModel):
    id: int
    tenant_id: int
    account_id: int
    target_id: int | None
    operation_type: str
    content: str
    status: str
    failure_type: str
    failure_detail: str
    remote_message_id: str
    actor: str
    created_at: datetime


__all__ = [
    "OperationTargetCreate",
    "OperationTargetUpdate",
    "OperationTargetOut",
    "ChannelMessageCreate",
    "ChannelMessageCommentOut",
    "ChannelMessageCommentSyncOut",
    "ChannelMessageOut",
    "OperationTargetAccountOut",
    "OperationTargetAccountUpdate",
    "OperationTargetGroupMessageOut",
    "OperationTargetLinkedGroupOut",
    "OperationTargetDetailOut",
    "OperationTargetMessageSyncOut",
    "OperationTargetsSyncFailureOut",
    "OperationTargetsSyncOut",
    "OperationTaskCreate",
    "OperationTaskOut",
    "OperationTaskAttemptOut",
    "ManualSendRequest",
    "ManualOperationRecordOut",
]
