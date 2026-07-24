from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from app.models import Task
from app.timezone import BEIJING_TZ

from .hard_hourly_history import (
    HardHourlyAction,
    recent_actions as _recent_actions,
    recent_actions_query as _recent_actions_query,
)
from .hard_hourly_pacing import (
    daily_coverage_recheck_at as _daily_coverage_recheck_at,
    next_check_at as _next_check_at,
    planning_rate,
)
from .datetime_compat import to_zone

OPEN_STATUSES = {"pending", "claiming", "executing"}
STRATEGY_FORCE_PLANNING = "force_planning"
HARD_HOURLY_FRONTLOAD_WINDOW_SECONDS = 15 * 60
TRANSIENT_REFRESH_BLOCKERS = frozenset({"account_capacity", "account_offline", "dispatcher_lag"})
MEMBERSHIP_BLOCKERS = frozenset({"target_join_pending", "target_membership_pending", "target_required_channel_pending"})
VERIFICATION_BLOCKERS = frozenset({"target_verification_pending", "target_verification_failed", "verification_context_unreadable"})
CAN_SEND_BLOCKERS = frozenset({"target_can_send_blocked", "target_permission", "rule_binding_missing"})
AI_DRAFT_BLOCKERS = frozenset({"ai_generation_unavailable", "ai_mino_draft_unavailable", "quality_filter", "content_policy"})
PLANNER_PROGRESS_SESSION_KEY = "task_center.hard_hourly_planner_progress"


def enabled(task_or_config: Task | dict[str, Any]) -> bool:
    config = task_or_config.type_config if isinstance(task_or_config, Task) else task_or_config
    return bool((config or {}).get("hard_hourly_target_enabled")) and goal(config or {}) > 0


def goal(config: dict[str, Any]) -> int:
    try:
        return max(0, int(config.get("hourly_min_messages") or 0))
    except (TypeError, ValueError):
        return 0


def current_progress(session: Session, task: Task, now: datetime, *, fresh: bool = False) -> dict[str, Any]:
    cache = session.info.get(PLANNER_PROGRESS_SESSION_KEY, {})
    cached = cache.get((task.tenant_id, task.id))
    if cached is not None and not fresh:
        return dict(cached)
    return _current_progress(session, task, now)


def planner_progress_snapshot(session: Session, task: Task, now: datetime) -> dict[str, Any]:
    cache = session.info.setdefault(PLANNER_PROGRESS_SESSION_KEY, {})
    key = (task.tenant_id, task.id)
    if key not in cache:
        cache[key] = _current_progress(session, task, now)
    return dict(cache[key])


def seed_planner_progress_snapshot(session: Session, task: Task, progress: dict[str, Any]) -> dict[str, Any]:
    cache = session.info.setdefault(PLANNER_PROGRESS_SESSION_KEY, {})
    snapshot = dict(progress)
    cache[(task.tenant_id, task.id)] = snapshot
    return dict(snapshot)


def _current_progress(session: Session, task: Task, now: datetime) -> dict[str, Any]:
    stats = hard_hourly_stats(session, task, now, task.stats or {})
    now_local = normalize(task, now)
    delivery_deficit = int(stats.get("hard_hourly_deficit") or 0)
    planning_deficit = int(stats.get("hard_hourly_planning_deficit", delivery_deficit) or 0)
    backfill_delivery_deficit = int(stats.get("hard_hourly_backfill_delivery_deficit") or 0)
    backfill_planning_deficit = int(stats.get("hard_hourly_backfill_planning_deficit") or 0)
    total_planning_deficit = planning_deficit + backfill_planning_deficit
    overdue_open_count = int(stats.get("hard_hourly_overdue_open_count") or 0)
    legacy_progress = {
        "enabled": bool(stats.get("hard_hourly_target_enabled")),
        "goal": int(stats.get("hard_hourly_goal") or 0),
        "bucket": str(stats.get("hard_hourly_bucket") or ""),
        "deficit": total_planning_deficit,
        "delivery_deficit": delivery_deficit + backfill_delivery_deficit,
        "backfill_debt": int(stats.get("hard_hourly_backfill_debt") or 0),
        "backfill_planning_deficit": backfill_planning_deficit,
        "backfill_delivery_deficit": backfill_delivery_deficit,
        "future_open_count": int(stats.get("hard_hourly_open_count") or 0),
        "overdue_open_count": overdue_open_count,
        "planning_blocked": overdue_open_count > 0 and total_planning_deficit > 0,
        "hour_end": hour_bounds(task, now)[1],
        "now": now_local,
    }
    from .continuity_rollout import continuity_enabled

    if not continuity_enabled(session, task.tenant_id):
        return legacy_progress
    from .hard_hourly_ledger import progress_overlay_from_ledger
    config = task.type_config or {}
    # Never create buckets from progress/stats reads — only credit/plan writers may ensure_bucket.
    return progress_overlay_from_ledger(
        session,
        task,
        legacy_progress,
        operation_target_id=int(config.get("target_operation_target_id") or 0),
        target_reference_revision=int(config.get("target_reference_revision") or 0),
        create_bucket=False,
    )


