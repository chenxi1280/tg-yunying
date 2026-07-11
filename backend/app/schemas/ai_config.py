from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .api import ApiModel

DEFAULT_AI_MAX_TOKENS_LIMIT = 100000
MINIMAX_MAX_TOKENS_LIMIT = 250000


# ── AI Provider ──

class AiProviderCreate(BaseModel):
    provider_name: str
    provider_type: str = "openai_compatible"
    base_url: str = "mock://openai-compatible"
    model_name: str = "deepseek-v4-flash"
    api_key: str = Field(..., min_length=4)
    api_key_header: str = "Authorization"
    input_price_per_1k: float = Field(default=0, ge=0)
    output_price_per_1k: float = Field(default=0, ge=0)
    currency: str = "CNY"
    is_billable: bool = True
    is_active: bool = True
    notes: str = ""


class AiProviderUpdate(BaseModel):
    provider_name: str | None = None
    provider_type: str | None = None
    base_url: str | None = None
    model_name: str | None = None
    api_key: str | None = Field(default=None, min_length=4)
    api_key_header: str | None = None
    input_price_per_1k: float | None = Field(default=None, ge=0)
    output_price_per_1k: float | None = Field(default=None, ge=0)
    currency: str | None = None
    is_billable: bool | None = None
    is_active: bool | None = None
    notes: str | None = None


class AiProviderOut(ApiModel):
    id: int
    provider_name: str
    provider_type: str
    base_url: str
    model_name: str
    api_key_header: str
    input_price_per_1k: float
    output_price_per_1k: float
    currency: str
    is_billable: bool
    is_active: bool
    health_status: str
    last_check_at: datetime | None
    last_error: str
    notes: str
    created_at: datetime
    updated_at: datetime


# ── Prompt Templates ──

class PromptTemplateCreate(BaseModel):
    tenant_id: int | None = None
    template_type: str = "群活跃草稿"
    name: str
    content: str
    is_active: bool = True


class PromptTemplateUpdate(BaseModel):
    template_type: str | None = None
    name: str | None = None
    content: str | None = None
    is_active: bool | None = None


class PromptTemplateOut(ApiModel):
    id: int
    tenant_id: int | None
    template_type: str
    name: str
    content: str
    version: int
    is_active: bool
    created_at: datetime
    updated_at: datetime


# ── Tenant AI Settings ──

class TenantAiSettingUpdate(BaseModel):
    default_provider_id: int | None = None
    ai_enabled: bool | None = None
    fallback_to_mock: bool | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_tokens: int | None = Field(default=None, ge=128, le=MINIMAX_MAX_TOKENS_LIMIT)


class TenantAiSettingOut(ApiModel):
    id: int
    tenant_id: int
    default_provider_id: int | None
    ai_enabled: bool
    fallback_to_mock: bool
    temperature: float
    max_tokens: int
    updated_at: datetime


# ── AI Account Voice Profiles ──

class AiAccountVoiceProfileUpdate(BaseModel):
    mask_name: str | None = None
    audience_archetype: str | None = None
    identity_frame: str | None = None
    preference_tags: list[str] | None = None
    age_band: str | None = None
    persona_experiences: list[str] | None = None
    consumption_experiences: list[str] | None = None
    sentence_length: str | None = None
    interaction_habits: list[str] | None = None
    tone_strength: str | None = None
    lexical_preferences: list[str] | None = None
    emoji_policy: str | None = None
    forbidden_expressions: list[str] | None = None
    short_prompt_summary: str | None = None
    status: str | None = None
    quality_status: str | None = None


class AiAccountVoiceProfileBatchRebuildRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    missing_only: bool = True


class AiAccountVoiceProfileBatchItemOut(ApiModel):
    account_id: int
    status: str
    version: int = 0
    similarity_score: int | None = None
    failure_reason: str = ""
    skipped_reason: str = ""


class AiAccountVoiceProfileBatchRebuildOut(ApiModel):
    created: int = 0
    skipped: int = 0
    items: list[AiAccountVoiceProfileBatchItemOut] = Field(default_factory=list)


class AiAccountVoiceProfileBatchStatusRequest(BaseModel):
    account_ids: list[int] = Field(default_factory=list)
    status: str


