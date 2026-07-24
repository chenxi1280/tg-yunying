from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


class TaskHardHourlyBucket(Base):
    __tablename__ = "task_hard_hourly_buckets"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_id",
            "operation_target_id",
            "target_reference_revision",
            "bucket_key",
            name="uq_hard_hourly_bucket_epoch",
        ),
        Index(
            "ix_hard_hourly_bucket_lookup",
            "tenant_id",
            "task_id",
            "operation_target_id",
            "target_reference_revision",
            "bucket_start",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"), default=1)
    task_id: Mapped[str] = mapped_column(String(36), ForeignKey("tasks.id"))
    operation_target_id: Mapped[int] = mapped_column(Integer, default=0)
    target_reference_revision: Mapped[int] = mapped_column(Integer, default=1)
    bucket_key: Mapped[str] = mapped_column(String(80))
    bucket_start: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    bucket_end: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    timezone: Mapped[str] = mapped_column(String(50), default="Asia/Shanghai")
    goal: Mapped[int] = mapped_column(Integer, default=0)
    task_config_revision: Mapped[int] = mapped_column(Integer, default=1)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    terminal_blocker_code: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskHardHourlyDeliveryCredit(Base):
    __tablename__ = "task_hard_hourly_delivery_credits"
    __table_args__ = (
        UniqueConstraint("action_id", name="uq_hard_hourly_credit_action"),
        Index("ix_hard_hourly_credit_bucket_executed", "bucket_id", "executed_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    bucket_id: Mapped[int] = mapped_column(ForeignKey("task_hard_hourly_buckets.id"))
    action_id: Mapped[str] = mapped_column(String(36), ForeignKey("actions.id"))
    execution_attempt_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    remote_message_id: Mapped[str] = mapped_column(String(160))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["TaskHardHourlyBucket", "TaskHardHourlyDeliveryCredit"]
