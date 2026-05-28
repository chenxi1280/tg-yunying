from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now
from .task_center import new_uuid


class TenantLearningProfile(Base):
    __tablename__ = "tenant_learning_profiles"
    __table_args__ = (UniqueConstraint("tenant_id", name="uq_tenant_learning_profiles_tenant"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(40), default="sample_insufficient")
    learning_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    style_summary: Mapped[str] = mapped_column(Text, default="")
    topic_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    phrase_patterns: Mapped[list] = mapped_column(JSON, default=list)
    reply_patterns: Mapped[list] = mapped_column(JSON, default=list)
    comment_patterns: Mapped[list] = mapped_column(JSON, default=list)
    forbidden_learning: Mapped[list] = mapped_column(JSON, default=list)
    source_sample_count: Mapped[int] = mapped_column(Integer, default=0)
    last_rebuilt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantLearningSource(Base):
    __tablename__ = "tenant_learning_sources"
    __table_args__ = (
        UniqueConstraint("tenant_id", "target_id", name="uq_tenant_learning_sources_target"),
        Index("ix_tenant_learning_sources_status", "tenant_id", "source_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    target_id: Mapped[int] = mapped_column(ForeignKey("operation_targets.id"))
    source_kind: Mapped[str] = mapped_column(String(40), default="group")
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    source_status: Mapped[str] = mapped_column(String(40), default="active")
    listener_account_ids: Mapped[list] = mapped_column(JSON, default=list)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_history_pull_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    watermark: Mapped[dict] = mapped_column(JSON, default=dict)
    last_failure_detail: Mapped[str] = mapped_column(Text, default="")
    selected_by: Mapped[str] = mapped_column(String(120), default="")
    selected_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TenantLearningSample(Base):
    __tablename__ = "tenant_learning_samples"
    __table_args__ = (
        UniqueConstraint("tenant_id", "source_id", "source_message_id", name="uq_tenant_learning_samples_message"),
        Index("ix_tenant_learning_samples_status", "tenant_id", "learning_status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    source_id: Mapped[str] = mapped_column(ForeignKey("tenant_learning_sources.id"))
    source_message_id: Mapped[str] = mapped_column(String(160), default="")
    source_scene: Mapped[str] = mapped_column(String(60), default="listener")
    sender_peer_id: Mapped[str] = mapped_column(String(160), default="")
    sender_username: Mapped[str] = mapped_column(String(160), default="")
    sender_name: Mapped[str] = mapped_column(String(180), default="")
    is_bot: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_text_hash: Mapped[str] = mapped_column(String(128), default="")
    text: Mapped[str] = mapped_column(Text, default="")
    learning_status: Mapped[str] = mapped_column(String(40), default="candidate")
    quality_score: Mapped[int] = mapped_column(Integer, default=100)
    quality_rule_version: Mapped[int] = mapped_column(Integer, default=0)
    reject_reason: Mapped[str] = mapped_column(String(160), default="")
    downweight_reason: Mapped[str] = mapped_column(String(160), default="")
    decision_by: Mapped[str] = mapped_column(String(120), default="")
    decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantLearningQualityRule(Base):
    __tablename__ = "tenant_learning_quality_rules"
    __table_args__ = (UniqueConstraint("tenant_id", "rule_version", name="uq_tenant_learning_quality_rules_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    identity_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    text_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    template_filters: Mapped[dict] = mapped_column(JSON, default=dict)
    scoring_thresholds: Mapped[dict] = mapped_column(JSON, default=dict)
    scene_weights: Mapped[dict] = mapped_column(JSON, default=dict)
    forbidden_patterns: Mapped[dict] = mapped_column(JSON, default=dict)
    updated_by: Mapped[str] = mapped_column(String(120), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantLearningProfileVersion(Base):
    __tablename__ = "tenant_learning_profile_versions"
    __table_args__ = (Index("ix_tenant_learning_profile_versions_profile", "tenant_id", "profile_version"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    profile_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    source_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    quality_rule_version: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    created_by: Mapped[str] = mapped_column(String(120), default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TenantLearningRun(Base):
    __tablename__ = "tenant_learning_runs"
    __table_args__ = (Index("ix_tenant_learning_runs_status", "tenant_id", "run_type", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    run_type: Mapped[str] = mapped_column(String(40), default="sync")
    source_id: Mapped[str] = mapped_column(String(36), default="")
    status: Mapped[str] = mapped_column(String(40), default="queued")
    from_watermark: Mapped[dict] = mapped_column(JSON, default=dict)
    to_watermark: Mapped[dict] = mapped_column(JSON, default=dict)
    pulled_count: Mapped[int] = mapped_column(Integer, default=0)
    sample_count: Mapped[int] = mapped_column(Integer, default=0)
    accepted_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    quality_rule_version: Mapped[int] = mapped_column(Integer, default=0)
    profile_version: Mapped[int] = mapped_column(Integer, default=0)
    failure_detail: Mapped[str] = mapped_column(Text, default="")
    trace_id: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = [
    "TenantLearningProfile",
    "TenantLearningSource",
    "TenantLearningSample",
    "TenantLearningQualityRule",
    "TenantLearningProfileVersion",
    "TenantLearningRun",
]