def next_check_for_progress(task: Task, progress: dict[str, Any], now: datetime) -> datetime:
    if planning_blocked_by_dispatcher_lag(progress):
        return _next_check_at(
            {"dispatcher_lag": int(progress.get("overdue_open_count") or 1)},
            progress,
            normalize(task, now),
        )
    return progress["hour_end"] if int(progress.get("deficit") or 0) <= 0 else _next_check_at({}, progress, normalize(task, now))


def requires_planning(session: Session, task: Task, now: datetime, *, fresh: bool = False) -> bool:
    progress = current_progress(session, task, now, fresh=fresh)
    if not bool(progress["enabled"]) or int(progress["deficit"]) <= 0:
        return False
    from .continuity_rollout import continuity_enabled

    return continuity_enabled(session, task.tenant_id) or not planning_blocked_by_dispatcher_lag(progress)


def planning_blocked_by_dispatcher_lag(progress: dict[str, Any]) -> bool:
    if "planning_blocked" in progress:
        return bool(progress["planning_blocked"])
    return int(progress.get("overdue_open_count") or 0) > 0 and int(progress.get("deficit") or 0) > 0


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
    updated.update(_backfill_stat_values(task, buckets, current, current_stats))
    if last_blockers:
        updated["hard_hourly_last_blockers"] = last_blockers
    else:
        updated.pop("hard_hourly_last_blockers", None)
    updated["hard_hourly_recent_buckets"] = buckets
    planning_deficit = int(current.get("planning_deficit") or 0) + int(
        updated.get("hard_hourly_backfill_planning_deficit") or 0
    )
    if planning_deficit <= 0:
        updated["hard_hourly_next_check_at"] = bucket_end.isoformat()
        updated.pop("hard_hourly_last_blockers", None)
    updated.update(_ledger_stat_values(session, task, now))
    return updated


