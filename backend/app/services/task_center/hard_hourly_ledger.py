"""Persistent hard-hourly plan buckets and once-only delivery credits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task, TaskHardHourlyBucket, TaskHardHourlyDeliveryCredit
from app.services._common import _now
from app.services.task_center import hard_hourly
from app.services.task_center.datetime_compat import ensure_aware, parse_zone, to_zone


OPEN_STATUSES = frozenset({"pending", "claiming", "executing"})


@dataclass(frozen=True)
class CreditOutcome:
    """Result of attempting to write a hard-hourly delivery credit."""

    credited: bool
    reason: str  # credited | already_credited | not_hard_hourly | missing_attempt | missing_epoch | empty_remote_id

    def __bool__(self) -> bool:
        return self.credited


def get_bucket(
    session: Session,
    *,
    task: Task,
    operation_target_id: int,
    target_reference_revision: int,
    bucket_start: datetime,
    for_update: bool = False,
) -> TaskHardHourlyBucket | None:
    """Read-only bucket lookup. Never creates rows."""
    start = _bucket_start(task, bucket_start)
    return _bucket_for_key(
        session,
        task,
        operation_target_id,
        target_reference_revision,
        start.isoformat(),
        for_update=for_update,
    )


def ensure_bucket(
    session: Session,
    *,
    task: Task,
    operation_target_id: int,
    target_reference_revision: int,
    bucket_start: datetime,
    goal: int,
    task_config_revision: int | None = None,
) -> TaskHardHourlyBucket:
    """Create-or-lock a plan bucket. Only call from write paths (plan/credit)."""
    start = _bucket_start(task, bucket_start)
    existing = _bucket_for_key(
        session,
        task,
        operation_target_id,
        target_reference_revision,
        start.isoformat(),
        for_update=True,
    )
    if existing is not None:
        return existing
    return _create_bucket(
        session,
        task,
        operation_target_id,
        target_reference_revision,
        start,
        goal,
        task_config_revision,
    )


def credit_success_once(
    session: Session,
    *,
    action: Action,
    execution_attempt_id: str | None,
    remote_message_id: str,
    executed_at: datetime | None = None,
) -> CreditOutcome:
    """Credit one confirmed Telegram success to its immutable plan bucket."""
    remote_id = str(remote_message_id or "").strip()
    payload = action.payload if isinstance(action.payload, dict) else {}
    if not bool(payload.get("hard_hourly_target")):
        return CreditOutcome(False, "not_hard_hourly")
    if action.status != "success":
        return CreditOutcome(False, "action_not_success")
    if not remote_id:
        return CreditOutcome(False, "empty_remote_id")
    if _credit_exists(session, action.id):
        return CreditOutcome(False, "already_credited")
    attempt = _confirmed_success_attempt(session, action, execution_attempt_id, remote_id)
    if attempt is None:
        return CreditOutcome(False, "missing_attempt")
    bucket = _bucket_for_credit(session, action)
    if bucket is None:
        return CreditOutcome(False, "missing_epoch")
    credit = TaskHardHourlyDeliveryCredit(
        bucket_id=bucket.id,
        action_id=action.id,
        execution_attempt_id=attempt.id,
        executed_at=ensure_aware(executed_at or _now()),
        remote_message_id=remote_id,
    )
    try:
        with session.begin_nested():
            session.add(credit)
            session.flush()
    except IntegrityError:
        if _credit_exists(session, action.id):
            return CreditOutcome(False, "already_credited")
        raise
    _increment_bucket_success(session, bucket)
    session.flush()
    return CreditOutcome(True, "credited")


def _increment_bucket_success(session: Session, bucket: TaskHardHourlyBucket) -> None:
    session.execute(
        update(TaskHardHourlyBucket)
        .where(TaskHardHourlyBucket.id == bucket.id)
        .values(
            success_count=TaskHardHourlyBucket.success_count + 1,
            updated_at=_now(),
        )
    )
    session.expire(bucket, ["success_count", "updated_at"])


def durable_debt(
    session: Session,
    *,
    task: Task,
    operation_target_id: int,
    target_reference_revision: int,
    current_bucket_key: str,
    release_anchor: datetime | None = None,
) -> int:
    current_start = _bucket_start_from_key(task, current_bucket_key)
    anchor = _bucket_start(task, release_anchor) if release_anchor is not None else None
    rows = session.scalars(
        select(TaskHardHourlyBucket).where(
            TaskHardHourlyBucket.tenant_id == task.tenant_id,
            TaskHardHourlyBucket.task_id == task.id,
            TaskHardHourlyBucket.operation_target_id == operation_target_id,
            TaskHardHourlyBucket.target_reference_revision == target_reference_revision,
        )
    )
    return sum(
        max(0, int(row.goal or 0) - int(row.success_count or 0))
        for row in rows
        if (
            not str(row.terminal_blocker_code or "")
            and _bucket_start(task, row.bucket_start) < current_start
            and (anchor is None or _bucket_start(task, row.bucket_start) >= anchor)
        )
    )


def terminalize_task_buckets(session: Session, *, task: Task, blocker_code: str) -> int:
    """Close every still-open hard-hour obligation when the task is explicitly ended."""
    rows = session.scalars(
        select(TaskHardHourlyBucket).where(
            TaskHardHourlyBucket.tenant_id == task.tenant_id,
            TaskHardHourlyBucket.task_id == task.id,
            TaskHardHourlyBucket.terminal_blocker_code == "",
        )
    )
    count = 0
    for bucket in rows:
        bucket.terminal_blocker_code = blocker_code
        count += 1
    return count


def progress_overlay_from_ledger(
    session: Session,
    task: Task,
    base_progress: dict[str, Any],
    *,
    operation_target_id: int = 0,
    target_reference_revision: int = 0,
    create_bucket: bool = False,
) -> dict[str, Any]:
    """Replace legacy Action-history planning values when an epoch is bound."""
    progress = ledger_progress(
        session,
        task,
        base_progress.get("now") or _now(),
        operation_target_id=operation_target_id,
        target_reference_revision=target_reference_revision,
        create_bucket=create_bucket,
    )
    return {**base_progress, **progress} if progress is not None else dict(base_progress)


def ledger_progress(
    session: Session,
    task: Task,
    now: datetime,
    *,
    operation_target_id: int = 0,
    target_reference_revision: int = 0,
    create_bucket: bool = False,
) -> dict[str, Any] | None:
    """Return the planner/stats view for one immutable target epoch.

    Stats/read paths must pass create_bucket=False (default) so mere observation
    never inserts empty hour buckets. Planner write paths may pass create_bucket=True.
    """
    target_id, revision = _epoch_from_task(task, operation_target_id, target_reference_revision)
    if not target_id or not revision:
        return None
    goal = hard_hourly.goal(task.type_config or {})
    start = _bucket_start(task, now)
    if create_bucket:
        bucket = ensure_bucket(
            session,
            task=task,
            operation_target_id=target_id,
            target_reference_revision=revision,
            bucket_start=start,
            goal=goal,
        )
        bucket_key = bucket.bucket_key
        success_count = int(bucket.success_count or 0)
        goal_value = int(bucket.goal or 0)
    else:
        bucket = get_bucket(
            session,
            task=task,
            operation_target_id=target_id,
            target_reference_revision=revision,
            bucket_start=start,
            for_update=False,
        )
        bucket_key = bucket.bucket_key if bucket is not None else start.isoformat()
        success_count = int(bucket.success_count or 0) if bucket is not None else 0
        goal_value = int(bucket.goal or 0) if bucket is not None else int(goal or 0)
    current_open, current_overdue, eligible_open, unknown_hold = _epoch_reservations(
        session,
        task,
        operation_target_id=target_id,
        target_reference_revision=revision,
        current_bucket_key=bucket_key,
        now=now,
    )
    debt = durable_debt(
        session,
        task=task,
        operation_target_id=target_id,
        target_reference_revision=revision,
        current_bucket_key=bucket_key,
        release_anchor=_release_anchor(session, task),
    )
    return _planner_values_from_counts(
        bucket_key=bucket_key,
        goal=goal_value,
        success_count=success_count,
        debt=debt,
        current_open=current_open,
        current_overdue=current_overdue,
        eligible_open=eligible_open,
        unknown_hold=unknown_hold,
    )


def recent_bucket_summaries(
    session: Session,
    *,
    task: Task,
    now: datetime,
    operation_target_id: int,
    target_reference_revision: int,
) -> list[dict[str, Any]]:
    """Render plan-bucket facts without reassigning credit to executed_at's hour."""
    current_start = _bucket_start(task, now)
    starts = [current_start - timedelta(hours=offset) for offset in reversed(range(24))]
    rows = session.scalars(
        select(TaskHardHourlyBucket).where(
            TaskHardHourlyBucket.tenant_id == task.tenant_id,
            TaskHardHourlyBucket.task_id == task.id,
            TaskHardHourlyBucket.operation_target_id == operation_target_id,
            TaskHardHourlyBucket.target_reference_revision == target_reference_revision,
            TaskHardHourlyBucket.bucket_start >= starts[0],
            TaskHardHourlyBucket.bucket_start <= current_start,
        )
    )
    buckets = {row.bucket_key: row for row in rows}
    open_counts = _recent_open_counts(session, task, operation_target_id, target_reference_revision, starts, now)
    return [
        _bucket_summary(task, start, now, buckets.get(start.isoformat()), open_counts.get(start.isoformat(), (0, 0, 0)))
        for start in starts
    ]


