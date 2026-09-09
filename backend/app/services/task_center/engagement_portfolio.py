from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPortfolioLoadReservation,
    PortfolioFeasibilityPlanRevision,
    Task,
    TaskDayLedger,
)

from .account_assignment_eligibility import eligible_assignment_account_ids, UNIFIED_CONTRACT
from .engagement_portfolio_allocation import (
    _allocation_for_request, _demand_hash, _distribute, _normalized_request, _positive,
)
from .engagement_portfolio_capacity import ACTIVE_RESERVATION_STATE, read_portfolio_capacities
from .engagement_portfolio_records import _new_plan, _persist_reservations, _project_task
from .engagement_portfolio_recovery import recover_portfolio_deficit


@dataclass(frozen=True)
class PortfolioAllocationDecision:
    plan: PortfolioFeasibilityPlanRevision
    allocated_units_by_account: dict[int, int]
    requested_units: int
    allocated_units: int
    deficit_units: int

    @property
    def achievable(self) -> bool:
        return self.deficit_units == 0


def reserve_portfolio_units(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    total_units: int = 0,
    candidate_account_ids: list[int] | None = None,
    requested_units_by_account: dict[int, int] | None = None,
    allocatable_account_ids: frozenset[int] | None = None,
) -> PortfolioAllocationDecision:
    request = _normalized_request(
        total_units,
        candidate_account_ids,
        requested_units_by_account,
    )
    demand_hash = _demand_hash(
        task,
        ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        request=request,
    )
    existing = _existing_portfolio_decision(
        session,
        task,
        ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        demand_hash=demand_hash,
        request=request,
        allocatable_account_ids=allocatable_account_ids,
    )
    if existing is not None:
        return existing
    return _create_portfolio_decision(
        session,
        task,
        ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        demand_hash=demand_hash,
        request=request,
        allocatable_account_ids=allocatable_account_ids,
    )


def _create_portfolio_decision(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    demand_hash: str,
    request: dict,
    allocatable_account_ids: frozenset[int] | None = None,
) -> PortfolioAllocationDecision:
    allocation, capacities, policy_ids = _allocate_request(
        session,
        task,
        ledger,
        action_class=action_class,
        request=request,
        allocatable_account_ids=allocatable_account_ids,
    )
    plan = _persist_new_plan(
        session,
        task,
        ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        demand_hash=demand_hash,
        request=request,
        allocation=allocation,
        capacities=capacities,
        policy_ids=policy_ids,
    )
    _project_task(task, plan)
    return _allocation_decision(plan, allocation, request)


def _existing_portfolio_decision(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    demand_hash: str,
    request: dict,
    allocatable_account_ids: frozenset[int] | None = None,
) -> PortfolioAllocationDecision | None:
    plan = _existing_plan(
        session, task, ledger,
        action_class=action_class,
        demand_identity=demand_identity,
    )
    if plan is not None:
        if plan.demand_snapshot != [request]:
            rows = _existing_reservations(session, task, ledger, action_class=action_class,
                demand_identity=demand_identity)
            return _frozen_input_change_decision(session, task, rows, request=request, plan=plan)
        plan = recover_portfolio_deficit(
            session, task, ledger, plan=plan, request=request,
            allocatable_account_ids=allocatable_account_ids,
        )
        return _decision_for_plan(session, plan, request)
    rows = _existing_reservations(
        session, task, ledger,
        action_class=action_class,
        demand_identity=demand_identity,
    )
    if not rows:
        return None
    if any(row.demand_hash != demand_hash for row in rows):
        return _frozen_input_change_decision(session, task, rows, request=request)
    return _existing_decision(session, rows, request)


def _frozen_input_change_decision(
    session: Session,
    task: Task,
    rows: list[AccountPortfolioLoadReservation],
    *,
    request: dict,
    plan: PortfolioFeasibilityPlanRevision | None = None,
) -> PortfolioAllocationDecision:
    plan = plan or session.get(PortfolioFeasibilityPlanRevision, rows[0].portfolio_plan_id)
    if plan is None:
        raise RuntimeError("portfolio_plan_missing")
    allocation = _allocation_for_request(rows, request)
    requested = int(request["requested_units"])
    allocated = sum(allocation.values())
    deficit = requested - allocated
    stats = dict(task.stats or {})
    stats["portfolio_feasibility"] = {
        "plan_id": plan.id,
        "planning_horizon": plan.planning_horizon,
        "decision": "structurally_unachievable",
        "deficits": [{
            "reason": "portfolio_input_changed_after_freeze",
            "units": deficit,
        }],
    }
    task.stats = stats
    task.last_error = "portfolio_input_changed_after_freeze"
    return PortfolioAllocationDecision(
        plan, allocation, requested, allocated, deficit,
    )


def _persist_new_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    demand_hash: str,
    allocation: dict[int, int],
    capacities: dict[int, int],
    policy_ids: list[str],
    request: dict,
) -> PortfolioFeasibilityPlanRevision:
    requested = int(request["requested_units"])
    plan = _new_plan(
        session, task, ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        demand_hash=demand_hash,
        request=request,
        allocation=allocation,
        capacities=capacities,
        policy_ids=policy_ids,
        deficit=requested - sum(allocation.values()),
    )
    session.add(plan)
    session.flush()
    _persist_reservations(
        session, task, ledger, plan=plan, action_class=action_class,
        demand_identity=demand_identity, demand_hash=demand_hash,
        allocation=allocation,
    )
    return plan


def _allocation_decision(
    plan: PortfolioFeasibilityPlanRevision,
    allocation: dict[int, int],
    request: dict,
) -> PortfolioAllocationDecision:
    requested = int(request["requested_units"])
    allocated = sum(allocation.values())
    return PortfolioAllocationDecision(
        plan, allocation, requested, allocated, requested - allocated,
    )


