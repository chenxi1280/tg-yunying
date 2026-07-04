from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class TgAccountSecuritySnapshot(Base):
    __tablename__ = "tg_account_security_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"), unique=True)
    trusted_session_status: Mapped[str] = mapped_column(String(40), default="unknown")
    two_fa_status: Mapped[str] = mapped_column(String(40), default="unknown")
    two_fa_password_ciphertext: Mapped[str] = mapped_column(Text, default="")
    two_fa_password_hint: Mapped[str] = mapped_column(String(120), default="")
    two_fa_password_stored_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    external_authorization_count: Mapped[int] = mapped_column(Integer, default=0)
    last_device_scan_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_2fa_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    profile_status: Mapped[str] = mapped_column(String(40), default="unknown")
    profile_last_updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trusted_device_label: Mapped[str] = mapped_column(String(120), default="")
    last_hardened_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now, onupdate=now)


class TgAccountAuthorizationSnapshot(Base):
    __tablename__ = "tg_account_authorization_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_security_batches.id"), nullable=True)
    authorization_id: Mapped[int | None] = mapped_column(ForeignKey("tg_account_authorizations.id"), nullable=True)
    developer_app_id: Mapped[int | None] = mapped_column(ForeignKey("telegram_developer_apps.id"), nullable=True)
    session_role: Mapped[str] = mapped_column(String(24), default="")
    authorization_hash_ciphertext: Mapped[str] = mapped_column(Text, default="")
    is_platform_trusted: Mapped[bool] = mapped_column(Boolean, default=False)
    is_current_session: Mapped[bool] = mapped_column(Boolean, default=False)
    device_model: Mapped[str] = mapped_column(String(120), default="")
    platform: Mapped[str] = mapped_column(String(80), default="")
    system_version: Mapped[str] = mapped_column(String(80), default="")
    api_id: Mapped[int] = mapped_column(Integer, default=0)
    app_name: Mapped[str] = mapped_column(String(120), default="")
    app_version: Mapped[str] = mapped_column(String(80), default="")
    ip_masked: Mapped[str] = mapped_column(String(80), default="")
    country: Mapped[str] = mapped_column(String(80), default="")
    region: Mapped[str] = mapped_column(String(80), default="")
    date_created: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    date_active: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    status: Mapped[str] = mapped_column(String(40), default="active")
    scanned_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class TgAccountDeviceCleanupPrecheck(Base):
    __tablename__ = "tg_account_device_cleanup_prechecks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    precheck_id: Mapped[str] = mapped_column(String(80), unique=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    cleanup_authorization_hashes: Mapped[str] = mapped_column(Text, default="[]")
    cleanup_count: Mapped[int] = mapped_column(Integer, default=0)
    kept_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="ready")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    confirmed_by: Mapped[str] = mapped_column(String(100), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAccountSecurityBatch(Base):
    __tablename__ = "tg_account_security_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    action_types: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(40), default="draft")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(100), default="")
    confirmed_by: Mapped[str] = mapped_column(String(100), default="")
    confirm_text: Mapped[str] = mapped_column(String(120), default="")
    password_strategy: Mapped[str] = mapped_column(String(60), default="")
    password_secret_ref: Mapped[str] = mapped_column(String(160), default="")
    profile_strategy: Mapped[str] = mapped_column(Text, default="{}")
    username_strategy: Mapped[str] = mapped_column(Text, default="{}")
    avatar_strategy: Mapped[str] = mapped_column(Text, default="{}")
    overwrite_existing_profile: Mapped[bool] = mapped_column(Boolean, default=False)
    reason: Mapped[str] = mapped_column(String(255), default="")
    trace_id: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAccountSecurityBatchItem(Base):
    __tablename__ = "tg_account_security_batch_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tg_account_security_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    status: Mapped[str] = mapped_column(String(40), default="pending")
    precheck_status: Mapped[str] = mapped_column(String(40), default="pending")
    cleanup_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    device_cleanup_precheck_id: Mapped[str] = mapped_column(String(80), default="")
    two_fa_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    profile_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    username_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    avatar_status: Mapped[str] = mapped_column(String(40), default="not_requested")
    external_devices_before: Mapped[int] = mapped_column(Integer, default=0)
    external_devices_after: Mapped[int] = mapped_column(Integer, default=0)
    generated_display_name: Mapped[str] = mapped_column(String(120), default="")
    generated_first_name: Mapped[str] = mapped_column(String(80), default="")
    generated_last_name: Mapped[str] = mapped_column(String(80), default="")
    generated_bio: Mapped[str] = mapped_column(Text, default="")
    generated_username: Mapped[str] = mapped_column(String(120), default="")
    username_candidates: Mapped[str] = mapped_column(Text, default="[]")
    avatar_source: Mapped[str] = mapped_column(String(300), default="")
    skipped_reason: Mapped[str] = mapped_column(Text, default="")
    failure_type: Mapped[str] = mapped_column(String(80), default="")
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    trace_id: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class TgAccountProfileBatchRule(Base):
    __tablename__ = "tg_account_profile_batch_rules"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("tg_account_security_batches.id"))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    generation_mode: Mapped[str] = mapped_column(String(40), default="ai_random")
    ai_provider_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    ai_prompt_version: Mapped[str] = mapped_column(String(40), default="account_profile_v1")
    language_style: Mapped[str] = mapped_column(String(40), default="中文")
    persona_style: Mapped[str] = mapped_column(String(80), default="自然用户")
    gender_bias: Mapped[str] = mapped_column(String(40), default="不限")
    age_style: Mapped[str] = mapped_column(String(40), default="不限")
    forbidden_words: Mapped[str] = mapped_column(Text, default="")
    uniqueness_seed: Mapped[str] = mapped_column(String(80), default="")
    name_base: Mapped[str] = mapped_column(String(80), default="")
    name_start_index: Mapped[int] = mapped_column(Integer, default=1)
    name_padding: Mapped[int] = mapped_column(Integer, default=0)
    username_prefix: Mapped[str] = mapped_column(String(60), default="")
    username_start_index: Mapped[int] = mapped_column(Integer, default=1)
    username_padding: Mapped[int] = mapped_column(Integer, default=3)
    username_max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    bio_template: Mapped[str] = mapped_column(Text, default="")
    avatar_assignment_mode: Mapped[str] = mapped_column(String(40), default="none")
    overwrite_existing: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "TgAccountAuthorizationSnapshot",
    "TgAccountDeviceCleanupPrecheck",
    "TgAccountProfileBatchRule",
    "TgAccountSecurityBatch",
    "TgAccountSecurityBatchItem",
    "TgAccountSecuritySnapshot",
]
