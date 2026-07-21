from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, Task, Tenant
from app.models.search_rank_deboost import SearchRankDeboostActionStat, SearchRankDeboostClickReservation
from app.services._common import _now

REAL_ACTION_STATUSES = {"pending", "claiming", "executing", "success", "failed"}
NON_EXPIRING_RESERVATION_STATUSES = {"consumed", "unknown"}
DEFAULT_SOURCE_TIMEZONE = ZoneInfo("Asia/Shanghai")

DEFAULT_PER_ACCOUNT_DAILY_CLICK_LIMIT = 5
DEFAULT_PER_KEYWORD_ACCOUNT_DAILY_LIMIT = 2
DEFAULT_GROUP_IP_DAILY_CLICK_LIMIT = 50
DEFAULT_MAX_ACTIONS_PER_HOUR = 10
DEFAULT_PER_ACCOUNT_COOLDOWN_HOURS = 4
DEFAULT_DWELL_SECONDS_MIN = 10
DEFAULT_DWELL_SECONDS_MAX = 30


@dataclass(frozen=True)
class DeboostPacingWindow:
    local_date: date
    hour_start: datetime


@dataclass
class DeboostPacingStats:
    tenant_timezone: str = ""
    local_date: str = ""
    last_limit_reason: str = ""
    task_daily_action_count: int = 0
    task_daily_remaining: int = 0
    per_account_daily_click_limit_reached: int = 0
    per_keyword_account_daily_limit_reached: int = 0
    group_ip_daily_click_limit_reached: int = 0
    task_daily_limit_reached: int = 0
    task_hourly_limit_reached: int = 0
    per_account_cooldown_active: int = 0
    blocked_accounts: set[int] = field(default_factory=set)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "tenant_timezone": self.tenant_timezone,
            "local_date": self.local_date,
            "last_limit_reason": self.last_limit_reason,
            "task_daily_action_count": self.task_daily_action_count,
            "task_daily_remaining": self.task_daily_remaining,
            "per_account_daily_click_limit_reached": self.per_account_daily_click_limit_reached,
            "per_keyword_account_daily_limit_reached": self.per_keyword_account_daily_limit_reached,
            "group_ip_daily_click_limit_reached": self.group_ip_daily_click_limit_reached,
            "task_daily_limit_reached": self.task_daily_limit_reached,
            "task_hourly_limit_reached": self.task_hourly_limit_reached,
            "per_account_cooldown_active": self.per_account_cooldown_active,
        }


def runtime_search_rank_deboost_config(task: Task) -> dict:
    """合并 type_config 与 pacing_config，返回降权任务运行时配置。"""
    type_config = dict(task.type_config or {})
    pacing_config = {key: value for key, value in dict(task.pacing_config or {}).items() if value is not None}
    return {**type_config, **pacing_config}


def deboost_pacing_window(task: Task, now_value: datetime) -> DeboostPacingWindow:
    timezone = ZoneInfo(task.timezone or "Asia/Shanghai")
    source_now = now_value if now_value.tzinfo else now_value.replace(tzinfo=DEFAULT_SOURCE_TIMEZONE)
    local_now = source_now.astimezone(timezone)
    return DeboostPacingWindow(
        local_date=local_now.date(),
        hour_start=local_now.replace(minute=0, second=0, microsecond=0),
    )


def account_click_allowed(
    session: Session,
    task: Task,
    account_id: int,
    keyword_hash: str,
    account_pool_id: int,
    window: DeboostPacingWindow,
    stats: DeboostPacingStats,
) -> bool:
    """单账号 + 关键词 + 分组 IP + 任务日/小时级 + 账号冷却综合校验。"""
    lock_rank_deboost_quota_scope(session, task)
    config = runtime_search_rank_deboost_config(task)
    task_daily_count = _task_daily_clicks(session, task, window.local_date)
    daily_limit = int(config.get("max_actions_per_day") or 0)
    stats.task_daily_action_count = task_daily_count
    if daily_limit > 0:
        stats.task_daily_remaining = max(0, daily_limit - task_daily_count)
        if stats.task_daily_remaining <= 0:
            stats.task_daily_limit_reached += 1
            stats.last_limit_reason = "task_daily_limit_reached"
            return False
    cooldown_hours = config.get("per_account_cooldown_hours")
    cooldown_hours = int(cooldown_hours) if cooldown_hours is not None else DEFAULT_PER_ACCOUNT_COOLDOWN_HOURS
    if _account_cooldown_active(session, task, account_id, cooldown_hours):
        return _block(stats, account_id, "per_account_cooldown_active")
    if _account_daily_clicks(session, task, account_id, window.local_date) >= int(config.get("per_account_daily_click_limit") or DEFAULT_PER_ACCOUNT_DAILY_CLICK_LIMIT):
        return _block(stats, account_id, "per_account_daily_click_limit_reached")
    if _keyword_account_daily_clicks(session, task, account_id, keyword_hash, window.local_date) >= int(config.get("per_keyword_account_daily_limit") or DEFAULT_PER_KEYWORD_ACCOUNT_DAILY_LIMIT):
        return _block(stats, account_id, "per_keyword_account_daily_limit_reached")
    if _group_ip_daily_clicks(session, task, account_pool_id, window.local_date) >= int(config.get("group_ip_daily_click_limit") or DEFAULT_GROUP_IP_DAILY_CLICK_LIMIT):
        return _block(stats, account_id, "group_ip_daily_click_limit_reached")
    if _task_hourly_actions(session, task, window.hour_start) >= int(config.get("max_actions_per_hour") or DEFAULT_MAX_ACTIONS_PER_HOUR):
        stats.task_hourly_limit_reached += 1
        stats.last_limit_reason = "task_hourly_limit_reached"
        return False
    return True


