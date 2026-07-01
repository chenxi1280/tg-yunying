from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services.account_capacity import AccountCapacityReservation, account_capacity_decision
from app.timezone import BEIJING_TZ

OPEN_STATUSES = {"pending", "claiming", "executing"}
SEND_FILTER = (Action.task_type == "group_ai_chat", Action.action_type == "send_message")
STRATEGY_FORCE_PLANNING = "force_planning"
HARD_HOURLY_FRONTLOAD_WINDOW_SECONDS = 15 * 60
TRANSIENT_REFRESH_BLOCKERS = frozenset({"account_capacity", "account_offline", "dispatcher_lag"})
MEMBERSHIP_BLOCKERS = frozenset({"target_join_pending", "target_membership_pending", "target_required_channel_pending"})
VERIFICATION_BLOCKERS = frozenset({"target_verification_pending", "target_verification_failed", "verification_context_unreadable"})
CAN_SEND_BLOCKERS = frozenset({"target_can_send_blocked", "target_permission", "rule_binding_missing"})
AI_DRAFT_BLOCKERS = frozenset({"ai_generation_unavailable", "ai_mino_draft_unavailable", "quality_filter", "content_policy"})


@dataclass(frozen=True)
class HardHourlyAction:
    id: str
    status: str
    account_id: int | None
    scheduled_at: datetime | None
    executed_at: datetime | None


def enabled(task_or_config: Task | dict[str, Any]) -> bool:
    config = task_or_config.type_config if isinstance(task_or_config, Task) else task_or_config
    return bool((config or {}).get("hard_hourly_target_enabled")) and goal(config or {}) > 0


def goal(config: dict[str, Any]) -> int:
    try:
        return max(0, int(config.get("hourly_min_messages") or 0))
    except (TypeError, ValueError):
        return 0


def current_progress(session: Session, task: Task, now: datetime) -> dict[str, Any]:
    stats = hard_hourly_stats(session, task, now, task.stats or {})
    now_local = normalize(task, now)
    delivery_deficit = int(stats.get("hard_hourly_deficit") or 0)
    planning_deficit = int(stats.get("hard_hourly_planning_deficit", delivery_deficit) or 0)
    return {
        "enabled": bool(stats.get("hard_hourly_target_enabled")),
        "goal": int(stats.get("hard_hourly_goal") or 0),
        "bucket": str(stats.get("hard_hourly_bucket") or ""),
        "deficit": planning_deficit,
        "delivery_deficit": delivery_deficit,
        "future_open_count": int(stats.get("hard_hourly_open_count") or 0),
        "overdue_open_count": int(stats.get("hard_hourly_overdue_open_count") or 0),
        "hour_end": hour_bounds(task, now)[1],
        "now": now_local,
    }


def requires_planning(session: Session, task: Task, now: datetime) -> bool:
    progress = current_progress(session, task, now)
    return bool(progress["enabled"]) and int(progress["deficit"]) > 0


