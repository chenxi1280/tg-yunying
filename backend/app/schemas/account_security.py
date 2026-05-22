from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .api import ApiModel


class ProfileGenerationStrategy(BaseModel):
    generation_mode: str = "ai_random"
    language_style: str = "中文"
    persona_style: str = "自然用户"
    gender_bias: str = "不限"
    age_style: str = "不限"
    bio_enabled: bool = True
    username_enabled: bool = True
    username_prefix_hint: str = ""
    username_max_attempts: int = Field(default=3, ge=1, le=10)
    forbidden_words: list[str] = Field(default_factory=list)
    custom_prompt: str = Field(default="", max_length=1000)
    overwrite_existing: bool = False


class AvatarStrategy(BaseModel):
    mode: str = "none"
    material_group_id: int | None = None
    avatar_sources: list[str] = Field(default_factory=list)


class AccountSecurityProfileOverride(BaseModel):
    account_id: int
    generated_display_name: str = ""
    generated_first_name: str = ""
    generated_last_name: str = ""
    generated_bio: str = ""
    username_candidates: list[str] = Field(default_factory=list)
    avatar_source: str = ""


class AccountSecurityPrecheckRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    action_types: list[str] = Field(default_factory=list)
    password_strategy: str = ""
    profile_strategy: ProfileGenerationStrategy = Field(default_factory=ProfileGenerationStrategy)
    avatar_strategy: AvatarStrategy = Field(default_factory=AvatarStrategy)
    preview_overrides: list[AccountSecurityProfileOverride] = Field(default_factory=list)
    recovery_email: str = ""
    reason: str = ""


class AccountSecurityBatchCreate(AccountSecurityPrecheckRequest):
    confirm_text: str = ""


class AccountSecurityRetryRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)


class AccountAuthorizationSnapshotOut(ApiModel):
    id: int
    account_id: int
    batch_id: int | None = None
    authorization_hash_ciphertext: str = ""
    is_platform_trusted: bool
    is_current_session: bool
    device_model: str
    platform: str
    system_version: str
    api_id: int
    app_name: str
    app_version: str
    ip_masked: str
    country: str
    region: str
    date_created: datetime | None
    date_active: datetime | None
    status: str
    scanned_at: datetime


class AccountSecuritySnapshotOut(ApiModel):
    id: int
    account_id: int
    trusted_session_status: str
    two_fa_status: str
    external_authorization_count: int
    last_device_scan_at: datetime | None
    last_2fa_check_at: datetime | None
    profile_status: str
    profile_last_updated_at: datetime | None
    trusted_device_label: str
    last_hardened_at: datetime | None
    last_error: str
    trace_id: str
    created_at: datetime
    updated_at: datetime


class AccountSecurityPreviewItem(ApiModel):
    account_id: int
    account_name: str
    phone_masked: str
    phone_number: str | None = None
    session_status: str
    trusted_session_status: str
    external_authorization_count: int
    two_fa_status: str
    profile_status: str
    generated_display_name: str = ""
    generated_first_name: str = ""
    generated_last_name: str = ""
    generated_bio: str = ""
    username_candidates: list[str] = []
    avatar_source: str = ""
    precheck_status: str
    blockers: list[str] = []
    warnings: list[str] = []
    suggested_actions: list[str] = []


class AccountSecurityPrecheckOut(BaseModel):
    batch_preview_id: str
    summary: dict[str, int]
    items: list[AccountSecurityPreviewItem]
    action_types: list[str]
    trace_id: str


class AccountSecurityBatchItemOut(ApiModel):
    id: int
    batch_id: int
    tenant_id: int
    account_id: int
    status: str
    precheck_status: str
    cleanup_status: str
    two_fa_status: str
    profile_status: str
    username_status: str
    avatar_status: str
    external_devices_before: int
    external_devices_after: int
    generated_display_name: str
    generated_first_name: str
    generated_last_name: str
    generated_bio: str
    generated_username: str
    username_candidates: list[str] = []
    avatar_source: str
    skipped_reason: str
    failure_type: str
    failure_detail: str
    next_retry_at: datetime | None
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AccountSecurityBatchOut(ApiModel):
    id: int
    tenant_id: int
    action_types: list[str] = []
    status: str
    total_count: int
    success_count: int
    skipped_count: int
    failed_count: int
    created_by: str
    confirmed_by: str
    confirm_text: str
    password_strategy: str
    profile_strategy: dict[str, Any] = {}
    username_strategy: dict[str, Any] = {}
    avatar_strategy: dict[str, Any] = {}
    overwrite_existing_profile: bool
    reason: str
    trace_id: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    items: list[AccountSecurityBatchItemOut] = []


class AccountSecurityDetailOut(BaseModel):
    account_id: int
    snapshot: AccountSecuritySnapshotOut
    authorizations: list[AccountAuthorizationSnapshotOut]
    recent_batches: list[AccountSecurityBatchOut]


class AccountSecuritySummaryOut(BaseModel):
    total_accounts: int
    external_device_accounts: int
    missing_two_fa_accounts: int
    incomplete_profile_accounts: int
    recent_failed_batches: int
    pending_batches: int


__all__ = [
    "AccountAuthorizationSnapshotOut",
    "AccountSecurityBatchCreate",
    "AccountSecurityBatchItemOut",
    "AccountSecurityBatchOut",
    "AccountSecurityDetailOut",
    "AccountSecurityPrecheckOut",
    "AccountSecurityPrecheckRequest",
    "AccountSecurityProfileOverride",
    "AccountSecurityPreviewItem",
    "AccountSecurityRetryRequest",
    "AccountSecuritySnapshotOut",
    "AccountSecuritySummaryOut",
    "AvatarStrategy",
    "ProfileGenerationStrategy",
]