def _ledger_stat_values(session: Session | None, task: Task, now: datetime) -> dict[str, Any]:
    if session is None:
        return {}
    from .continuity_rollout import continuity_enabled

    if not continuity_enabled(session, task.tenant_id):
        return {}
    from .hard_hourly_ledger import ledger_progress, recent_bucket_summaries

    # Stats refresh is observational — never insert empty hour buckets.
    progress = ledger_progress(session, task, now, create_bucket=False)
    if progress is None:
        return {}
    current_deficit = int(progress["current_delivery_deficit"])
    debt = int(progress["durable_debt"])
    unknown = int(progress["unknown_after_send_hold_count"])
    status = "met" if current_deficit <= 0 and debt <= 0 else "awaiting_confirmation" if unknown else "catching_up"
    config = task.type_config or {}
    target_id = int(config.get("target_operation_target_id") or 0)
    revision = int(config.get("target_reference_revision") or 1)
    return {
        "hard_hourly_goal": progress["goal"],
        "hard_hourly_bucket": progress["bucket"],
        "hard_hourly_success_count": progress["success_count"],
        "hard_hourly_open_count": progress["future_open_count"],
        "hard_hourly_overdue_open_count": progress["overdue_open_count"],
        "hard_hourly_deficit": current_deficit,
        "hard_hourly_planning_deficit": progress["required_new"],
        "hard_hourly_backfill_debt": debt,
        "hard_hourly_backfill_planning_deficit": debt,
        "hard_hourly_backfill_delivery_deficit": debt,
        "hard_hourly_durable_debt": debt,
        "hard_hourly_eligible_open_count": progress["eligible_open_count"],
        "hard_hourly_unknown_after_send_hold_count": unknown,
        "hard_hourly_planning_rate": progress["planning_rate"],
        "hard_hourly_required_new": progress["required_new"],
        "hard_hourly_awaiting_confirmation": unknown > 0,
        "hard_hourly_target_operation_target_id": target_id or None,
        "hard_hourly_target_reference_revision": revision,
        "hard_hourly_task_config_revision": int(task.config_revision or 1),
        "hard_hourly_status": status,
        "hard_hourly_recent_buckets": recent_bucket_summaries(
            session,
            task=task,
            now=now,
            operation_target_id=target_id,
            target_reference_revision=revision,
        ),
    }


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
    current_value = progress.get("now")
    current = normalize(task, current_value if isinstance(current_value, datetime) else datetime.now())
    stats["hard_hourly_last_check_at"] = current.isoformat()
    stats["hard_hourly_last_planned_count"] = int(created)
    if blockers:
        stats["hard_hourly_last_blockers"] = blockers
    elif created > 0:
        stats.pop("hard_hourly_last_blockers", None)
    coverage_checkpoint = _parse_datetime((task.stats or {}).get("daily_coverage_next_check_at"))
    coverage_recheck_at = normalize(task, coverage_checkpoint) if coverage_checkpoint is not None else None
    next_check_at = _next_check_at(
        blockers or {},
        progress,
        current,
        created=created,
        coverage_recheck_at=_daily_coverage_recheck_at(blockers or {}, current, coverage_recheck_at),
    )
    stats["hard_hourly_next_check_at"] = next_check_at.isoformat()
    task.hard_hourly_next_check_at = next_check_at
    task.stats = stats


def normalize(task: Task, value: datetime | None) -> datetime:
    if value is None:
        raise ValueError("datetime value is required")
    return to_zone(value, _task_zone(task))


def hour_bounds(task: Task, value: datetime) -> tuple[datetime, datetime]:
    current = normalize(task, value)
    start = current.replace(minute=0, second=0, microsecond=0)
    return start, start + timedelta(hours=1)


def bucket_iso(task: Task, bucket_start: datetime) -> str:
    return normalize(task, bucket_start).isoformat()

def _disabled_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {**stats, "hard_hourly_target_enabled": False, "hard_hourly_status": "disabled"}


def _recent_buckets(session: Session, task: Task, now_local: datetime, current_start: datetime) -> list[dict[str, Any]]:
    actions = _recent_actions(session, task, current_start - timedelta(hours=23))
    starts = _recent_bucket_starts(current_start)
    counts = _recent_bucket_counts(task, actions, starts, now_local)
    return [
        _recent_bucket(task, start, now_local, *counts[index])
        for index, start in enumerate(starts)
    ]


def _recent_bucket_starts(current_start: datetime) -> list[datetime]:
    return [current_start - timedelta(hours=offset) for offset in reversed(range(24))]


def _recent_bucket_counts(
    task: Task,
    actions: list[HardHourlyAction],
    starts: list[datetime],
    now_local: datetime,
) -> list[tuple[int, int, int]]:
    counts = [[0, 0, 0] for _ in starts]
    bucket_indexes = {start: index for index, start in enumerate(starts)}
    for action in actions:
        if action.status == "success":
            executed_at = _normalize_optional(task, action.executed_at)
            index = _recent_bucket_index(bucket_indexes, executed_at)
            if index is not None:
                counts[index][0] += 1
            continue
        if action.status not in OPEN_STATUSES:
            continue
        scheduled_at = _normalize_optional(task, action.scheduled_at)
        index = _recent_bucket_index(bucket_indexes, scheduled_at)
        if index is None:
            continue
        counts[index][1 if scheduled_at >= now_local else 2] += 1
    return [tuple(values) for values in counts]


