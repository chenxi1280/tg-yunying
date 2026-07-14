from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.models import Action, Task

from .search_join_config import runtime_search_join_config

SEARCH_JOIN_OPEN_STATUSES = ("pending", "claiming", "executing")


def search_rank_deboost_hourly_execution(session: Session, task: Task, now_value: datetime) -> dict[str, Any]:
    """按账号 × 关键词 × 自然小时桶统计降权任务执行情况。"""
    from .search_rank_deboost_pacing import runtime_search_rank_deboost_config

    config = runtime_search_rank_deboost_config(task)
    bucket_start = now_value.replace(minute=0, second=0, microsecond=0)
    bucket_end = bucket_start + timedelta(hours=1)
    success_count = _search_rank_deboost_success_count(session, task, start=bucket_start, end=bucket_end)
    future_open = _search_rank_deboost_open_count(
        session, task, now_value=now_value, bucket_end=bucket_end, overdue=False
    )
    overdue_open = _search_rank_deboost_open_count(
        session, task, now_value=now_value, bucket_end=bucket_end, overdue=True
    )
    max_actions = int(config.get("max_actions_per_hour") or 0)
    click_count = _search_rank_deboost_hourly_click_count(session, task, start=bucket_start, end=bucket_end)
    capacity = max(0, max_actions - success_count - future_open - overdue_open)
    return {
        "bucket": bucket_start.isoformat(),
        "status": _search_rank_deboost_hourly_status(max_actions, success_count, capacity),
        "goal": max_actions,
        "success_count": success_count,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": max(0, max_actions - success_count - future_open),
        "capacity": capacity,
        "max_actions_per_hour": max_actions,
        "hourly_click_count": click_count,
    }


def _search_rank_deboost_success_count(
    session: Session, task: Task, *, start: datetime, end: datetime
) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.action_type == "search_rank_deboost",
                Action.status == "success",
                Action.executed_at >= start,
                Action.executed_at < end,
            )
        )
        or 0
    )


def _search_rank_deboost_open_count(
    session: Session,
    task: Task,
    *,
    now_value: datetime,
    bucket_end: datetime,
    overdue: bool,
) -> int:
    boundary = Action.scheduled_at < now_value if overdue else Action.scheduled_at >= now_value
    upper = true() if overdue else Action.scheduled_at < bucket_end
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.action_type == "search_rank_deboost",
                Action.status.in_(("pending", "claiming", "executing")),
                boundary,
                upper,
            )
        )
        or 0
    )


def _search_rank_deboost_hourly_click_count(
    session: Session, task: Task, *, start: datetime, end: datetime
) -> int:
    from app.models.search_rank_deboost import SearchRankDeboostActionStat

    return int(
        session.scalar(
            select(func.count(SearchRankDeboostActionStat.id)).where(
                SearchRankDeboostActionStat.task_id == task.id,
                SearchRankDeboostActionStat.captured_at >= start,
                SearchRankDeboostActionStat.captured_at < end,
                SearchRankDeboostActionStat.skip_reason == "",
            )
        )
        or 0
    )


def _search_rank_deboost_hourly_status(goal: int, success_count: int, capacity: int) -> str:
    if goal <= 0:
        return "open"
    if success_count >= goal:
        return "met"
    if capacity <= 0:
        return "blocked"
    return "catching_up"


def search_join_hourly_execution(session: Session, task: Task, now_value: datetime) -> dict[str, Any]:
    config = runtime_search_join_config(task)
    bucket_start = now_value.replace(minute=0, second=0, microsecond=0)
    bucket_end = bucket_start + timedelta(hours=1)
    success_count = _search_join_success_count(session, task, start=bucket_start, end=bucket_end)
    future_open = _search_join_open_count(
        session, task, now_value=now_value, bucket_end=bucket_end, overdue=False
    )
    overdue_open = _search_join_open_count(
        session, task, now_value=now_value, bucket_end=bucket_end, overdue=True
    )
    goal = int(config.get("hourly_min_successful_joins") or 0)
    max_actions = int(config.get("max_actions_per_hour") or 0)
    deficit = max(0, goal - success_count - future_open)
    capacity = max(0, max_actions - success_count - future_open - overdue_open)
    return {
        "bucket": bucket_start.isoformat(),
        "status": _search_join_hourly_status(goal, deficit, capacity),
        "goal": goal,
        "success_count": success_count,
        "future_open_count": future_open,
        "overdue_open_count": overdue_open,
        "deficit": deficit,
        "capacity": capacity,
        "max_actions_per_hour": max_actions,
    }


def _search_join_success_count(session: Session, task: Task, *, start: datetime, end: datetime) -> int:
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.action_type == "search_join",
                Action.status == "success",
                Action.executed_at >= start,
                Action.executed_at < end,
            )
        )
        or 0
    )


def _search_join_open_count(
    session: Session,
    task: Task,
    *,
    now_value: datetime,
    bucket_end: datetime,
    overdue: bool,
) -> int:
    boundary = Action.scheduled_at < now_value if overdue else Action.scheduled_at >= now_value
    upper = true() if overdue else Action.scheduled_at < bucket_end
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.action_type == "search_join",
                Action.status.in_(SEARCH_JOIN_OPEN_STATUSES),
                boundary,
                upper,
            )
        )
        or 0
    )


def _search_join_hourly_status(goal: int, deficit: int, capacity: int) -> str:
    if goal <= 0:
        return "open"
    if deficit <= 0:
        return "met"
    if capacity <= 0:
        return "blocked"
    return "catching_up"


__all__ = ["search_join_hourly_execution", "search_rank_deboost_hourly_execution"]
