from __future__ import annotations
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class GroupPolicyUpdate(BaseModel):
    auth_status: str | None = None
    active_window: str | None = None
    daily_limit: int | None = None
    account_cooldown_seconds: int | None = None
    group_cooldown_seconds: int | None = None
    topic_direction: str | None = None
    banned_words: str | None = None
    link_whitelist: str | None = None
    require_review: bool | None = None


class AuthorizeGroupRequest(BaseModel):
    actor: str = "普通用户"
    auth_status: str = "已授权运营"


class VerificationTaskConfirmRequest(BaseModel):
    actor: str = "普通用户"


class GroupOut(ApiModel):
    id: int
    tenant_id: int
    tg_peer_id: str
    title: str
    group_type: str
    member_count: int
    auth_status: str
    can_send: bool
    active_window: str
    daily_limit: int
    account_cooldown_seconds: int
    group_cooldown_seconds: int
    topic_direction: str
    banned_words: str
    link_whitelist: str
    require_review: bool


class VerificationTaskOut(ApiModel):
    id: int
    tenant_id: int
    account_id: int | None
    group_id: int | None
    message_task_id: int | None
    verification_type: str
    detected_reason: str
    suggested_action: str
    target_peer_id: str
    target_display: str
    requires_user_confirm: bool
    status: str
    failure_detail: str
    created_at: datetime
    handled_at: datetime | None


# ── Composite outputs (imports from other domain files handled lazily) ──

class GroupDetailOut(BaseModel):
    group: GroupOut
    accounts: list[dict[str, Any]]
    recent_campaigns: list[Any]  # list[CampaignOut] — lazy to avoid circular import
    recent_archives: list[Any]   # list[ArchiveOut]
    verification_tasks: list[VerificationTaskOut] = []
    stats: dict[str, Any]


__all__ = [
    "GroupPolicyUpdate", "AuthorizeGroupRequest", "VerificationTaskConfirmRequest",
    "GroupOut", "VerificationTaskOut", "GroupDetailOut",
]