def _block(stats: DeboostPacingStats, account_id: int, reason: str) -> bool:
    stats.blocked_accounts.add(account_id)
    setattr(stats, reason, int(getattr(stats, reason)) + 1)
    stats.last_limit_reason = reason
    return False


def _account_daily_clicks(session: Session, task: Task, account_id: int, local_date: date) -> int:
    reservation_count = _reservation_count(
        session,
        task,
        local_date,
        SearchRankDeboostClickReservation.account_id == account_id,
    )
    return reservation_count + _legacy_stat_clicks(
        session,
        task,
        local_date,
        SearchRankDeboostActionStat.account_id == account_id,
    )


def _keyword_account_daily_clicks(session: Session, task: Task, account_id: int, keyword_hash: str, local_date: date) -> int:
    if not keyword_hash:
        return 0
    reservation_count = _reservation_count(
        session,
        task,
        local_date,
        SearchRankDeboostClickReservation.account_id == account_id,
        SearchRankDeboostClickReservation.keyword_hash == keyword_hash,
    )
    return reservation_count + _legacy_stat_clicks(
        session,
        task,
        local_date,
        SearchRankDeboostActionStat.account_id == account_id,
        SearchRankDeboostActionStat.keyword_hash == keyword_hash,
    )


def _group_ip_daily_clicks(session: Session, task: Task, account_pool_id: int, local_date: date) -> int:
    reservation_count = _reservation_count(
        session,
        task,
        local_date,
        SearchRankDeboostClickReservation.account_pool_id == account_pool_id,
    )
    return reservation_count + _legacy_stat_clicks(
        session,
        task,
        local_date,
        SearchRankDeboostActionStat.account_pool_id == account_pool_id,
    )


def _task_daily_clicks(session: Session, task: Task, local_date: date) -> int:
    reservation_count = _reservation_count(
        session,
        task,
        local_date,
        SearchRankDeboostClickReservation.task_id == task.id,
    )
    return reservation_count + _legacy_stat_clicks(
        session,
        task,
        local_date,
        SearchRankDeboostActionStat.task_id == task.id,
    )


def planned_action_at(task: Task, candidate_key: str, now_value: datetime) -> datetime | None:
    config = runtime_search_rank_deboost_config(task)
    local_now = _task_local_datetime(task, now_value)
    daily_candidate = _delay_within_local_day(
        task.id,
        candidate_key,
        local_now,
        config.get("daily_jitter_percent"),
    )
    scheduled_at = _source_naive(
        _delay_within_local_hour(task.id, candidate_key, daily_candidate, config.get("hourly_jitter_percent"))
    )
    scheduled_end = _source_naive(task.scheduled_end) if task.scheduled_end else None
    if scheduled_end is not None and scheduled_end <= _source_naive(now_value):
        return None
    return min(scheduled_at, scheduled_end - timedelta(seconds=1)) if scheduled_end else scheduled_at


def _task_local_datetime(task: Task, value: datetime) -> datetime:
    source = value if value.tzinfo else value.replace(tzinfo=DEFAULT_SOURCE_TIMEZONE)
    return source.astimezone(ZoneInfo(task.timezone or "Asia/Shanghai"))


def _delay_within_local_day(task_id: str, key: str, value: datetime, percent: object) -> datetime:
    day_end = value.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return _delayed(value, day_end, percent, _jitter_ratio(task_id, f"daily:{value.date().isoformat()}", key))


def _delay_within_local_hour(task_id: str, key: str, value: datetime, percent: object) -> datetime:
    hour_end = value.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
    return _delayed(value, hour_end, percent, _jitter_ratio(task_id, f"hourly:{value.hour}", key))


def _delayed(value: datetime, end: datetime, percent: object, ratio: float) -> datetime:
    seconds = max(0, int((end - value).total_seconds()) - 1)
    offset = int(seconds * max(0, min(100, int(percent or 0))) / 100 * ratio)
    return value + timedelta(seconds=offset)