class AiAccountVoiceProfileBatchStatusOut(ApiModel):
    updated: int = 0
    skipped: int = 0
    items: list[AiAccountVoiceProfileBatchItemOut] = Field(default_factory=list)


class AiAccountVoiceProfileRollbackRequest(BaseModel):
    source_version: int = Field(..., ge=1)


class AiAccountVoiceProfileOut(ApiModel):
    account_id: int
    display_name: str = ""
    username: str = ""
    phone_masked: str = ""
    account_status: str = ""
    profile_status: str = "missing"
    version: int = 0
    mask_name: str = ""
    audience_archetype: str = ""
    identity_frame: str = ""
    preference_tags: list[str] = Field(default_factory=list)
    age_band: str = ""
    persona_experiences: list[str] = Field(default_factory=list)
    consumption_experiences: list[str] = Field(default_factory=list)
    sentence_length: str = ""
    interaction_habits: list[str] = Field(default_factory=list)
    tone_strength: str = ""
    lexical_preferences: list[str] = Field(default_factory=list)
    emoji_policy: str = ""
    forbidden_expressions: list[str] = Field(default_factory=list)
    short_prompt_summary: str = ""
    quality_status: str = ""
    similarity_score: int | None = None
    updated_by: str = ""
    updated_at: datetime | None = None


class AiAccountVoiceProfileVersionOut(ApiModel):
    version: int
    status: str = ""
    source: str = ""
    mask_name: str = ""
    audience_archetype: str = ""
    identity_frame: str = ""
    preference_tags: list[str] = Field(default_factory=list)
    age_band: str = ""
    sentence_length: str = ""
    tone_strength: str = ""
    emoji_policy: str = ""
    short_prompt_summary: str = ""
    quality_status: str = ""
    similarity_score: int | None = None
    updated_by: str = ""
    updated_at: datetime | None = None


class AiAccountVoiceProfileAuditOut(ApiModel):
    id: int
    actor: str = ""
    action: str = ""
    detail: str = ""
    created_at: datetime | None = None


# ── Scheduling ──

class SchedulingSettingUpdate(BaseModel):
    jitter_min_seconds: int | None = Field(default=None, ge=0)
    jitter_max_seconds: int | None = Field(default=None, ge=0)
    batch_interval_seconds: int | None = Field(default=None, ge=0)
    respect_send_window: bool | None = None
    quiet_hours_enabled: bool | None = None
    quiet_start: str | None = None
    quiet_end: str | None = None
    quiet_timezone: str | None = None
    default_max_retries: int | None = Field(default=None, ge=0, le=10)
    default_retry_delay_seconds: int | None = Field(default=None, ge=0)
    default_retry_backoff: Literal["none", "linear", "exponential"] | None = None
    default_on_account_banned: Literal["skip_account", "pause_task", "stop_task"] | None = None
    default_on_api_rate_limit: Literal["wait_and_retry", "skip", "pause"] | None = None
    default_on_content_rejected: Literal["skip_message", "rewrite_and_retry", "pause"] | None = None
    default_account_hour_limit: int | None = Field(default=None, ge=0)
    default_account_day_limit: int | None = Field(default=None, ge=0)
    default_account_cooldown_seconds: int | None = Field(default=None, ge=0)


class SchedulingSettingOut(ApiModel):
    id: int
    tenant_id: int | None
    jitter_min_seconds: int
    jitter_max_seconds: int
    batch_interval_seconds: int
    respect_send_window: bool
    quiet_hours_enabled: bool
    quiet_start: str
    quiet_end: str
    quiet_timezone: str
    default_max_retries: int
    default_retry_delay_seconds: int
    default_retry_backoff: str
    default_on_account_banned: str
    default_on_api_rate_limit: str
    default_on_content_rejected: str
    default_account_hour_limit: int
    default_account_day_limit: int
    default_account_cooldown_seconds: int
    updated_at: datetime


# ── Materials ──

