"""Persist immutable portfolio snapshots and their current reservation ownership."""
from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models import AccountPortfolioLoadReservation, PortfolioFeasibilityPlanRevision, Task, TaskDayLedger
from .engagement_portfolio_allocation import _hash


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

