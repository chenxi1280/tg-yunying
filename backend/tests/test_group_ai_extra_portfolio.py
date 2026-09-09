from datetime import datetime, timedelta

import pytest

from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation,
    AccountPortfolioLoadReservation, Action, PortfolioFeasibilityPlanRevision,
    TaskGroupDailyMessageSlot,
)
from app.services.task_center.executors.group_ai_extra_candidates import (
    DAILY_GROUP_EXTRA_CANDIDATE_LIMIT, daily_group_extra_candidate_ids,
)
from tests import test_group_ai_extra_candidates as candidate_support


pytestmark = pytest.mark.no_postgres
session = candidate_support.session
_seed_candidates = candidate_support._seed_candidates


def _allowance(session, spec, *, account_id=1, units=3, day=None, task_id=None):
    session.add(AccountPortfolioLoadReservation(tenant_id=spec.tenant_id,
        task_id=task_id or spec.task_id, task_day_ledger_id=spec.task_day_ledger_id,
        portfolio_plan_id="QA-plan", account_id=account_id, task_day=day or spec.coverage_date,
        action_class="authored_message", demand_identity=f"QA-{account_id}", demand_hash="QA",
        reserved_units=units, state="active"))


def _budget(session, spec, *, action_id="used", account_id=1, units=1, state="confirmed", day=None):
    identity = f"budget-{action_id}"
    session.add(AccountBehaviorBudgetLedger(id=identity, tenant_id=spec.tenant_id,
        account_id=account_id, task_day=day or spec.coverage_date, policy_revision_id="QA"))
    session.flush()
    session.add(AccountBehaviorBudgetReservation(ledger_id=identity, task_id=spec.task_id,
        action_id=action_id, attempt_id=identity, action_class="authored_message", amount=units, state=state))


def _pending(session, spec, *, action_id="pending", ledger_id=None, slot=False, status="pending"):
    identity = ledger_id or spec.task_day_ledger_id
    action = Action(id=action_id, tenant_id=spec.tenant_id, task_id=spec.task_id,
        task_type="group_ai_chat", action_type="send_message", status=status,
        account_id=1, scheduled_at=datetime(2026, 8, 18, 10),
        payload={} if slot else {"task_day_ledger_id": identity})
    if slot:
        session.add(TaskGroupDailyMessageSlot(id=f"slot-{action_id}", tenant_id=spec.tenant_id,
            task_id=spec.task_id, task_day_ledger_id=identity,
            target_operation_target_id=1, slot_kind="extra", slot_ordinal=1))
        action.primary_quantity_slot_id = f"slot-{action_id}"
    session.add(action)
    return action


@pytest.mark.parametrize("state", ["reserved", "call_issued", "unknown", "confirmed"])
def test_used_full_allowance_excludes_extra_candidate(session, state):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec)
    _budget(session, spec, units=3, state=state)
    assert daily_group_extra_candidate_ids(session, spec) == []


@pytest.mark.parametrize("slot", [False, True])
@pytest.mark.parametrize("status", ["pending", "claiming", "executing", "retryable_failed"])
def test_unbudgeted_pending_work_consumes_remaining_supply(session, slot, status):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec)
    _budget(session, spec, units=2)
    _pending(session, spec, slot=slot, status=status)
    session.autoflush = False
    assert daily_group_extra_candidate_ids(session, spec) == []


def test_same_action_budget_and_pending_projection_are_not_double_counted(session):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec, units=2)
    _budget(session, spec, action_id="pending", state="reserved")
    _pending(session, spec)
    assert daily_group_extra_candidate_ids(session, spec) == [1]


def test_released_budget_and_terminal_actions_do_not_consume_supply(session):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec, units=1)
    _budget(session, spec, state="released")
    _pending(session, spec, status="failed")
    assert daily_group_extra_candidate_ids(session, spec) == [1]


def test_previous_day_work_does_not_consume_new_day_supply(session):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec, units=1)
    _budget(session, spec, day=spec.coverage_date - timedelta(days=1))
    _pending(session, spec, ledger_id="previous-ledger", slot=True)
    assert daily_group_extra_candidate_ids(session, spec) == [1]


def test_another_task_allowance_cannot_supply_this_task(session):
    spec = _seed_candidates(session, 2)
    _allowance(session, spec, account_id=2)
    _allowance(session, spec, task_id="another-task")
    assert daily_group_extra_candidate_ids(session, spec) == [2]


def test_zero_allocation_plan_is_not_legacy_unrestricted_supply(session):
    spec = _seed_candidates(session, 1)
    session.add(PortfolioFeasibilityPlanRevision(tenant_id=1, planning_horizon=str(spec.coverage_date),
        trigger_task_id=spec.task_id, trigger_kind="authored_message", trigger_identity="QA",
        plan_revision=1, task_set_hash="QA", input_hash="QA", decision="structurally_unachievable"))
    assert daily_group_extra_candidate_ids(session, spec) == []


def test_capacity_filter_precedes_candidate_limit(session):
    spec = _seed_candidates(session, DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 1)
    eligible_id = DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 1
    _allowance(session, spec, account_id=eligible_id, units=1)
    assert daily_group_extra_candidate_ids(session, spec) == [eligible_id]


def test_second_scan_in_same_transaction_sees_newly_planned_work(session):
    spec = _seed_candidates(session, 1)
    _allowance(session, spec, units=1)
    session.autoflush = False
    assert daily_group_extra_candidate_ids(session, spec) == [1]
    _pending(session, spec)
    assert daily_group_extra_candidate_ids(session, spec) == []
