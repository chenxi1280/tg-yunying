from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyTarget,
    TaskMembershipAdmissionItem,
    TgGroup,
)
from app.services._common import _now


def effective_daily_message_target(configured: int, current_required: int) -> int:
    return max(1, int(configured or 0), int(current_required or 0))


def ensure_task_group_daily_target(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
    *,
    now: datetime | None = None,
) -> TaskGroupDailyTarget:
    timestamp = now or _now()
    target = _locked_target(session, task, group, target_date)
    if target is None:
        target = _new_target(session, task, group, target_date, timestamp)
        session.add(target)
        session.flush()
    return refresh_task_group_daily_target(session, target)


def refresh_task_group_daily_target(
    session: Session,
    target: TaskGroupDailyTarget,
) -> TaskGroupDailyTarget:
    start = datetime.combine(target.target_date, time.min)
    end = start + timedelta(days=1)
    task = session.get(Task, target.task_id)
    if task is None:
        raise ValueError("task_group_daily_target_task_missing")
    configured = max(1, int((task.type_config or {}).get("daily_message_target") or 1))
    current_required = _current_required_account_count(session, target)
    planned = effective_daily_message_target(configured, current_required)
    _apply_current_target(
        target,
        configured=configured,
        current_required=current_required,
        planned=planned,
    )
    target.confirmed_message_count = _confirmed_message_count(session, target, start, end)
    target.gateway_started_count = _gateway_started_count(
        session,
        target,
        start=start,
        end=end,
    )
    target.unknown_hold_count = _unknown_hold_count(
        session,
        target,
        start=start,
        end=end,
    )
    target.target_reduction_overage_count = max(
        0,
        target.confirmed_message_count
        + target.gateway_started_count
        + target.unknown_hold_count
        - planned,
    )
    target.coverage_confirmed_account_count = _coverage_confirmed_count(session, target)
    target.updated_at = _now()
    return target


def daily_group_due_message_count(
    target: TaskGroupDailyTarget,
    pacing_config: dict,
    *,
    immediate: bool = False,
    now: datetime | None = None,
) -> int:
    timestamp = _wall_time(now or _now())
    day_start = datetime.combine(target.target_date, time.min)
    day_end = day_start + timedelta(days=1)
    start = max(day_start, _wall_time(target.scope_frozen_at))
    if timestamp < start:
        return 0
    if immediate or timestamp >= day_end:
        return target.effective_message_target
    curve = _positive_hourly_curve(pacing_config)
    ratio = _weighted_seconds(start, timestamp, curve) / max(
        1.0,
        _weighted_seconds(start, day_end, curve),
    )
    return max(1, min(
        target.effective_message_target,
        math.floor(target.effective_message_target * ratio),
    ))


def _locked_target(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
) -> TaskGroupDailyTarget | None:
    statement = select(TaskGroupDailyTarget).where(
        TaskGroupDailyTarget.tenant_id == task.tenant_id,
        TaskGroupDailyTarget.task_id == task.id,
        TaskGroupDailyTarget.group_id == group.id,
        TaskGroupDailyTarget.target_date == target_date,
    )
    return session.scalar(statement)


def _new_target(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
    timestamp: datetime,
) -> TaskGroupDailyTarget:
    frozen_accounts = _frozen_account_count(session, task, group, target_date)
    configured = max(1, int((task.type_config or {}).get("daily_message_target") or 1))
    phase, committed_at = _fulfillment_phase(task, target_date)
    scope_frozen_at = timestamp if phase == "admission_warming" else committed_at
    return TaskGroupDailyTarget(
        tenant_id=task.tenant_id,
        task_id=task.id,
        group_id=group.id,
        target_date=target_date,
        configured_message_target=configured,
        frozen_account_count=frozen_accounts,
        effective_message_target=effective_daily_message_target(configured, frozen_accounts),
        planned_daily_target=effective_daily_message_target(configured, frozen_accounts),
        planned_target_revision=1,
        target_changed_at=timestamp,
        target_change_reason="created_from_current_task_scope",
        daily_fulfillment_phase=phase,
        scope_frozen_at=scope_frozen_at,
        full_day_committed_at=committed_at,
    )


def _frozen_account_count(
    session: Session,
    task: Task,
    group: TgGroup,
    target_date: date,
) -> int:
    coverage_count = int(session.scalar(
        select(func.count(TaskAccountDailyCoverage.id)).where(
            TaskAccountDailyCoverage.tenant_id == task.tenant_id,
            TaskAccountDailyCoverage.task_id == task.id,
            TaskAccountDailyCoverage.group_id == group.id,
            TaskAccountDailyCoverage.coverage_date == target_date,
        )
    ) or 0)
    if coverage_count > 0:
        return coverage_count
    return int(session.scalar(
        select(func.count(TaskMembershipAdmissionItem.id)).where(
            TaskMembershipAdmissionItem.tenant_id == task.tenant_id,
            TaskMembershipAdmissionItem.task_id == task.id,
        )
    ) or 0)


def _current_required_account_count(
    session: Session,
    target: TaskGroupDailyTarget,
) -> int:
    total, required = session.execute(
        select(
            func.count(TaskAccountDailyCoverage.id),
            func.count(TaskAccountDailyCoverage.id).filter(
                TaskAccountDailyCoverage.state != "abandoned_for_day"
            ),
        ).where(
            TaskAccountDailyCoverage.tenant_id == target.tenant_id,
            TaskAccountDailyCoverage.task_id == target.task_id,
            TaskAccountDailyCoverage.group_id == target.group_id,
            TaskAccountDailyCoverage.coverage_date == target.target_date,
        )
    ).one()
    if int(total or 0) > 0:
        return int(required or 0)
    return int(session.scalar(select(func.count(TaskMembershipAdmissionItem.id)).where(
        TaskMembershipAdmissionItem.tenant_id == target.tenant_id,
        TaskMembershipAdmissionItem.task_id == target.task_id,
    )) or 0)


