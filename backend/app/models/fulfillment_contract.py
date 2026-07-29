from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskDayLedger(Base):
    __tablename__ = "task_day_ledgers"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "period_start_at",
            name="uq_task_day_ledgers_task_period",
        ),
        Index("ix_task_day_ledgers_task_deadline", "task_id", "deadline_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    timezone_snapshot: Mapped[str] = mapped_column(String(50))
    timezone_revision: Mapped[int] = mapped_column(Integer)
    obligation_local_date: Mapped[date] = mapped_column(Date)
    period_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    day_phase: Mapped[str] = mapped_column(String(32))
    planning_anchor_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lifecycle_status: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskGroupDailyMessageSlot(Base):
    __tablename__ = "task_group_daily_message_slots"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id",
            "target_operation_target_id",
            "slot_ordinal",
            name="uq_task_group_daily_message_slot_ordinal",
        ),
        UniqueConstraint(
            "task_account_daily_coverage_id",
            name="uq_task_group_daily_message_slot_coverage",
        ),
        Index(
            "ix_task_group_daily_message_slots_open",
            "task_day_ledger_id",
            "target_operation_target_id",
            "state",
        ),
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
    task_account_daily_coverage_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "task_account_daily_coverage.id",
            ondelete="SET NULL",
            name="fk_group_message_slot_account_coverage",
        ),
        nullable=True,
    )
    slot_kind: Mapped[str] = mapped_column(String(24))
    slot_ordinal: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentMixContract(Base):
    __tablename__ = "content_mix_contracts"
    __table_args__ = (
        UniqueConstraint(
            "content_mix_scope_key",
            "content_contract_version",
            name="uq_content_mix_contract_scope_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    content_mix_scope_key: Mapped[str] = mapped_column(String(255))
    content_contract_version: Mapped[int] = mapped_column(Integer)
    scope_total_slots: Mapped[int] = mapped_column(Integer)
    allocation_seed: Mapped[str] = mapped_column(String(128))
    reply_min_required_count: Mapped[int] = mapped_column(Integer, default=0)
    reply_planned_count: Mapped[int] = mapped_column(Integer, default=0)
    direct_planned_count: Mapped[int] = mapped_column(Integer, default=0)
    normal_text_emoji_required_count: Mapped[int] = mapped_column(Integer, default=0)
    normal_text_emoji_max_count: Mapped[int] = mapped_column(Integer, default=0)
    image_required_count: Mapped[int] = mapped_column(Integer, default=0)
    image_max_count: Mapped[int] = mapped_column(Integer, default=0)
    sticker_required_count: Mapped[int] = mapped_column(Integer, default=0)
    sticker_max_count: Mapped[int] = mapped_column(Integer, default=0)
    custom_emoji_required_count: Mapped[int] = mapped_column(Integer, default=0)
    custom_emoji_max_count: Mapped[int] = mapped_column(Integer, default=0)
    material_policy_rule_set_id: Mapped[str | None] = mapped_column(
        String(36),
        nullable=True,
    )
    material_policy_rule_set_version: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
    )
    target_resolution_trace: Mapped[str] = mapped_column(String(500), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentMixCycle(Base):
    __tablename__ = "content_mix_cycles"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "target_operation_target_id",
            "task_day_ledger_id",
            "cycle_seq",
            name="uq_content_mix_cycles_scope_sequence",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    target_operation_target_id: Mapped[int] = mapped_column(
        ForeignKey("operation_targets.id", ondelete="CASCADE")
    )
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    cycle_seq: Mapped[int] = mapped_column(Integer)
    config_revision: Mapped[int] = mapped_column(Integer)
    scope_total_slots: Mapped[int] = mapped_column(Integer)
    allocation_seed: Mapped[str] = mapped_column(String(128))
    allocation_closed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    materialization_status: Mapped[str] = mapped_column(String(20), default="pending")
    materialized_slot_count: Mapped[int] = mapped_column(Integer, default=0)
    settlement_status: Mapped[str] = mapped_column(String(20), default="open")
    settlement_outcome: Mapped[str | None] = mapped_column(String(20), nullable=True)
    settled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentMixCycleSlot(Base):
    __tablename__ = "content_mix_cycle_slots"
    __table_args__ = (
        UniqueConstraint("cycle_id", "slot_index", name="uq_content_mix_cycle_slot"),
        UniqueConstraint(
            "primary_quantity_slot_id",
            name="uq_content_mix_cycle_slot_quantity",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("content_mix_cycles.id", ondelete="CASCADE")
    )
    slot_index: Mapped[int] = mapped_column(Integer)
    primary_quantity_slot_id: Mapped[str] = mapped_column(
        ForeignKey("task_group_daily_message_slots.id", ondelete="CASCADE")
    )
    relation_kind: Mapped[str] = mapped_column(String(16))
    reply_requirement_key: Mapped[str] = mapped_column(String(120), default="")
    initial_reply_to_message_id: Mapped[str] = mapped_column(String(160), default="")
    slot_attempt: Mapped[int] = mapped_column(Integer, default=0)
    current_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="SET NULL",
            name="fk_content_mix_slot_current_action",
        ),
        nullable=True,
    )
    slot_state: Mapped[str] = mapped_column(String(32), default="unmaterialized")
    terminal_reason: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ContentMixObligation(Base):
    __tablename__ = "content_mix_obligations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "content_mix_scope_key",
            "obligation_source",
            "obligation_kind",
            "obligation_ordinal",
            name="uq_content_mix_obligation_scope_kind_ordinal",
        ),
        Index(
            "ix_content_mix_obligations_status",
            "content_mix_contract_id",
            "status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    content_mix_contract_id: Mapped[str] = mapped_column(
        ForeignKey("content_mix_contracts.id", ondelete="CASCADE")
    )
    content_mix_scope_key: Mapped[str] = mapped_column(String(255))
    obligation_source: Mapped[str] = mapped_column(String(24))
    obligation_kind: Mapped[str] = mapped_column(String(32))
    obligation_ordinal: Mapped[int] = mapped_column(Integer)
    assigned_cycle_slot_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "content_mix_cycle_slots.id",
            ondelete="SET NULL",
            name="content_mix_obligations_assigned_cycle_slot_id_fkey",
        ),
        nullable=True,
    )
    assigned_action_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "actions.id",
            ondelete="SET NULL",
            name="fk_content_mix_obligation_assigned_action",
        ),
        nullable=True,
    )
    assignment_version: Mapped[int] = mapped_column(Integer, default=1)
    required_count: Mapped[int] = mapped_column(Integer)
    planned_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    shortfall_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskStartOperation(Base):
    __tablename__ = "task_start_operations"
    __table_args__ = (
        UniqueConstraint(
            "start_operation_id",
            name="uq_task_start_operations_operation_id",
        ),
    )

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"),
        primary_key=True,
    )
    start_operation_id: Mapped[str] = mapped_column(String(120))
    operation_version: Mapped[int] = mapped_column(Integer)
    requested_by_user_id: Mapped[int] = mapped_column(Integer)
    source: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16))
    task_day_ledger_id: Mapped[str | None] = mapped_column(
        ForeignKey("task_day_ledgers.id"),
        nullable=True,
    )
    start_failure_code: Mapped[str] = mapped_column(String(80), default="")
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=now,
        onupdate=now,
    )


__all__ = [
    "ContentMixContract",
    "ContentMixCycle",
    "ContentMixCycleSlot",
    "ContentMixObligation",
    "TaskDayLedger",
    "TaskGroupDailyMessageSlot",
    "TaskStartOperation",
]
