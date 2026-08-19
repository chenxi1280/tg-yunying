from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _uuid() -> str:
    return str(uuid4())


class SourcePacingCapacityPolicyVersion(Base):
    __tablename__ = "source_pacing_capacity_policy_versions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "pacing_domain",
            "revision",
            name="uq_source_capacity_policy_revision",
        ),
        Index(
            "uq_source_capacity_policy_active",
            "tenant_id",
            "pacing_domain",
            unique=True,
            postgresql_where=text("status = 'active'"),
            sqlite_where=text("status = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    pacing_domain: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int] = mapped_column(Integer)
    hourly_curve: Mapped[list] = mapped_column(JSON, default=list)
    minimum_gap_seconds: Mapped[int] = mapped_column(Integer)
    hourly_ceiling: Mapped[int] = mapped_column(Integer)
    telemetry_window: Mapped[dict] = mapped_column(JSON, default=dict)
    headroom_floor: Mapped[float] = mapped_column(Float)
    provider_retry_slots: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default="draft")
    content_hash: Mapped[str] = mapped_column(String(64))
    approved_by: Mapped[str] = mapped_column(String(160), default="")
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SourcePacingCapacityPlan(Base):
    __tablename__ = "source_pacing_capacity_plans"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "pacing_domain",
            "source_key_hash",
            "window_start_at",
            "window_end_at",
            "policy_version_id",
            "revision",
            name="uq_source_capacity_plan_scope",
        ),
        Index("ix_source_capacity_plan_window", "window_start_at", "window_end_at", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    pacing_domain: Mapped[str] = mapped_column(String(40))
    source_key_hash: Mapped[str] = mapped_column(String(64))
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    policy_version_id: Mapped[str] = mapped_column(
        ForeignKey("source_pacing_capacity_policy_versions.id", ondelete="RESTRICT")
    )
    curve_hash: Mapped[str] = mapped_column(String(64))
    capacity_slots: Mapped[list] = mapped_column(JSON, default=list)
    occupied_count: Mapped[int] = mapped_column(Integer, default=0)
    incoming_count: Mapped[int] = mapped_column(Integer, default=0)
    replacement_headroom: Mapped[int] = mapped_column(Integer, default=0)
    available_count: Mapped[int] = mapped_column(Integer, default=0)
    deficit_count: Mapped[int] = mapped_column(Integer, default=0)
    last_safe_release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state: Mapped[str] = mapped_column(String(24), default="draft")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    version: Mapped[int] = mapped_column(Integer, default=1)
    plan_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["SourcePacingCapacityPlan", "SourcePacingCapacityPolicyVersion"]
