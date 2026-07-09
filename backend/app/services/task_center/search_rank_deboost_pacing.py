from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.models.search_rank_deboost import SearchRankDeboostActionStat
from app.services._common import _now

REAL_ACTION_STATUSES = {"pending", "claiming", "executing", "success", "failed"}
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
    per_account_daily_click_limit_reached: int = 0
    per_keyword_account_daily_limit_reached: int = 0
    group_ip_daily_click_limit_reached: int = 0
    task_hourly_limit_reached: int = 0
    per_account_cooldown_active: int = 0
    blocked_accounts: set[int] = field(default_factory=set)

    def as_dict(self) -> dict[str, int | str]:
        return {
            "tenant_timezone": self.tenant_timezone,
            "local_date": self.local_date,
            "last_limit_reason": self.last_limit_reason,
            "per_account_daily_click_limit_reached": self.per_account_daily_click_limit_reached,
            "per_keyword_account_daily_limit_reached": self.per_keyword_account_daily_limit_reached,
            "group_ip_daily_click_limit_reached": self.group_ip_daily_click_limit_reached,
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
    """单账号 + 关键词 + 分组 IP + 任务小时级 + 账号冷却 综合校验。"""
    config = runtime_search_rank_deboost_config(task)
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
    start_at, end_at = _local_day_bounds(task.timezone, local_date)
    return int(
        session.scalar(
            select(func.count(SearchRankDeboostActionStat.id)).where(
                SearchRankDeboostActionStat.tenant_id == task.tenant_id,
                SearchRankDeboostActionStat.task_id == task.id,
                SearchRankDeboostActionStat.account_id == account_id,
                SearchRankDeboostActionStat.captured_at >= start_at,
                SearchRankDeboostActionStat.captured_at < end_at,
                SearchRankDeboostActionStat.skip_reason == "",
            )
        )
        or 0
    )


def _keyword_account_daily_clicks(session: Session, task: Task, account_id: int, keyword_hash: str, local_date: date) -> int:
    if not keyword_hash:
        return 0
    start_at, end_at = _local_day_bounds(task.timezone, local_date)
    return int(
        session.scalar(
            select(func.count(SearchRankDeboostActionStat.id)).where(
                SearchRankDeboostActionStat.tenant_id == task.tenant_id,
                SearchRankDeboostActionStat.task_id == task.id,
                SearchRankDeboostActionStat.account_id == account_id,
                SearchRankDeboostActionStat.keyword_hash == keyword_hash,
                SearchRankDeboostActionStat.captured_at >= start_at,
                SearchRankDeboostActionStat.captured_at < end_at,
                SearchRankDeboostActionStat.skip_reason == "",
            )
        )
        or 0
    )


def _group_ip_daily_clicks(session: Session, task: Task, account_pool_id: int, local_date: date) -> int:
    start_at, end_at = _local_day_bounds(task.timezone, local_date)
    return int(
        session.scalar(
            select(func.count(SearchRankDeboostActionStat.id)).where(
                SearchRankDeboostActionStat.tenant_id == task.tenant_id,
                SearchRankDeboostActionStat.task_id == task.id,
                SearchRankDeboostActionStat.account_pool_id == account_pool_id,
                SearchRankDeboostActionStat.captured_at >= start_at,
                SearchRankDeboostActionStat.captured_at < end_at,
                SearchRankDeboostActionStat.skip_reason == "",
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
            Action.task_id == task.id,
            Action.action_type == "search_rank_deboost",
            Action.account_id == account_id,
            Action.status.in_(REAL_ACTION_STATUSES),
        )
    )
    return bool(last_at and last_at + timedelta(hours=cooldown_hours) > _now())


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
    "runtime_search_rank_deboost_config",
]
