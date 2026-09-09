"""Add only previously unallocated demand when current capacity becomes available."""
from sqlalchemy import select

from app.models import AccountPortfolioLoadReservation, PortfolioFeasibilityPlanRevision
from app.services._common import _now
from app.timezone import as_beijing
from .account_assignment_eligibility import assignment_decisions, publish_assignment_summary
from .engagement_portfolio_allocation import _distribute, _hash, _positive
from .engagement_portfolio_capacity import read_portfolio_capacities
from .engagement_portfolio_records import _new_plan, _persist_reservations


def recover_portfolio_deficit(
    session, task, ledger, *, plan, request, allocatable_account_ids=None,
):
    if not _can_recover(task, ledger, plan):
        return plan
    plan = _lock_current_plan(session, plan)
    if plan.demand_snapshot != [request] or not _can_recover(task, ledger, plan):
        return plan
    rows = _reservations(session, plan, ledger)
    frozen = _frozen_allocation(plan, rows)
    remaining = int(request["requested_units"]) - sum(frozen.values())
    if remaining <= 0:
        return plan
    candidates = set(request["candidate_account_ids"]) - {row.account_id for row in rows if row.state != "active"}
    if allocatable_account_ids is not None:
        candidates &= set(allocatable_account_ids)
    qualifications = assignment_decisions(session, task.tenant_id, candidates, skip_busy=True)
    capacities, policies = read_portfolio_capacities(session, task.tenant_id, ledger.obligation_local_date,
        account_ids=sorted(key for key in candidates if not qualifications[key]), action_class=plan.trigger_kind)
    publish_assignment_summary(task, qualifications)
    additional = _additional_allocation(task, request, capacities=capacities, frozen=frozen, remaining=remaining)
    if not additional:
        return plan
    return _persist_successor(session, task, ledger, plan=plan, request=request, rows=rows,
        frozen=frozen, additional=additional, capacities=capacities, policies=policies)


def _can_recover(task, ledger, plan):
    return (task.status == "running" and task.deleted_at is None and task.retired_at is None
        and ledger.lifecycle_status == "open" and as_beijing(ledger.deadline_at) > as_beijing(_now())
        and bool(plan.deficits))


def _lock_current_plan(session, plan):
    current = session.scalar(select(PortfolioFeasibilityPlanRevision).where(
        PortfolioFeasibilityPlanRevision.id == plan.id,
    ).with_for_update().execution_options(populate_existing=True))
    if current.state == "active":
        return current
    current = session.scalar(select(PortfolioFeasibilityPlanRevision).where(
        PortfolioFeasibilityPlanRevision.tenant_id == plan.tenant_id,
        PortfolioFeasibilityPlanRevision.trigger_task_id == plan.trigger_task_id,
        PortfolioFeasibilityPlanRevision.planning_horizon == plan.planning_horizon,
        PortfolioFeasibilityPlanRevision.trigger_kind == plan.trigger_kind,
        PortfolioFeasibilityPlanRevision.trigger_identity == plan.trigger_identity,
        PortfolioFeasibilityPlanRevision.state == "active",
    ).order_by(PortfolioFeasibilityPlanRevision.plan_revision.desc(),
        PortfolioFeasibilityPlanRevision.created_at.desc(), PortfolioFeasibilityPlanRevision.id.desc())
        .limit(1).with_for_update().execution_options(populate_existing=True))
    if current is None:
        raise RuntimeError("portfolio_active_successor_missing")
    return current


def _reservations(session, plan, ledger):
    return list(session.scalars(select(AccountPortfolioLoadReservation).where(
        AccountPortfolioLoadReservation.tenant_id == plan.tenant_id,
        AccountPortfolioLoadReservation.task_id == plan.trigger_task_id,
        AccountPortfolioLoadReservation.task_day_ledger_id == ledger.id,
        AccountPortfolioLoadReservation.action_class == plan.trigger_kind,
        AccountPortfolioLoadReservation.demand_identity == plan.trigger_identity,
    ).order_by(AccountPortfolioLoadReservation.account_id).with_for_update()
        .execution_options(populate_existing=True)))


def _frozen_allocation(plan, rows):
    frozen = _positive({int(item["account_id"]): int(item["allocated_units"])
        for item in plan.account_task_day_load or []})
    reserved = {row.account_id: int(row.reserved_units) for row in rows}
    if frozen != reserved:
        raise RuntimeError("portfolio_reservation_snapshot_mismatch")
    return frozen


def _additional_allocation(task, request, *, capacities, frozen, remaining):
    fixed = request["requested_units_by_account"]
    if fixed:
        return _positive({key: min(capacities[key], max(0, int(fixed.get(str(key), 0)) - frozen.get(key, 0)))
            for key in capacities})
    return _distribute(task.id, request, capacities, remaining)


def _persist_successor(session, task, ledger, *, plan, request, rows, frozen, additional, capacities, policies):
    allocation = {key: frozen.get(key, 0) + additional.get(key, 0) for key in set(frozen) | set(additional)}
    digest = _hash({"request": request, "previous_plan_id": plan.id,
        "additional": additional, "capacities": capacities, "policies": policies})
    successor = _new_plan(session, task, ledger, action_class=plan.trigger_kind,
        demand_identity=plan.trigger_identity, demand_hash=digest, request=request,
        allocation=allocation, capacities={**dict.fromkeys(frozen, 0), **capacities},
        policy_ids=sorted(set(plan.policy_revision_ids or []) | set(policies)),
        deficit=int(request["requested_units"]) - sum(allocation.values()))
    session.add(successor)
    plan.state = "superseded"
    session.flush()
    existing = {row.account_id: row for row in rows}
    for row in rows:
        row.portfolio_plan_id = successor.id
        if row.state == "active":
            row.reserved_units += additional.get(row.account_id, 0)
    original_hash = rows[0].demand_hash if rows else plan.input_hash
    _persist_reservations(session, task, ledger, plan=successor, action_class=plan.trigger_kind,
        demand_identity=plan.trigger_identity, demand_hash=original_hash,
        allocation={key: units for key, units in additional.items() if key not in existing})
    return successor
