from __future__ import annotations

from datetime import date, datetime
from uuid import uuid4

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, UniqueConstraint, text
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class TaskParticipationUnitPlan(Base):
    __tablename__ = "task_participation_unit_plans"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "participation_kind",
            "participation_unit",
            "plan_revision",
            name="uq_task_participation_unit_revision",
        ),
        Index(
            "uq_task_participation_unit_active",
            "task_id",
            "task_lifecycle_epoch",
            "participation_kind",
            "participation_unit",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    task_day_ledger_id: Mapped[str | None] = mapped_column(ForeignKey("task_day_ledgers.id", ondelete="CASCADE"), nullable=True)
    membership_snapshot_set_id: Mapped[str] = mapped_column(ForeignKey("account_group_membership_snapshot_sets.id"))
    participation_kind: Mapped[str] = mapped_column(String(48))
    participation_unit: Mapped[str] = mapped_column(String(200))
    plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision: Mapped[str] = mapped_column(String(64))
    policy_eligible_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    selected_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    selected_origin_groups: Mapped[dict] = mapped_column(JSON, default=dict)
    sampled_ratio_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rounded_selected_count: Mapped[int] = mapped_column(Integer, default=0)
    participation_min_count: Mapped[int] = mapped_column(Integer, default=0)
    participation_max_count: Mapped[int] = mapped_column(Integer, default=0)
    realized_participation_bps: Mapped[int | None] = mapped_column(Integer, nullable=True)
    integer_quantization_adjustment: Mapped[bool] = mapped_column(Boolean, default=False)
    required_count: Mapped[int] = mapped_column(Integer, default=0)
    selection_seed: Mapped[str] = mapped_column(String(64))
    selection_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PlanningAdmissionSnapshot(Base):
    __tablename__ = "planning_admission_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "participation_unit",
            "planning_horizon",
            "dependency_revision_set_hash",
            name="uq_planning_admission_dependency_set",
        ),
        Index("ix_planning_admission_plan", "participation_plan_id", "decision"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    participation_plan_id: Mapped[str] = mapped_column(ForeignKey("task_participation_unit_plans.id", ondelete="CASCADE"))
    participation_unit: Mapped[str] = mapped_column(String(200))
    planning_horizon: Mapped[str] = mapped_column(String(100))
    dependency_revision_set_hash: Mapped[str] = mapped_column(String(64))
    account_paths: Mapped[list[dict]] = mapped_column(JSON, default=list)
    admissible_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    deficit_account_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(32))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountBehaviorSessionPlan(Base):
    __tablename__ = "account_behavior_session_plans"
    __table_args__ = (
        UniqueConstraint("tenant_id", "account_id", "task_day", "plan_revision", name="uq_behavior_session_account_day_revision"),
        Index(
            "uq_behavior_session_account_day_active",
            "tenant_id",
            "account_id",
            "task_day",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id", ondelete="CASCADE"))
    task_day: Mapped[date] = mapped_column(Date)
    plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision_id: Mapped[str] = mapped_column(ForeignKey("account_behavior_budget_policy_revisions.id"))
    chronotype: Mapped[str] = mapped_column(String(32))
    weekday_class: Mapped[str] = mapped_column(String(24))
    windows: Mapped[list[dict]] = mapped_column(JSON, default=list)
    visible_action_capacity: Mapped[dict] = mapped_column(JSON, default=dict)
    rest_debt: Mapped[int] = mapped_column(Integer, default=0)
    wake_policy: Mapped[dict] = mapped_column(JSON, default=dict)
    seed: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ReactionCapacityAllocationEpoch(Base):
    __tablename__ = "reaction_capacity_allocation_epochs"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "task_day_ledger_id",
            "allocation_revision",
            name="uq_reaction_capacity_epoch_revision",
        ),
        Index(
            "uq_reaction_capacity_epoch_active",
            "task_id",
            "task_lifecycle_epoch",
            "task_day_ledger_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    task_day_ledger_id: Mapped[str] = mapped_column(ForeignKey("task_day_ledgers.id", ondelete="CASCADE"))
    allocation_revision: Mapped[int] = mapped_column(Integer, default=1)
    policy_revision: Mapped[str] = mapped_column(String(64))
    daily_reaction_cap: Mapped[int] = mapped_column(Integer)
    source_demands: Mapped[list[dict]] = mapped_column(JSON, default=list)
    source_allocations: Mapped[list[dict]] = mapped_column(JSON, default=list)
    planning_admission_snapshot_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    allocated_count: Mapped[int] = mapped_column(Integer, default=0)
    unallocated_count: Mapped[int] = mapped_column(Integer, default=0)
    unallocated_reasons: Mapped[dict] = mapped_column(JSON, default=dict)
    allocation_hash: Mapped[str] = mapped_column(String(64))
    supersedes_epoch_id: Mapped[str | None] = mapped_column(
        ForeignKey("reaction_capacity_allocation_epochs.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ViewAccountSourceAllocationPlan(Base):
    __tablename__ = "view_account_source_allocation_plans"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "task_lifecycle_epoch",
            "task_day_ledger_id",
            "allocation_revision",
            name="uq_view_account_source_allocation_revision",
        ),
        Index(
            "uq_view_account_source_allocation_active",
            "task_id",
            "task_lifecycle_epoch",
            "task_day_ledger_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_lifecycle_epoch: Mapped[int] = mapped_column(Integer, default=1)
    task_day_ledger_id: Mapped[str] = mapped_column(ForeignKey("task_day_ledgers.id", ondelete="CASCADE"))
    participation_plan_id: Mapped[str] = mapped_column(
        ForeignKey("task_participation_unit_plans.id", ondelete="RESTRICT")
    )
    planning_admission_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("planning_admission_snapshots.id", ondelete="RESTRICT")
    )
    allocation_revision: Mapped[int] = mapped_column(Integer, default=1)
    allocation_mode: Mapped[str] = mapped_column(String(32))
    algorithm_revision: Mapped[str] = mapped_column(String(64))
    source_set: Mapped[list[dict]] = mapped_column(JSON, default=list)
    source_set_hash: Mapped[str] = mapped_column(String(64))
    account_degrees: Mapped[list[dict]] = mapped_column(JSON, default=list)
    source_exposures: Mapped[list[dict]] = mapped_column(JSON, default=list)
    edge_set: Mapped[list[dict]] = mapped_column(JSON, default=list)
    edge_count: Mapped[int] = mapped_column(Integer, default=0)
    unallocated_sources: Mapped[list[dict]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(40))
    allocation_seed: Mapped[str] = mapped_column(String(64))
    allocation_hash: Mapped[str] = mapped_column(String(64))
    supersedes_plan_id: Mapped[str | None] = mapped_column(
        ForeignKey("view_account_source_allocation_plans.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class PortfolioFeasibilityPlanRevision(Base):
    __tablename__ = "portfolio_feasibility_plan_revisions"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "planning_horizon",
            "trigger_kind",
            "trigger_identity",
            "input_hash",
            name="uq_portfolio_feasibility_input",
        ),
        Index(
            "ix_portfolio_feasibility_horizon",
            "tenant_id",
            "planning_horizon",
            "decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    planning_horizon: Mapped[str] = mapped_column(String(100))
    plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    trigger_task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE")
    )
    trigger_kind: Mapped[str] = mapped_column(String(48))
    trigger_identity: Mapped[str] = mapped_column(String(240))
    task_set_hash: Mapped[str] = mapped_column(String(64))
    policy_revision_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    demand_snapshot: Mapped[list[dict]] = mapped_column(JSON, default=list)
    account_task_day_load: Mapped[list[dict]] = mapped_column(JSON, default=list)
    deficits: Mapped[list[dict]] = mapped_column(JSON, default=list)
    decision: Mapped[str] = mapped_column(String(40))
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class AccountPortfolioLoadReservation(Base):
    __tablename__ = "account_portfolio_load_reservations"
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "task_day",
            "task_id",
            "action_class",
            "demand_identity",
            "account_id",
            name="uq_account_portfolio_demand_account",
        ),
        Index(
            "ix_account_portfolio_capacity",
            "tenant_id",
            "task_day",
            "account_id",
            "action_class",
            "state",
        ),
        Index(
            "ix_account_portfolio_task",
            "task_id",
            "task_day_ledger_id",
            "action_class",
            "state",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE")
    )
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    portfolio_plan_id: Mapped[str] = mapped_column(
        ForeignKey("portfolio_feasibility_plan_revisions.id", ondelete="CASCADE")
    )
    account_id: Mapped[int] = mapped_column(
        ForeignKey("tg_accounts.id", ondelete="CASCADE")
    )
    task_day: Mapped[date] = mapped_column(Date)
    action_class: Mapped[str] = mapped_column(String(40))
    demand_identity: Mapped[str] = mapped_column(String(240))
    demand_hash: Mapped[str] = mapped_column(String(64))
    reserved_units: Mapped[int] = mapped_column(Integer)
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class NaturalOpportunitySupplyPlanRevision(Base):
    __tablename__ = "natural_opportunity_supply_plan_revisions"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id", "plan_revision",
            name="uq_natural_opportunity_plan_revision",
        ),
        Index(
            "uq_natural_opportunity_plan_active",
            "task_day_ledger_id",
            unique=True,
            postgresql_where=text("state = 'active'"),
            sqlite_where=text("state = 'active'"),
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    canonical_peer_id: Mapped[str] = mapped_column(String(120))
    plan_revision: Mapped[int] = mapped_column(Integer, default=1)
    required_capacity: Mapped[int] = mapped_column(Integer)
    guaranteed_now_capacity: Mapped[int] = mapped_column(Integer)
    forecast_conditional_capacity: Mapped[int] = mapped_column(Integer, default=0)
    deficit: Mapped[int] = mapped_column(Integer, default=0)
    commitment_status: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(24), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class ManagedPresencePlan(Base):
    __tablename__ = "managed_presence_plans"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id", "policy_revision_id",
            name="uq_managed_presence_task_day_policy",
        ),
        Index("ix_managed_presence_peer_day", "tenant_id", "canonical_peer_id", "task_day"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("managed_presence_policy_revisions.id")
    )
    canonical_peer_id: Mapped[str] = mapped_column(String(120))
    task_day: Mapped[date] = mapped_column(Date)
    external_human_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    visible_managed_authored_count: Mapped[int] = mapped_column(Integer, default=0)
    planned_managed_authored_count: Mapped[int] = mapped_column(Integer, default=0)
    trailing_managed_turn_count: Mapped[int] = mapped_column(Integer, default=0)
    allowed_managed_authored: Mapped[int] = mapped_column(Integer, default=0)
    remaining_capacity: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


class InteractionContinuityCapacityPlan(Base):
    __tablename__ = "interaction_continuity_capacity_plans"
    __table_args__ = (
        UniqueConstraint(
            "task_day_ledger_id", "policy_revision_id",
            name="uq_interaction_continuity_task_day_policy",
        ),
        Index(
            "ix_interaction_continuity_peer_day",
            "tenant_id", "canonical_peer_id", "task_day",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    policy_revision_id: Mapped[str] = mapped_column(
        ForeignKey("managed_presence_policy_revisions.id")
    )
    canonical_peer_id: Mapped[str] = mapped_column(String(120))
    task_day: Mapped[date] = mapped_column(Date)
    observed_eligible_demand: Mapped[int] = mapped_column(Integer, default=0)
    max_service_count: Mapped[int] = mapped_column(Integer, default=0)
    protected_reserved_count: Mapped[int] = mapped_column(Integer, default=0)
    borrowed_count: Mapped[int] = mapped_column(Integer, default=0)
    recalled_count: Mapped[int] = mapped_column(Integer, default=0)
    admitted_count: Mapped[int] = mapped_column(Integer, default=0)
    served_count: Mapped[int] = mapped_column(Integer, default=0)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_by_capacity_count: Mapped[int] = mapped_column(Integer, default=0)
    remaining_capacity: Mapped[int] = mapped_column(Integer, default=0)
    decision: Mapped[str] = mapped_column(String(40))
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


__all__ = [
    "AccountBehaviorSessionPlan",
    "AccountPortfolioLoadReservation",
    "InteractionContinuityCapacityPlan",
    "ManagedPresencePlan",
    "NaturalOpportunitySupplyPlanRevision",
    "PlanningAdmissionSnapshot",
    "PortfolioFeasibilityPlanRevision",
    "ReactionCapacityAllocationEpoch",
    "TaskParticipationUnitPlan",
    "ViewAccountSourceAllocationPlan",
]