def _bucket_for_key(
    session: Session,
    task: Task,
    target_id: int,
    revision: int,
    key: str,
    *,
    for_update: bool = False,
) -> TaskHardHourlyBucket | None:
    statement = select(TaskHardHourlyBucket).where(
        TaskHardHourlyBucket.tenant_id == task.tenant_id,
        TaskHardHourlyBucket.task_id == task.id,
        TaskHardHourlyBucket.operation_target_id == target_id,
        TaskHardHourlyBucket.target_reference_revision == revision,
        TaskHardHourlyBucket.bucket_key == key,
    )
    if for_update and session.bind and session.bind.dialect.name != "sqlite":
        statement = statement.with_for_update()
    return session.scalar(statement)


def _recent_open_counts(
    session: Session,
    task: Task,
    target_id: int,
    revision: int,
    starts: list[datetime],
    now: datetime,
) -> dict[str, tuple[int, int, int]]:
    counts = {start.isoformat(): [0, 0, 0] for start in starts}
    for action in _hard_hourly_epoch_actions(
        session,
        task,
        operation_target_id=target_id,
        target_reference_revision=revision,
    ):
        payload = action.payload if isinstance(action.payload, dict) else {}
        key = _bucket_start_from_key(task, str(payload.get("hard_hourly_bucket") or "")).isoformat()
        values = counts.get(key)
        if values is None:
            continue
        if action.status == "unknown_after_send":
            values[2] += 1
        elif to_zone(action.scheduled_at, parse_zone(task.timezone)) < to_zone(now, parse_zone(task.timezone)):
            values[1] += 1
        else:
            values[0] += 1
    return {key: tuple(values) for key, values in counts.items()}