def task_account_portfolio_allowance(
    session: Session,
    *,
    task_id: str,
    task_day,
    account_id: int,
    action_class: str,
) -> tuple[int, int] | None:
    task_total = session.scalar(
        select(func.sum(AccountPortfolioLoadReservation.reserved_units)).where(
            AccountPortfolioLoadReservation.task_id == task_id,
            AccountPortfolioLoadReservation.task_day == task_day,
            AccountPortfolioLoadReservation.action_class == action_class,
            AccountPortfolioLoadReservation.state == ACTIVE_RESERVATION_STATE,
        )
    )
    if task_total is None:
        planned = session.scalar(
            select(PortfolioFeasibilityPlanRevision.id)
            .where(
                PortfolioFeasibilityPlanRevision.trigger_task_id == task_id,
                PortfolioFeasibilityPlanRevision.planning_horizon == str(task_day),
                PortfolioFeasibilityPlanRevision.trigger_kind == action_class,
            )
            .limit(1)
        )
        return (0, 0) if planned else None
    allowance = session.scalar(
        select(func.sum(AccountPortfolioLoadReservation.reserved_units)).where(
            AccountPortfolioLoadReservation.task_id == task_id,
            AccountPortfolioLoadReservation.task_day == task_day,
            AccountPortfolioLoadReservation.account_id == account_id,
            AccountPortfolioLoadReservation.action_class == action_class,
            AccountPortfolioLoadReservation.state == ACTIVE_RESERVATION_STATE,
        )
    )
    return int(allowance or 0), int(task_total or 0)


def _allocate_request(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    request: dict,
    allocatable_account_ids: frozenset[int] | None = None,
) -> tuple[dict[int, int], dict[int, int], list[str]]:
    account_ids = [int(item) for item in request["candidate_account_ids"]]
    if allocatable_account_ids is not None:
        account_ids = [item for item in account_ids if item in allocatable_account_ids]
    if (task.type_config or {}).get("engagement_contract_version") == UNIFIED_CONTRACT:
        account_ids = list(eligible_assignment_account_ids(session, task.tenant_id, account_ids))
    capacities, policy_ids = read_portfolio_capacities(
        session, task.tenant_id, ledger.obligation_local_date,
        account_ids=account_ids, action_class=action_class,
    )
    fixed = {
        int(key): int(value)
        for key, value in request["requested_units_by_account"].items()
    }
    if fixed:
        allocation = {
            account_id: min(units, capacities.get(account_id, 0))
            for account_id, units in fixed.items()
        }
        return _positive(allocation), capacities, sorted(policy_ids)
    allocation = _distribute(
        task.id,
        request,
        capacities,
        int(request["requested_units"]),
    )
    return allocation, capacities, sorted(policy_ids)


def _existing_reservations(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
) -> list[AccountPortfolioLoadReservation]:
    return list(
        session.scalars(
            select(AccountPortfolioLoadReservation).where(
                AccountPortfolioLoadReservation.task_id == task.id,
                AccountPortfolioLoadReservation.task_day_ledger_id == ledger.id,
                AccountPortfolioLoadReservation.action_class == action_class,
                AccountPortfolioLoadReservation.demand_identity == demand_identity,
                AccountPortfolioLoadReservation.state == ACTIVE_RESERVATION_STATE,
            )
        )
    )


def _existing_decision(
    session: Session,
    rows: list[AccountPortfolioLoadReservation],
    request: dict,
) -> PortfolioAllocationDecision:
    plan = session.get(PortfolioFeasibilityPlanRevision, rows[0].portfolio_plan_id)
    if plan is None:
        raise RuntimeError("portfolio_plan_missing")
    allocation = {row.account_id: int(row.reserved_units) for row in rows}
    requested = int(request["requested_units"])
    allocated = sum(allocation.values())
    return PortfolioAllocationDecision(
        plan,
        allocation,
        requested,
        allocated,
        requested - allocated,
    )


def _existing_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
) -> PortfolioFeasibilityPlanRevision | None:
    return session.scalar(
        select(PortfolioFeasibilityPlanRevision).where(
            PortfolioFeasibilityPlanRevision.tenant_id == task.tenant_id,
            PortfolioFeasibilityPlanRevision.planning_horizon
            == str(ledger.obligation_local_date),
            PortfolioFeasibilityPlanRevision.trigger_task_id == task.id,
            PortfolioFeasibilityPlanRevision.trigger_kind == action_class,
            PortfolioFeasibilityPlanRevision.trigger_identity == demand_identity,
            PortfolioFeasibilityPlanRevision.state == "active",
        ).order_by(PortfolioFeasibilityPlanRevision.plan_revision.desc(),
            PortfolioFeasibilityPlanRevision.created_at.desc(), PortfolioFeasibilityPlanRevision.id.desc()).limit(1)
    )


def _decision_for_plan(
    session: Session,
    plan: PortfolioFeasibilityPlanRevision,
    request: dict,
) -> PortfolioAllocationDecision:
    rows = list(
        session.scalars(
            select(AccountPortfolioLoadReservation).where(
                AccountPortfolioLoadReservation.portfolio_plan_id == plan.id,
                AccountPortfolioLoadReservation.state == ACTIVE_RESERVATION_STATE,
            )
        )
    )
    allocation = {row.account_id: int(row.reserved_units) for row in rows}
    requested = int(request["requested_units"])
    allocated = sum(allocation.values())
    _project_task(session.get(Task, plan.trigger_task_id), plan)
    return PortfolioAllocationDecision(
        plan,
        allocation,
        requested,
        allocated,
        requested - allocated,
    )


__all__ = [
    "PortfolioAllocationDecision",
    "reserve_portfolio_units",
    "task_account_portfolio_allowance",
]
