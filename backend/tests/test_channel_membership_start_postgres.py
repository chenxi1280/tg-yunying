from datetime import timedelta

import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError

from app.database import SessionLocal
from app.models import AccountPool, Action, ExecutionAttempt, OperationTarget, Task, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.channel_membership_start import prepare_channel_membership_on_start
from app.services.task_center.direct_action_claims import claim_fact_first_candidates
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.task_retirement import guard_attempt_call_start


TENANT_ID = 957_224


@pytest.fixture
def scope():
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="创建关注事务"))
        session.flush()
        pool = AccountPool(tenant_id=TENANT_ID, name="关注账号", is_default=True)
        target = OperationTarget(tenant_id=TENANT_ID, target_type="channel", tg_peer_id="-100957224", title="关注频道")
        session.add_all([pool, target])
        session.flush()
        account = TgAccount(tenant_id=TENANT_ID, pool_id=pool.id, display_name="关注账号", phone_masked="***", status="在线")
        session.add(account)
        session.flush()
        task = Task(
            tenant_id=TENANT_ID, name="定时频道浏览", type="channel_view", status="pending",
            task_lifecycle_epoch=2, fulfillment_contract_version="fact_first_v3",
            scheduled_start=_now() + timedelta(days=2), type_config={"target_channel_id": target.id},
            account_config={"selection_mode": "manual", "account_ids": [account.id]},
        )
        session.add(task)
        session.flush()
        prepare_channel_membership_on_start(session, task)
        session.commit()
        action = session.scalar(select(Action).where(Action.task_id == task.id))
        identities = task.id, action.id
    yield identities
    with SessionLocal() as session:
        for model in (ExecutionAttempt, Action, Task, TgGroup, OperationTarget, TgAccount, AccountPool):
            session.execute(delete(model).where(model.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()


def _attempt(session, action):
    attempt = ExecutionAttempt(
        tenant_id=TENANT_ID, action_id=action.id, account_id=action.account_id, attempt_no=1,
        task_lifecycle_epoch=action.task_lifecycle_epoch, status="before_call", before_call_at=_now(),
    )
    session.add(attempt)
    return attempt


def test_pending_membership_claim_and_final_gateway_guard_use_postgres(scope):
    with SessionLocal() as session:
        batch = claim_fact_first_candidates(session, owner="test", limit=1, now=_now(), lease_seconds=60)
        assert batch.action_ids == (scope[1],)
        assert dispatcher._confirm_claim(session, scope[1], owner=batch.owner, token=batch.token)
        action = session.get(Action, scope[1])
        assert dispatcher._fulfillment_route_allows_gateway(session, action)
        attempt = _attempt(session, action)
        session.flush()
        guard_attempt_call_start(session, attempt)
        session.commit()


def test_concurrent_pause_still_fences_scheduled_membership_call(scope):
    with SessionLocal() as worker, SessionLocal() as operator:
        action = worker.get(Action, scope[1])
        attempt = _attempt(worker, action)
        worker.commit()
        task = operator.scalar(select(Task).where(Task.id == scope[0]).with_for_update())
        task.status = "paused"
        task.task_lifecycle_epoch += 1
        operator.flush()
        with pytest.raises(RuntimeResourceBlocked, match="task_lifecycle_admission_busy"):
            guard_attempt_call_start(worker, attempt)
        assert attempt.gateway_call_started_at is None
        worker.rollback()
        operator.rollback()


def test_resume_row_lock_prevents_late_attempt_insertion(scope):
    with SessionLocal() as worker, SessionLocal() as caller:
        task = worker.scalar(select(Task).where(Task.id == scope[0]).with_for_update())
        task.task_lifecycle_epoch += 2
        worker.flush()
        prepare_channel_membership_on_start(worker, task)
        action = caller.get(Action, scope[1])
        caller.execute(text("SET LOCAL lock_timeout = '100ms'"))
        _attempt(caller, action)
        with pytest.raises(DBAPIError) as raised:
            caller.flush()
        assert raised.value.orig.sqlstate == "55P03"
        caller.rollback()
        worker.commit()
