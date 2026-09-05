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

from .engagement_portfolio_allocation import (
    _allocation_for_request, _demand_hash, _distribute, _hash, _normalized_request, _positive,
)
from .engagement_portfolio_capacity import ACTIVE_RESERVATION_STATE, read_portfolio_capacities


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
) -> PortfolioAllocationDecision:
    allocation, capacities, policy_ids = _allocate_request(
        session,
        task,
        ledger,
        action_class=action_class,
        request=request,
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
) -> PortfolioAllocationDecision | None:
    plan = _existing_plan(
        session, task, ledger,
        action_class=action_class,
        demand_identity=demand_identity,
        demand_hash=demand_hash,
    )
    if plan is not None:
        return _decision_for_plan(session, plan, request)
    rows = _existing_reservations(
        session, task, ledger,
        action_class=action_class,
        demand_identity=demand_identity,
    )
    if not rows:
        return None
    if any(row.demand_hash != demand_hash for row in rows):
        return _frozen_input_change_decision(session, task, rows, request)
    return _existing_decision(session, rows, request)


def _frozen_input_change_decision(
    session: Session,
    task: Task,
    rows: list[AccountPortfolioLoadReservation],
    request: dict,
) -> PortfolioAllocationDecision:
    plan = session.get(PortfolioFeasibilityPlanRevision, rows[0].portfolio_plan_id)
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


def _persist_reservations(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    plan: PortfolioFeasibilityPlanRevision,
    action_class: str,
    demand_identity: str,
    demand_hash: str,
    allocation: dict[int, int],
) -> None:
    for account_id, units in allocation.items():
        session.add(AccountPortfolioLoadReservation(
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            portfolio_plan_id=plan.id,
            account_id=account_id,
            task_day=ledger.obligation_local_date,
            action_class=action_class,
            demand_identity=demand_identity,
            demand_hash=demand_hash,
            reserved_units=units,
        ))
    session.flush()


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
) -> tuple[dict[int, int], dict[int, int], list[str]]:
    account_ids = [int(item) for item in request["candidate_account_ids"]]
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
    demand_hash: str,
) -> PortfolioFeasibilityPlanRevision | None:
    return session.scalar(
        select(PortfolioFeasibilityPlanRevision).where(
            PortfolioFeasibilityPlanRevision.tenant_id == task.tenant_id,
            PortfolioFeasibilityPlanRevision.planning_horizon
            == str(ledger.obligation_local_date),
            PortfolioFeasibilityPlanRevision.trigger_task_id == task.id,
            PortfolioFeasibilityPlanRevision.trigger_kind == action_class,
            PortfolioFeasibilityPlanRevision.trigger_identity == demand_identity,
            PortfolioFeasibilityPlanRevision.input_hash == demand_hash,
        )
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


def _new_plan(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    demand_hash: str,
    request: dict,
    allocation: dict[int, int],
    capacities: dict[int, int],
    policy_ids: list[str],
    deficit: int,
) -> PortfolioFeasibilityPlanRevision:
    revision = int(
        session.scalar(
            select(func.max(PortfolioFeasibilityPlanRevision.plan_revision)).where(
                PortfolioFeasibilityPlanRevision.tenant_id == task.tenant_id,
                PortfolioFeasibilityPlanRevision.planning_horizon
                == str(ledger.obligation_local_date),
            )
        )
        or 0
    ) + 1
    return PortfolioFeasibilityPlanRevision(
        tenant_id=task.tenant_id,
        planning_horizon=str(ledger.obligation_local_date),
        plan_revision=revision,
        trigger_task_id=task.id,
        trigger_kind=action_class,
        trigger_identity=demand_identity,
        task_set_hash=_task_set_hash(session, task.tenant_id),
        policy_revision_ids=policy_ids,
        demand_snapshot=[request],
        account_task_day_load=[
            {
                "account_id": account_id,
                "capacity_before": capacities.get(account_id, 0),
                "allocated_units": allocation.get(account_id, 0),
            }
            for account_id in sorted(capacities)
        ],
        deficits=[{"action_class": action_class, "units": deficit}]
        if deficit
        else [],
        decision="structurally_unachievable" if deficit else "guaranteed_achievable",
        input_hash=demand_hash,
    )


def _task_set_hash(session: Session, tenant_id: int) -> str:
    rows = session.execute(
        select(Task.id, Task.config_revision, Task.status)
        .where(
            Task.tenant_id == tenant_id,
            Task.status.in_(("pending", "running")),
        )
        .order_by(Task.id)
    ).all()
    return _hash(
        [
            {"task_id": task_id, "config_revision": revision, "status": status}
            for task_id, revision, status in rows
        ]
    )


def _project_task(task: Task, plan: PortfolioFeasibilityPlanRevision) -> None:
    stats = dict(task.stats or {})
    stats["portfolio_feasibility"] = {
        "plan_id": plan.id,
        "planning_horizon": plan.planning_horizon,
        "decision": plan.decision,
        "deficits": list(plan.deficits or []),
    }
    task.stats = stats
    if plan.decision == "structurally_unachievable":
        task.last_error = "portfolio_capacity_insufficient"
    elif task.last_error == "portfolio_capacity_insufficient":
        task.last_error = ""


__all__ = [
    "PortfolioAllocationDecision",
    "reserve_portfolio_units",
    "task_account_portfolio_allowance",
]
