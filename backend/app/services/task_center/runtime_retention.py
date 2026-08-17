from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
import math
from uuid import uuid4
from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiCoverageVariationIntent,
    DailyRuntimeStat,
    ExecutionAttempt,
    ReviewQueue,
    RuntimeCleanupAudit,
    RuntimeMetricSnapshot,
    SearchRankDeboostClickReservation,
    TaskAccountDailyCoverage,
    TaskHardHourlyDeliveryCredit,
    TaskMembershipAdmissionItem,
    WorkerRuntimeResourceRollup,
    WorkerRuntimeResourceSample,
)
from app.services._common import _now

RUNTIME_DETAIL_CLEANUP_KIND = "runtime_details"
RUNTIME_METRIC_CLEANUP_KIND = "runtime_metric_snapshots"
TERMINAL_ACTION_STATUSES = ("success", "failed", "skipped")
RESOURCE_ROLLUP_SECONDS = 300


def cleanup_runtime_details(
    session: Session,
    *,
    retention_days: int = 5,
    today: date | None = None,
    batch_size: int = 100,
    now_value: datetime | None = None,
) -> int:
    """Summarize, audit, and delete one bounded batch of expired runtime details."""

    retention_days = max(1, int(retention_days or 5))
    batch_size = max(1, int(batch_size or 100))
    now_value = now_value or _now()
    today = today or now_value.date()
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
            created_at=now_value,
        )
    )
    return len(action_ids) + int(attempt_count or 0) + int(review_count or 0)


def cleanup_runtime_details_if_due(
    session: Session,
    *,
    retention_days: int = 5,
    batch_size: int = 100,
    interval_seconds: int = 300,
    now_value: datetime | None = None,
) -> int:
    now_value = now_value or _now()
    retention_days = max(1, int(retention_days or 5))
    batch_size = max(1, int(batch_size or 100))
    interval_seconds = max(1, int(interval_seconds or 300))
    latest = _latest_runtime_cleanup_at(session, RUNTIME_DETAIL_CLEANUP_KIND)
    if latest is not None and _elapsed_seconds(latest, now_value) < interval_seconds:
        return 0
    deleted = cleanup_runtime_details(
        session,
        retention_days=retention_days,
        today=now_value.date(),
        batch_size=batch_size,
        now_value=now_value,
    )
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=now_value.date(),
            status_counts={},
            deleted_counts={RUNTIME_DETAIL_CLEANUP_KIND: deleted},
            summary={
                "cleanup_kind": RUNTIME_DETAIL_CLEANUP_KIND,
                "retention_days": retention_days,
                "batch_size": batch_size,
                "interval_seconds": interval_seconds,
            },
            created_at=now_value,
        )
    )
    return deleted


def _runtime_detail_batch(session: Session, cutoff_dt: datetime, batch_size: int) -> list:
    age = func.coalesce(Action.executed_at, Action.scheduled_at, Action.created_at)
    target_dimension = func.coalesce(
        Action.payload["operation_target_id"].as_string(),
        Action.payload["target_operation_target_id"].as_string(),
        Action.payload["group_id"].as_string(),
        Action.payload["channel_target_id"].as_string(),
        Action.payload["chat_id"].as_string(),
        "",
    ).label("target_dimension")
    statement = (
        select(
            Action.id,
            Action.task_id,
            Action.account_id,
            Action.task_type,
            Action.status,
            Action.executed_at,
            Action.scheduled_at,
            Action.created_at,
            target_dimension,
        )
        .where(
            age < cutoff_dt,
            Action.status.in_(TERMINAL_ACTION_STATUSES),
        )
        .order_by(age.asc(), Action.created_at.asc(), Action.id.asc())
        .limit(batch_size)
        .with_for_update(of=Action, skip_locked=True)
    )
    return list(session.execute(statement))