def _jitter_ratio(task_id: str, scope: str, candidate_key: str) -> float:
    digest = hashlib.sha256(f"{task_id}:{scope}:{candidate_key}".encode("utf-8")).hexdigest()
    return int(digest[:16], 16) / float(0xFFFFFFFFFFFFFFFF)


def _reservation_count(session: Session, task: Task, local_date: date, *conditions) -> int:
    return int(
        session.scalar(
            select(func.coalesce(func.sum(SearchRankDeboostClickReservation.reserved_count), 0))
            .select_from(SearchRankDeboostClickReservation)
            .outerjoin(Action, Action.id == SearchRankDeboostClickReservation.action_id)
            .where(
                SearchRankDeboostClickReservation.tenant_id == task.tenant_id,
                SearchRankDeboostClickReservation.local_date == local_date,
                or_(
                    SearchRankDeboostClickReservation.status.in_(NON_EXPIRING_RESERVATION_STATUSES),
                    and_(
                        SearchRankDeboostClickReservation.status == "reserved",
                        or_(
                            SearchRankDeboostClickReservation.expires_at > _now(),
                            Action.id.is_(None),
                            Action.status != "pending",
                        ),
                    ),
                ),
                *conditions,
            )
        )
        or 0
    )


def _legacy_stat_clicks(session: Session, task: Task, local_date: date, *conditions) -> int:
    start_at, end_at = _local_day_bounds(task.timezone, local_date)
    reserved_actions = select(SearchRankDeboostClickReservation.action_id).where(
        SearchRankDeboostClickReservation.tenant_id == task.tenant_id,
    )
    return int(
        session.scalar(
            select(func.count(SearchRankDeboostActionStat.id)).where(
                SearchRankDeboostActionStat.tenant_id == task.tenant_id,
                SearchRankDeboostActionStat.captured_at >= start_at,
                SearchRankDeboostActionStat.captured_at < end_at,
                SearchRankDeboostActionStat.skip_reason == "",
                SearchRankDeboostActionStat.action_id.not_in(reserved_actions),
                *conditions,
            )
        )
        or 0
    )


def _task_hourly_actions(session: Session, task: Task, hour_start: datetime) -> int:
    start_at, end_at = _hour_bounds(task.timezone, hour_start)
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.task_id == task.id,
                Action.action_type == "search_rank_deboost",
                Action.status.in_(REAL_ACTION_STATUSES),
                func.coalesce(Action.executed_at, Action.scheduled_at) >= start_at,
                func.coalesce(Action.executed_at, Action.scheduled_at) < end_at,
            )
        )
        or 0
    )


def _account_cooldown_active(session: Session, task: Task, account_id: int, cooldown_hours: int) -> bool:
    if cooldown_hours <= 0:
        return False
    last_at = session.scalar(
        select(func.max(func.coalesce(Action.executed_at, Action.scheduled_at))).where(
            Action.tenant_id == task.tenant_id,
            Action.action_type == "search_rank_deboost",
            Action.account_id == account_id,
            Action.status.in_(REAL_ACTION_STATUSES),
        )
    )
    return bool(last_at and last_at + timedelta(hours=cooldown_hours) > _now())


def lock_rank_deboost_quota_scope(session: Session, task: Task) -> None:
    """串行化同租户 rank planner 的计数和预留，避免跨任务超额。"""
    session.execute(
        select(Tenant.id).where(Tenant.id == task.tenant_id).with_for_update()
    ).scalar_one()


def _local_day_bounds(timezone_name: str, local_date: date) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    start = datetime(local_date.year, local_date.month, local_date.day, tzinfo=timezone)
    end = start + timedelta(days=1)
    return _source_naive(start), _source_naive(end)


def _hour_bounds(timezone_name: str, hour_start: datetime) -> tuple[datetime, datetime]:
    timezone = ZoneInfo(timezone_name or "Asia/Shanghai")
    start = hour_start.replace(tzinfo=timezone) if hour_start.tzinfo is None else hour_start
    end = start + timedelta(hours=1)
    return _source_naive(start), _source_naive(end)


def _source_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(DEFAULT_SOURCE_TIMEZONE).replace(tzinfo=None)


__all__ = [
    "DEFAULT_DWELL_SECONDS_MAX",
    "DEFAULT_DWELL_SECONDS_MIN",
    "DEFAULT_GROUP_IP_DAILY_CLICK_LIMIT",
    "DEFAULT_MAX_ACTIONS_PER_HOUR",
    "DEFAULT_PER_ACCOUNT_COOLDOWN_HOURS",
    "DEFAULT_PER_ACCOUNT_DAILY_CLICK_LIMIT",
    "DEFAULT_PER_KEYWORD_ACCOUNT_DAILY_LIMIT",
    "DeboostPacingStats",
    "DeboostPacingWindow",
    "account_click_allowed",
    "deboost_pacing_window",
    "lock_rank_deboost_quota_scope",
    "planned_action_at",
    "runtime_search_rank_deboost_config",
]
