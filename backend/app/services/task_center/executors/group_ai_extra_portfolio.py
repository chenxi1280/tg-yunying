"""Filter extra-volume supply against its existing task/day portfolio."""
from sqlalchemy import and_, func, or_, select

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation,
    AccountPortfolioLoadReservation, Action, PortfolioFeasibilityPlanRevision,
    TaskGroupDailyMessageSlot, TaskGroupDailyTarget, TaskMembershipAdmissionItem,
)

from ..engagement_shared_usage import RESERVATION_OCCUPIED_STATES


ACTION_CLASS = "authored_message"
OPEN_MESSAGE_STATES = ("pending", "claiming", "executing", "retryable_failed")


def with_extra_portfolio_capacity(statement, spec):
    allowance = _allowance(spec)
    occupied = _occupied_budget(spec)
    used = select(occupied.c.account_id, func.sum(occupied.c.units).label("units")).group_by(
        occupied.c.account_id).subquery()
    pending = _pending_units(spec, occupied)
    account_id = TaskMembershipAdmissionItem.account_id
    remaining = func.coalesce(allowance.c.units, 0) - func.coalesce(used.c.units, 0) - func.coalesce(pending.c.units, 0)
    return (statement.outerjoin(allowance, allowance.c.account_id == account_id)
        .outerjoin(used, used.c.account_id == account_id)
        .outerjoin(pending, pending.c.account_id == account_id)
        .where(or_(remaining > 0, and_(~_has_plan(spec), ~_has_reservations(spec)))))


def _allowance(spec):
    row = AccountPortfolioLoadReservation
    return (select(row.account_id, func.sum(row.reserved_units).label("units"))
        .where(*_portfolio_scope(spec)).group_by(row.account_id).subquery())


def _portfolio_scope(spec):
    row = AccountPortfolioLoadReservation
    return (row.tenant_id == spec.tenant_id, row.task_id == spec.task_id,
        row.task_day == spec.coverage_date, row.action_class == ACTION_CLASS, row.state == "active")


def _has_reservations(spec):
    return select(AccountPortfolioLoadReservation.id).where(*_portfolio_scope(spec)).exists()


def _has_plan(spec):
    row = PortfolioFeasibilityPlanRevision
    return select(row.id).where(row.tenant_id == spec.tenant_id,
        row.trigger_task_id == spec.task_id, row.planning_horizon == str(spec.coverage_date),
        row.trigger_kind == ACTION_CLASS).exists()


def _occupied_budget(spec):
    row, ledger = AccountBehaviorBudgetReservation, AccountBehaviorBudgetLedger
    return (select(ledger.account_id, row.action_id, func.sum(row.amount).label("units"))
        .join(ledger, ledger.id == row.ledger_id)
        .where(ledger.tenant_id == spec.tenant_id, ledger.task_day == spec.coverage_date,
            row.task_id == spec.task_id, row.action_class == ACTION_CLASS,
            row.state.in_(RESERVATION_OCCUPIED_STATES))
        .group_by(ledger.account_id, row.action_id).cte("extra_budget_occupancy"))


def _pending_units(spec, occupied):
    slot, target = TaskGroupDailyMessageSlot, TaskGroupDailyTarget
    original_ledger = func.coalesce(
        func.nullif(Action.payload["task_day_ledger_id"].as_string(), ""),
        slot.task_day_ledger_id, target.task_day_ledger_id)
    return (select(Action.account_id, func.count(Action.id).label("units"))
        .outerjoin(slot, and_(slot.id == Action.primary_quantity_slot_id,
            slot.tenant_id == Action.tenant_id, slot.task_id == Action.task_id))
        .outerjoin(target, and_(target.id == Action.payload["daily_group_target_id"].as_string(),
            target.tenant_id == Action.tenant_id, target.task_id == Action.task_id))
        .where(Action.tenant_id == spec.tenant_id, Action.task_id == spec.task_id,
            Action.action_type == "send_message", Action.status.in_(OPEN_MESSAGE_STATES),
            original_ledger == spec.task_day_ledger_id,
            Action.id.not_in(select(occupied.c.action_id)))
        .group_by(Action.account_id).subquery())