def _recent_bucket_index(bucket_indexes: dict[datetime, int], value: datetime | None) -> int | None:
    if value is None:
        return None
    return bucket_indexes.get(value.replace(minute=0, second=0, microsecond=0))


def _recent_bucket(
    task: Task,
    start: datetime,
    now_local: datetime,
    success: int,
    future_open: int,
    overdue_open: int,
) -> dict[str, Any]:
    end = start + timedelta(hours=1)
    bucket_goal = goal(task.type_config or {})
    delivery_deficit = max(0, bucket_goal - success)
    planning_deficit = max(0, bucket_goal - success - future_open)
    blockers = _bucket_blockers(delivery_deficit, overdue_open, 0)
    return {
        "bucket": bucket_iso(task, start),
        "goal": bucket_goal,
        "success_count": success,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": delivery_deficit,
        "planning_deficit": planning_deficit,
        "status": _bucket_status(success, delivery_deficit, overdue_open, start, end, now_local),
        "blockers": blockers,
    }


def _bucket_blockers(deficit: int, overdue_open: int, capacity_blocked: int) -> dict[str, int]:
    blockers: dict[str, int] = {}
    if overdue_open and deficit:
        blockers["dispatcher_lag"] = overdue_open
    if capacity_blocked and deficit:
        blockers["account_capacity"] = capacity_blocked
    return blockers


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


def _backfill_stat_values(
    task: Task,
    buckets: list[dict[str, Any]],
    current: dict[str, Any],
    current_stats: dict[str, Any],
) -> dict[str, Any]:
    debt, missed_count = _history_debt(task, buckets, str(current.get("bucket") or ""), current_stats)
    current_goal = int(current.get("goal") or 0)
    current_success = int(current.get("success_count") or 0)
    current_open = int(current.get("future_open_count") or 0)
    planned_surplus = max(0, current_success + current_open - current_goal)
    delivered_surplus = max(0, current_success - current_goal)
    return {
        "hard_hourly_backfill_debt": debt,
        "hard_hourly_backfill_missed_bucket_count": missed_count,
        "hard_hourly_backfill_planning_deficit": max(0, debt - planned_surplus),
        "hard_hourly_backfill_delivery_deficit": max(0, debt - delivered_surplus),
    }


def _history_debt(
    task: Task,
    buckets: list[dict[str, Any]],
    current_bucket: str,
    current_stats: dict[str, Any],
) -> tuple[int, int]:
    active_since = _active_since(task, current_stats)
    raw_debt = 0
    surplus = 0
    missed_count = 0
    for bucket in buckets:
        if str(bucket.get("bucket") or "") == current_bucket:
            continue
        if not _bucket_counts_after_active(task, bucket, active_since):
            continue
        bucket_debt = max(0, int(bucket.get("goal") or 0) - int(bucket.get("success_count") or 0))
        raw_debt += bucket_debt
        surplus += max(0, int(bucket.get("success_count") or 0) - int(bucket.get("goal") or 0))
        missed_count += 1 if bucket_debt > 0 else 0
    return max(0, raw_debt - surplus), missed_count


def _active_since(task: Task, current_stats: dict[str, Any]) -> datetime:
    started_at = _parse_datetime(current_stats.get("started_at"))
    if started_at is not None:
        return normalize(task, started_at)
    if task.scheduled_start is not None:
        return normalize(task, task.scheduled_start)
    return normalize(task, task.created_at)


def _bucket_counts_after_active(task: Task, bucket: dict[str, Any], active_since: datetime) -> bool:
    bucket_start = _parse_datetime(bucket.get("bucket"))
    if bucket_start is None:
        return False
    return normalize(task, bucket_start) + timedelta(hours=1) > active_since


def _parse_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


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


def _task_zone(task: Task) -> ZoneInfo:
    try:
        return ZoneInfo(str(task.timezone or "Asia/Shanghai"))
    except ZoneInfoNotFoundError:
        return BEIJING_TZ


def _normalize_optional(task: Task, value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize(task, value)
