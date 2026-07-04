from __future__ import annotations

import hashlib
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, SearchJoinPacingDecision, Task

REAL_ACTION_STATUSES = {"pending", "claiming", "executing", "success", "failed"}
DEFAULT_SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class PacingWindow:
    local_date: date
    hour_start: datetime


@dataclass
class PacingStats:
    tenant_timezone: str = ""
    local_date: str = ""
    task_daily_action_count: int = 0
    task_daily_remaining: int = 0
    per_account_daily_limit_reached: int = 0
    per_account_total_limit_reached: int = 0
    per_account_cooldown_days_active: int = 0
    per_keyword_account_daily_limit_reached: int = 0
    task_daily_limit_reached: int = 0
    hourly_skipped_by_pacing: int = 0
    daily_skipped_by_pacing: int = 0
    last_limit_reason: str = ""
    blocked_accounts: set[int] = field(default_factory=set)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "task_daily_action_count": self.task_daily_action_count,
            "task_daily_remaining": self.task_daily_remaining,
            "tenant_timezone": self.tenant_timezone,
            "local_date": self.local_date,
            "per_account_daily_limit_reached": self.per_account_daily_limit_reached,
            "per_account_total_limit_reached": self.per_account_total_limit_reached,
            "per_account_cooldown_days_active": self.per_account_cooldown_days_active,
            "per_keyword_account_daily_limit_reached": self.per_keyword_account_daily_limit_reached,
            "task_daily_limit_reached": self.task_daily_limit_reached,
            "hourly_skipped_by_pacing": self.hourly_skipped_by_pacing,
            "daily_skipped_by_pacing": self.daily_skipped_by_pacing,
            "last_limit_reason": self.last_limit_reason,
        }


def pacing_window(task: Task, now_value: datetime) -> PacingWindow:
    timezone = ZoneInfo(task.timezone or "Asia/Shanghai")
    source_now = now_value if now_value.tzinfo else now_value.replace(tzinfo=DEFAULT_SOURCE_TIMEZONE)
    local_now = source_now.astimezone(timezone)
    return PacingWindow(local_date=local_now.date(), hour_start=local_now.replace(minute=0, second=0, microsecond=0))


def should_skip_window(session: Session, task: Task, scope: str, probability: float, window: PacingWindow) -> bool:
    if probability <= 0:
        return False
    if probability >= 1:
        skipped = True
    else:
        skipped = _seeded_bool(task.id, scope, _scope_key(scope, window), probability)
    decision = _decision(session, task, scope, _scope_key(scope, window), window, skipped=skipped)
    return bool(decision.decision_value.get("skipped"))


def should_skip_action(session: Session, task: Task, candidate_key: str, probability: float, window: PacingWindow) -> bool:
    decision = planned_action_decision(session, task, candidate_key, probability, 0, 0, window)
    return bool(decision.decision_value.get("skipped"))


def planned_action_decision(
    session: Session,
    task: Task,
    candidate_key: str,
    skip_probability: float,
    hourly_jitter_percent: int,
    daily_jitter_percent: int,
    window: PacingWindow,
    *,
    account_id: int | None = None,
    keyword_hash: str = "",
    base_scheduled_at: datetime | None = None,
) -> SearchJoinPacingDecision:
    threshold = max(0.0, min(1.0, float(skip_probability or 0)))
    sampled = _seeded_float(task.id, "action", candidate_key)
    skipped = sampled < threshold
    scheduled_at = _jittered_at(
        task.id,
        candidate_key,
        base_scheduled_at or datetime.now(),
        int(hourly_jitter_percent or 0),
        int(daily_jitter_percent or 0),
        window,
    )
    return _decision(
        session,
        task,
        "action",
        candidate_key,
        window,
        skipped=skipped,
        account_id=account_id,
        keyword_hash=keyword_hash,
        sampled_value=sampled,
        threshold=threshold,
        scheduled_at=scheduled_at,
        reason="skipped_by_behavior_pacing" if skipped else "planned",
        decision_value={
            "skipped": skipped,
            "hourly_jitter_percent": hourly_jitter_percent,
            "daily_jitter_percent": daily_jitter_percent,
        },
    )


