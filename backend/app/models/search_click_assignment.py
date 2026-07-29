from __future__ import annotations

from datetime import datetime
from uuid import uuid4

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base

from .enums import now


def _new_uuid() -> str:
    return str(uuid4())


class SearchClickAssignmentEpoch(Base):
    __tablename__ = "search_click_assignment_epochs"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_claim_window_id",
            "dispatch_allocation_epoch",
            name="uq_search_click_assignment_epoch_window",
        ),
        Index("ix_search_click_assignment_epoch_state", "finalize_status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatch_claim_window_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_windows.id", ondelete="CASCADE")
    )
    dispatch_allocation_epoch: Mapped[int] = mapped_column(Integer)
    solver_owner_lease_id: Mapped[str] = mapped_column(String(120))
    solver_fencing_token: Mapped[str] = mapped_column(String(120))
    solver_claimed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    solver_problem_hash: Mapped[str] = mapped_column(String(64))
    solver_input_hash: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(24), default="open")
    outcome_hash: Mapped[str] = mapped_column(String(64), default="")
    release_unit_set_hash: Mapped[str] = mapped_column(String(64), default="")
    matched_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    released_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    rebuild_input_version_before: Mapped[int] = mapped_column(Integer, default=0)
    rebuild_input_version_after: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    finalize_status: Mapped[str] = mapped_column(String(24), default="open")
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchClickTaskFairnessState(Base):
    __tablename__ = "search_click_task_fairness_states"

    task_id: Mapped[str] = mapped_column(
        ForeignKey("tasks.id", ondelete="CASCADE"), primary_key=True
    )
    persistent_task_cursor: Mapped[int] = mapped_column(Integer, default=0)
    cursor_version: Mapped[int] = mapped_column(Integer, default=1)
    last_click_opportunity_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


class SearchClickSolverProblemSnapshot(Base):
    __tablename__ = "search_click_solver_problem_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "search_click_assignment_epoch_id",
            name="uq_search_click_solver_snapshot_epoch",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    search_click_assignment_epoch_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_assignment_epochs.id", ondelete="CASCADE")
    )
    solver_contract_version: Mapped[str] = mapped_column(String(40))
    canonical_problem_payload: Mapped[dict] = mapped_column(JSON)
    canonical_carrier_payload: Mapped[dict] = mapped_column(JSON)
    solver_problem_hash: Mapped[str] = mapped_column(String(64))
    solver_input_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchClickSolverProblemComponent(Base):
    __tablename__ = "search_click_solver_problem_components"
    __table_args__ = (
        UniqueConstraint(
            "search_click_solver_snapshot_id",
            "stable_component_key",
            name="uq_search_click_solver_component_key",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    search_click_solver_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_solver_problem_snapshots.id", ondelete="CASCADE")
    )
    stable_component_key: Mapped[str] = mapped_column(String(64))
    canonical_component_payload: Mapped[dict] = mapped_column(JSON)
    solver_problem_component_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchClickSolverCarrierUnitBinding(Base):
    __tablename__ = "search_click_solver_carrier_unit_bindings"
    __table_args__ = (
        UniqueConstraint(
            "search_click_solver_snapshot_id",
            "dispatch_claim_reservation_id",
            "fulfillment_lane_claim_ordinal",
            name="uq_search_click_solver_carrier_unit",
        ),
        UniqueConstraint(
            "search_click_solver_snapshot_id",
            "obligation_id",
            name="uq_search_click_solver_carrier_obligation",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    search_click_solver_snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_solver_problem_snapshots.id", ondelete="CASCADE")
    )
    dispatch_claim_reservation_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_reservations.id", ondelete="CASCADE")
    )
    fulfillment_lane_claim_ordinal: Mapped[int] = mapped_column(Integer)
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_fulfillment_obligations.id", ondelete="CASCADE")
    )
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    stable_component_key: Mapped[str] = mapped_column(String(64))
    solver_problem_component_hash: Mapped[str] = mapped_column(String(64))
    canonical_binding_payload: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class SearchClickOpportunityAssignment(Base):
    __tablename__ = "search_click_opportunity_assignments"
    __table_args__ = (
        UniqueConstraint(
            "search_click_assignment_epoch_id",
            "obligation_id",
            name="uq_search_click_assignment_epoch_obligation",
        ),
        UniqueConstraint(
            "dispatch_claim_reservation_id",
            "fulfillment_lane_claim_ordinal",
            name="uq_search_click_assignment_reservation_ordinal",
        ),
        UniqueConstraint("action_id", name="uq_search_click_assignment_action"),
        Index("ix_search_click_assignment_state", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    tenant_id: Mapped[int] = mapped_column(ForeignKey("tenants.id"))
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"))
    task_day_ledger_id: Mapped[str] = mapped_column(
        ForeignKey("task_day_ledgers.id", ondelete="CASCADE")
    )
    obligation_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_fulfillment_obligations.id", ondelete="CASCADE")
    )
    search_click_assignment_epoch_id: Mapped[str] = mapped_column(
        ForeignKey("search_click_assignment_epochs.id", ondelete="CASCADE")
    )
    dispatch_claim_reservation_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_reservations.id", ondelete="CASCADE")
    )
    fulfillment_lane_claim_ordinal: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[int] = mapped_column(ForeignKey("tg_accounts.id"))
    authorization_id: Mapped[int] = mapped_column(
        ForeignKey("tg_account_authorizations.id")
    )
    keyword_hash: Mapped[str] = mapped_column(String(64))
    proxy_route_id: Mapped[str] = mapped_column(String(120))
    protocol_sample_version: Mapped[str] = mapped_column(String(80))
    resource_snapshot_hash: Mapped[str] = mapped_column(String(64))
    action_id: Mapped[str | None] = mapped_column(
        ForeignKey("actions.id"), nullable=True
    )
    state: Mapped[str] = mapped_column(String(24), default="reserved")
    version: Mapped[int] = mapped_column(Integer, default=1)
    release_reason: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=now, onupdate=now
    )


