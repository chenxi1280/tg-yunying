from datetime import date, datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from app.database import SessionLocal
from app.models import (
    AccountBehaviorBudgetLedger, AccountBehaviorBudgetReservation, AccountPool,
    AccountPoolConcurrencyLease, Action, ExecutionAttempt, RemoteInvocationFence,
    Task, Tenant, TgAccount,
)
from app.services.task_center.engagement_binding import (
    freeze_initial_binding, freeze_membership_snapshot, validate_engagement_binding,
)
from app.services.task_center.engagement_runtime_resources import (
    mark_attempt_call_issued, reserve_attempt_resources, settle_attempt_resources,
)


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 952_201
POOL_ID = 952_202
ACCOUNT_ID = 952_203
BOUNDARY = datetime(2026, 9, 5, 16, tzinfo=timezone.utc)


def _seed(session):
    session.add(Tenant(id=TENANT_ID, name="存量资源日期测试"))
    session.flush()
    session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="原组"))
    session.flush()
    session.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID, pool_id=POOL_ID,
        display_name="原账号", phone_masked="test"))
    config = {"engagement_contract_version": "unified_engagement_v1",
        "account_selection_mode": "group", "account_group_ids": [POOL_ID]}
    task = Task(tenant_id=TENANT_ID, type="channel_like", name="存量接管", type_config=config)
    session.add(task)
    session.flush()
    binding = freeze_initial_binding(session, task,
        validate_engagement_binding(session, TENANT_ID, task.type, config))
    binding.effective_from = BOUNDARY
    session.flush()
    snapshot = freeze_membership_snapshot(session, task,
        participation_unit=f"legacy_cutover:{binding.id}")
    action = Action(tenant_id=TENANT_ID, task_id=task.id, task_type=task.type,
        action_type="like_message", account_id=ACCOUNT_ID,
        created_at=BOUNDARY - timedelta(seconds=1), pacing_due_at=BOUNDARY - timedelta(seconds=1))
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(tenant_id=TENANT_ID, action_id=action.id,
        account_id=ACCOUNT_ID, status="before_call")
    session.add(attempt)
    session.flush()
    return action, attempt, snapshot


def test_postgres_legacy_triplet_keeps_original_beijing_day_and_unknown():
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "postgresql"
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        action, attempt, snapshot = _seed(session)
        session.expire_all()

        reserve_attempt_resources(session, action, attempt)
        mark_attempt_call_issued(session, attempt)
        action.status, attempt.status = "unknown_after_send", "result_unknown"
        settle_attempt_resources(attempt, action, remote_mutation_started=None)
        session.flush()
        session.expire_all()

        ledger = session.scalar(select(AccountBehaviorBudgetLedger).where(
            AccountBehaviorBudgetLedger.account_id == ACCOUNT_ID))
        reservation = session.scalar(select(AccountBehaviorBudgetReservation).where(
            AccountBehaviorBudgetReservation.attempt_id == attempt.id))
        lease = session.scalar(select(AccountPoolConcurrencyLease).where(
            AccountPoolConcurrencyLease.attempt_id == attempt.id))
        fence = session.scalar(select(RemoteInvocationFence).where(
            RemoteInvocationFence.attempt_id == attempt.id))
        assert ledger.task_day == date(2026, 9, 5)
        assert reservation.ledger_id == ledger.id and reservation.state == "unknown"
        assert lease.account_pool_id == POOL_ID and lease.state == "remote_unknown"
        assert fence.state == "remote_unknown" and fence.transport_terminated_at is None
        assert attempt.result_snapshot["engagement_membership_snapshot_set_id"] == snapshot.id
        assert action.pacing_due_at == BOUNDARY - timedelta(seconds=1)
        session.rollback()