def account_allowed(session: Session, task: Task, account_id: int, keyword_hash: str, window: PacingWindow, stats: PacingStats) -> bool:
    return account_base_allowed(session, task, account_id, window, stats) and keyword_allowed(session, task, account_id, keyword_hash, window, stats)


def account_base_allowed(session: Session, task: Task, account_id: int, window: PacingWindow, stats: PacingStats) -> bool:
    pacing = task.pacing_config or {}
    allowed = True
    if _total_count(session, task, account_id) >= int(pacing.get("per_account_total_action_limit") or 0) > 0:
        allowed = _block_account(stats, account_id, "per_account_total_limit_reached")
    if _cooldown_active(session, task, account_id, int(pacing.get("per_account_cooldown_days") or 0)):
        allowed = _block_account(stats, account_id, "per_account_cooldown_days_active")
    if _daily_count(session, task, account_id, window.local_date) >= int(pacing.get("per_account_daily_action_limit") or 0) > 0:
        allowed = _block_account(stats, account_id, "per_account_daily_limit_reached")
    return allowed


def keyword_allowed(session: Session, task: Task, account_id: int, keyword_hash: str, window: PacingWindow, stats: PacingStats) -> bool:
    pacing = task.pacing_config or {}
    limit = int(pacing.get("per_keyword_account_daily_limit") or 0)
    if limit > 0 and _daily_count(session, task, account_id, window.local_date, keyword_hash=keyword_hash) >= limit:
        return _block_account(stats, account_id, "per_keyword_account_daily_limit_reached")
    return True


def task_daily_capacity(session: Session, task: Task, window: PacingWindow, requested: int, stats: PacingStats) -> int:
    max_daily = int((task.pacing_config or {}).get("max_actions_per_day") or 0)
    count = _task_daily_count(session, task, window.local_date)
    stats.tenant_timezone = task.timezone or "Asia/Shanghai"
    stats.local_date = window.local_date.isoformat()
    stats.task_daily_action_count = count
    if max_daily <= 0:
        stats.task_daily_remaining = requested
        return requested
    remaining = max(0, max_daily - count)
    stats.task_daily_remaining = remaining
    if remaining <= 0:
        stats.task_daily_limit_reached = 1
        stats.last_limit_reason = "task_daily_limit_reached"
    return min(requested, remaining)


def _decision(
    session: Session,
    task: Task,
    scope: str,
    key: str,
    window: PacingWindow,
    *,
    skipped: bool,
    account_id: int | None = None,
    keyword_hash: str = "",
    sampled_value: float | None = None,
    threshold: float | None = None,
    scheduled_at: datetime | None = None,
    reason: str = "",
    decision_value: dict | None = None,
) -> SearchJoinPacingDecision:
    existing = session.scalar(
        select(SearchJoinPacingDecision).where(
            SearchJoinPacingDecision.tenant_id == task.tenant_id,
            SearchJoinPacingDecision.task_id == task.id,
            SearchJoinPacingDecision.decision_scope == scope,
            SearchJoinPacingDecision.scope_key == key,
        )
    )
    if existing:
        return existing
    decision = SearchJoinPacingDecision(
        tenant_id=task.tenant_id,
        task_id=task.id,
        decision_scope=scope,
        scope_key=key,
        tenant_timezone=task.timezone or "Asia/Shanghai",
        local_date=window.local_date,
        window_start=window.hour_start,
        account_id=account_id,
        keyword_hash=keyword_hash,
        sampled_value=sampled_value,
        threshold=threshold,
        scheduled_at=scheduled_at,
        reason=reason,
        decision_value=decision_value or {"skipped": skipped},
    )
    session.add(decision)
    session.flush()
    return decision


def _seeded_bool(task_id: str, scope: str, key: str, probability: float) -> bool:
    return _seeded_float(task_id, scope, key) < probability


def _seeded_float(task_id: str, scope: str, key: str) -> float:
    seed = hashlib.sha256(f"{task_id}:{scope}:{key}".encode("utf-8")).hexdigest()
    return random.Random(seed).random()


