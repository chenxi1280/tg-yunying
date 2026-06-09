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
    listener_enabled: bool | None = None
    listener_auto_reply_enabled: bool | None = None
    listener_interval_seconds: int | None = Field(default=None, ge=30)
    listener_context_limit: int | None = Field(default=None, ge=1, le=100)
    listener_account_ids: list[int] | None = None


class AuthorizeGroupRequest(BaseModel):
    actor: str = "普通用户"
    auth_status: str = "已授权运营"


class VerificationTaskConfirmRequest(BaseModel):
    actor: str = "普通用户"


class VerificationTaskResponseRequest(BaseModel):
    actor: str = "普通用户"
    response_text: str = Field(min_length=1, max_length=500)


class VerificationChallengeMessageOut(ApiModel):
    message_id: int | str
    sender: str = ""
    text: str
    sent_at: datetime | None = None


class VerificationChallengeContextOut(ApiModel):
    task_id: int
    target_display: str
    target_peer_id: str = ""
    detected_reason: str = ""
    failure_detail: str = ""
    suggested_action: str = ""
    messages: list[VerificationChallengeMessageOut]


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
    listener_enabled: bool = False
    listener_auto_reply_enabled: bool = True
    listener_interval_seconds: int = 60
    listener_context_limit: int = 20
    listener_last_polled_at: datetime | None = None
    listener_last_reply_at: datetime | None = None
    listener_last_error: str = ""
    listener_account_ids: list[int] = []


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
    issue_scope: str = "account"
    issue_category: str = "verification"
    can_auto_resolve: bool = False
    requires_target_recheck: bool = False
    resolution_entry_label: str = "处理验证辅助"


class VerificationTaskBatchResolveOut(ApiModel):
    group_id: int
    target_peer_id: str
    target_display: str
    checked_count: int
    restored_count: int
    blocked_count: int
    failed_count: int
    approval_status: str
    approval_detail: str
    approval_account_id: int | None
    message: str
    tasks: list[VerificationTaskOut]


# ── Composite outputs (imports from other domain files handled lazily) ──

class GroupDetailOut(BaseModel):
    group: GroupOut
    accounts: list[dict[str, Any]]
    listener_accounts: list[dict[str, Any]] = []
    recent_context_messages: list[dict[str, Any]] = []
    recent_campaigns: list[Any]  # list[CampaignOut] — lazy to avoid circular import
    recent_archives: list[Any]   # list[ArchiveOut]
    verification_tasks: list[VerificationTaskOut] = []
    stats: dict[str, Any]


__all__ = [
    "GroupPolicyUpdate", "AuthorizeGroupRequest", "VerificationChallengeContextOut",
    "VerificationChallengeMessageOut", "VerificationTaskConfirmRequest", "VerificationTaskResponseRequest",
    "GroupOut", "VerificationTaskOut", "VerificationTaskBatchResolveOut", "GroupDetailOut",
]