def hard_hourly_stats(session: Session, task: Task, now: datetime, current_stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "group_ai_chat" or not enabled(task):
        return _disabled_stats(current_stats)
    now_local = normalize(task, now)
    bucket_start, bucket_end = hour_bounds(task, now)
    buckets = _recent_buckets(session, task, now_local, bucket_start)
    current = buckets[-1]
    last_blockers = _effective_last_blockers(current, current_stats)
    status = _current_status(current, now_local, bucket_end, last_blockers)
    updated = dict(current_stats)
    updated.update(_current_stat_values(task, now_local, current, status, last_blockers))
    if last_blockers:
        updated["hard_hourly_last_blockers"] = last_blockers
    else:
        updated.pop("hard_hourly_last_blockers", None)
    updated["hard_hourly_recent_buckets"] = buckets
    if int(current.get("deficit") or 0) <= 0:
        updated.pop("hard_hourly_next_check_at", None)
        updated.pop("hard_hourly_last_blockers", None)
    return updated


def hard_schedule_times(total: int, task: Task, now: datetime, *, target_total: int | None = None) -> list[datetime]:
    if total <= 0:
        return []
    current = normalize(task, now)
    _start, hour_end = hour_bounds(task, current)
    available = max(0, int((hour_end - current).total_seconds()) - 1)
    available = min(available, HARD_HOURLY_FRONTLOAD_WINDOW_SECONDS)
    if available <= 0 or total == 1:
        return [current for _ in range(total)]
    spacing_total = max(total, int(target_total or total), 1)
    step = available // spacing_total
    if step <= 0:
        return [current for _ in range(total)]
    return [
        min(current + timedelta(seconds=step * index), hour_end - timedelta(seconds=1))
        for index in range(total)
    ]


def mark_plan_result(task: Task, progress: dict[str, Any], created: int, blockers: dict[str, int] | None = None) -> None:
    stats = dict(task.stats or {})
    current = progress.get("now")
    current = current if isinstance(current, datetime) else normalize(task, datetime.now())
    stats["hard_hourly_last_check_at"] = current.isoformat()
    stats["hard_hourly_last_planned_count"] = int(created)
    if blockers:
        stats["hard_hourly_last_blockers"] = blockers
    elif created > 0:
        stats.pop("hard_hourly_last_blockers", None)
    stats["hard_hourly_next_check_at"] = _next_check_at(task, blockers or {}, progress, current).isoformat()
    task.stats = stats


def normalize(task: Task, value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    if value.tzinfo is None:
        return value
    return value.astimezone(_task_zone(task)).replace(tzinfo=None)


def hour_bounds(task: Task, value: datetime) -> tuple[datetime, datetime]:
    current = normalize(task, value)
    start = current.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


def bucket_iso(task: Task, bucket_start: datetime) -> str:
    return bucket_start.replace(tzinfo=_task_zone(task)).isoformat()


def _disabled_stats(stats: dict[str, Any]) -> dict[str, Any]:
    updated = dict(stats)
    updated.update({"hard_hourly_target_enabled": False, "hard_hourly_status": "disabled"})
    return updated


def _recent_buckets(session: Session, task: Task, now_local: datetime, current_start: datetime) -> list[dict[str, Any]]:
    actions = _recent_actions(session, task, current_start - timedelta(hours=23))
    return [
        _bucket_summary(session, task, actions, current_start - timedelta(hours=offset), now_local)
        for offset in reversed(range(24))
    ]


def _recent_actions(session: Session, task: Task, earliest: datetime) -> list[HardHourlyAction]:
    rows = session.execute(
        select(
            Action.id,
            Action.status,
            Action.account_id,
            Action.scheduled_at,
            Action.executed_at,
        ).where(
            Action.task_id == task.id,
            *SEND_FILTER,
            (Action.executed_at >= earliest) | (Action.scheduled_at >= earliest),
        )
    )
    return [
        HardHourlyAction(
            str(row.id),
            str(row.status),
            row.account_id,
            row.scheduled_at,
            row.executed_at,
        )
        for row in rows
    ]


def _bucket_summary(
    session: Session,
    task: Task,
    actions: list[HardHourlyAction],
    start: datetime,
    now_local: datetime,
) -> dict[str, Any]:
    end = start + timedelta(hours=1)
    success = sum(1 for action in actions if _is_success_in_bucket(task, action, start, end))
    future_open, capacity_blocked = _effective_future_open_count(
        session,
        task,
        actions,
        start,
        end,
        now_local,
    )
    overdue_open = sum(
        1
        for action in actions
        if _is_overdue_open_in_bucket(task, action, start, end, now_local)
    )
    # Hard-hourly acceptance is based on delivered messages.
    # Future pending work stays visible, but it is not counted as done.
    effective_future_open = 0 if enabled(task) else future_open
    delivery_deficit = max(0, goal(task.type_config or {}) - success - effective_future_open)
    planning_deficit = max(0, goal(task.type_config or {}) - success - future_open)
    blockers = _bucket_blockers(delivery_deficit, overdue_open, capacity_blocked)
    return {
        "bucket": bucket_iso(task, start),
        "goal": goal(task.type_config or {}),
        "success_count": success,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": delivery_deficit,
        "planning_deficit": planning_deficit,
        "status": _bucket_status(success, delivery_deficit, overdue_open, start, end, now_local),
        "blockers": blockers,
    }


def _effective_future_open_count(
    session: Session,
    task: Task,
    actions: list[HardHourlyAction],
    start: datetime,
    end: datetime,
    now_local: datetime,
) -> tuple[int, int]:
    future_actions = _future_open_actions(task, actions, start, end, now_local)
    if enabled(task):
        return len(future_actions), 0
    excluded_ids = {action.id for action in future_actions}
    reservations: list[AccountCapacityReservation] = []
    accepted = 0
    blocked = 0
    for action in future_actions:
        if not action.account_id or not action.scheduled_at:
            blocked += 1
            continue
        scheduled_at = _normalize_optional(task, action.scheduled_at)
        decision = account_capacity_decision(
            session,
            tenant_id=task.tenant_id,
            account_id=int(action.account_id),
            scheduled_at=scheduled_at,
            exclude_action_ids=excluded_ids,
            reservations=reservations,
        )
        if decision.available:
            accepted += 1
            reservations.append(AccountCapacityReservation(int(action.account_id), scheduled_at))
        else:
            blocked += 1
    return accepted, blocked


def _future_open_actions(
    task: Task,
    actions: list[HardHourlyAction],
    start: datetime,
    end: datetime,
    now_local: datetime,
) -> list[HardHourlyAction]:
    rows = [action for action in actions if _is_future_open_in_bucket(task, action, start, end, now_local)]
    return sorted(rows, key=lambda action: _normalize_optional(task, action.scheduled_at) or end)


def _bucket_blockers(deficit: int, overdue_open: int, capacity_blocked: int) -> dict[str, int]:
    blockers: dict[str, int] = {}
    if overdue_open and deficit:
        blockers["dispatcher_lag"] = overdue_open
    if capacity_blocked and deficit:
        blockers["account_capacity"] = capacity_blocked
    return blockers


def _is_success_in_bucket(task: Task, action: HardHourlyAction, start: datetime, end: datetime) -> bool:
    executed_at = _normalize_optional(task, action.executed_at)
    return (
        action.status == "success"
        and executed_at is not None
        and start <= executed_at < end
    )


def _is_future_open_in_bucket(task: Task, action: HardHourlyAction, start: datetime, end: datetime, now_local: datetime) -> bool:
    scheduled_at = _normalize_optional(task, action.scheduled_at)
    return (
        action.status in OPEN_STATUSES
        and scheduled_at is not None
        and start <= scheduled_at < end
        and scheduled_at >= now_local
    )


def _is_overdue_open_in_bucket(task: Task, action: HardHourlyAction, start: datetime, end: datetime, now_local: datetime) -> bool:
    scheduled_at = _normalize_optional(task, action.scheduled_at)
    return (
        action.status in OPEN_STATUSES
        and scheduled_at is not None
        and start <= scheduled_at < end
        and scheduled_at < now_local
    )


def _bucket_status(success: int, deficit: int, overdue: int, start: datetime, end: datetime, now_local: datetime) -> str:
    if deficit <= 0:
        return "met"
    if end <= now_local:
        return "missed"
    if overdue and start <= now_local < end:
        return "blocked"
    return "catching_up"


def _current_status(bucket: dict[str, Any], now_local: datetime, hour_end: datetime, blockers: dict[str, Any]) -> str:
    if int(bucket["success_count"]) >= int(bucket["goal"]):
        return "met"
    if hour_end <= now_local:
        return "missed"
    if int(bucket.get("overdue_open_count") or 0) and int(bucket.get("deficit") or 0):
        return "blocked"
    if blockers and int(bucket.get("deficit") or 0):
        return "blocked"
    return "catching_up"


def _current_stat_values(
    task: Task,
    now_local: datetime,
    bucket: dict[str, Any],
    status: str,
    blockers: dict[str, int],
) -> dict[str, Any]:
    return {
        "hard_hourly_target_enabled": True,
        "hard_hourly_goal": goal(task.type_config or {}),
        "hard_hourly_bucket": bucket["bucket"],
        "hard_hourly_success_count": bucket["success_count"],
        "hard_hourly_open_count": bucket["future_open_count"],
        "hard_hourly_overdue_open_count": bucket["overdue_open_count"],
        "hard_hourly_deficit": bucket["deficit"],
        "hard_hourly_planning_deficit": bucket["planning_deficit"],
        "hard_hourly_status": status,
        "hard_hourly_pipeline": _pipeline_status(status, blockers),
    }


def _pipeline_status(status: str, blockers: dict[str, int]) -> dict[str, str]:
    reasons = set(blockers.keys())
    return {
        "membership": _stage_status(reasons, MEMBERSHIP_BLOCKERS),
        "verification": _stage_status(reasons, VERIFICATION_BLOCKERS),
        "can_send": _stage_status(reasons, CAN_SEND_BLOCKERS),
        "ai_draft": _stage_status(reasons, AI_DRAFT_BLOCKERS),
        "dispatcher": _stage_status(reasons, frozenset({"dispatcher_lag"})),
        "hourly_target": status,
    }


def _stage_status(reasons: set[str], stage_reasons: frozenset[str]) -> str:
    return "blocked" if reasons & stage_reasons else "ready"


def _effective_last_blockers(current: dict[str, Any], current_stats: dict[str, Any]) -> dict[str, int]:
    blockers = _int_blockers(current.get("blockers"))
    if blockers:
        return blockers
    raw_planning_deficit = current.get("planning_deficit")
    planning_deficit = raw_planning_deficit if raw_planning_deficit is not None else current.get("deficit")
    if int(planning_deficit or 0) <= 0:
        return {}
    return {
        reason: count
        for reason, count in _int_blockers(current_stats.get("hard_hourly_last_blockers")).items()
        if reason not in TRANSIENT_REFRESH_BLOCKERS
    }


def _int_blockers(value: object) -> dict[str, int]:
    if not isinstance(value, dict):
        return {}
    return {
        str(reason): count
        for reason, raw_count in value.items()
        if (count := _positive_int(raw_count)) > 0
    }


def _positive_int(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _next_check_at(task: Task, blockers: dict[str, int], progress: dict[str, Any], current: datetime) -> datetime:
    if blockers.get("ai_generation_unavailable"):
        return current + timedelta(minutes=1)
    if blockers.get("quality_filter"):
        return current + timedelta(seconds=60)
    if blockers.get("dispatcher_lag"):
        return current + timedelta(seconds=30)
    return current + timedelta(seconds=30 if int(progress.get("deficit") or 0) else 300)


def _task_zone(task: Task) -> ZoneInfo:
    try:
        return ZoneInfo(str(task.timezone or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return BEIJING_TZ


def _normalize_optional(task: Task, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize(task, value)