def _jittered_at(task_id: str, key: str, base: datetime, hourly_jitter_percent: int, daily_jitter_percent: int, window: PacingWindow) -> datetime:
    hourly_seconds = _jitter_seconds(hourly_jitter_percent)
    daily_seconds = _jitter_seconds(daily_jitter_percent)
    if hourly_seconds <= 0 and daily_seconds <= 0:
        return base
    max_seconds = max(hourly_seconds, daily_seconds)
    hourly_weight = _seeded_float(task_id, "hourly_jitter", key) * hourly_seconds
    daily_weight = _seeded_float(task_id, f"daily_jitter:{window.local_date.isoformat()}", key) * daily_seconds
    divisor = max(1, hourly_seconds + daily_seconds)
    offset = int(((hourly_weight + daily_weight) / divisor) * max_seconds)
    candidate = _source_naive(base) + timedelta(seconds=offset)
    return min(candidate, _bucket_end_source(window) - timedelta(seconds=1))


def _jitter_seconds(percent: int) -> int:
    return int(3600 * max(0, min(100, int(percent or 0))) / 100)


def _scope_key(scope: str, window: PacingWindow) -> str:
    return window.local_date.isoformat() if scope == "daily" else window.hour_start.isoformat()


def _block_account(stats: PacingStats, account_id: int, reason: str) -> bool:
    stats.blocked_accounts.add(account_id)
    setattr(stats, reason, int(getattr(stats, reason)) + 1)
    stats.last_limit_reason = reason
    return False


def _total_count(session: Session, task: Task, account_id: int) -> int:
    return _count(session, task, account_id)


def _daily_count(session: Session, task: Task, account_id: int, local_date: date, *, keyword_hash: str = "") -> int:
    return _count_for_local_date(session, task, local_date, account_id=account_id, keyword_hash=keyword_hash)


def _task_daily_count(session: Session, task: Task, local_date: date) -> int:
    return _count_for_local_date(session, task, local_date)


def _count_for_local_date(session: Session, task: Task, local_date: date, *, account_id: int | None = None, keyword_hash: str = "") -> int:
    start_at, end_at = _local_day_bounds_source(task.timezone, local_date)
    action_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    filters = [
        Action.task_id == task.id,
        Action.action_type == "search_join",
        Action.status.in_(REAL_ACTION_STATUSES),
        action_at >= start_at,
        action_at < end_at,
    ]
    if account_id is not None:
        filters.append(Action.account_id == account_id)
    if keyword_hash:
        filters.append(Action.payload["keyword_hash"].as_string() == keyword_hash)
    actions = session.scalars(select(Action).where(*filters))
    return sum(1 for action in actions if not _is_behavior_pacing_skip(action))


def _local_day_bounds_source(timezone_name: str, local_date: date) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=timezone)
    end = start + timedelta(days=1)
    return _source_naive(start), _source_naive(end)


def _bucket_end_source(window: PacingWindow) -> datetime:
    return _source_naive(window.hour_start + timedelta(hours=1))


def _source_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(DEFAULT_SOURCE_TIMEZONE).replace(tzinfo=None)


def _is_behavior_pacing_skip(action: Action) -> bool:
    result = action.result or {}
    return result.get("skip_reason") == "skipped_by_behavior_pacing"


def _cooldown_active(session: Session, task: Task, account_id: int, cooldown_days: int) -> bool:
    if cooldown_days <= 0:
        return False
    last_at = session.scalar(
        select(func.max(func.coalesce(Action.executed_at, Action.scheduled_at))).where(
            Action.task_id == task.id,
            Action.action_type == "search_join",
            Action.account_id == account_id,
            Action.status.in_(REAL_ACTION_STATUSES),
        )
    )
    return bool(last_at and last_at + timedelta(days=cooldown_days) > datetime.now())


def _count(session: Session, task: Task, account_id: int, extra_filters: list | None = None) -> int:
    filters = [
        Action.task_id == task.id,
        Action.action_type == "search_join",
        Action.account_id == account_id,
        Action.status.in_(REAL_ACTION_STATUSES),
    ]
    filters.extend(extra_filters or [])
    return int(session.scalar(select(func.count(Action.id)).where(*filters)) or 0)


__all__ = [
    "PacingStats",
    "account_allowed",
    "account_base_allowed",
    "keyword_allowed",
    "pacing_window",
    "planned_action_decision",
    "should_skip_action",
    "should_skip_window",
    "task_daily_capacity",
]
