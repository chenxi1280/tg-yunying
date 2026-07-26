from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
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
        Index(
            "ix_task_daily_coverage_plan_ready",
            "task_id", "coverage_date", "state", "targeted_at", "account_id", "id",
        ),
        Index("ix_task_daily_coverage_account_date", "tenant_id", "account_id", "coverage_date"),
        Index("ix_task_daily_coverage_reserved_action", "reserved_action_id"),
        Index("ix_task_daily_coverage_last_success_action", "last_success_action_id"),
        Index("ix_task_daily_coverage_last_action", "last_action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    group_id: Mapped[int] = mapped_column(ForeignKey("tg_groups.id"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    membership_item_id: Mapped[int | None] = mapped_column(
        ForeignKey("task_membership_admission_items.id", ondelete="CASCADE"), nullable=True,
    )
    coverage_date: Mapped[date] = mapped_column(Date)
    target_count: Mapped[int] = mapped_column(Integer, default=1)
    confirmed_count: Mapped[int] = mapped_column(Integer, default=0)
    state: Mapped[str] = mapped_column(String(40), default="pending_admission")
    reserved_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    last_success_action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    last_action_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_remote_message_id: Mapped[str] = mapped_column(String(160), default="")
    blocker_code: Mapped[str] = mapped_column(String(80), default="")
    blocker_stage: Mapped[str] = mapped_column(String(40), default="")
    blocker_detail: Mapped[str] = mapped_column(Text, default="")
    recovery_path: Mapped[str] = mapped_column(String(80), default="")
    next_eligible_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    targeted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskDailyCoveragePlanCursor(Base):
    __tablename__ = "task_daily_coverage_plan_cursors"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "task_id", "coverage_date",
            name="uq_task_daily_coverage_plan_cursor",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    coverage_date: Mapped[date] = mapped_column(Date)
    last_targeted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_account_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_coverage_id: Mapped[str] = mapped_column(String(36), default="")
    wrap_count: Mapped[int] = mapped_column(Integer, default=0)
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class AiCoverageVariationIntent(Base):
    __tablename__ = "ai_coverage_variation_intents"
    __table_args__ = (
        UniqueConstraint("coverage_ledger_id", "content_variation_key", name="uq_ai_coverage_variation_key"),
        Index("ix_ai_coverage_variation_action", "action_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    coverage_ledger_id: Mapped[str] = mapped_column(ForeignKey("task_account_daily_coverage.id", ondelete="CASCADE"))
    action_id: Mapped[str | None] = mapped_column(ForeignKey("actions.id"), nullable=True)
    content_variation_key: Mapped[str] = mapped_column(String(128))
    context_version: Mapped[str] = mapped_column(String(120), default="")
    intent_snapshot_hash: Mapped[str] = mapped_column(String(64), default="")
    outcome: Mapped[str] = mapped_column(String(60), default="planned")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)


class TaskDailyFulfillmentDecision(Base):
    __tablename__ = "task_daily_fulfillment_decisions"
    __table_args__ = (
        Index("ix_task_daily_fulfillment_decision_task_date", "task_id", "coverage_date", "decided_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    coverage_date: Mapped[date] = mapped_column(Date)
    decided_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    full_shortfall_count: Mapped[int] = mapped_column(Integer, default=0)
    valid_future_open_cover_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_hold_count: Mapped[int] = mapped_column(Integer, default=0)
    ready_to_plan_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_shortfall_count: Mapped[int] = mapped_column(Integer, default=0)
    required_new: Mapped[int] = mapped_column(Integer, default=0)
    hard_hourly_required_new: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(120), default="")
    next_decision_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class AiGenerationContractAudit(Base):
    __tablename__ = "ai_generation_contract_audits"
    __table_args__ = (
        UniqueConstraint("generation_attempt_id", name="uq_ai_generation_contract_attempt"),
        Index("ix_ai_generation_contract_task_created", "task_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    generation_attempt_id: Mapped[str] = mapped_column(String(36))
    request_id: Mapped[str] = mapped_column(String(36), default="")
    provider_id: Mapped[str] = mapped_column(String(80), default="")
    model_id: Mapped[str] = mapped_column(String(120), default="")
    prompt_contract_version: Mapped[str] = mapped_column(String(80), default="")
    parser_version: Mapped[str] = mapped_column(String(80), default="")
    expected_slot_count: Mapped[int] = mapped_column(Integer, default=0)
    received_slot_count: Mapped[int] = mapped_column(Integer, default=0)
    slot_summary: Mapped[dict] = mapped_column(JSON, default=dict)
    error_code: Mapped[str] = mapped_column(String(100), default="")
    restricted_response_summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


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


__all__ = [
    "AccountEligibilityEvent",
    "AiCoverageVariationIntent",
    "AiGenerationContractAudit",
    "TaskAccountDailyCoverage",
    "TaskDailyCoveragePlanCursor",
    "TaskDailyFulfillmentDecision",
]
