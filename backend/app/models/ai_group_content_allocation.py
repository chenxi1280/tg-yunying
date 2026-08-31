from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class AiGroupContentAllocationPlan(Base):
    __tablename__ = "ai_group_content_allocation_plans"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id",
            "target_operation_target_id",
            "route_family",
            name="uq_ai_group_content_plan_surface_day",
        ),
        Index("ix_ai_group_content_plan_scope", "surface_scope_key", "task_day"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    target_operation_target_id: Mapped[int] = mapped_column(
        ForeignKey("operation_targets.id", ondelete="CASCADE")
    )
    task_day: Mapped[date] = mapped_column(Date)
    route_family: Mapped[str] = mapped_column(String(24))
    surface_scope_key: Mapped[str] = mapped_column(String(255))
    config_revision: Mapped[int] = mapped_column(Integer)
    config_snapshot_hash: Mapped[str] = mapped_column(String(64))
    topic_rate_bps: Mapped[int] = mapped_column(Integer)
    normal_text_cursor: Mapped[int] = mapped_column(Integer, default=0)
    question_count: Mapped[int] = mapped_column(Integer, default=0)
    daily_vocabulary_theme_id: Mapped[int] = mapped_column(Integer)
    daily_vocabulary_theme_version: Mapped[str] = mapped_column(String(32))
    plan_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AiGroupContentIntent(Base):
    __tablename__ = "ai_group_content_intents"
    __table_args__ = (
        UniqueConstraint(
            "allocation_plan_id",
            "normal_text_ordinal",
            name="uq_ai_group_content_intent_ordinal",
        ),
        UniqueConstraint(
            "primary_quantity_slot_id",
            name="uq_ai_group_content_intent_quantity_slot",
        ),
        Index("ix_ai_group_content_intent_plan_created", "allocation_plan_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    allocation_plan_id: Mapped[str] = mapped_column(
        ForeignKey("ai_group_content_allocation_plans.id", ondelete="CASCADE")
    )
    primary_quantity_slot_id: Mapped[str] = mapped_column(
        ForeignKey("task_group_daily_message_slots.id", ondelete="CASCADE")
    )
    normal_text_ordinal: Mapped[int] = mapped_column(Integer)
    config_revision: Mapped[int] = mapped_column(Integer)
    config_snapshot_hash: Mapped[str] = mapped_column(String(64))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    target_reference_revision: Mapped[int] = mapped_column(Integer)
    relation_kind: Mapped[str] = mapped_column(String(16))
    act_type: Mapped[str] = mapped_column(String(32))
    stance: Mapped[str] = mapped_column(String(24), default="")
    topic_budget_eligible: Mapped[bool] = mapped_column(Boolean, default=False)
    topic_mode: Mapped[str] = mapped_column(String(32))
    topic_direction_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    teacher_target_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    topic_capacity_reservation_id: Mapped[str] = mapped_column(String(36), default="")
    daily_vocabulary_theme_id: Mapped[int] = mapped_column(Integer)
    daily_vocabulary_theme_effective_state: Mapped[str] = mapped_column(String(48))
    vocabulary_catalog_version: Mapped[str] = mapped_column(String(32))
    vocabulary_sample_ids: Mapped[list] = mapped_column(JSON, default=list)
    vocabulary_surface_terms: Mapped[list] = mapped_column(JSON, default=list)
    vocabulary_normalized_term_ids: Mapped[list] = mapped_column(JSON, default=list)
    vocabulary_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    vocabulary_reservation_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["AiGroupContentAllocationPlan", "AiGroupContentIntent"]
