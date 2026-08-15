from __future__ import annotations

from sqlalchemy import inspect, select
from sqlalchemy.orm import Session, object_session

from app.models import Action, Task


PROVIDER_ADMISSION_UNAVAILABLE = "provider_admission_unavailable"


def record_quality_event(
    task: Task,
    action: Action,
    key: str,
    *,
    blocker: str = "",
) -> None:
    current = _lock_current_task(task)
    stats = dict(current.stats or {})
    stats[key] = int(stats.get(key) or 0) + 1
    if blocker:
        blockers = _current_blockers(stats)
        blockers[quality_scope_key(action)] = blocker
        stats["conversation_quality_active_blockers"] = blockers
        stats["conversation_quality_active_blocker"] = _project_blocker(blockers)
    current.stats = stats


def clear_quality_blocker(task: Task, action: Action) -> None:
    current = _lock_current_task(task)
    stats = dict(current.stats or {})
    blockers = dict(stats.get("conversation_quality_active_blockers") or {})
    if not blockers:
        return
    blockers.pop(quality_scope_key(action), None)
    if blockers:
        stats["conversation_quality_active_blockers"] = blockers
        stats["conversation_quality_active_blocker"] = _project_blocker(blockers)
    else:
        stats.pop("conversation_quality_active_blockers", None)
        stats.pop("conversation_quality_active_blocker", None)
    current.stats = stats


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


def _lock_current_task(task: Task) -> Task:
    session = object_session(task)
    if session is None or not task.id:
        return task
    local = dict(task.stats or {})
    original = _original_stats(task, local)
    statement = select(Task.stats).where(Task.id == task.id)
    if session.bind is not None and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    with session.no_autoflush:
        latest = dict(session.scalar(statement) or {})
    task.stats = _merge_local_stats(latest, original, local)
    return task


def _original_stats(task: Task, local: dict) -> dict:
    history = inspect(task).attrs.stats.history
    if not history.has_changes():
        return dict(local)
    if history.deleted:
        return dict(history.deleted[0] or {})
    return {}


def _merge_local_stats(latest: dict, original: dict, local: dict) -> dict:
    merged = dict(latest)
    for key in original.keys() - local.keys():
        merged.pop(key, None)
    for key, value in local.items():
        if key not in original or original[key] != value:
            merged[key] = value
    return merged


def _current_blockers(stats: dict) -> dict[str, str]:
    blockers = dict(stats.get("conversation_quality_active_blockers") or {})
    legacy = str(stats.get("conversation_quality_active_blocker") or "")
    if not blockers and legacy:
        blockers["legacy_unscoped"] = legacy
    return blockers


def _project_blocker(blockers: dict[str, str]) -> str:
    durable = [value for value in blockers.values() if value != "context_superseded_requeue"]
    return durable[-1] if durable else list(blockers.values())[-1]


__all__ = [
    "PROVIDER_ADMISSION_UNAVAILABLE",
    "clear_quality_blocker",
    "quality_scope_key",
    "record_provider_admission_unavailable",
    "record_quality_event",
]
