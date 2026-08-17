from __future__ import annotations

from collections import Counter
from datetime import datetime
import hashlib

from sqlalchemy import delete, select
from sqlalchemy.orm import Session, object_session

from app.models import (
    Action,
    Task,
    TaskRuntimeActiveBlocker,
    TaskRuntimeSummary,
)
from app.services._common import _now


PROVIDER_ADMISSION_UNAVAILABLE = "provider_admission_unavailable"


def record_quality_event(
    task: Task,
    action: Action,
    key: str,
    *,
    blocker: str = "",
) -> None:
    session = _required_session(task)
    summary = _lock_runtime_summary(session, task)
    payload = dict(summary.summary or {})
    counters = dict(payload.get("quality_event_counts") or {})
    counters[key] = int(counters.get(key) or 0) + 1
    payload["quality_event_counts"] = counters
    summary.summary = payload
    if blocker:
        _upsert_active_blocker(session, task, action, blocker)
    _refresh_blocker_summary(session, task, summary)


def clear_quality_blocker(task: Task, action: Action) -> None:
    session = _required_session(task)
    scope_hash = _scope_hash(quality_scope_key(action))
    result = session.execute(delete(TaskRuntimeActiveBlocker).where(
        TaskRuntimeActiveBlocker.tenant_id == task.tenant_id,
        TaskRuntimeActiveBlocker.task_id == task.id,
        TaskRuntimeActiveBlocker.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        TaskRuntimeActiveBlocker.blocker_domain == "conversation_quality",
        TaskRuntimeActiveBlocker.scope_key_hash == scope_hash,
    ))
    if not result.rowcount:
        return
    summary = _lock_runtime_summary(session, task)
    summary.blocker_revision = int(summary.blocker_revision or 0) + 1
    _refresh_blocker_summary(session, task, summary)


def record_provider_admission_unavailable(
    session: Session,
    action: Action,
) -> None:
    task = session.get(Task, action.task_id)
    if task is None:
        raise RuntimeError("provider_admission_task_missing")
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": PROVIDER_ADMISSION_UNAVAILABLE,
        "error_message": "AI Provider 共享准入状态不可用，已停止生成调用",
        "generation_stage": "provider_admission",
        "generation_outcome": "pending",
    }
    record_quality_event(
        task,
        action,
        "provider_admission_unavailable_count",
        blocker=PROVIDER_ADMISSION_UNAVAILABLE,
    )


def quality_scope_key(action: Action) -> str:
    return str(action.content_mix_cycle_slot_id or action.id)


def _required_session(task: Task) -> Session:
    session = object_session(task)
    if session is None or not task.id:
        raise RuntimeError("quality_projection_session_required")
    return session


def _lock_runtime_summary(session: Session, task: Task) -> TaskRuntimeSummary:
    summary = session.scalar(
        select(TaskRuntimeSummary).where(
            TaskRuntimeSummary.tenant_id == task.tenant_id,
            TaskRuntimeSummary.task_id == task.id,
        ).with_for_update()
    )
    if summary is None:
        summary = TaskRuntimeSummary(
            tenant_id=task.tenant_id,
            task_id=task.id,
            task_status=task.status,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        )
        session.add(summary)
        session.flush()
    if summary.lifecycle_epoch != int(task.task_lifecycle_epoch or 1):
        summary.lifecycle_epoch = int(task.task_lifecycle_epoch or 1)
        summary.blocker_revision = 0
        summary.summary = {}
    return summary


def _upsert_active_blocker(
    session: Session,
    task: Task,
    action: Action,
    blocker: str,
) -> None:
    scope_hash = _scope_hash(quality_scope_key(action))
    row = session.scalar(select(TaskRuntimeActiveBlocker).where(
        TaskRuntimeActiveBlocker.tenant_id == task.tenant_id,
        TaskRuntimeActiveBlocker.task_id == task.id,
        TaskRuntimeActiveBlocker.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        TaskRuntimeActiveBlocker.blocker_domain == "conversation_quality",
        TaskRuntimeActiveBlocker.scope_key_hash == scope_hash,
    ).with_for_update())
    if row is None:
        row = TaskRuntimeActiveBlocker(
            tenant_id=task.tenant_id,
            task_id=task.id,
            lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
            blocker_domain="conversation_quality",
            scope_key_hash=scope_hash,
            blocker_code=blocker,
            source_type="action",
            source_id_hash=_scope_hash(action.id),
        )
        session.add(row)
    else:
        row.blocker_code = blocker
        row.source_revision = int(row.source_revision or 0) + 1
        row.updated_at = _now()
    summary = _lock_runtime_summary(session, task)
    summary.blocker_revision = int(summary.blocker_revision or 0) + 1


def _refresh_blocker_summary(
    session: Session,
    task: Task,
    summary: TaskRuntimeSummary,
) -> None:
    session.flush()
    rows = list(session.scalars(
        select(TaskRuntimeActiveBlocker).where(
            TaskRuntimeActiveBlocker.tenant_id == task.tenant_id,
            TaskRuntimeActiveBlocker.task_id == task.id,
            TaskRuntimeActiveBlocker.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        ).order_by(TaskRuntimeActiveBlocker.opened_at, TaskRuntimeActiveBlocker.id)
    ))
    payload = dict(summary.summary or {})
    payload["runtime_blocker_summary_v2"] = {
        "active_count": len(rows),
        "code_counts": dict(Counter(row.blocker_code for row in rows)),
        "oldest_at": _oldest_at(rows),
        "revision": int(summary.blocker_revision or 0),
        "samples": [row.scope_key_hash for row in rows[:10]],
    }
    summary.summary = payload


def _oldest_at(rows: list[TaskRuntimeActiveBlocker]) -> str | None:
    values = [row.opened_at for row in rows if isinstance(row.opened_at, datetime)]
    return min(values).isoformat() if values else None


def _scope_hash(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


__all__ = [
    "PROVIDER_ADMISSION_UNAVAILABLE",
    "clear_quality_blocker",
    "quality_scope_key",
    "record_provider_admission_unavailable",
    "record_quality_event",
]
