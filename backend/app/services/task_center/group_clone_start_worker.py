"""Establish Clone start boundaries in listener, outside Planner's IO guard."""
from sqlalchemy import select
from app.models import Task
from app.services._common import _now
from .group_clone_source_stream import advance_group_clone_start
from .planner_wake import wake_task_planner


def advance_pending_group_clones(session_factory, *, tenant_id=None, limit=100):
    conditions = _pending_conditions(tenant_id)
    with session_factory() as session:
        task_ids = list(session.scalars(select(Task.id).where(*conditions)
            .order_by(Task.priority, Task.created_at, Task.id).limit(limit)))
    advanced = 0
    for task_id in task_ids:
        with session_factory() as session:
            task = session.scalar(select(Task).where(Task.id == task_id, *conditions)
                .with_for_update(skip_locked=True))
            if task is None:
                continue
            if advance_group_clone_start(session, task):
                task.next_run_at = _now()
                wake_task_planner(session, task, reason_code="task_activated",
                    not_before_at=task.next_run_at)
            session.commit()
            advanced += 1
    return advanced


def _pending_conditions(tenant_id):
    conditions = [Task.type == "group_clone", Task.status == "pending",
        Task.deleted_at.is_(None),
        (Task.scheduled_start.is_(None)) | (Task.scheduled_start <= _now())]
    return conditions if tenant_id is None else [*conditions, Task.tenant_id == tenant_id]
