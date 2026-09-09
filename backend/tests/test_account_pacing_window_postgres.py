from datetime import timedelta

import pytest
from sqlalchemy import select, text

from app.models import AccountBehaviorBudgetPolicyRevision, Action, Task, Tenant, TgAccount
from app.services.task_center.engagement_behavior_sessions import ensure_behavior_session_plan
from app.services.task_center.account_pacing_guard import (
    bind_account_pacing_reservation, revalidate_action_pacing_before_claim,
)
from tests.test_account_pacing_window_intersection import (
    DAY, DEADLINE, FIRST_END, FIRST_START, NEXT_START, _block, _reserve,
)
from tests.postgres_pacing_e4_fixture import factory as factory


pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _setup_postgres(session):
    session.add(Tenant(id=1, name="window test"))
    session.flush()
    session.add(TgAccount(id=11, tenant_id=1, display_name="window", phone_masked="11"))
    task = Task(id="group-task", tenant_id=1, name="window", type="group_ai_chat",
                type_config={"engagement_contract_version": "unified_engagement_v1"})
    session.add(task)
    session.add(AccountBehaviorBudgetPolicyRevision(
        id="window-policy", tenant_id=1, account_class="normal",
        action_budgets={"authored_message": 37}, session_budget={}, wake_budget=2,
        pair_gap_policy={"authored_to_authored_seconds": 300},
    ))
    session.flush()
    plan = ensure_behavior_session_plan(session, tenant_id=1, account_id=11, task_day=DAY.date())
    plan.windows = [
        {"start_at": FIRST_START.isoformat(), "end_at": FIRST_END.isoformat()},
        {"start_at": NEXT_START.isoformat(), "end_at": DEADLINE.isoformat()},
    ]
    session.flush()
    return task


def test_window_claim_keeps_account_and_task_locks_until_commit(factory):
    with factory() as session:
        assert session.scalar(text('SHOW timezone')) == 'Asia/Shanghai'
        task = _setup_postgres(session)
        reservation = _reserve(session, task, due=FIRST_START)
        mine = _block(session, task, at=FIRST_START)
        mine.pacing_due_at = FIRST_START
        mine.pacing_slot_key = reservation.pacing_slot_key
        bind_account_pacing_reservation(reservation, mine)
        _block(session, task, at=FIRST_END - timedelta(minutes=2), status="executing")
        action_id, task_id = mine.id, task.id
        session.commit()
    with factory() as owner, factory() as contender:
        mine = owner.get(Action, action_id)
        decision = revalidate_action_pacing_before_claim(
            owner, mine, now_value=FIRST_END - timedelta(minutes=1),
        )
        assert not decision.allowed
        assert decision.effective_claim_at == NEXT_START
        account_lock = select(TgAccount.id).where(TgAccount.id == 11).with_for_update(skip_locked=True)
        task_lock = select(Task.id).where(Task.id == task_id).with_for_update(skip_locked=True)
        assert contender.scalar(account_lock) is None
        assert contender.scalar(task_lock) is None
        owner.commit()
        assert contender.scalar(account_lock) == 11
        assert contender.scalar(task_lock) == task_id