def _apply_current_target(
    target: TaskGroupDailyTarget,
    *,
    configured: int,
    current_required: int,
    planned: int,
) -> None:
    previous = int(target.planned_daily_target or target.effective_message_target or 1)
    previous_configured = int(target.configured_message_target or 1)
    target.configured_message_target = configured
    target.frozen_account_count = current_required
    target.effective_message_target = planned
    target.planned_daily_target = planned
    if planned == previous:
        return
    target.planned_target_revision = int(target.planned_target_revision or 1) + 1
    target.target_changed_at = _now()
    target.target_change_reason = (
        "current_required_account_count_changed"
        if configured == previous_configured
        else "configured_daily_target_changed"
    )


def _gateway_started_count(
    session: Session,
    target: TaskGroupDailyTarget,
    *,
    start: datetime,
    end: datetime,
) -> int:
    return int(session.scalar(
        select(func.count(func.distinct(Action.id)))
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.task_id == target.task_id,
            Action.status == "executing",
            ExecutionAttempt.gateway_call_started_at >= start,
            ExecutionAttempt.gateway_call_started_at < end,
        )
    ) or 0)


def _unknown_hold_count(
    session: Session,
    target: TaskGroupDailyTarget,
    *,
    start: datetime,
    end: datetime,
) -> int:
    return int(session.scalar(select(func.count(Action.id)).where(
        Action.task_id == target.task_id,
        Action.status == "unknown_after_send",
        Action.executed_at >= start,
        Action.executed_at < end,
    )) or 0)


def _fulfillment_phase(task: Task, target_date: date) -> tuple[str, datetime]:
    day_start = datetime.combine(target_date, time.min)
    started_at = task.scheduled_start or task.created_at
    if started_at and _wall_time(started_at).date() == target_date and _wall_time(started_at) > day_start:
        return "admission_warming", day_start + timedelta(days=1)
    return "full_day_committed", day_start


def _confirmed_message_count(
    session: Session,
    target: TaskGroupDailyTarget,
    start: datetime,
    end: datetime,
) -> int:
    actions = list(session.scalars(
        select(Action)
        .where(
            Action.tenant_id == target.tenant_id,
            Action.task_id == target.task_id,
            Action.action_type == "send_message",
            Action.status == "success",
            Action.executed_at >= start,
            Action.executed_at < end,
            select(ExecutionAttempt.id).where(
                ExecutionAttempt.action_id == Action.id,
                ExecutionAttempt.status == "success",
                ExecutionAttempt.remote_message_id != "",
                ExecutionAttempt.account_id == Action.account_id,
            ).exists(),
        )
    ))
    memory_ids = {
        str((action.payload or {}).get("ai_message_memory_id") or "")
        for action in actions
    }
    memories = {
        memory.id: memory
        for memory in session.scalars(select(AiGroupMessageMemory).where(
            AiGroupMessageMemory.id.in_(memory_ids - {""}),
        ))
    }
    return sum(
        1
        for action in actions
        if _content_evidence_valid(
            action,
            memories.get(str((action.payload or {}).get("ai_message_memory_id") or "")),
        )
    )


def _content_evidence_valid(
    action: Action,
    memory: AiGroupMessageMemory | None,
) -> bool:
    payload = action.payload or {}
    if not memory or memory.action_id != action.id or memory.account_id != action.account_id:
        return False
    source = str(payload.get("content_source") or "")
    if source == "mask_missing_check_in":
        return bool(
            payload.get("coverage_ledger_id")
            and memory.content_source == source
            and memory.mask_status == "missing"
        )
    return bool(
        payload.get("account_mask_id")
        and int(payload.get("account_mask_version") or 0) > 0
        and memory.account_mask_id == payload.get("account_mask_id")
        and memory.mask_status == "active"
        and int(memory.account_mask_version or 0) == int(payload.get("account_mask_version") or 0)
        and memory.mask_contract_version == payload.get("voice_profile_contract_version")
        and memory.mask_snapshot_hash == payload.get("account_mask_snapshot_hash")
    )


def _coverage_confirmed_count(session: Session, target: TaskGroupDailyTarget) -> int:
    return int(session.scalar(
        select(func.count(TaskAccountDailyCoverage.id)).where(
            TaskAccountDailyCoverage.tenant_id == target.tenant_id,
            TaskAccountDailyCoverage.task_id == target.task_id,
            TaskAccountDailyCoverage.group_id == target.group_id,
            TaskAccountDailyCoverage.coverage_date == target.target_date,
            TaskAccountDailyCoverage.confirmed_count >= 1,
        )
    ) or 0)


def _wall_time(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


def _positive_hourly_curve(pacing_config: dict) -> list[int]:
    profile = pacing_config.get("operation_profile") or {}
    raw = profile.get("hourly_activity_curve") if isinstance(profile, dict) else None
    if not isinstance(raw, list) or len(raw) != 24:
        return [1] * 24
    try:
        return [max(1, int(value)) for value in raw]
    except (TypeError, ValueError):
        return [1] * 24


def _weighted_seconds(start: datetime, end: datetime, curve: list[int]) -> float:
    cursor = start
    total = 0.0
    while cursor < end:
        next_hour = cursor.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        boundary = min(end, next_hour)
        total += curve[cursor.hour] * (boundary - cursor).total_seconds()
        cursor = boundary
    return total


__all__ = [
    "effective_daily_message_target",
    "daily_group_due_message_count",
    "ensure_task_group_daily_target",
    "refresh_task_group_daily_target",
]