def _bucket_summary(
    task: Task,
    start: datetime,
    now: datetime,
    row: TaskHardHourlyBucket | None,
    opens: tuple[int, int, int],
) -> dict[str, Any]:
    future_open, overdue_open, unknown_hold = opens
    if row is None:
        return _untracked_bucket(task, start)
    goal = int(row.goal or 0)
    success = int(row.success_count or 0)
    deficit = max(0, goal - success)
    end = start + timedelta(hours=1)
    status = _bucket_status(row, deficit, overdue_open, unknown_hold, end, now)
    return {
        "bucket": row.bucket_key,
        "goal": goal,
        "success_count": success,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "unknown_after_send_hold_count": unknown_hold,
        "deficit": deficit,
        "planning_deficit": max(0, deficit - future_open - overdue_open - unknown_hold),
        "status": status,
        "terminal_blocker_code": str(row.terminal_blocker_code or ""),
        "blockers": _bucket_blockers(row, overdue_open, unknown_hold),
    }


def _untracked_bucket(task: Task, start: datetime) -> dict[str, Any]:
    return {
        "bucket": start.isoformat(),
        "goal": 0,
        "success_count": 0,
        "future_open_count": 0,
        "overdue_open_count": 0,
        "unknown_after_send_hold_count": 0,
        "deficit": 0,
        "planning_deficit": 0,
        "status": "untracked",
        "terminal_blocker_code": "",
        "blockers": {},
    }


