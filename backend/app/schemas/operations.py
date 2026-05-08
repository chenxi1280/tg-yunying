from __future__ import annotations

from datetime import datetime

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
    "ChannelMessageOut",
    "OperationTaskCreate",
    "OperationTaskOut",
    "OperationTaskAttemptOut",
    "ManualSendRequest",
    "ManualOperationRecordOut",
]
