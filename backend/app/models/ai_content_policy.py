from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _uuid() -> str:
    return str(uuid4())


class AiContentPolicyVersion(Base):
    __tablename__ = "ai_content_policy_versions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "version", name="uq_ai_content_policy_tenant_version"),
        Index(
            "uq_ai_content_policy_active",
            "tenant_id",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    route_rules: Mapped[dict] = mapped_column(JSON, default=dict)
    prompt_registry: Mapped[dict] = mapped_column(JSON, default=dict)
    gate_config: Mapped[dict] = mapped_column(JSON, default=dict)
    example_set: Mapped[dict] = mapped_column(JSON, default=dict)
    policy_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(24), default="draft")
    approved_by: Mapped[str] = mapped_column(String(160), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AdultSubjectAttestation(Base):
    __tablename__ = "adult_subject_attestations"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('task_group','task_source')",
            name="ck_adult_attestation_scope_type",
        ),
        Index(
            "ix_adult_attestation_scope",
            "tenant_id",
            "scope_type",
            "scope_id",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(24))
    scope_id: Mapped[str] = mapped_column(String(160))
    subject_class: Mapped[str] = mapped_column(String(48))
    evidence_codes: Mapped[list] = mapped_column(JSON, default=list)
    actor_user_id: Mapped[int | None] = mapped_column(
        ForeignKey("app_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    permission_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    attested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task_config_revision: Mapped[int] = mapped_column(Integer)
    policy_version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default="active")
    evidence_hash: Mapped[str] = mapped_column(String(64))


class TaskAiContentPolicyBinding(Base):
    __tablename__ = "task_ai_content_policy_bindings"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "task_config_revision",
            name="uq_task_ai_content_policy_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    task_config_revision: Mapped[int] = mapped_column(Integer)
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("ai_content_policy_versions.id", ondelete="RESTRICT")
    )
    allowed_routes: Mapped[list] = mapped_column(JSON, default=list)
    attestation_ids: Mapped[list] = mapped_column(JSON, default=list)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    style_overlay_id: Mapped[str] = mapped_column(String(80), default="")
    approved_by: Mapped[str] = mapped_column(String(160), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContextScopeRevision(Base):
    __tablename__ = "context_scope_revisions"
    __table_args__ = (
        CheckConstraint(
            "scope_type IN ('group','comment_source')",
            name="ck_context_scope_revision_type",
        ),
        UniqueConstraint(
            "tenant_id",
            "scope_type",
            "scope_id",
            name="uq_context_scope_revision_scope",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    scope_type: Mapped[str] = mapped_column(String(24))
    scope_id: Mapped[str] = mapped_column(String(160))
    context_scope_revision: Mapped[int] = mapped_column(Integer, default=1)
    context_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    last_human_message_id: Mapped[str] = mapped_column(String(80), default="")
    reply_target_hash: Mapped[str] = mapped_column(String(64), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = [
    "AdultSubjectAttestation",
    "AiContentPolicyVersion",
    "ContextScopeRevision",
    "TaskAiContentPolicyBinding",
]