def _bucket_status(
    row: TaskHardHourlyBucket,
    deficit: int,
    overdue_open: int,
    unknown_hold: int,
    end: datetime,
    now: datetime,
) -> str:
    if str(row.terminal_blocker_code or ""):
        return "blocked"
    if deficit <= 0:
        return "met"
    if end <= to_zone(now, parse_zone(row.timezone)):
        return "missed"
    if unknown_hold:
        return "awaiting_confirmation"
    return "blocked" if overdue_open else "catching_up"


def _bucket_blockers(row: TaskHardHourlyBucket, overdue_open: int, unknown_hold: int) -> dict[str, int]:
    if str(row.terminal_blocker_code or ""):
        return {str(row.terminal_blocker_code): 1}
    if unknown_hold:
        return {"unknown_after_send": unknown_hold}
    return {"dispatcher_lag": overdue_open} if overdue_open else {}


def _create_bucket(
    session: Session,
    task: Task,
    target_id: int,
    revision: int,
    start: datetime,
    goal: int,
    task_config_revision: int | None,
) -> TaskHardHourlyBucket:
    try:
        with session.begin_nested():
            bucket = TaskHardHourlyBucket(
                tenant_id=task.tenant_id,
                task_id=task.id,
                operation_target_id=target_id,
                target_reference_revision=revision,
                bucket_key=start.isoformat(),
                bucket_start=start,
                bucket_end=start + timedelta(hours=1),
                timezone=str(task.timezone or "Asia/Shanghai"),
                goal=int(goal or 0),
                task_config_revision=int(task_config_revision or task.config_revision or 1),
                success_count=0,
            )
            session.add(bucket)
            session.flush()
    except IntegrityError:
        existing = _bucket_for_key(
            session, task, target_id, revision, start.isoformat(), for_update=True
        )
        if existing is not None:
            return existing
        raise
    return bucket


def _creditable_action(action: Action, remote_id: str) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return bool(payload.get("hard_hourly_target")) and action.status == "success" and bool(remote_id)


def _confirmed_success_attempt(
    session: Session,
    action: Action,
    attempt_id: str | None,
    remote_id: str,
) -> ExecutionAttempt | None:
    if not attempt_id:
        return None
    attempt = session.get(ExecutionAttempt, str(attempt_id))
    if attempt is None or attempt.tenant_id != action.tenant_id or attempt.action_id != action.id:
        return None
    if attempt.status != "success" or not str(attempt.remote_message_id or "").strip():
        return None
    return attempt if str(attempt.remote_message_id) == remote_id else None


def _bucket_for_credit(session: Session, action: Action) -> TaskHardHourlyBucket | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    task = session.get(Task, action.task_id)
    if task is None:
        return None
    start = _bucket_start_from_key(task, str(payload.get("hard_hourly_bucket") or ""))
    target_id = int(payload.get("target_operation_target_id") or 0)
    revision = int(payload.get("target_reference_revision") or 0)
    if not target_id or not revision:
        return None
    return ensure_bucket(
        session,
        task=task,
        operation_target_id=target_id,
        target_reference_revision=revision,
        bucket_start=start,
        goal=int(payload.get("hard_hourly_goal_at_plan") or hard_hourly.goal(task.type_config or {})),
        task_config_revision=int(payload.get("task_config_revision") or task.config_revision or 1),
    )


def _credit_exists(session: Session, action_id: str) -> bool:
    return session.scalar(
        select(TaskHardHourlyDeliveryCredit.id)
        .where(TaskHardHourlyDeliveryCredit.action_id == action_id)
        .limit(1)
    ) is not None


def _epoch_from_task(task: Task, target_id: int, revision: int) -> tuple[int, int]:
    config = task.type_config or {}
    return (
        int(target_id or config.get("target_operation_target_id") or 0),
        int(revision or config.get("target_reference_revision") or 0),
    )


