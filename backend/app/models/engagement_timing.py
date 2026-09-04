from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base
from .enums import now


class ExecutionTimingSample(Base):
    __tablename__ = "execution_timing_samples"
    __table_args__ = (
        UniqueConstraint("tenant_id", "evidence_kind", "evidence_reference", name="uq_execution_timing_sample_evidence"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    adapter: Mapped[str] = mapped_column(String(40))
    lane: Mapped[str] = mapped_column(String(40))
    execution_path: Mapped[dict] = mapped_column(JSON)
    execution_path_hash: Mapped[str] = mapped_column(String(64))
    evidence_kind: Mapped[str] = mapped_column(String(32))
    evidence_reference: Mapped[str] = mapped_column(String(160))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    execution_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("execution_attempts.id", ondelete="RESTRICT"), nullable=True)
    boundary_timestamps: Mapped[dict] = mapped_column(JSON)
    stage_durations_ms: Mapped[dict] = mapped_column(JSON)
    remaining_path_ms: Mapped[dict] = mapped_column(JSON)
    joint_path_ms: Mapped[dict] = mapped_column(JSON)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sample_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ExecutionTimingProfileRevision(Base):
    __tablename__ = "execution_timing_profile_revisions"
    __table_args__ = (
        UniqueConstraint("tenant_id", "adapter", "lane", "execution_path_hash", "profile_revision", name="uq_execution_timing_profile_revision"),
        UniqueConstraint("tenant_id", "adapter", "lane", "input_hash", name="uq_execution_timing_profile_input"),
        Index("uq_execution_timing_profile_active", "tenant_id", "adapter", "lane", "execution_path_hash", unique=True,
              postgresql_where=text("state = 'active'"), sqlite_where=text("state = 'active'")),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    adapter: Mapped[str] = mapped_column(String(40))
    lane: Mapped[str] = mapped_column(String(40))
    profile_revision: Mapped[int] = mapped_column(Integer)
    execution_path: Mapped[dict] = mapped_column(JSON)
    execution_path_hash: Mapped[str] = mapped_column(String(64))
    policy_revision: Mapped[str] = mapped_column(String(80))
    sample_ids: Mapped[list[str]] = mapped_column(JSON)
    sample_manifest_hash: Mapped[str] = mapped_column(String(64))
    sample_count: Mapped[int] = mapped_column(Integer)
    minimum_sample_count: Mapped[int] = mapped_column(Integer)
    sample_window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    sample_window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    stage_p95_ms: Mapped[dict] = mapped_column(JSON)
    remaining_path_p95_ms: Mapped[dict] = mapped_column(JSON)
    joint_path_p95_ms: Mapped[dict] = mapped_column(JSON)
    safety_margin_ms: Mapped[dict] = mapped_column(JSON)
    confidence: Mapped[str] = mapped_column(String(32))
    approved_by: Mapped[str] = mapped_column(String(160))
    approval_reference: Mapped[str] = mapped_column(String(200))
    effective_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    supersedes_profile_id: Mapped[str | None] = mapped_column(ForeignKey("execution_timing_profile_revisions.id", ondelete="RESTRICT"), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="active")
    input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["ExecutionTimingSample", "ExecutionTimingProfileRevision"]
