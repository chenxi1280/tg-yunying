"""Fresh lifecycle checks that serialize retirement with planning and call issuance."""
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task
from app.services._common import _now

from .engagement_runtime_error import RuntimeResourceBlocked


ENGAGEMENT_TYPES = frozenset({"group_ai_chat", "channel_comment", "channel_like", "channel_view"})
RETIREMENT_REASON = "task_retired"
RETIREMENT_DETAIL = "旧任务已退役，请使用对应的新任务"


class TaskGatewayFenced(ValueError):
    pass


def require_task_not_retired(session: Session, task: Task) -> None:
    if task.type not in ENGAGEMENT_TYPES:
        return
    current = session.scalar(select(Task).where(Task.id == task.id,
        Task.tenant_id == task.tenant_id).with_for_update(nowait=True)
        .execution_options(populate_existing=True))
    if current is None or current.retired_at is not None:
        raise ValueError(f"{RETIREMENT_REASON}:{RETIREMENT_DETAIL}")


def lock_task_for_planning(session: Session, task_id: str) -> Task | None:
    task = session.scalar(select(Task).where(Task.id == task_id)
        .with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if task is None or task.status != "running" or task.retired_at is not None:
        return None
    return task


def guard_attempt_call_start(session: Session, attempt: ExecutionAttempt) -> None:
    action = session.get(Action, attempt.action_id)
    if action is None:
        raise ValueError("gateway_attempt_action_missing")
    if action.task_type not in ENGAGEMENT_TYPES:
        return
    current = _lock_gateway_task(session, action)
    if _gateway_task_is_current(current, attempt):
        return
    cached_task = session.identity_map.get(session.identity_key(Task, action.task_id))
    if cached_task is not None:
        session.expire(cached_task, ["status", "retired_at", "deleted_at", "task_lifecycle_epoch",
            "replaced_by_task_id", "next_run_at"])
    _finish_uncalled(attempt, "task_lifecycle_gateway_fenced")
    raise TaskGatewayFenced("任务已停止或生命周期已变更，本次调用未发出")


def _lock_gateway_task(session, action):
    try:
        with session.begin_nested():
            return session.execute(select(Task.status, Task.retired_at, Task.deleted_at,
                Task.task_lifecycle_epoch).where(Task.id == action.task_id,
                Task.tenant_id == action.tenant_id).with_for_update(read=True, nowait=True)).one_or_none()
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) != "55P03":
            raise
        raise RuntimeResourceBlocked("task_lifecycle_admission_busy", "任务生命周期事务正在处理") from exc


def _gateway_task_is_current(current, attempt):
    return (current is not None and current.status == "running" and current.retired_at is None
        and current.deleted_at is None and current.task_lifecycle_epoch == attempt.task_lifecycle_epoch)


def _finish_uncalled(attempt: ExecutionAttempt, reason: str) -> None:
    if (attempt.gateway_call_started_at is not None or attempt.remote_message_id
            or attempt.status not in {"before_call", "before_gateway", "skipped_before_gateway", "call_not_started"}):
        raise RuntimeError("task_lifecycle_fence_attempt_already_called")
    attempt.status = "skipped_before_gateway"
    attempt.after_call_at = _now()
    attempt.failure_type = reason
    attempt.result_snapshot = {**dict(attempt.result_snapshot or {}), "remote_mutation_started": False,
        "task_lifecycle_fence": reason}
