from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountBehaviorBudgetLedger,
    AccountBehaviorBudgetPolicyRevision,
    AccountPortfolioLoadReservation,
    PortfolioFeasibilityPlanRevision,
    Task,
    TaskDayLedger,
    TgAccount,
)


POLICY_REVISION = "portfolio_account_budget_v1"
ACTIVE_RESERVATION_STATE = "active"


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


def _demand_hash(
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    demand_identity: str,
    request: dict,
) -> str:
    return _hash({
        "policy": POLICY_REVISION,
        "task": task.id,
        "day": str(ledger.obligation_local_date),
        "class": action_class,
        "identity": demand_identity,
        **request,
    })


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


def _allocation_for_request(
    rows: list[AccountPortfolioLoadReservation],
    request: dict,
) -> dict[int, int]:
    frozen = {row.account_id: int(row.reserved_units) for row in rows}
    fixed = {
        int(key): int(value)
        for key, value in request["requested_units_by_account"].items()
    }
    if fixed:
        return _positive({
            account_id: min(units, frozen.get(account_id, 0))
            for account_id, units in fixed.items()
        })
    candidates = {int(item) for item in request["candidate_account_ids"]}
    remaining = int(request["requested_units"])
    allocation: dict[int, int] = {}
    for account_id in sorted(frozen):
        if account_id not in candidates or remaining <= 0:
            continue
        units = min(frozen[account_id], remaining)
        allocation[account_id] = units
        remaining -= units
    return allocation


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


def _normalized_request(
    total_units: int,
    candidate_ids: list[int] | None,
    requested_by_account: dict[int, int] | None,
) -> dict:
    fixed = {
        int(account_id): max(0, int(units))
        for account_id, units in (requested_by_account or {}).items()
        if int(units) > 0
    }
    candidates = sorted({int(item) for item in (candidate_ids or fixed.keys())})
    requested = sum(fixed.values()) if fixed else max(0, int(total_units))
    return {
        "requested_units": requested,
        "candidate_account_ids": candidates,
        "requested_units_by_account": {str(key): value for key, value in fixed.items()},
    }


def _allocate_request(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    action_class: str,
    request: dict,
) -> tuple[dict[int, int], dict[int, int], list[str]]:
    account_ids = [int(item) for item in request["candidate_account_ids"]]
    accounts = _accounts(session, task, account_ids)
    capacities: dict[int, int] = {}
    policy_ids: set[str] = set()
    for account_id in account_ids:
        account = accounts.get(account_id)
        if account is None:
            capacities[account_id] = 0
            continue
        policy = _budget_policy(session, task.tenant_id, account.account_identity)
        policy_ids.add(policy.id)
        budgets = dict(policy.action_budgets or {})
        class_remaining = int(budgets.get(action_class) or 0) - _occupied_capacity(
            session,
            task,
            ledger,
            account_id=account_id,
            action_class=action_class,
        )
        total_limit = int(budgets.get("total") or sum(
            int(value or 0) for key, value in budgets.items() if key != "total"
        ))
        total_remaining = total_limit - _occupied_total_capacity(
            session, task, ledger, account_id=account_id, budgets=budgets,
        )
        capacities[account_id] = max(0, min(class_remaining, total_remaining))
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


def _distribute(
    task_id: str,
    request: dict,
    capacities: dict[int, int],
    requested: int,
) -> dict[int, int]:
    ordered = sorted(
        capacities,
        key=lambda account_id: _hash(
            {"task": task_id, "request": request, "account": account_id}
        ),
    )
    allocation = {account_id: 0 for account_id in ordered}
    remaining = requested
    while remaining > 0:
        progressed = False
        for account_id in ordered:
            if allocation[account_id] >= capacities[account_id]:
                continue
            allocation[account_id] += 1
            remaining -= 1
            progressed = True
            if remaining == 0:
                break
        if not progressed:
            break
    return _positive(allocation)


def _occupied_capacity(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    account_id: int,
    action_class: str,
) -> int:
    planned = int(
        session.scalar(
            select(func.sum(AccountPortfolioLoadReservation.reserved_units)).where(
                AccountPortfolioLoadReservation.tenant_id == task.tenant_id,
                AccountPortfolioLoadReservation.task_day == ledger.obligation_local_date,
                AccountPortfolioLoadReservation.account_id == account_id,
                AccountPortfolioLoadReservation.action_class == action_class,
                AccountPortfolioLoadReservation.state == ACTIVE_RESERVATION_STATE,
            )
        )
        or 0
    )
    behavior = session.scalar(
        select(AccountBehaviorBudgetLedger).where(
            AccountBehaviorBudgetLedger.tenant_id == task.tenant_id,
            AccountBehaviorBudgetLedger.account_id == account_id,
            AccountBehaviorBudgetLedger.task_day == ledger.obligation_local_date,
        )
    )
    states = dict((behavior.counters or {}).get(action_class) or {}) if behavior else {}
    actual = sum(
        int(states.get(key) or 0)
        for key in ("reserved", "call_issued", "unknown", "confirmed", "unowned")
    )
    return max(planned, actual)


def _occupied_total_capacity(
    session: Session,
    task: Task,
    ledger: TaskDayLedger,
    *,
    account_id: int,
    budgets: dict,
) -> int:
    return sum(
        _occupied_capacity(
            session,
            task,
            ledger,
            account_id=account_id,
            action_class=action_class,
        )
        for action_class in budgets
        if action_class != "total"
    )


def _budget_policy(
    session: Session,
    tenant_id: int,
    account_class: str,
) -> AccountBehaviorBudgetPolicyRevision:
    policy = session.scalar(
        select(AccountBehaviorBudgetPolicyRevision)
        .where(
            AccountBehaviorBudgetPolicyRevision.tenant_id == tenant_id,
            AccountBehaviorBudgetPolicyRevision.account_class == account_class,
            AccountBehaviorBudgetPolicyRevision.state == "active",
        )
        .with_for_update()
    )
    if policy is None:
        raise RuntimeError("account_behavior_budget_policy_missing")
    return policy


def _accounts(
    session: Session,
    task: Task,
    account_ids: list[int],
) -> dict[int, TgAccount]:
    rows = session.scalars(
        select(TgAccount).where(
            TgAccount.tenant_id == task.tenant_id,
            TgAccount.id.in_(account_ids),
            TgAccount.deleted_at.is_(None),
        )
    )
    return {row.id: row for row in rows}


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


def _positive(values: dict[int, int]) -> dict[int, int]:
    return {key: value for key, value in values.items() if value > 0}


def _hash(value) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


__all__ = [
    "PortfolioAllocationDecision",
    "reserve_portfolio_units",
    "task_account_portfolio_allowance",
]
