from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import AiProviderHealthStatus, now


class AiProvider(Base):
    __tablename__ = "ai_providers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    provider_name: Mapped[str] = mapped_column(String(100))
    provider_type: Mapped[str] = mapped_column(String(40), default="openai_compatible")
    base_url: Mapped[str] = mapped_column(String(300))
    model_name: Mapped[str] = mapped_column(String(120))
    api_key_ciphertext: Mapped[str] = mapped_column(Text)
    api_key_header: Mapped[str] = mapped_column(String(80), default="Authorization")
    input_price_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    output_price_per_1k: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    is_billable: Mapped[bool] = mapped_column(Boolean, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    health_status: Mapped[str] = mapped_column(String(30), default=AiProviderHealthStatus.HEALTHY.value)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class PromptTemplate(Base):
    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    template_type: Mapped[str] = mapped_column(String(60))
    name: Mapped[str] = mapped_column(String(120))
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TenantAiSetting(Base):
    __tablename__ = "tenant_ai_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    default_provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    ai_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    fallback_to_mock: Mapped[bool] = mapped_column(Boolean, default=False)
    temperature: Mapped[float] = mapped_column(Float, default=0.8)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class SchedulingSetting(Base):
    __tablename__ = "scheduling_settings"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int | None] = mapped_column(ForeignKey("tenants.id"), nullable=True)
    jitter_min_seconds: Mapped[int] = mapped_column(Integer, default=15)
    jitter_max_seconds: Mapped[int] = mapped_column(Integer, default=180)
    batch_interval_seconds: Mapped[int] = mapped_column(Integer, default=45)
    respect_send_window: Mapped[bool] = mapped_column(Boolean, default=True)
    quiet_hours_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    quiet_start: Mapped[str] = mapped_column(String(16), default="02:00")
    quiet_end: Mapped[str] = mapped_column(String(16), default="08:00")
    quiet_timezone: Mapped[str] = mapped_column(String(64), default="Asia/Shanghai")
    default_max_retries: Mapped[int] = mapped_column(Integer, default=3)
    default_retry_delay_seconds: Mapped[int] = mapped_column(Integer, default=60)
    default_retry_backoff: Mapped[str] = mapped_column(String(20), default="exponential")
    default_on_account_banned: Mapped[str] = mapped_column(String(30), default="skip_account")
    default_on_api_rate_limit: Mapped[str] = mapped_column(String(30), default="wait_and_retry")
    default_on_content_rejected: Mapped[str] = mapped_column(String(30), default="skip_message")
    default_account_hour_limit: Mapped[int] = mapped_column(Integer, default=0)
    default_account_day_limit: Mapped[int] = mapped_column(Integer, default=0)
    default_account_cooldown_seconds: Mapped[int] = mapped_column(Integer, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MaterialCacheConfig(Base):
    __tablename__ = "material_cache_configs"
    __table_args__ = (UniqueConstraint("tenant_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    material_cache_input: Mapped[str] = mapped_column(String(300), default="")
    material_cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    source_media_cache_input: Mapped[str] = mapped_column(String(300), default="")
    source_media_cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    material_cache_last_error: Mapped[str] = mapped_column(Text, default="")
    source_media_cache_last_error: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AiUsageLedger(Base):
    __tablename__ = "ai_usage_ledgers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    provider_name: Mapped[str] = mapped_column(String(100), default="")
    model_name: Mapped[str] = mapped_column(String(120), default="")
    prompt_template_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True)
    request_type: Mapped[str] = mapped_column(String(60), default="campaign_draft_generation")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, default=0)
    input_unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    output_unit_price: Mapped[float] = mapped_column(Float, default=0.0)
    total_cost: Mapped[float] = mapped_column(Float, default=0.0)
    currency: Mapped[str] = mapped_column(String(16), default="CNY")
    billable: Mapped[bool] = mapped_column(Boolean, default=False)
    request_status: Mapped[str] = mapped_column(String(30), default="success")
    error_detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class ContentKeywordRule(Base):
    __tablename__ = "content_keyword_rules"
    __table_args__ = (UniqueConstraint("tenant_id", "keyword"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    keyword: Mapped[str] = mapped_column(String(160))
    match_type: Mapped[str] = mapped_column(String(40), default="contains")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    note: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "AiProvider",
    "PromptTemplate",
    "TenantAiSetting",
    "SchedulingSetting",
    "MaterialCacheConfig",
    "AiUsageLedger",
    "ContentKeywordRule",
]
