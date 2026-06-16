from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, OperationTarget, Task

from .channel_membership import (
    ACTION_TYPE,
    LEGACY_ACTION_TYPE,
    OPEN_STATUSES,
    gate_channel_membership,
)
from .hard_hourly import enabled as hard_hourly_enabled


def recover_missing_hard_hourly_memberships(session: Session, *, limit: int) -> int:
    recovered = 0
    for task in _candidate_tasks(session, limit=limit):
        if not _should_recover_task(session, task):
            continue
        target = _target_for_task(session, task)
        if not target:
            continue
        gate = gate_channel_membership(session, task, target, require_send=True)
        recovered += int(gate.created or 0)
    return recovered


def _candidate_tasks(session: Session, *, limit: int) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(Task.type == "group_ai_chat", Task.status == "running")
            .order_by(Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
            .limit(max(1, int(limit or 1)))
        )
    )


def _should_recover_task(session: Session, task: Task) -> bool:
    return (
        hard_hourly_enabled(task)
        and _membership_need_join_count(task.stats or {}) > 0
        and _open_membership_action_count(session, task.id) == 0
    )


def _membership_need_join_count(stats: dict[str, Any]) -> int:
    try:
        return int(stats.get("membership_need_join_count") or 0)
    except (TypeError, ValueError):
        return 0


def _open_membership_action_count(session: Session, task_id: str) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task_id,
                Action.action_type.in_([ACTION_TYPE, LEGACY_ACTION_TYPE]),
                Action.status.in_(OPEN_STATUSES),
            )
        )
        or 0
    )


def _target_for_task(session: Session, task: Task) -> OperationTarget | None:
    target_id = _as_int((task.type_config or {}).get("target_operation_target_id"))
    if target_id <= 0:
        return None
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != task.tenant_id or target.target_type != "group":
        return None
    return target


def _as_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