class DispatchAllocationReleaseBatch(Base):
    __tablename__ = "dispatch_allocation_release_batches"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_claim_window_id",
            "trigger_key",
            name="uq_dispatch_allocation_release_trigger",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatch_claim_window_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_windows.id", ondelete="CASCADE")
    )
    dispatch_allocation_epoch: Mapped[int] = mapped_column(Integer)
    trigger_key: Mapped[str] = mapped_column(String(160))
    candidate_unit_set_hash: Mapped[str] = mapped_column(String(64))
    release_unit_set_hash: Mapped[str] = mapped_column(String(64), default="")
    candidate_unit_count: Mapped[int] = mapped_column(Integer)
    release_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    already_released_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    precondition_lost_unit_count: Mapped[int] = mapped_column(Integer, default=0)
    rebuild_input_version_after: Mapped[int | None] = mapped_column(
        Integer, nullable=True
    )
    outcome_hash: Mapped[str] = mapped_column(String(64), default="")
    finalize_status: Mapped[str] = mapped_column(String(24), default="open")
    finalized_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DispatchAllocationReleaseBatchItem(Base):
    __tablename__ = "dispatch_allocation_release_batch_items"
    __table_args__ = (
        UniqueConstraint(
            "release_batch_id",
            "dispatch_claim_reservation_id",
            "fulfillment_lane_claim_ordinal",
            name="uq_dispatch_allocation_release_batch_item",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    release_batch_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_allocation_release_batches.id", ondelete="CASCADE")
    )
    assignment_id: Mapped[str | None] = mapped_column(
        ForeignKey("search_click_opportunity_assignments.id"), nullable=True
    )
    dispatch_claim_reservation_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_reservations.id", ondelete="CASCADE")
    )
    fulfillment_lane_claim_ordinal: Mapped[int] = mapped_column(Integer)
    expected_assignment_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_assignment_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_action_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    observed_action_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    classification: Mapped[str] = mapped_column(String(32))
    first_carrier_type: Mapped[str] = mapped_column(String(40), default="")
    first_carrier_id: Mapped[str] = mapped_column(String(36), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class DispatchAllocationExclusion(Base):
    __tablename__ = "dispatch_allocation_exclusions"
    __table_args__ = (
        UniqueConstraint(
            "dispatch_claim_window_id",
            "dispatch_claim_reservation_id",
            "fulfillment_lane_claim_ordinal",
            name="uq_dispatch_allocation_exclusion_unit",
        ),
        Index("ix_dispatch_allocation_exclusion_state", "state", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_new_uuid)
    dispatch_claim_window_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_windows.id", ondelete="CASCADE")
    )
    dispatch_claim_reservation_id: Mapped[str] = mapped_column(
        ForeignKey("dispatch_claim_reservations.id", ondelete="CASCADE")
    )
    fulfillment_lane_claim_ordinal: Mapped[int] = mapped_column(Integer)
    carrier_type: Mapped[str] = mapped_column(String(40))
    carrier_id: Mapped[str] = mapped_column(String(36))
    reason_code: Mapped[str] = mapped_column(String(80))
    solver_problem_component_hash: Mapped[str] = mapped_column(String(64), default="")
    resource_snapshot_hash: Mapped[str] = mapped_column(String(64))
    release_count: Mapped[int] = mapped_column(Integer, default=1)
    state: Mapped[str] = mapped_column(String(24), default="active")
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


__all__ = [
    "DispatchAllocationExclusion",
    "DispatchAllocationReleaseBatch",
    "DispatchAllocationReleaseBatchItem",
    "SearchClickAssignmentEpoch",
    "SearchClickOpportunityAssignment",
    "SearchClickSolverCarrierUnitBinding",
    "SearchClickSolverProblemComponent",
    "SearchClickSolverProblemSnapshot",
    "SearchClickTaskFairnessState",
]