class MaterialCreate(BaseModel):
    tenant_id: int = 1
    title: str
    material_type: str = "文本"
    content: str
    tags: str = ""
    review_status: str = "已审核"
    source_kind: str = "url"
    asset_fingerprint: str = ""
    delivery_mode: str = "download_reupload"
    emoji_asset_kind: str = ""
    gateway_type: str = "telethon"
    cache_ready_status: str = "not_cached"
    tg_cache_account_id: int | None = None
    tg_cache_peer_id: str = ""
    tg_cache_message_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int = 0
    width: int = 0
    height: int = 0
    caption: str = ""


class MaterialUpdate(BaseModel):
    title: str | None = None
    material_type: str | None = None
    content: str | None = None
    tags: str | None = None
    review_status: str | None = None
    source_kind: str | None = None
    asset_fingerprint: str | None = None
    delivery_mode: str | None = None
    emoji_asset_kind: str | None = None
    gateway_type: str | None = None
    cache_ready_status: str | None = None
    tg_cache_account_id: int | None = None
    tg_cache_peer_id: str | None = None
    tg_cache_message_id: str | None = None
    file_name: str | None = None
    mime_type: str | None = None
    file_size: int | None = None
    width: int | None = None
    height: int | None = None
    caption: str | None = None


class MaterialReferenceSummary(ApiModel):
    message_task_count: int = 0
    action_count: int = 0
    rule_version_count: int = 0
    operation_plan_count: int = 0
    account_profile_batch_count: int = 0
    total_count: int = 0


class MaterialActionRequest(BaseModel):
    reason: str = ""


class MaterialOut(ApiModel):
    id: int
    tenant_id: int
    title: str
    material_type: str
    content: str
    tags: str
    review_status: str
    source_kind: str
    asset_fingerprint: str
    asset_version_id: int
    delivery_mode: str
    emoji_asset_kind: str
    gateway_type: str
    cache_ready_status: str
    last_cache_flood_wait_until: datetime | None
    tg_cache_account_id: int | None
    tg_cache_peer_id: str
    tg_cache_message_id: str
    tg_ref_version_id: int
    file_name: str
    mime_type: str
    file_size: int
    width: int
    height: int
    caption: str
    last_cache_error: str
    usage_count: int
    last_used_at: datetime | None
    referenced_by_count: int = 0
    reference_summary: MaterialReferenceSummary = Field(default_factory=MaterialReferenceSummary)


class MaterialAssetVersionOut(ApiModel):
    id: int
    material_id: int
    asset_version_id: int
    source_kind: str
    content: str
    asset_fingerprint: str
    file_name: str
    mime_type: str
    file_size: int
    width: int
    height: int
    caption: str
    created_by: str
    created_at: datetime | None = None


class MaterialTgRefVersionOut(ApiModel):
    id: int
    material_id: int
    asset_version_id: int
    tg_ref_version_id: int
    cache_status: str
    tg_cache_account_id: int | None
    tg_cache_peer_id: str
    tg_cache_message_id: str
    gateway_type: str
    failure_reason: str
    created_by: str
    created_at: datetime | None = None


class MaterialVersionHistoryOut(ApiModel):
    material_id: int
    asset_versions: list[MaterialAssetVersionOut]
    tg_ref_versions: list[MaterialTgRefVersionOut]


class MaterialReferenceItemOut(ApiModel):
    source_type: str
    source_id: str
    title: str = ""
    status: str = ""


class MaterialReferencesOut(ApiModel):
    material_id: int
    summary: MaterialReferenceSummary
    items: list[MaterialReferenceItemOut] = Field(default_factory=list)


class MaterialGroupCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=160)
    group_type: str = ""
    description: str = ""
    is_active: bool = True


class MaterialGroupUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=160)
    group_type: str | None = None
    description: str | None = None
    is_active: bool | None = None


class MaterialGroupOut(ApiModel):
    id: int
    tenant_id: int
    name: str
    group_type: str
    description: str
    is_active: bool
    material_count: int = 0
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MaterialCacheStatusCount(ApiModel):
    status: str
    count: int


class MaterialCacheErrorItem(ApiModel):
    scope: str
    id: str
    title: str
    status: str
    reason: str


