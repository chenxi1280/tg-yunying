from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Action, DailyRuntimeStat, ExecutionAttempt, ReviewQueue, RuntimeCleanupAudit
from app.services._common import _now


def cleanup_runtime_details(session: Session, *, retention_days: int = 5, today: date | None = None) -> int:
    """Roll runtime detail tables forward while keeping daily totals.

    Details are retained for the most recent ``retention_days`` natural days.
    Older action and attempt rows are summarized, audited, then physically
    deleted. Long-lived business configuration is not touched here.
    """

    retention_days = max(1, int(retention_days or 5))
    today = today or _now().date()
    cutoff_date = today - timedelta(days=retention_days)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())
    rows = list(session.scalars(select(Action).where(func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at) < cutoff_dt)))
    if not rows:
        return 0
    stats = _summarize_actions(rows)
    for key, value in stats.items():
        stat_date, dimension_type, dimension_id, metric_name = key
        _upsert_stat(session, stat_date, dimension_type, dimension_id, metric_name, value)
    action_ids = [row.id for row in rows]
    status_counts = Counter(str(row.status or "unknown") for row in rows)
    attempt_count = session.scalar(select(func.count(ExecutionAttempt.id)).where(ExecutionAttempt.action_id.in_(action_ids))) or 0
    review_count = session.scalar(select(func.count(ReviewQueue.id)).where(ReviewQueue.action_id.in_(action_ids))) or 0
    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids)))
    session.execute(delete(ReviewQueue).where(ReviewQueue.action_id.in_(action_ids)))
    session.execute(delete(Action).where(Action.id.in_(action_ids)))
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=cutoff_date,
            status_counts=dict(status_counts),
            deleted_counts={"actions": len(action_ids), "execution_attempts": int(attempt_count or 0), "review_queue": int(review_count or 0)},
            summary={"retention_days": retention_days, "cutoff_date": cutoff_date.isoformat()},
            created_at=_now(),
        )
    )
    return len(action_ids) + int(attempt_count or 0) + int(review_count or 0)


def _summarize_actions(actions: list[Action]) -> dict[tuple[date, str, str, str], int]:
    stats: dict[tuple[date, str, str, str], int] = defaultdict(int)
    for action in actions:
        stat_date = _action_date(action)
        status = str(action.status or "unknown")
        _add(stats, stat_date, "global", "all", "total", 1)
        _add(stats, stat_date, "global", "all", f"status.{status}", 1)
        _add(stats, stat_date, "task", action.task_id or "", "total", 1)
        _add(stats, stat_date, "task", action.task_id or "", f"status.{status}", 1)
        if action.account_id is not None:
            _add(stats, stat_date, "account", str(action.account_id), "total", 1)
            _add(stats, stat_date, "account", str(action.account_id), f"status.{status}", 1)
        if action.task_type:
            _add(stats, stat_date, "task_type", action.task_type, "total", 1)
            _add(stats, stat_date, "task_type", action.task_type, f"status.{status}", 1)
        target_id = _target_dimension(action.payload or {})
        if target_id:
            _add(stats, stat_date, "target", target_id, "total", 1)
            _add(stats, stat_date, "target", target_id, f"status.{status}", 1)
        if status in {"unknown_after_send", "executing", "claiming", "pending", "retryable_failed"}:
            _add(stats, stat_date, "global", "all", "window_deleted_unresolved", 1)
    return dict(stats)


def _add(stats: dict[tuple[date, str, str, str], int], stat_date: date, dimension_type: str, dimension_id: str, metric_name: str, value: int) -> None:
    stats[(stat_date, dimension_type, dimension_id, metric_name)] += int(value or 0)


def _upsert_stat(session: Session, stat_date: date, dimension_type: str, dimension_id: str, metric_name: str, value: int) -> None:
    existing = session.scalar(
        select(DailyRuntimeStat).where(
            DailyRuntimeStat.stat_date == stat_date,
            DailyRuntimeStat.dimension_type == dimension_type,
            DailyRuntimeStat.dimension_id == dimension_id,
            DailyRuntimeStat.metric_name == metric_name,
        )
    )
    if existing:
        existing.metric_value = int(value or 0)
        existing.updated_at = _now()
        return
    session.add(
        DailyRuntimeStat(
            stat_date=stat_date,
            dimension_type=dimension_type,
            dimension_id=dimension_id,
            metric_name=metric_name,
            metric_value=int(value or 0),
            updated_at=_now(),
        )
    )


def _action_date(action: Action) -> date:
    value = action.executed_at or action.scheduled_at or action.created_at or _now()
    return value.date()


def _target_dimension(payload: dict[str, Any]) -> str:
    for key in ("operation_target_id", "target_operation_target_id", "group_id", "channel_target_id", "chat_id"):
        value = payload.get(key)
        if value:
            return str(value)
    return ""


__all__ = ["cleanup_runtime_details"]
