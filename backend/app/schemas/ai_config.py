from __future__ import annotations
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from .api import ApiModel


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
    max_tokens: int | None = Field(default=None, ge=128, le=8192)


class TenantAiSettingOut(ApiModel):
    id: int
    tenant_id: int
    default_provider_id: int | None
    ai_enabled: bool
    fallback_to_mock: bool
    temperature: float
    max_tokens: int
    updated_at: datetime


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
    "MaterialCreate", "MaterialUpdate", "MaterialOut",
    "ContentKeywordRuleCreate", "ContentKeywordRuleUpdate", "ContentKeywordRuleOut",
    "AiUsageLedgerOut", "AiUsageSummaryOut",
]
