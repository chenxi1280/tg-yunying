from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Action,
    DailyRuntimeStat,
    ExecutionAttempt,
    ReviewQueue,
    RuntimeCleanupAudit,
    RuntimeMetricSnapshot,
    SearchRankDeboostClickReservation,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
)
from app.services._common import _now

RUNTIME_METRIC_CLEANUP_KIND = "runtime_metric_snapshots"


def cleanup_runtime_details(
    session: Session,
    *,
    retention_days: int = 5,
    today: date | None = None,
    batch_size: int = 100,
) -> int:
    """Summarize, audit, and delete one bounded batch of expired runtime details."""

    retention_days = max(1, int(retention_days or 5))
    batch_size = max(1, int(batch_size or 100))
    today = today or _now().date()
    cutoff_date = today - timedelta(days=retention_days)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())
    rows = _runtime_detail_batch(session, cutoff_dt, batch_size)
    if not rows:
        return 0
    stats = _summarize_actions(rows)
    for key, value in stats.items():
        _upsert_stat(session, key, value)
    action_ids = [row.id for row in rows]
    status_counts = Counter(str(row.status or "unknown") for row in rows)
    attempt_count = session.scalar(select(func.count(ExecutionAttempt.id)).where(ExecutionAttempt.action_id.in_(action_ids))) or 0
    review_count = session.scalar(select(func.count(ReviewQueue.id)).where(ReviewQueue.action_id.in_(action_ids))) or 0
    reference_counts = _remove_action_references(session, action_ids)
    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids)))
    session.execute(delete(ReviewQueue).where(ReviewQueue.action_id.in_(action_ids)))
    session.execute(delete(Action).where(Action.id.in_(action_ids)))
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=cutoff_date,
            status_counts=dict(status_counts),
            deleted_counts={
                "actions": len(action_ids),
                "execution_attempts": int(attempt_count or 0),
                "review_queue": int(review_count or 0),
                **reference_counts,
            },
            summary={
                "retention_days": retention_days,
                "cutoff_date": cutoff_date.isoformat(),
                "batch_size": batch_size,
            },
            created_at=_now(),
        )
    )
    return len(action_ids) + int(attempt_count or 0) + int(review_count or 0)


def _runtime_detail_batch(session: Session, cutoff_dt: datetime, batch_size: int) -> list[Action]:
    age = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    statement = (
        select(Action)
        .where(age < cutoff_dt)
        .order_by(Action.created_at, Action.id)
        .limit(batch_size)
        .with_for_update(of=Action, skip_locked=True)
    )
    return list(session.scalars(statement))


def _remove_action_references(session: Session, action_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    nullable_fields = (
        (TaskAccountDailyCoverage, TaskAccountDailyCoverage.reserved_action_id),
        (TaskAccountDailyCoverage, TaskAccountDailyCoverage.last_success_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.membership_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.test_message_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.delete_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.rescue_action_id),
    )
    for model, field in nullable_fields:
        result = session.execute(update(model).where(field.in_(action_ids)).values({field.key: None}))
        counts[f"cleared.{model.__tablename__}.{field.key}"] = int(result.rowcount or 0)
    result = session.execute(
        delete(SearchRankDeboostClickReservation).where(SearchRankDeboostClickReservation.action_id.in_(action_ids))
    )
    counts["search_rank_deboost_click_reservations"] = int(result.rowcount or 0)
    return counts


def cleanup_runtime_metric_snapshots(
    session: Session,
    *,
    retention_days: int = 7,
    today: date | None = None,
    batch_size: int = 10000,
) -> int:
    retention_days = max(1, int(retention_days or 7))
    batch_size = max(1, int(batch_size or 10000))
    today = today or _now().date()
    cutoff_date = today - timedelta(days=retention_days)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())
    ids = (
        select(RuntimeMetricSnapshot.id)
        .where(RuntimeMetricSnapshot.captured_at < cutoff_dt)
        .order_by(RuntimeMetricSnapshot.captured_at.asc(), RuntimeMetricSnapshot.id.asc())
        .limit(batch_size)
        .subquery()
    )
    result = session.execute(delete(RuntimeMetricSnapshot).where(RuntimeMetricSnapshot.id.in_(select(ids.c.id))))
    return int(result.rowcount or 0)


def cleanup_runtime_metric_snapshots_if_due(
    session: Session,
    *,
    retention_days: int = 3,
    batch_size: int = 20000,
    interval_seconds: int = 60,
    now_value: datetime | None = None,
) -> int:
    now_value = now_value or _now()
    latest = _latest_runtime_metric_cleanup_at(session)
    if latest is not None and _elapsed_seconds(latest, now_value) < max(1, int(interval_seconds or 60)):
        return 0
    deleted = cleanup_runtime_metric_snapshots(
        session,
        retention_days=retention_days,
        today=now_value.date(),
        batch_size=batch_size,
    )
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=now_value.date(),
            status_counts={},
            deleted_counts={RUNTIME_METRIC_CLEANUP_KIND: deleted},
            summary={
                "cleanup_kind": RUNTIME_METRIC_CLEANUP_KIND,
                "retention_days": max(1, int(retention_days or 3)),
                "batch_size": max(1, int(batch_size or 20000)),
                "interval_seconds": max(1, int(interval_seconds or 60)),
            },
            created_at=now_value,
        )
    )
    return deleted


def _latest_runtime_metric_cleanup_at(session: Session) -> datetime | None:
    return session.scalar(
        select(RuntimeCleanupAudit.created_at)
        .where(RuntimeCleanupAudit.summary["cleanup_kind"].as_string() == RUNTIME_METRIC_CLEANUP_KIND)
        .order_by(RuntimeCleanupAudit.created_at.desc())
        .limit(1)
    )


def _elapsed_seconds(start: datetime, end: datetime) -> float:
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    if end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    return (end - start).total_seconds()


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


def _upsert_stat(session: Session, key: tuple[date, str, str, str], value: int) -> None:
    stat_date, dimension_type, dimension_id, metric_name = key
    timestamp = _now()
    statement = _daily_stat_insert(session).values(
        stat_date=stat_date,
        dimension_type=dimension_type,
        dimension_id=dimension_id,
        metric_name=metric_name,
        metric_value=int(value or 0),
        updated_at=timestamp,
    )
    statement = statement.on_conflict_do_update(
        index_elements=[
            DailyRuntimeStat.stat_date,
            DailyRuntimeStat.dimension_type,
            DailyRuntimeStat.dimension_id,
            DailyRuntimeStat.metric_name,
        ],
        set_={
            "metric_value": DailyRuntimeStat.metric_value + statement.excluded.metric_value,
            "updated_at": timestamp,
        },
    )
    session.execute(statement)


def _daily_stat_insert(session: Session):
    dialect = session.get_bind().dialect.name
    if dialect == "postgresql":
        return pg_insert(DailyRuntimeStat)
    if dialect == "sqlite":
        return sqlite_insert(DailyRuntimeStat)
    raise RuntimeError(f"unsupported runtime retention dialect: {dialect}")


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
