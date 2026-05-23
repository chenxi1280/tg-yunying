from __future__ import annotations
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .api import ApiModel
from .campaigns import MessageTaskOut
from .groups import VerificationTaskOut
from .operations import ManualOperationRecordOut, OperationTargetOut, OperationTaskAttemptOut


# ── Request schemas ──

class TgAccountCreate(BaseModel):
    tenant_id: int = 1
    pool_id: int | None = None
    display_name: str
    username: str | None = None
    phone_masked: str | None = None
    phone_number: str | None = None


class AccountPoolCreate(BaseModel):
    tenant_id: int = 1
    name: str
    description: str = ""
    is_default: bool = False


class AccountPoolUpdate(BaseModel):
    name: str | None = None
    description: str | None = None
    is_default: bool | None = None


class MoveAccountPoolRequest(BaseModel):
    pool_id: int


class TgAccountProfileUpdate(BaseModel):
    display_name: str
    tg_first_name: str = ""
    tg_last_name: str = ""
    tg_bio: str = Field(default="", max_length=220)
    avatar_object_key: str = ""


class LoginStartRequest(BaseModel):
    method: str = Field(pattern="^(code|qr)$")
    force: bool = False


class LoginVerifyRequest(BaseModel):
    code: str | None = None
    password_2fa: str | None = None


class SensitiveActionReasonRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def normalize_reason(self) -> "SensitiveActionReasonRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("操作原因不能为空")
        return self


class AccountClonePlanCreate(BaseModel):
    tenant_id: int = 1
    source_account_id: int
    target_account_id: int | None = None
    target_account_ids: list[int] = Field(default_factory=list)
    clone_scope: list[str] = Field(default_factory=lambda: ["contacts", "groups"])


# ── Output schemas ──

class AvatarUploadOut(BaseModel):
    object_key: str
    preview_url: str
    content_type: str
    size: int


class AccountOut(ApiModel):
    id: int
    tenant_id: int
    pool_id: int | None = None
    pool_name: str = "默认账号池"
    display_name: str
    username: str | None
    tg_first_name: str = ""
    tg_last_name: str = ""
    tg_bio: str = ""
    avatar_object_key: str = ""
    avatar_preview_url: str = ""
    profile_sync_status: str = "未同步"
    profile_sync_error: str = ""
    profile_synced_at: datetime | None = None
    phone_masked: str
    phone_number: str | None = None
    status: str
    health_score: float
    last_active_at: datetime | None
    created_at: datetime
    developer_app_id: int | None
    developer_app_name: str | None = None
    developer_api_id: int | None = None
    developer_app_health_status: str | None = None
    developer_app_version: int
    proxy_id: int | None = None
    proxy_name: str | None = None
    proxy_local_address: str | None = None
    proxy_status: str | None = None
    proxy_alert_status: str | None = None
    deleted_at: datetime | None = None
    deleted_by: str = ""
    delete_reason: str = ""


class AccountPoolOut(ApiModel):
    id: int
    tenant_id: int
    name: str
    description: str
    is_default: bool
    account_count: int = 0
    created_at: datetime
    updated_at: datetime


class LoginFlowOut(ApiModel):
    id: int
    account_id: int
    method: str
    status: str
    code_preview: str | None
    code_expires_at: datetime | None
    qr_payload: str | None
    created_at: datetime


class VerificationCodeOut(ApiModel):
    id: int
    account_id: int
    source: str
    code_preview: str | None
    expires_at: datetime | None
    viewed_by: str
    viewed_at: datetime | None
    status: str
    raw_hint: str
    created_at: datetime


class ProfileSyncRecordOut(ApiModel):
    id: int
    tenant_id: int
    account_id: int
    actor: str
    before_snapshot: str
    after_snapshot: str
    avatar_object_key: str
    status: str
    failure_type: str
    failure_detail: str
    remote_detail: str
    created_at: datetime
    synced_at: datetime | None


