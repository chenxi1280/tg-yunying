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


__all__ = ["AiProvider", "PromptTemplate", "TenantAiSetting", "SchedulingSetting", "AiUsageLedger"]
