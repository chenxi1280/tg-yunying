from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
import math
from uuid import uuid4
from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiCoverageVariationIntent,
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
from app.timezone import as_beijing
from app.services.task_center.runtime_retention_policy import (
    DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    PROTECTED_ATTEMPT_STATUSES,
    RetentionCandidate,
    RuntimeActionRetentionPolicy,
    candidate_fingerprint,
    terminal_reason_code,
)
from app.services.task_center.runtime_retention_summary import (
    summarize_actions,
    summarize_terminal_actions,
    upsert_daily_stats,
    upsert_terminal_stats,
)

RUNTIME_DETAIL_CLEANUP_KIND = "runtime_details"
RUNTIME_DETAIL_BATCH_KIND = "runtime_detail_batch"
RUNTIME_METRIC_CLEANUP_KIND = "runtime_metric_snapshots"
RESOURCE_ROLLUP_SECONDS = 300


@dataclass(frozen=True)
class _RetentionBatch:
    rows: list
    reasons: dict[str, str]
    fingerprint: str
    cutoffs: dict[str, datetime]
    as_of: datetime


def cleanup_runtime_details(
    session: Session,
    *,
    policy: RuntimeActionRetentionPolicy = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    today: date | None = None,
    batch_size: int = 100,
    now_value: datetime | None = None,
    as_of: datetime | None = None,
    expected_fingerprint: str = "",
    audit_context: dict | None = None,
) -> int:
    """Summarize, audit, and delete one bounded batch of expired runtime details."""

    batch_size = max(1, int(batch_size or 100))
    now_value = now_value or _now()
    frozen_as_of = as_of or (datetime.combine(today, datetime.min.time()) if today else now_value)
    batch = _prepare_retention_batch(
        session,
        policy=policy,
        as_of=frozen_as_of,
        batch_size=batch_size,
        expected_fingerprint=expected_fingerprint,
    )
    if batch is None:
        return 0
    return _apply_retention_batch(
        session,
        batch=batch,
        policy=policy,
        batch_size=batch_size,
        now_value=now_value,
        audit_context=audit_context,
    )


def _prepare_retention_batch(
    session: Session,
    *,
    policy: RuntimeActionRetentionPolicy,
    as_of: datetime,
    batch_size: int,
    expected_fingerprint: str,
) -> _RetentionBatch | None:
    cutoffs = policy.cutoffs(as_of)
    rows = _runtime_detail_batch(session, cutoffs, batch_size)
    if not rows:
        return None
    reasons, protected_attempts = _attempt_analysis(session, rows)
    if protected_attempts:
        raise RuntimeError(f"runtime_retention_protected_attempt:{protected_attempts[0]}")
    fingerprint = _candidate_fingerprint(rows, reasons)
    if expected_fingerprint and fingerprint != expected_fingerprint:
        raise RuntimeError("runtime_retention_candidate_fingerprint_drift")
    return _RetentionBatch(rows, reasons, fingerprint, cutoffs, as_of)


def _apply_retention_batch(
    session: Session,
    *,
    batch: _RetentionBatch,
    policy: RuntimeActionRetentionPolicy,
    batch_size: int,
    now_value: datetime,
    audit_context: dict | None,
) -> int:
    action_ids = [row.id for row in batch.rows]
    terminal_stats = summarize_terminal_actions(batch.rows, batch.reasons)
    upsert_daily_stats(session, summarize_actions(batch.rows))
    upsert_terminal_stats(session, terminal_stats)
    status_counts = Counter(str(row.status or "unknown") for row in batch.rows)
    attempt_count = session.scalar(select(func.count(ExecutionAttempt.id)).where(ExecutionAttempt.action_id.in_(action_ids))) or 0
    review_count = session.scalar(select(func.count(ReviewQueue.id)).where(ReviewQueue.action_id.in_(action_ids))) or 0
    reference_counts = _remove_action_references(session, action_ids)
    session.execute(delete(ExecutionAttempt).where(ExecutionAttempt.action_id.in_(action_ids)))
    session.execute(delete(ReviewQueue).where(ReviewQueue.action_id.in_(action_ids)))
    session.execute(delete(Action).where(Action.id.in_(action_ids)))
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=as_beijing(batch.as_of).date(),
            status_counts=dict(status_counts),
            deleted_counts={
                "actions": len(action_ids),
                "execution_attempts": int(attempt_count or 0),
                "review_queue": int(review_count or 0),
                **reference_counts,
            },
            summary={
                "cleanup_kind": RUNTIME_DETAIL_BATCH_KIND,
                "policy_version": policy.version,
                "retention_days": policy.retention_days(),
                "cutoffs": {status: cutoff.isoformat() for status, cutoff in batch.cutoffs.items()},
                "as_of": batch.as_of.isoformat(),
                "candidate_fingerprint": batch.fingerprint,
                "candidate_count": len(action_ids),
                "candidate_ids": action_ids if audit_context else [],
                "batch_size": batch_size,
                "typed_summary_count": sum(terminal_stats.values()),
                **(audit_context or {}),
            },
            created_at=now_value,
        )
    )
    return len(action_ids) + int(attempt_count or 0) + int(review_count or 0)


