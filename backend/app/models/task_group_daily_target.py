from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class TaskGroupDailyTarget(Base):
    __tablename__ = "task_group_daily_targets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "group_id",
            "target_date",
            name="uq_task_group_daily_target",
        ),
        Index("ix_task_group_daily_target_task_date", "task_id", "target_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid4()))
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE"),
        nullable=True,
    )
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    target_date: Mapped[date] = mapped_column(Date)
    configured_message_target: Mapped[int] = mapped_column(Integer)
    frozen_account_count: Mapped[int] = mapped_column(Integer)
    effective_message_target: Mapped[int] = mapped_column(Integer)
    planned_target_revision: Mapped[int] = mapped_column(Integer, default=1)
    planned_daily_target: Mapped[int] = mapped_column(Integer, default=1)
    gateway_started_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_hold_count: Mapped[int] = mapped_column(Integer, default=0)
    target_reduction_overage_count: Mapped[int] = mapped_column(Integer, default=0)
    target_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    target_change_reason: Mapped[str] = mapped_column(String(120), default="created")
    daily_fulfillment_phase: Mapped[str] = mapped_column(String(32))
    scope_frozen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    full_day_committed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    due_message_count: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_message_count: Mapped[int] = mapped_column(Integer, default=0)
    coverage_confirmed_account_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


__all__ = ["TaskGroupDailyTarget"]
