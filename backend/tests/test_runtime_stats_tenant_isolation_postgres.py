from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

from sqlalchemy import delete, select

from app.database import Base, SessionLocal, engine
from app.models import Action, Task, TaskRuntimeSummary, Tenant
from app.services._common import _now
from app.services.task_center.service import _claim_stale_executing_action_ids
from app.services.task_center.stats import refresh_task_stats


TENANT_ID = 914_793
FOREIGN_TENANT_ID = TENANT_ID + 1
TASK_ID = "pg-runtime-tenant-task"
OTHER_TASK_ID = f"{TASK_ID}-other"
FOREIGN_ACTION_ID = "pg-runtime-foreign-action"


def test_task_stats_ignore_cross_tenant_action_reference() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_task(TASK_ID)
        _seed_foreign_action()
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            stats = refresh_task_stats(session, task, include_configured_accounts=False)
            session.commit()
            summary = session.scalar(select(TaskRuntimeSummary).where(TaskRuntimeSummary.task_id == TASK_ID))

        assert stats["total_actions"] == 0
        assert summary.planned_count == 0
    finally:
        _cleanup()


def test_recovery_claim_ignores_cross_tenant_action_reference() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_task(TASK_ID)
        _seed_foreign_action()
        with SessionLocal() as session:
            claims, _stale_workers = _claim_stale_executing_action_ids(
                session,
                now=_now(),
                timeout_minutes=30,
                limit=20,
            )

        assert [claim.action_id for claim in claims] == []
    finally:
        _cleanup()


def test_recovery_workers_continue_across_multiple_tasks() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_task(TASK_ID, action_count=20, action_prefix="pg-runtime-a")
        _seed_task(OTHER_TASK_ID, action_count=20, action_prefix="pg-runtime-b")
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda _worker: _claim_batch(barrier), range(2)))

        assert [len(batch) for batch in batches] == [20, 20]
        assert len(set().union(*map(set, batches))) == 40
    finally:
        _cleanup()


def _seed_task(task_id: str, *, action_count: int = 0, action_prefix: str = "pg-runtime") -> None:
    timestamp = _now() - timedelta(hours=2)
    with SessionLocal() as session:
        if session.get(Tenant, TENANT_ID) is None:
            session.add(Tenant(id=TENANT_ID, name="runtime-tenant"))
            session.commit()
        session.add(Task(id=task_id, tenant_id=TENANT_ID, name=task_id, type="target_admission_retry", status="running"))
        session.flush()
        session.add_all(
            Action(
                id=f"{action_prefix}-{index:03d}",
                tenant_id=TENANT_ID,
                task_id=task_id,
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                status="executing",
                scheduled_at=timestamp + timedelta(microseconds=index),
                lease_expires_at=timestamp,
            )
            for index in range(action_count)
        )
        session.commit()


def _seed_foreign_action() -> None:
    timestamp = _now() - timedelta(hours=2)
    with SessionLocal() as session:
        session.add(Tenant(id=FOREIGN_TENANT_ID, name="runtime-foreign"))
        session.commit()
        session.add(Action(
            id=FOREIGN_ACTION_ID,
            tenant_id=FOREIGN_TENANT_ID,
            task_id=TASK_ID,
            task_type="target_admission_retry",
            action_type="ensure_target_membership",
            status="executing",
            scheduled_at=timestamp,
            lease_expires_at=timestamp,
        ))
        session.commit()


def _claim_batch(barrier: Barrier) -> list[str]:
    barrier.wait()
    with SessionLocal() as session:
        claims, _stale_workers = _claim_stale_executing_action_ids(
            session,
            now=_now(),
            timeout_minutes=30,
            limit=20,
        )
        return [claim.action_id for claim in claims]


def _cleanup() -> None:
    tenant_ids = (TENANT_ID, FOREIGN_TENANT_ID)
    with SessionLocal() as session:
        session.execute(delete(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id.in_(tenant_ids)))
        session.execute(delete(Action).where(Action.tenant_id.in_(tenant_ids)))
        session.execute(delete(Task).where(Task.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
