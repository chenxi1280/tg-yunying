from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class ChannelMessageSourceRevision(Base):
    __tablename__ = "channel_message_source_revisions"
    __table_args__ = (
        UniqueConstraint(
            "channel_message_id", "source_revision",
            name="uq_channel_message_source_revision",
        ),
        UniqueConstraint(
            "observation_identity_hash",
            name="uq_channel_message_source_observation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE"),
    )
    source_revision: Mapped[int] = mapped_column(Integer)
    source_remote_message_id: Mapped[int] = mapped_column(Integer)
    source_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_text_snapshot: Mapped[str] = mapped_column(Text)
    source_content_hash: Mapped[str] = mapped_column(String(64))
    observation_identity_hash: Mapped[str] = mapped_column(String(64))
    source_operation: Mapped[str] = mapped_column(String(24), default="observed")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentPlanContract(Base):
    __tablename__ = "channel_comment_plan_contracts"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "channel_message_id", "comment_plan_revision",
            name="uq_channel_comment_plan_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    channel_message_id: Mapped[int] = mapped_column(
        ForeignKey("channel_messages.id", ondelete="CASCADE"),
    )
    comment_plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"),
    )
    source_published_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    window_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    deadline_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    eligible_account_count: Mapped[int] = mapped_column(Integer)
    eligible_account_ids_hash: Mapped[str] = mapped_column(String(64))
    participation_seed: Mapped[str] = mapped_column(String(128))
    effective_participation_bps: Mapped[int] = mapped_column(Integer)
    required_distinct_account_count: Mapped[int] = mapped_column(Integer)
    grounding_required_count: Mapped[int] = mapped_column(Integer)
    planned_fallback_count: Mapped[int] = mapped_column(Integer)
    initial_quality_target_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "channel_comment_quality_target_revisions.id",
            ondelete="RESTRICT", name="fk_channel_comment_plan_initial_quality_target",
        ),
        nullable=True,
    )
    current_quality_target_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "channel_comment_quality_target_revisions.id",
            ondelete="RESTRICT", name="fk_channel_comment_plan_current_quality_target",
        ),
        nullable=True,
    )
    daily_comment_cap: Mapped[int] = mapped_column(Integer)
    quantity_contract_version: Mapped[str] = mapped_column(
        String(48), default="channel_comment_participation_v1",
    )
    contract_state: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentQualityTargetRevision(Base):
    __tablename__ = "channel_comment_quality_target_revisions"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "quality_target_revision",
            name="uq_channel_comment_quality_target_revision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    quality_target_revision: Mapped[int] = mapped_column(Integer)
    supersedes_quality_target_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "channel_comment_quality_target_revisions.id",
            ondelete="RESTRICT", name="fk_channel_comment_quality_target_supersedes",
        ),
        nullable=True,
    )
    component_targets_json: Mapped[list] = mapped_column(JSON, default=list)
    aggregate_grounding_required_count: Mapped[int] = mapped_column(Integer)
    aggregate_planned_fallback_count: Mapped[int] = mapped_column(Integer)
    component_set_hash: Mapped[str] = mapped_column(String(64))
    target_state: Mapped[str] = mapped_column(String(32), default="frozen")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentEligibleAccountSnapshotRow(Base):
    __tablename__ = "channel_comment_eligible_account_snapshot_rows"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "account_id",
            name="uq_channel_comment_plan_eligible_account",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="RESTRICT"))
    eligibility_state: Mapped[str] = mapped_column(String(32), default="eligible")
    stable_rank: Mapped[int] = mapped_column(Integer)
    eligibility_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentOrdinalAccountBinding(Base):
    __tablename__ = "channel_comment_ordinal_account_bindings"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "target_ordinal", "binding_attempt",
            name="uq_channel_comment_ordinal_binding_attempt",
        ),
        UniqueConstraint(
            "plan_contract_id", "account_id", "binding_attempt",
            name="uq_channel_comment_plan_account_binding_attempt",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    target_ordinal: Mapped[int] = mapped_column(Integer)
    binding_attempt: Mapped[int] = mapped_column(Integer, default=1)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="RESTRICT"))
    binding_state: Mapped[str] = mapped_column(String(32), default="active")
    replacement_reason: Mapped[str] = mapped_column(String(120), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentGroundingAssignment(Base):
    __tablename__ = "channel_comment_grounding_assignments"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "target_ordinal", "assignment_version",
            name="uq_channel_comment_grounding_assignment",
        ),
        Index(
            "uq_channel_comment_grounding_active",
            "plan_contract_id", "target_ordinal",
            unique=True,
            postgresql_where=text("assignment_state = 'active'"),
            sqlite_where=text("assignment_state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"),
    )
    target_ordinal: Mapped[int] = mapped_column(Integer)
    assignment_version: Mapped[int] = mapped_column(Integer, default=1)
    supersedes_assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("channel_comment_grounding_assignments.id", ondelete="RESTRICT"),
        nullable=True,
    )
    quality_target_revision_id: Mapped[str | None] = mapped_column(
        ForeignKey(
            "channel_comment_quality_target_revisions.id",
            ondelete="RESTRICT", name="fk_channel_comment_assignment_quality_target",
        ),
        nullable=True,
    )
    quality_component_key: Mapped[str] = mapped_column(String(64), default="")
    evidence_text: Mapped[str] = mapped_column(Text)
    evidence_hash: Mapped[str] = mapped_column(String(64))
    primary_aspect_code: Mapped[str] = mapped_column(String(64), default="source_fact")
    primary_aspect_text: Mapped[str] = mapped_column(Text)
    teacher_name: Mapped[str] = mapped_column(String(160), default="")
    speech_act: Mapped[str] = mapped_column(String(48), default="reaction")
    assignment_state: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentContentRevisionOperation(Base):
    __tablename__ = "channel_comment_content_revision_operations"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "to_source_revision_id",
            name="uq_channel_comment_content_revision_operation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    from_source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"),
    )
    to_source_revision_id: Mapped[str] = mapped_column(
        ForeignKey("channel_message_source_revisions.id", ondelete="RESTRICT"),
    )
    operation_version: Mapped[int] = mapped_column(Integer, default=1)
    operation_state: Mapped[str] = mapped_column(String(32), default="completed")
    result_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentPlanLifecycleEvent(Base):
    __tablename__ = "channel_comment_plan_lifecycle_events"
    __table_args__ = (
        UniqueConstraint(
            "plan_contract_id", "lifecycle_epoch", "event_type", "evidence_hash",
            name="uq_channel_comment_plan_lifecycle_event",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    lifecycle_epoch: Mapped[int] = mapped_column(Integer)
    event_type: Mapped[str] = mapped_column(String(32))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    task_revision: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(String(120))
    evidence_hash: Mapped[str] = mapped_column(String(64))
    event_state: Mapped[str] = mapped_column(String(32), default="completed")
    result_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskCommentCapacityPeriod(Base):
    __tablename__ = "task_comment_capacity_periods"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "period_start_at",
            name="uq_task_comment_capacity_period_start",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    calendar_revision: Mapped[int] = mapped_column(Integer)
    timezone_snapshot: Mapped[str] = mapped_column(String(80))
    period_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    period_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    capacity_limit: Mapped[int] = mapped_column(Integer)
    period_state: Mapped[str] = mapped_column(String(32), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ChannelCommentCapacityAllocationEpoch(Base):
    __tablename__ = "channel_comment_capacity_allocation_epochs"
    __table_args__ = (
        UniqueConstraint(
            "task_id", "allocation_epoch",
            name="uq_channel_comment_capacity_allocation_epoch",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    allocation_epoch: Mapped[int] = mapped_column(Integer)
    horizon_start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    horizon_end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    open_plan_set_hash: Mapped[str] = mapped_column(String(64))
    immutable_usage_hash: Mapped[str] = mapped_column(String(64))
    allocation_result_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class TaskCommentCapacityReservation(Base):
    __tablename__ = "task_comment_capacity_reservations"
    __table_args__ = (
        UniqueConstraint(
            "obligation_id", name="uq_task_comment_capacity_obligation",
        ),
        Index(
            "ix_task_comment_capacity_rolling",
            "task_id", "scheduled_for_at", "reservation_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    capacity_period_id: Mapped[str] = mapped_column(
        ForeignKey("task_comment_capacity_periods.id", ondelete="CASCADE"),
    )
    plan_contract_id: Mapped[str] = mapped_column(
        ForeignKey("channel_comment_plan_contracts.id", ondelete="CASCADE"),
    )
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("comment_fulfillment_obligations.id", ondelete="CASCADE"),
    )
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id", ondelete="SET NULL"), nullable=True,
    )
    capacity_units: Mapped[int] = mapped_column(Integer, default=1)
    reservation_state: Mapped[str] = mapped_column(String(32), default="plan_reserved")
    allocation_epoch: Mapped[int | None] = mapped_column(Integer, nullable=True)
    scheduled_for_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "ChannelCommentEligibleAccountSnapshotRow",
    "ChannelCommentCapacityAllocationEpoch",
    "ChannelCommentContentRevisionOperation",
    "ChannelCommentGroundingAssignment",
    "ChannelCommentOrdinalAccountBinding",
    "ChannelCommentPlanLifecycleEvent",
    "ChannelCommentPlanContract",
    "ChannelCommentQualityTargetRevision",
    "ChannelMessageSourceRevision",
    "TaskCommentCapacityPeriod",
    "TaskCommentCapacityReservation",
]
