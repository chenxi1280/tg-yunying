from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskAccountDailyCoverage(Base):
    __tablename__ = "task_account_daily_coverage"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "group_id", "account_id", "coverage_date",
            name="uq_task_daily_coverage_obligation",
        ),
        Index(
            "ix_task_daily_coverage_task_date_state",
            "task_id", "coverage_date", "state", "next_eligible_at",
        ),
        Index("ix_task_daily_coverage_account_date", "tenant_id", "account_id", "coverage_date"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    membership_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_membership_admission_items.id"), nullable=True,
    )
    coverage_date: Mapped[date] = mapped_column(Date)
    target_count: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(40), default="pending_admission")
    reserved_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    last_success_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    last_remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    blocker_code: Mapped[str] = mapped_column(String(80), default="")
    blocker_detail: Mapped[str] = mapped_column(Text, default="")
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    targeted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AccountEligibilityEvent(Base):
    __tablename__ = "account_eligibility_events"
    __table_args__ = (
        Index("ix_account_eligibility_events_pending", "processed_at", "next_attempt_at", "occurred_at"),
        Index("ix_account_eligibility_events_account", "tenant_id", "account_id", "occurred_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    event_type: Mapped[str] = mapped_column(String(60))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    processing_error: Mapped[str] = mapped_column(Text, default="")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = ["AccountEligibilityEvent", "TaskAccountDailyCoverage"]