def _epoch_reservations(
    session: Session,
    task: Task,
    *,
    operation_target_id: int,
    target_reference_revision: int,
    current_bucket_key: str,
    now: datetime,
) -> tuple[int, int, int, int]:
    current_open = current_overdue = eligible_open = unknown_hold = 0
    now_value = to_zone(now, parse_zone(task.timezone))
    for action in _hard_hourly_epoch_actions(
        session,
        task,
        operation_target_id=operation_target_id,
        target_reference_revision=target_reference_revision,
    ):
        payload = action.payload if isinstance(action.payload, dict) else {}
        try:
            action_key = _bucket_start_from_key(task, str(payload.get("hard_hourly_bucket") or "")).isoformat()
        except ValueError:
            continue
        if action.status == "unknown_after_send":
            unknown_hold += 1
            continue
        if action.status not in OPEN_STATUSES:
            continue
        eligible_open += 1
        if action_key != current_bucket_key:
            continue
        scheduled_at = to_zone(action.scheduled_at, parse_zone(task.timezone))
        if scheduled_at < now_value:
            current_overdue += 1
        else:
            current_open += 1
    return current_open, current_overdue, eligible_open, unknown_hold


def _hard_hourly_epoch_actions(
    session: Session,
    task: Task,
    *,
    operation_target_id: int,
    target_reference_revision: int,
) -> list[Action]:
    rows = session.scalars(
        select(Action).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_([*OPEN_STATUSES, "unknown_after_send"]),
        )
    )
    return [
        action
        for action in rows
        if _action_matches_epoch(action, operation_target_id, target_reference_revision)
    ]


def _action_matches_epoch(action: Action, target_id: int, revision: int) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return (
        bool(payload.get("hard_hourly_target"))
        and int(payload.get("target_operation_target_id") or 0) == target_id
        and int(payload.get("target_reference_revision") or 0) == revision
    )


def _planner_values_from_counts(
    *,
    bucket_key: str,
    goal: int,
    success_count: int,
    debt: int,
    current_open: int,
    current_overdue: int,
    eligible_open: int,
    unknown_hold: int,
) -> dict[str, Any]:
    current_deficit = max(0, int(goal or 0) - int(success_count or 0))
    rate = hard_hourly.planning_rate({"goal": goal, "backfill_planning_deficit": debt})
    required_new = min(rate, max(0, current_deficit + debt - eligible_open - unknown_hold))
    return {
        "bucket": bucket_key,
        "goal": int(goal or 0),
        "success_count": int(success_count or 0),
        "future_open_count": current_open,
        "overdue_open_count": current_overdue,
        "eligible_open_count": eligible_open,
        "unknown_after_send_hold_count": unknown_hold,
        "backfill_debt": debt,
        "durable_debt": debt,
        "backfill_planning_deficit": debt,
        "backfill_delivery_deficit": debt,
        "delivery_deficit": current_deficit + debt,
        "current_delivery_deficit": current_deficit,
        "planning_rate": rate,
        "required_new": required_new,
        "deficit": required_new,
        "planning_blocked": False,
        "continuity_ledger": True,
    }


def _bucket_start(task: Task, value: datetime) -> datetime:
    return to_zone(value, parse_zone(task.timezone)).replace(minute=0, second=0, microsecond=0)


def _release_anchor(session: Session, task: Task) -> datetime | None:
    from .continuity_rollout import continuity_release_anchor

    return continuity_release_anchor(session, task.tenant_id)


def _bucket_start_from_key(task: Task, key: str) -> datetime:
    try:
        value = datetime.fromisoformat(key)
    except ValueError as exc:
        raise ValueError("hard_hourly_bucket 必须是 ISO 小时桶") from exc
    return _bucket_start(task, value)


__all__ = [
    "CreditOutcome",
    "credit_success_once",
    "durable_debt",
    "ensure_bucket",
    "get_bucket",
    "ledger_progress",
    "progress_overlay_from_ledger",
    "recent_bucket_summaries",
    "terminalize_task_buckets",
]