def _remove_action_references(session: Session, action_ids: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    nullable_fields = (
        (TaskAccountDailyCoverage, TaskAccountDailyCoverage.reserved_action_id),
        (TaskAccountDailyCoverage, TaskAccountDailyCoverage.last_success_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.membership_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.test_message_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.delete_action_id),
        (TaskMembershipAdmissionItem, TaskMembershipAdmissionItem.rescue_action_id),
        (AiCoverageVariationIntent, AiCoverageVariationIntent.action_id),
    )
    for model, field in nullable_fields:
        result = session.execute(update(model).where(field.in_(action_ids)).values({field.key: None}))
        counts[f"cleared.{model.__tablename__}.{field.key}"] = int(result.rowcount or 0)
    result = session.execute(
        delete(SearchRankDeboostClickReservation).where(SearchRankDeboostClickReservation.action_id.in_(action_ids))
    )
    counts["search_rank_deboost_click_reservations"] = int(result.rowcount or 0)
    result = session.execute(
        delete(TaskHardHourlyDeliveryCredit).where(TaskHardHourlyDeliveryCredit.action_id.in_(action_ids))
    )
    counts["task_hard_hourly_delivery_credits"] = int(result.rowcount or 0)
    return counts


def cleanup_runtime_metric_snapshots(
    session: Session,
    *,
    retention_days: int = 7,
    resource_raw_hours: int = 24,
    resource_rollup_days: int = 7,
    today: date | None = None,
    batch_size: int = 10000,
    now_value: datetime | None = None,
) -> int:
    retention_days = max(1, int(retention_days or 7))
    batch_size = max(1, int(batch_size or 10000))
    now_value = now_value or _now()
    today = today or now_value.date()
    cutoff_date = today - timedelta(days=retention_days)
    cutoff_dt = datetime.combine(cutoff_date, datetime.min.time())
    deleted = _delete_expired_batch(
        session,
        model=RuntimeMetricSnapshot,
        time_field=RuntimeMetricSnapshot.captured_at,
        cutoff=cutoff_dt,
        batch_size=batch_size,
    )
    deleted += _delete_expired_batch(
        session,
        model=WorkerRuntimeResourceSample,
        time_field=WorkerRuntimeResourceSample.captured_at,
        cutoff=now_value - timedelta(hours=max(1, int(resource_raw_hours or 24))),
        batch_size=batch_size,
    )
    deleted += _delete_expired_batch(
        session,
        model=WorkerRuntimeResourceRollup,
        time_field=WorkerRuntimeResourceRollup.bucket_at,
        cutoff=now_value - timedelta(days=max(1, int(resource_rollup_days or 7))),
        batch_size=batch_size,
    )
    return deleted


def _delete_expired_batch(
    session: Session,
    *,
    model,
    time_field,
    cutoff: datetime,
    batch_size: int,
) -> int:
    ids = (
        select(model.id)
        .where(time_field < cutoff)
        .order_by(time_field.asc(), model.id.asc())
        .limit(batch_size)
        .subquery()
    )
    result = session.execute(delete(model).where(model.id.in_(select(ids.c.id))))
    return int(result.rowcount or 0)


def rollup_worker_runtime_resources(
    session: Session,
    *,
    now_value: datetime | None = None,
) -> int:
    timestamp = now_value or _now()
    bucket_end = _resource_bucket_start(timestamp)
    bucket_start = bucket_end - timedelta(seconds=RESOURCE_ROLLUP_SECONDS)
    rows = list(session.scalars(select(WorkerRuntimeResourceSample).where(
        WorkerRuntimeResourceSample.captured_at >= bucket_start,
        WorkerRuntimeResourceSample.captured_at < bucket_end,
    )))
    groups = defaultdict(list)
    for row in rows:
        key = (row.worker_id_hash, row.process_type, row.release_sha or "")
        groups[key].append(row)
    for key, samples in groups.items():
        _upsert_resource_rollup(
            session,
            key=key,
            samples=samples,
            bucket_at=bucket_start,
            timestamp=timestamp,
        )
    return len(groups)


def _upsert_resource_rollup(
    session: Session,
    *,
    key: tuple[str, str, str],
    samples: list[WorkerRuntimeResourceSample],
    bucket_at: datetime,
    timestamp: datetime,
) -> None:
    worker_id_hash, process_type, release_sha = key
    values = {
        "id": str(uuid4()),
        "worker_id_hash": worker_id_hash,
        "process_type": process_type,
        "release_sha": release_sha,
        "bucket_at": bucket_at,
        "sample_count": len(samples),
        **_resource_rollup_values(samples),
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    insert = _resource_rollup_insert(session).values(**values)
    update_values = {
        key: value
        for key, value in values.items()
        if key not in {"id", "worker_id_hash", "process_type", "release_sha", "bucket_at", "created_at"}
    }
    session.execute(insert.on_conflict_do_update(
        index_elements=["worker_id_hash", "process_type", "release_sha", "bucket_at"],
        set_=update_values,
    ))


def _resource_rollup_values(samples: list[WorkerRuntimeResourceSample]) -> dict:
    return {
        "pss_kib_p95": _p95(samples, "pss_kib"),
        "pss_kib_max": _maximum(samples, "pss_kib"),
        "private_dirty_kib_p95": _p95(samples, "private_dirty_kib"),
        "anonymous_kib_p95": _p95(samples, "anonymous_kib"),
        "cgroup_current_bytes_p95": _p95(samples, "cgroup_current_bytes"),
        "cgroup_current_bytes_max": _maximum(samples, "cgroup_current_bytes"),
        "cgroup_event_count_max": _maximum(samples, "cgroup_event_count"),
        "cpu_percent_p95": _p95(samples, "cpu_percent"),
        "thread_count_max": _maximum(samples, "thread_count"),
        "telethon_client_count_max": _maximum(samples, "telethon_client_count"),
    }


def _p95(samples: list[WorkerRuntimeResourceSample], field: str):
    values = sorted(getattr(sample, field) or 0 for sample in samples)
    index = max(0, math.ceil(len(values) * 0.95) - 1)
    return values[index] if values else 0


def _maximum(samples: list[WorkerRuntimeResourceSample], field: str):
    return max((getattr(sample, field) or 0 for sample in samples), default=0)


def _resource_bucket_start(value: datetime) -> datetime:
    minute = value.minute - value.minute % (RESOURCE_ROLLUP_SECONDS // 60)
    return value.replace(minute=minute, second=0, microsecond=0)


def _resource_rollup_insert(session: Session):
    if session.get_bind().dialect.name == "postgresql":
        return pg_insert(WorkerRuntimeResourceRollup)
    if session.get_bind().dialect.name == "sqlite":
        return sqlite_insert(WorkerRuntimeResourceRollup)
    raise RuntimeError("unsupported resource rollup dialect")


def cleanup_runtime_metric_snapshots_if_due(
    session: Session,
    *,
    retention_days: int = 3,
    resource_raw_hours: int = 24,
    resource_rollup_days: int = 7,
    batch_size: int = 20000,
    interval_seconds: int = 300,
    now_value: datetime | None = None,
) -> int:
    now_value = now_value or _now()
    latest = _latest_runtime_cleanup_at(session, RUNTIME_METRIC_CLEANUP_KIND)
    if latest is not None and _elapsed_seconds(latest, now_value) < max(1, int(interval_seconds or 300)):
        return 0
    rollup_count = rollup_worker_runtime_resources(session, now_value=now_value)
    deleted = cleanup_runtime_metric_snapshots(
        session,
        retention_days=retention_days,
        resource_raw_hours=resource_raw_hours,
        resource_rollup_days=resource_rollup_days,
        today=now_value.date(),
        batch_size=batch_size,
        now_value=now_value,
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
                "interval_seconds": max(1, int(interval_seconds or 300)),
                "resource_rollup_count": rollup_count,
            },
            created_at=now_value,
        )
    )
    return deleted


def _latest_runtime_cleanup_at(session: Session, cleanup_kind: str) -> datetime | None:
    return session.scalar(
        select(RuntimeCleanupAudit.created_at)
        .where(RuntimeCleanupAudit.summary["cleanup_kind"].as_string() == cleanup_kind)
        .order_by(RuntimeCleanupAudit.created_at.desc())
        .limit(1)
    )


def _elapsed_seconds(start: datetime, end: datetime) -> float:
    if start.tzinfo is None and end.tzinfo is not None:
        start = start.replace(tzinfo=end.tzinfo)
    if end.tzinfo is None and start.tzinfo is not None:
        end = end.replace(tzinfo=start.tzinfo)
    return (end - start).total_seconds()


def _summarize_actions(actions: list) -> dict[tuple[date, str, str, str], int]:
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
        target_id = str(action.target_dimension or "")
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


def _action_date(action) -> date:
    value = action.executed_at or action.scheduled_at or action.created_at or _now()
    return value.date()


__all__ = ["cleanup_runtime_details", "cleanup_runtime_details_if_due"]
