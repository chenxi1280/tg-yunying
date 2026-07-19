from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import OperationTarget, Task
from app.services._common import _now

from .channel_membership import (
    gate_channel_membership,
)
from .hard_hourly import enabled as hard_hourly_enabled


def recover_missing_hard_hourly_memberships(session: Session, *, limit: int) -> int:
    recovered = 0
    for task in _candidate_tasks(session, limit=limit, now=_now()):
        if not _should_recover_task(session, task):
            continue
        target = _target_for_task(session, task)
        if not target:
            continue
        gate = gate_channel_membership(session, task, target, require_send=True)
        recovered += int(gate.created or 0)
    return recovered


def _candidate_tasks(session: Session, *, limit: int, now: datetime) -> list[Task]:
    return list(
        session.scalars(
            select(Task)
            .where(
                Task.type == "group_ai_chat",
                Task.status == "running",
                Task.deleted_at.is_(None),
                or_(Task.hard_hourly_next_check_at.is_(None), Task.hard_hourly_next_check_at <= now),
            )
            .order_by(Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
            .limit(max(1, int(limit or 1)))
        )
    )


def _should_recover_task(session: Session, task: Task) -> bool:
    return (
        hard_hourly_enabled(task)
        and _membership_recovery_pending_count(task.stats or {}) > 0
    )


def _membership_recovery_pending_count(stats: dict[str, Any]) -> int:
    return _stats_int(stats, "membership_need_join_count") + _stats_int(stats, "membership_failed_count")


def _stats_int(stats: dict[str, Any], key: str) -> int:
    try:
        return int(stats.get(key) or 0)
    except (TypeError, ValueError):
        return 0


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
