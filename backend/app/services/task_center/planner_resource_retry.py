"""Persist typed transient planner contention without changing task lifecycle."""
from datetime import timedelta

from app.services._common import _now

from .engagement_runtime_error import RuntimeResourceBlocked
from .planner_wake import complete_task_planner_wake
from .task_retirement import lock_task_with_planner_wake


def record_planner_resource_retry(
    session_factory,
    task_id: str,
    exc: RuntimeResourceBlocked,
) -> None:
    with session_factory() as session:
        task = lock_task_with_planner_wake(session, task_id)
        if task is None or task.status != "running":
            return
        stats = dict(task.stats or {})
        stats["planner_resource_busy"] = {
            "code": exc.code,
            "detail": exc.detail,
            "recorded_at": _now().isoformat(),
        }
        task.stats = stats
        task.next_run_at = _now() + timedelta(
            seconds=exc.retry_after_seconds,
        )
        complete_task_planner_wake(session, task, next_run_at=task.next_run_at)
        session.commit()