def cleanup_runtime_details_if_due(
    session: Session,
    *,
    policy: RuntimeActionRetentionPolicy = DEFAULT_RUNTIME_ACTION_RETENTION_POLICY,
    batch_size: int = 100,
    interval_seconds: int = 300,
    now_value: datetime | None = None,
) -> int:
    now_value = now_value or _now()
    batch_size = max(1, int(batch_size or 100))
    interval_seconds = max(1, int(interval_seconds or 300))
    latest = _latest_runtime_cleanup_at(session, RUNTIME_DETAIL_CLEANUP_KIND)
    if latest is not None and _elapsed_seconds(latest, now_value) < interval_seconds:
        return 0
    deleted = cleanup_runtime_details(
        session,
        policy=policy,
        batch_size=batch_size,
        now_value=now_value,
        as_of=now_value,
    )
    session.add(
        RuntimeCleanupAudit(
            cleanup_date=now_value.date(),
            status_counts={},
            deleted_counts={RUNTIME_DETAIL_CLEANUP_KIND: deleted},
            summary={
                "cleanup_kind": RUNTIME_DETAIL_CLEANUP_KIND,
                "policy_version": policy.version,
                "retention_days": policy.retention_days(),
                "batch_size": batch_size,
                "interval_seconds": interval_seconds,
            },
            created_at=now_value,
        )
    )
    return deleted


def _runtime_detail_batch(
    session: Session,
    cutoffs: dict[str, datetime],
    batch_size: int,
    *,
    lock: bool = True,
) -> list:
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
            Action.action_type,
            Action.status,
            Action.result,
            Action.executed_at,
            Action.scheduled_at,
            Action.created_at,
            age.label("age_at"),
            target_dimension,
        )
        .where(
            or_(*(
                and_(Action.status == status, age < cutoff)
                for status, cutoff in cutoffs.items()
            )),
            ~select(ExecutionAttempt.id).where(
                ExecutionAttempt.action_id == Action.id,
                ExecutionAttempt.status.in_(PROTECTED_ATTEMPT_STATUSES),
            ).exists(),
        )
        .order_by(age.asc(), Action.created_at.asc(), Action.id.asc())
        .limit(batch_size)
    )
    if lock:
        statement = statement.with_for_update(of=Action, skip_locked=True)
    return list(session.execute(statement))


def _attempt_analysis(session: Session, rows: list) -> tuple[dict[str, str], list[str]]:
    action_ids = [row.id for row in rows]
    if not action_ids:
        return {}, []
    attempts = session.execute(
        select(
            ExecutionAttempt.action_id,
            ExecutionAttempt.status,
            ExecutionAttempt.failure_type,
        )
        .where(ExecutionAttempt.action_id.in_(action_ids))
        .order_by(ExecutionAttempt.action_id, ExecutionAttempt.attempt_no.desc())
    )
    failure_by_action_id: dict[str, str] = {}
    protected_attempts: list[str] = []
    for attempt in attempts:
        if attempt.status in PROTECTED_ATTEMPT_STATUSES:
            protected_attempts.append(f"{attempt.action_id}:{attempt.status}")
        if attempt.failure_type and attempt.action_id not in failure_by_action_id:
            failure_by_action_id[attempt.action_id] = attempt.failure_type
    reasons = {
        row.id: terminal_reason_code(row.result, failure_by_action_id.get(row.id, ""))
        for row in rows
    }
    return reasons, protected_attempts


def _candidate_fingerprint(rows: list, reasons: dict[str, str]) -> str:
    candidates = [
        RetentionCandidate(
            id=row.id,
            status=row.status,
            age_at=row.age_at,
            action_type=row.action_type,
            reason_code=reasons[row.id],
        )
        for row in rows
    ]
    return candidate_fingerprint(candidates)


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


__all__ = [
    "cleanup_runtime_details",
    "cleanup_runtime_details_if_due",
]
