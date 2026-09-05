"""Read one tenant/day candidate set once for deterministic portfolio allocation."""
from collections import defaultdict

from sqlalchemy import func, select
from sqlalchemy.orm import load_only

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetPolicyRevision,
    AccountPortfolioLoadReservation, TgAccount,
)

from .engagement_shared_usage import OCCUPIED_BUDGET_STATES


ACTIVE_RESERVATION_STATE = "active"


def read_portfolio_capacities(session, tenant_id, task_day, *, account_ids, action_class, lock_policies=True):
    rows = session.scalars(select(TgAccount).options(load_only(TgAccount.id, TgAccount.account_identity)).where(
        TgAccount.tenant_id == tenant_id, TgAccount.id.in_(account_ids),
        TgAccount.deleted_at.is_(None)))
    accounts = {row.id: row.account_identity for row in rows}
    policies = _policies(session, tenant_id, tuple(sorted(set(accounts.values()))), lock=lock_policies)
    planned = _planned_usage(session, tenant_id, task_day, account_ids=account_ids)
    rows = session.scalars(select(AccountBehaviorBudgetLedger).options(load_only(
        AccountBehaviorBudgetLedger.account_id, AccountBehaviorBudgetLedger.counters)).where(
            AccountBehaviorBudgetLedger.tenant_id == tenant_id,
            AccountBehaviorBudgetLedger.task_day == task_day,
            AccountBehaviorBudgetLedger.account_id.in_(account_ids)))
    actual = {row.account_id: row.counters for row in rows}
    capacities, policy_ids = {}, set()
    for account_id in account_ids:
        if account_id not in accounts:
            capacities[account_id] = 0
            continue
        policy = policies.get(accounts[account_id])
        if policy is None:
            raise RuntimeError("account_behavior_budget_policy_missing")
        policy_ids.add(policy.id)
        capacities[account_id] = _remaining_capacity(policy.action_budgets or {},
            planned.get(account_id, {}), actual.get(account_id) or {}, action_class=action_class)
    return capacities, sorted(policy_ids)


def _policies(session, tenant_id, account_classes, *, lock):
    policy = AccountBehaviorBudgetPolicyRevision
    query = select(policy).options(load_only(
        policy.id, policy.account_class, policy.action_budgets)).where(
        policy.tenant_id == tenant_id, policy.account_class.in_(account_classes),
        policy.state == "active").order_by(policy.account_class)
    rows = session.scalars(query.with_for_update() if lock else query)
    return {row.account_class: row for row in rows}


def _planned_usage(session, tenant_id, task_day, *, account_ids):
    reservation = AccountPortfolioLoadReservation
    rows = session.execute(select(reservation.account_id, reservation.action_class,
        func.sum(reservation.reserved_units)).where(
            reservation.tenant_id == tenant_id, reservation.task_day == task_day,
            reservation.account_id.in_(account_ids), reservation.state == ACTIVE_RESERVATION_STATE,
        ).group_by(reservation.account_id, reservation.action_class))
    planned = defaultdict(dict)
    for account_id, action_class, units in rows:
        planned[account_id][action_class] = int(units or 0)
    return dict(planned)


def _remaining_capacity(budgets, planned, actual, *, action_class):
    classes = set(budgets) - {"total"}
    occupied = {key: max(int(planned.get(key) or 0), _actual_count(actual.get(key)))
        for key in classes | {action_class}}
    class_remaining = int(budgets.get(action_class) or 0) - occupied[action_class]
    total_limit = int(budgets.get("total") or sum(int(budgets[key] or 0) for key in classes))
    total_remaining = total_limit - sum(occupied[key] for key in classes)
    return max(0, min(class_remaining, total_remaining))


def _actual_count(states):
    states = dict(states or {})
    return sum(int(states.get(key) or 0) for key in OCCUPIED_BUDGET_STATES)
