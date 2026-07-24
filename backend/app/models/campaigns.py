from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import TaskStatus, now


class Campaign(Base):
    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    title: Mapped[str] = mapped_column(String(160))
    campaign_type: Mapped[str] = mapped_column(String(40))
    topic: Mapped[str] = mapped_column(String(240))
    send_window: Mapped[str] = mapped_column(String(80), default="10:00-22:00")
    intensity: Mapped[str] = mapped_column(String(40), default="轻度")
    ai_provider_id: Mapped[int | None] = mapped_column(ForeignKey("ai_providers.id"), nullable=True)
    prompt_template_id: Mapped[int | None] = mapped_column(ForeignKey("prompt_templates.id"), nullable=True)
    jitter_min_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    jitter_max_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batch_interval_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    respect_send_window: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    material_ids: Mapped[str] = mapped_column(Text, default="")
    target_group_ids: Mapped[str] = mapped_column(Text, default="")
    source_group_ids: Mapped[str] = mapped_column(Text, default="")
    selected_account_ids_by_group: Mapped[str] = mapped_column(Text, default="")
    execution_mode: Mapped[str] = mapped_column(String(40), default="manual_draft")
    run_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    max_ai_tokens: Mapped[int | None] = mapped_column(Integer, nullable=True, default=100000)
    used_ai_tokens: Mapped[int] = mapped_column(Integer, default=0)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    consecutive_failure_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")
    participation_min_ratio: Mapped[float] = mapped_column(Float, default=0.6)
    participation_max_ratio: Mapped[float] = mapped_column(Float, default=1.0)
    max_messages_per_account: Mapped[int] = mapped_column(Integer, default=2)
    max_drafts_per_batch: Mapped[int] = mapped_column(Integer, default=50)
    filtered_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.DRAFT.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class AiDraft(Base):
    __tablename__ = "ai_drafts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    persona: Mapped[str] = mapped_column(String(80))
    content: Mapped[str] = mapped_column(Text)
    risk_level: Mapped[str] = mapped_column(String(20), default="低")
    provider_name: Mapped[str] = mapped_column(String(100), default="Mock")
    model_name: Mapped[str] = mapped_column(String(120), default="mock")
    prompt_template_name: Mapped[str] = mapped_column(String(120), default="默认模板")
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id"), nullable=True)
    suggested_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    sequence_index: Mapped[int] = mapped_column(Integer, default=0)
    reply_to_draft_id: Mapped[int | None] = mapped_column(ForeignKey("ai_drafts.id"), nullable=True)
    generation_source: Mapped[str] = mapped_column(String(40), default="mock")
    generation_error: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.PENDING_REVIEW.value)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MessageTask(Base):
    __tablename__ = "message_tasks"
    __table_args__ = (
        UniqueConstraint("idempotency_key"),
        Index(
            "ix_message_tasks_account_occupied_at",
            "tenant_id",
            text("(coalesce(account_id, preferred_account_id))"),
            "status",
            text("(coalesce(sent_at, scheduled_at))"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), nullable=True)
    group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    draft_id: Mapped[int | None] = mapped_column(ForeignKey("ai_drafts.id"), nullable=True)
    content: Mapped[str] = mapped_column(Text)
    message_type: Mapped[str] = mapped_column(String(40), default="文本")
    material_id: Mapped[int | None] = mapped_column(ForeignKey("materials.id"), nullable=True)
    target_type: Mapped[str] = mapped_column(String(30), default="group")
    target_peer_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    target_display: Mapped[str] = mapped_column(String(160), default="")
    operation_target_id: Mapped[int | None] = mapped_column(ForeignKey("operation_targets.id"), nullable=True)
    target_reference_revision: Mapped[int | None] = mapped_column(Integer, nullable=True)
    target_reference_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    preferred_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    planned_delay_seconds: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(30), default=TaskStatus.QUEUED.value)
    idempotency_key: Mapped[str] = mapped_column(String(80))
    failure_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    failure_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    media_sent: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    media_failure_reason: Mapped[str] = mapped_column(String(80), default="")
    material_asset_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    material_cache_ready_status: Mapped[str] = mapped_column(String(40), default="")
    scheduled_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    gateway_call_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)

    @property
    def actual_account_changed(self) -> bool:
        return bool(self.preferred_account_id and self.account_id and self.preferred_account_id != self.account_id)