class MaterialCacheHealthOut(ApiModel):
    material_cache_peer_configured: bool
    source_media_cache_peer_configured: bool
    active_cache_account_count: int
    material_status_counts: list[MaterialCacheStatusCount]
    source_media_status_counts: list[MaterialCacheStatusCount]
    material_oldest_pending_at: datetime | None
    source_media_oldest_pending_at: datetime | None
    flood_wait_count: int
    cache_failed_count: int
    waiting_action_count: int
    recent_errors: list[MaterialCacheErrorItem]


class CacheChannelConfigOut(ApiModel):
    raw_input: str = ""
    normalized_peer: str = ""
    source: Literal["saved", "env", "empty"] = "empty"
    last_error: str = ""


class CacheExecutionAccountOut(ApiModel):
    id: int
    display_name: str
    username: str | None = None
    phone_masked: str = ""
    status: str = ""
    health_score: float = 0


class MaterialCacheConfigUpdate(BaseModel):
    material_cache_input: str | None = None
    source_media_cache_input: str | None = None
    material_cache_account_id: int | None = None


class MaterialCacheConfigOut(ApiModel):
    material_cache: CacheChannelConfigOut
    source_media_cache: CacheChannelConfigOut
    cache_account: CacheExecutionAccountOut | None = None
    health: MaterialCacheHealthOut


class MaterialImportItemOut(ApiModel):
    file_name: str
    status: Literal["created", "skipped", "failed"]
    reason: str = ""
    material_id: int | None = None
    file_size: int = 0


class MaterialImportResultOut(ApiModel):
    import_id: str
    source_filename: str
    import_type: str
    target_group_name: str
    status: str
    total_count: int
    success_count: int
    failed_count: int
    skipped_count: int
    duplicate_count: int
    oversize_count: int
    items: list[MaterialImportItemOut]
    created_at: datetime | None = None
    updated_at: datetime | None = None


# ── Content keyword rules ──

class ContentKeywordRuleCreate(BaseModel):
    tenant_id: int = 1
    keyword: str = Field(..., min_length=1, max_length=160)
    match_type: str = "contains"
    is_active: bool = True
    note: str = ""


class ContentKeywordRuleUpdate(BaseModel):
    keyword: str | None = Field(default=None, min_length=1, max_length=160)
    match_type: str | None = None
    is_active: bool | None = None
    note: str | None = None


class ContentKeywordRuleOut(ApiModel):
    id: int
    tenant_id: int
    keyword: str
    match_type: str
    is_active: bool
    note: str
    created_at: datetime
    updated_at: datetime


# ── AI Usage ──

class AiUsageLedgerOut(ApiModel):
    id: int
    tenant_id: int
    user_id: int
    campaign_id: int | None
    group_id: int | None
    provider_id: int | None
    provider_name: str
    model_name: str
    prompt_template_id: int | None
    request_type: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_unit_price: float
    output_unit_price: float
    total_cost: float
    currency: str
    billable: bool
    request_status: str
    error_detail: str
    created_at: datetime


class AiUsageSummaryOut(BaseModel):
    total_requests: int
    successful_requests: int
    failed_requests: int
    billable_requests: int
    total_prompt_tokens: int
    total_completion_tokens: int
    total_tokens: int
    total_cost: float
    currency: str
    by_user: list[dict[str, Any]]


__all__ = [
    "AiProviderCreate", "AiProviderUpdate", "AiProviderOut",
    "PromptTemplateCreate", "PromptTemplateUpdate", "PromptTemplateOut",
    "TenantAiSettingUpdate", "TenantAiSettingOut",
    "SchedulingSettingUpdate", "SchedulingSettingOut",
    "MaterialActionRequest", "MaterialCreate", "MaterialReferenceSummary", "MaterialUpdate", "MaterialOut",
    "CacheChannelConfigOut", "CacheExecutionAccountOut", "MaterialCacheConfigOut", "MaterialCacheConfigUpdate",
    "MaterialImportItemOut", "MaterialImportResultOut",
    "MaterialCacheErrorItem", "MaterialCacheHealthOut", "MaterialCacheStatusCount",
    "ContentKeywordRuleCreate", "ContentKeywordRuleUpdate", "ContentKeywordRuleOut",
    "AiUsageLedgerOut", "AiUsageSummaryOut",
]