class AccountSyncRecordOut(ApiModel):
    id: int
    tenant_id: int
    account_id: int
    sync_type: str
    trigger_source: str
    status: str
    result_count: int
    failure_type: str
    failure_detail: str
    scheduled_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class ContactOut(ApiModel):
    id: int
    tenant_id: int
    account_id: int
    peer_id: str
    display_name: str
    username: str | None
    phone_masked: str
    phone_number: str | None = None
    contact_type: str
    is_mutual: bool
    last_message_at: datetime | None
    last_synced_at: datetime
    created_at: datetime


class AccountGroupOut(ApiModel):
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
    permission_label: str = "普通成员"
    account_can_send: bool = True
    last_sent_at: datetime | None = None


class AccountCloneItemOut(ApiModel):
    id: int
    tenant_id: int
    plan_id: int
    source_account_id: int
    target_account_id: int
    target_type: str
    target_peer_id: str
    target_display: str
    status: str
    failure_type: str
    failure_detail: str
    created_at: datetime
    executed_at: datetime | None


class AccountClonePlanOut(ApiModel):
    id: int
    tenant_id: int
    source_account_id: int
    target_account_id: int | None = None
    target_account_ids: list[int] = []
    target_accounts_summary: list[dict[str, Any]] = []
    clone_scope: str
    status: str
    items_total: int
    items_done: int
    items_failed: int
    failure_detail: str
    created_by: str
    created_at: datetime
    confirmed_at: datetime | None
    items: list[AccountCloneItemOut] = []
    items_by_target: dict[str, list[AccountCloneItemOut]] = {}


class RecommendedAccountOut(BaseModel):
    group_id: int
    group_title: str
    account_id: int
    account_name: str
    username: str | None
    health_score: float
    can_send: bool
    is_selectable: bool = True
    unavailable_reason: str | None = None
    cooldown_until: datetime | None = None
    recommended: bool
    reason: str


# ── Composite detail outputs ──

class AccountRiskDiagnosticOut(BaseModel):
    level: str
    code: str
    title: str
    detail: str
    source: str
    action: str
    occurred_at: datetime | None = None


class AccountDetailOut(BaseModel):
    account: AccountOut
    risk_diagnostics: list[AccountRiskDiagnosticOut] = []
    login_flows: list[LoginFlowOut]
    verification_codes: list[VerificationCodeOut]
    profile_sync_records: list[ProfileSyncRecordOut]
    sync_records: list[AccountSyncRecordOut] = []
    next_sync_at: datetime | None = None
    sync_due: bool = False
    sync_status_text: str = ""
    contacts: list[ContactOut]
    groups: list[AccountGroupOut]
    operation_targets: list[OperationTargetOut] = []
    message_records: list[MessageTaskOut]
    manual_operation_records: list[ManualOperationRecordOut] = []
    operation_task_attempts: list[OperationTaskAttemptOut] = []
    clone_plans: list[AccountClonePlanOut] = []
    verification_tasks: list[VerificationTaskOut] = []
    stats: dict[str, Any]


class AccountPoolDetailOut(BaseModel):
    pool: AccountPoolOut
    accounts: list[AccountOut]
    contacts: list[ContactOut]
    verification_tasks: list[VerificationTaskOut] = []
    clone_plans: list[AccountClonePlanOut] = []
    message_records: list[MessageTaskOut] = []
    stats: dict[str, Any]


__all__ = [
    "TgAccountCreate", "AccountPoolCreate", "AccountPoolUpdate",
    "MoveAccountPoolRequest", "TgAccountProfileUpdate",
    "LoginStartRequest", "LoginVerifyRequest", "SensitiveActionReasonRequest", "AccountClonePlanCreate",
    "AvatarUploadOut",
    "AccountOut", "AccountPoolOut", "LoginFlowOut", "VerificationCodeOut",
    "ProfileSyncRecordOut", "AccountSyncRecordOut",
    "ContactOut", "AccountGroupOut",
    "AccountCloneItemOut", "AccountClonePlanOut",
    "RecommendedAccountOut", "AccountRiskDiagnosticOut",
    "AccountDetailOut", "AccountPoolDetailOut",
]
