import pytest
from sqlalchemy import delete, select, text
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.database import SessionLocal
from app.models import Action, ExecutionAttempt, Task, Tenant
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from app.services.task_center.task_retirement import TaskGatewayFenced, guard_attempt_call_start


TENANT_ID = 956_226


@pytest.fixture
def retirement_scope():
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "postgresql"
        session.add(Tenant(id=TENANT_ID, name="任务退役并发"))
        session.flush()
        old = Task(tenant_id=TENANT_ID, type="channel_like", name="旧任务", status="running")
        replacement = Task(tenant_id=TENANT_ID, type="channel_like", name="新任务")
        session.add_all([old, replacement])
        session.flush()
        action = Action(tenant_id=TENANT_ID, task_id=old.id, task_type=old.type,
            action_type="like_message", status="executing", task_lifecycle_epoch=1)
        session.add(action)
        session.flush()
        attempt = ExecutionAttempt(tenant_id=TENANT_ID, action_id=action.id,
            task_lifecycle_epoch=1, status="before_call")
        session.add(attempt)
        session.commit()
        scope = (old.id, replacement.id, attempt.id)
    yield scope
    with SessionLocal() as session:
        session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.tenant_id == TENANT_ID))
        session.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()


def _retire(session, scope):
    old = session.scalar(select(Task).where(Task.id == scope[0]).with_for_update(nowait=True))
    old.status = "stopped"
    old.next_run_at = None
    old.retired_at = _now()
    old.replaced_by_task_id = scope[1]
    old.task_lifecycle_epoch += 1
    session.flush()


def test_retirement_wins_against_cached_gateway_task_and_cannot_be_overwritten(retirement_scope):
    with SessionLocal() as worker, SessionLocal() as operator:
        cached = worker.get(Task, retirement_scope[0])
        attempt = worker.get(ExecutionAttempt, retirement_scope[2])
        _retire(operator, retirement_scope)
        operator.commit()
        assert cached.status == "running"
        with pytest.raises(TaskGatewayFenced):
            guard_attempt_call_start(worker, attempt)
        assert cached.status == "stopped"
        assert not dispatcher._channel_action_replan_allowed(worker, worker.get(Action, attempt.action_id))
        assert attempt.status == "skipped_before_gateway"
        assert attempt.gateway_call_started_at is None
        worker.commit()
        with pytest.raises(IntegrityError), worker.begin_nested():
            worker.execute(text("UPDATE tasks SET status='running' WHERE id=:task"),
                {"task": retirement_scope[0]})


def test_in_progress_retirement_yields_explicit_pre_call_resource_wait(retirement_scope):
    with SessionLocal() as worker, SessionLocal() as operator:
        attempt = worker.get(ExecutionAttempt, retirement_scope[2])
        _retire(operator, retirement_scope)
        with pytest.raises(RuntimeResourceBlocked, match="task_lifecycle_admission_busy") as blocked:
            guard_attempt_call_start(worker, attempt)
        action = worker.get(Action, attempt.action_id)
        dispatcher._defer_engagement_gateway_admission(worker, action, blocked.value)
        assert attempt.gateway_call_started_at is None
        assert attempt.status == "skipped_before_gateway"
        assert action.status == "pending"
        worker.commit()
        operator.commit()


def test_call_issuance_wins_and_retirement_waits_for_its_commit(retirement_scope):
    with SessionLocal() as worker, SessionLocal() as operator:
        attempt = worker.get(ExecutionAttempt, retirement_scope[2])
        guard_attempt_call_start(worker, attempt)
        attempt.gateway_call_started_at = _now()
        attempt.status = "gateway_call_started"
        worker.flush()
        with pytest.raises(DBAPIError) as raised, operator.begin_nested():
            _retire(operator, retirement_scope)
        assert raised.value.orig.sqlstate == "55P03"
        worker.commit()
        _retire(operator, retirement_scope)
        operator.commit()
        worker.refresh(attempt)
        assert attempt.status == "gateway_call_started"
        assert attempt.gateway_call_started_at is not None