class MessageTaskAttempt(Base):
    __tablename__ = "message_task_attempts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[int] = mapped_column(ForeignKey("message_tasks.id"))
    account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    failure_type: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class CampaignProcessedMessage(Base):
    __tablename__ = "campaign_processed_messages"
    __table_args__ = (UniqueConstraint("campaign_id", "source_group_id", "source_remote_message_id", "target_group_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    campaign_id: Mapped[int] = mapped_column(ForeignKey("campaigns.id"))
    source_group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    source_remote_message_id: Mapped[str] = mapped_column(String(160))
    target_group_id: Mapped[int | None] = mapped_column(ForeignKey("tg_groups.id"), nullable=True)
    action: Mapped[str] = mapped_column(String(40), default="queued")
    reason: Mapped[str] = mapped_column(Text, default="")
    content: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class Material(Base):
    __tablename__ = "materials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    title: Mapped[str] = mapped_column(String(160))
    material_type: Mapped[str] = mapped_column(String(40))
    content: Mapped[str] = mapped_column(Text)
    tags: Mapped[str] = mapped_column(String(240), default="")
    review_status: Mapped[str] = mapped_column(String(40), default="已审核")
    source_kind: Mapped[str] = mapped_column(String(40), default="url")
    asset_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    asset_version_id: Mapped[int] = mapped_column(Integer, default=1)
    delivery_mode: Mapped[str] = mapped_column(String(40), default="download_reupload")
    emoji_asset_kind: Mapped[str] = mapped_column(String(40), default="")
    gateway_type: Mapped[str] = mapped_column(String(40), default="telethon")
    cache_ready_status: Mapped[str] = mapped_column(String(40), default="not_cached")
    last_cache_flood_wait_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    tg_cache_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    tg_cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    tg_cache_message_id: Mapped[str] = mapped_column(String(160), default="")
    tg_ref_version_id: Mapped[int] = mapped_column(Integer, default=1)
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    caption: Mapped[str] = mapped_column(Text, default="")
    last_cache_error: Mapped[str] = mapped_column(Text, default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class MaterialImportJob(Base):
    __tablename__ = "material_import_jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    source_filename: Mapped[str] = mapped_column(String(255), default="")
    import_type: Mapped[str] = mapped_column(String(40), default="zip")
    target_group_name: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(40), default="completed")
    total_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    skipped_count: Mapped[int] = mapped_column(Integer, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0)
    oversize_count: Mapped[int] = mapped_column(Integer, default=0)
    item_details: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MaterialGroup(Base):
    __tablename__ = "material_groups"
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_material_groups_tenant_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    name: Mapped[str] = mapped_column(String(160))
    group_type: Mapped[str] = mapped_column(String(40), default="")
    description: Mapped[str] = mapped_column(Text, default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MaterialAssetVersion(Base):
    __tablename__ = "material_asset_versions"
    __table_args__ = (UniqueConstraint("material_id", "asset_version_id", name="uq_material_asset_versions_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    asset_version_id: Mapped[int] = mapped_column(Integer)
    source_kind: Mapped[str] = mapped_column(String(40), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    asset_fingerprint: Mapped[str] = mapped_column(String(128), default="")
    file_name: Mapped[str] = mapped_column(String(255), default="")
    mime_type: Mapped[str] = mapped_column(String(120), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    width: Mapped[int] = mapped_column(Integer, default=0)
    height: Mapped[int] = mapped_column(Integer, default=0)
    caption: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


class MaterialTgRefVersion(Base):
    __tablename__ = "material_tg_ref_versions"
    __table_args__ = (UniqueConstraint("material_id", "tg_ref_version_id", name="uq_material_tg_ref_versions_version"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    material_id: Mapped[int] = mapped_column(ForeignKey("materials.id"))
    asset_version_id: Mapped[int] = mapped_column(Integer)
    tg_ref_version_id: Mapped[int] = mapped_column(Integer)
    cache_status: Mapped[str] = mapped_column(String(40), default="")
    tg_cache_account_id: Mapped[int | None] = mapped_column(ForeignKey("tg_accounts.id"), nullable=True)
    tg_cache_peer_id: Mapped[str] = mapped_column(String(160), default="")
    tg_cache_message_id: Mapped[str] = mapped_column(String(160), default="")
    gateway_type: Mapped[str] = mapped_column(String(40), default="")
    failure_reason: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(100), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now)


__all__ = [
    "Campaign",
    "AiDraft",
    "MessageTask",
    "MessageTaskAttempt",
    "CampaignProcessedMessage",
    "Material",
    "MaterialGroup",
    "MaterialImportJob",
    "MaterialAssetVersion",
    "MaterialTgRefVersion",
]
