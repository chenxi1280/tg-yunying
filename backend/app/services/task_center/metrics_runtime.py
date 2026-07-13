from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, select

from app.models import Action, OperationIssue, RuntimeMetricSnapshot, WorkerHeartbeat
from app.services._common import _now
from app.services.runtime_summary import reconcile_stale_operation_issues
from app.services.runtime_summary_batches import (
    DEFAULT_ACCOUNT_SUMMARY_BATCH_SIZE,
    refresh_account_runtime_summary_batch,
)

from .heartbeat import record_worker_heartbeat


def drain_task_metrics(session_factory, limit: int = 100) -> int:
    now_value = _now()
    with session_factory() as session:
        record_count = _record_runtime_metrics(session, now_value, limit)
        session.commit()
    account_count = _refresh_account_summary_batch(session_factory, limit)
    return record_count + account_count


def _record_runtime_metrics(session, now_value: datetime, limit: int) -> int:
    record_worker_heartbeat(session, process_type="metrics", metadata={"limit": limit})
    metrics = _runtime_metric_values(session, now_value)
    session.add_all(_runtime_metric_rows(metrics, now_value))
    tenant_ids = session.scalars(select(OperationIssue.tenant_id).where(OperationIssue.status == "open").distinct())
    for tenant_id in tenant_ids:
        reconcile_stale_operation_issues(session, int(tenant_id))
    return len(metrics)


def _runtime_metric_values(session, now_value: datetime) -> dict[str, int]:
    statuses = dict(session.execute(select(Action.status, func.count()).select_from(Action).group_by(Action.status)).all())
    oldest_pending = session.scalar(select(func.min(Action.scheduled_at)).where(Action.status == "pending"))
    oldest_age = int((now_value - _naive_datetime(oldest_pending)).total_seconds()) if oldest_pending else 0
    minute_cutoff = now_value - timedelta(minutes=1)
    recent = dict(
        session.execute(
            select(Action.status, func.count()).select_from(Action)
            .where(Action.executed_at >= minute_cutoff).group_by(Action.status)
        ).all()
    )
    created = session.scalar(select(func.count()).select_from(Action).where(Action.created_at >= minute_cutoff)) or 0
    heartbeat_cutoff = now_value - timedelta(minutes=2)
    active = session.scalar(select(func.count(WorkerHeartbeat.worker_id)).where(WorkerHeartbeat.last_seen_at >= heartbeat_cutoff)) or 0
    stale = session.scalar(select(func.count(WorkerHeartbeat.worker_id)).where(WorkerHeartbeat.last_seen_at < heartbeat_cutoff)) or 0
    return _metric_counts(statuses, recent, created=created, oldest_age=oldest_age, active=active, stale=stale)


def _metric_counts(statuses: dict, recent: dict, **values: int) -> dict[str, int]:
    return {
        "actions.pending.count": int(statuses.get("pending") or 0),
        "actions.claiming.count": int(statuses.get("claiming") or 0),
        "actions.executing.count": int(statuses.get("executing") or 0),
        "actions.success.count": int(statuses.get("success") or 0),
        "actions.failed.count": int(statuses.get("failed") or 0),
        "actions.skipped.count": int(statuses.get("skipped") or 0),
        "actions.unknown_after_send.count": int(statuses.get("unknown_after_send") or 0),
        "actions.created.per_minute": int(values["created"] or 0),
        "actions.success.per_minute": int(recent.get("success") or 0),
        "actions.failed.per_minute": int(recent.get("failed") or 0),
        "actions.skipped.per_minute": int(recent.get("skipped") or 0),
        "actions.oldest_pending_age_seconds": max(0, values["oldest_age"]),
        "worker.active.count": int(values["active"] or 0),
        "worker.stale.count": int(values["stale"] or 0),
    }


def _runtime_metric_rows(metrics: dict[str, int], captured_at: datetime) -> list[RuntimeMetricSnapshot]:
    return [
        RuntimeMetricSnapshot(
            captured_at=captured_at,
            metric_name=name,
            dimension_type="global",
            dimension_id="all",
            metric_value=value,
            tags={"worker_role": "metrics"},
        )
        for name, value in metrics.items()
    ]


def _refresh_account_summary_batch(session_factory, limit: int) -> int:
    batch_size = min(DEFAULT_ACCOUNT_SUMMARY_BATCH_SIZE, max(1, int(limit)))
    with session_factory() as session:
        count = refresh_account_runtime_summary_batch(session, limit=batch_size)
        session.commit()
        return count


def _naive_datetime(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["drain_task_metrics"]
