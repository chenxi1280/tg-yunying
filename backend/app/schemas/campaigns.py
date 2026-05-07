from __future__ import annotations
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class CampaignCreate(BaseModel):
    tenant_id: int = 1
    group_id: int
    title: str
    campaign_type: str
    topic: str
    send_window: str = "10:00-22:00"
    intensity: str = "轻度"
    ai_provider_id: int | None = None
    prompt_template_id: int | None = None
    jitter_min_seconds: int | None = Field(default=None, ge=0)
    jitter_max_seconds: int | None = Field(default=None, ge=0)
    batch_interval_seconds: int | None = Field(default=None, ge=0)
    respect_send_window: bool | None = None
    material_ids: str = ""
    target_group_ids: list[int] = Field(default_factory=list)
    selected_account_ids_by_group: dict[str, list[int]] = Field(default_factory=dict)


class ConversationContextMessage(BaseModel):
    sender_name: str = "真人用户"
    content: str
    sent_at: str | None = None
    account_id: int | None = None


class GenerateDraftsRequest(BaseModel):
    count: int = Field(default=4, ge=1, le=12)
    tone: str = "自然、像真实群成员聊天"
    persona_set: list[str] = Field(default_factory=lambda: ["老用户", "新用户", "客服", "活跃成员"])
    use_ai: bool = True
    fallback_to_mock: bool = False
    selected_account_ids_by_group: dict[str, list[int]] = Field(default_factory=dict)
    listener_account_id: int | None = None
    conversation_context: list[ConversationContextMessage] = Field(default_factory=list)


class ApproveDraftRequest(BaseModel):
    actor: str = "普通用户"


class ApproveAllRequest(BaseModel):
    actor: str = "普通用户"


class RetryTaskRequest(BaseModel):
    actor: str = "普通用户"
    dispatch_now: bool = True


class AiDraftUpdate(BaseModel):
    content: str | None = None
    risk_level: str | None = None
    suggested_account_id: int | None = None


class DirectMessageTaskCreate(BaseModel):
    account_id: int | None = None
    target_peer_id: str
    target_display: str = ""
    content: str
    material_id: int | None = None
    message_type: str = "文本"


class CampaignRecommendAccountsRequest(BaseModel):
    tenant_id: int = 1
    target_group_ids: list[int] = Field(default_factory=list)


# ── Output schemas ──

class CampaignOut(ApiModel):
    id: int
    tenant_id: int
    group_id: int
    title: str
    campaign_type: str
    topic: str
    send_window: str
    intensity: str
    ai_provider_id: int | None
    prompt_template_id: int | None
    jitter_min_seconds: int | None
    jitter_max_seconds: int | None
    batch_interval_seconds: int | None
    respect_send_window: bool | None
    material_ids: str
    target_group_ids: str
    selected_account_ids_by_group: str
    status: str
    created_at: datetime


class AiDraftOut(ApiModel):
    id: int
    tenant_id: int
    campaign_id: int
    group_id: int
    persona: str
    content: str
    risk_level: str
    provider_name: str
    model_name: str
    prompt_template_name: str
    material_id: int | None
    suggested_account_id: int | None = None
    suggested_account_name: str | None = None
    sequence_index: int = 0
    reply_to_draft_id: int | None = None
    generation_source: str
    generation_error: str
    status: str
    created_at: datetime


class MessageTaskOut(ApiModel):
    id: int
    tenant_id: int
    campaign_id: int | None
    group_id: int | None
    account_id: int | None
    draft_id: int | None
    content: str
    message_type: str
    material_id: int | None
    target_type: str
    target_peer_id: str | None
    target_display: str
    preferred_account_id: int | None = None
    preferred_account_name: str | None = None
    actual_account_changed: bool = False
    planned_delay_seconds: int
    status: str
    idempotency_key: str
    failure_type: str | None
    failure_detail: str | None
    scheduled_at: datetime
    sent_at: datetime | None
    created_at: datetime


# ── Composite output ──

class CampaignDetailOut(BaseModel):
    campaign: CampaignOut
    target_groups: list[Any]   # list[GroupOut]
    selected_accounts_by_group: dict[str, list[Any]]  # dict[str, list[AccountOut]]
    drafts: list[AiDraftOut]
    message_tasks: list[MessageTaskOut]
    stats: dict[str, Any]


__all__ = [
    "CampaignCreate", "ConversationContextMessage", "GenerateDraftsRequest", "ApproveDraftRequest",
    "ApproveAllRequest", "RetryTaskRequest", "AiDraftUpdate",
    "DirectMessageTaskCreate", "CampaignRecommendAccountsRequest",
    "CampaignOut", "AiDraftOut", "MessageTaskOut", "CampaignDetailOut",
]
