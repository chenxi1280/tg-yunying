from __future__ import annotations

from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, ExecutionAttempt, Task, TgAccount
from app.services._common import _now

from ..ai_limits import allocate_message_budget
from .common import quantity_with_jitter

COMMENT_RESERVATION_STATUSES = ("pending", "claiming", "executing", "success", "unknown_after_send")
CURRENT_HOUR_BUDGET_STATUSES = COMMENT_RESERVATION_STATUSES
TOTAL_BUDGET_STATUSES = COMMENT_RESERVATION_STATUSES
OPEN_TOTAL_BUDGET_STATUSES = ("pending", "claiming", "executing")
DEFAULT_MAX_TOTAL_COMMENTS = 80
DEFAULT_MAX_TOTAL_COMMENTS_JITTER = 0.2
MAX_TOTAL_COMMENTS_JITTER = 0.3
MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE = 4
LIFETIME_CAP_RUNTIME_STAT_KEYS = (
    "lifetime_cap_phase",
    "lifetime_cap_open_count",
    "lifetime_cap_reserved_count",
)


def message_comment_quantities(
    session: Session,
    task: Task,
    config: dict,
    messages: list[ChannelMessage],
    *,
    daily_coverage_min_total: int = 0,
    total_remaining: int | None = None,
) -> list[tuple[ChannelMessage, int]]:
    usernames = _tenant_account_usernames(session, task.tenant_id)
    deficits = [_message_comment_deficit(session, task, config, message, usernames) for message in messages]
    coverage_floor = min(max(0, int(daily_coverage_min_total or 0)), sum(deficits))
    deficits = _apply_daily_coverage_minimum(deficits, coverage_floor)
    hour_limit = _task_hour_limit(task)
    budget = _remaining_current_hour_budget(session, task, hour_limit)
    if total_remaining is not None:
        total_budget = max(0, int(total_remaining or 0))
        budget = min(budget, total_budget) if hour_limit > 0 else total_budget
    quantities = allocate_message_budget(deficits, budget) if hour_limit > 0 or total_remaining is not None else deficits
    return list(zip(messages, [min(value, MAX_COMMENT_GENERATION_BATCH_PER_MESSAGE) for value in quantities], strict=False))


def reconcile_lifetime_cap(session: Session, task: Task, config: dict | None = None) -> int:
    limit = resolved_total_comment_limit(task, config if config is not None else (task.type_config or {}))
    counts = _total_comment_action_counts(session, task)
    reserved = sum(counts.get(status, 0) for status in TOTAL_BUDGET_STATUSES)
    remaining = max(0, limit - reserved)
    if remaining > 0:
        _clear_lifetime_cap_runtime_stats(task)
        return remaining
    open_count = sum(counts.get(status, 0) for status in OPEN_TOTAL_BUDGET_STATUSES)
    if open_count > 0:
        _mark_lifetime_cap_draining(task, limit, reserved, open_count)
        return 0
    _complete_lifetime_cap(session, task, limit, counts)
    return 0


def resolved_total_comment_limit(task: Task, config: dict) -> int:
    stats = dict(task.stats or {})
    existing = int(stats.get("max_total_comments_resolved") or 0)
    if existing > 0:
        return existing
    base = max(1, int(config.get("max_total_comments") or DEFAULT_MAX_TOTAL_COMMENTS))
    resolved = quantity_with_jitter(base, _total_comment_limit_jitter(config))
    stats["max_total_comments_resolved"] = resolved
    task.stats = stats
    return resolved


def total_comment_action_count(session: Session, task: Task, *, exclude_action_id: str | None = None) -> int:
    stmt = select(func.count(Action.id)).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.task_type == "channel_comment",
        Action.action_type == "post_comment",
        Action.status.in_(TOTAL_BUDGET_STATUSES),
    )
    if exclude_action_id:
        stmt = stmt.where(Action.id != exclude_action_id)
    return int(session.scalar(stmt) or 0)


def message_comment_reservation_count(session: Session, task: Task, message: ChannelMessage) -> int:
    count = 0
    payloads = session.scalars(
        select(Action.payload).where(
            Action.task_id == task.id,
            Action.action_type == "post_comment",
            Action.status.in_(COMMENT_RESERVATION_STATUSES),
        )
    )
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        if payload.get("channel_message_id") == message.id or payload.get("message_id") == message.message_id:
            count += 1
    return count


def _total_comment_action_counts(session: Session, task: Task) -> dict[str, int]:
    rows = session.execute(
        select(Action.status, func.count(Action.id))
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
        )
        .group_by(Action.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def _mark_lifetime_cap_draining(task: Task, limit: int, reserved: int, open_count: int) -> None:
    stats = dict(task.stats or {})
    stats.update(
        {
            "lifetime_cap_phase": "draining",
            "lifetime_cap_open_count": open_count,
            "lifetime_cap_reserved_count": reserved,
            "max_total_comments_resolved": limit,
        }
    )
    task.stats = stats
    task.last_error = ""


def _complete_lifetime_cap(session: Session, task: Task, limit: int, counts: dict[str, int]) -> None:
    unknown_count = counts.get("unknown_after_send", 0)
    existing_stats = dict(task.stats or {})
    completed_at = (
        existing_stats.get("completed_at")
        if task.status == "completed" and existing_stats.get("completion_reason") == "lifetime_cap_reached"
        else _now().isoformat()
    )
    stats = _without_lifetime_cap_runtime_stats(task.stats or {})
    stats.update(
        {
            "completion_reason": "lifetime_cap_reached",
            "completion_status": "completed_with_unknown" if unknown_count else "completed",
            "max_total_comments_resolved": limit,
            "remote_success_count": _remote_comment_success_count(session, task),
            "unknown_after_send_count": unknown_count,
            "completed_at": completed_at,
        }
    )
    task.stats = stats
    task.status = "completed"
    task.next_run_at = None
    task.last_error = ""


def _remote_comment_success_count(session: Session, task: Task) -> int:
    return int(
        session.scalar(
            select(func.count(func.distinct(Action.id)))
            .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
            .where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.action_type == "post_comment",
                Action.status == "success",
                ExecutionAttempt.status == "success",
                ExecutionAttempt.remote_message_id != "",
            )
        )
        or 0
    )


def _clear_lifetime_cap_runtime_stats(task: Task) -> None:
    task.stats = _without_lifetime_cap_runtime_stats(task.stats or {})


def _without_lifetime_cap_runtime_stats(stats: dict) -> dict:
    updated = dict(stats)
    for key in LIFETIME_CAP_RUNTIME_STAT_KEYS:
        updated.pop(key, None)
    return updated


def _total_comment_limit_jitter(config: dict) -> float:
    configured = config.get("max_total_comments_jitter")
    jitter = float(DEFAULT_MAX_TOTAL_COMMENTS_JITTER if configured is None else configured)
    if jitter > MAX_TOTAL_COMMENTS_JITTER:
        raise ValueError("max_total_comments_jitter 不能超过 0.3")
    return max(0.0, jitter)


def _task_hour_limit(task: Task) -> int:
    return max(0, int((task.pacing_config or {}).get("max_actions_per_hour") or 0))


def _remaining_current_hour_budget(session: Session, task: Task, hour_limit: int) -> int:
    if hour_limit <= 0:
        return 0
    return max(0, hour_limit - _current_hour_comment_action_count(session, task))


def _current_hour_comment_action_count(session: Session, task: Task) -> int:
    hour_start = _now().replace(minute=0, second=0, microsecond=0)
    hour_end = hour_start + timedelta(hours=1)
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.task_type == "channel_comment",
                Action.action_type == "post_comment",
                Action.status.in_(CURRENT_HOUR_BUDGET_STATUSES),
                or_(
                    (Action.scheduled_at >= hour_start) & (Action.scheduled_at < hour_end),
                    (Action.executed_at >= hour_start) & (Action.executed_at < hour_end),
                ),
            )
        )
        or 0
    )


def _apply_daily_coverage_minimum(deficits: list[int], minimum: int) -> list[int]:
    adjusted = [max(0, int(deficit or 0)) for deficit in deficits]
    remaining = max(0, int(minimum or 0) - sum(adjusted))
    index = 0
    while adjusted and remaining > 0:
        adjusted[index % len(adjusted)] += 1
        remaining -= 1
        index += 1
    return adjusted


def _message_comment_deficit(
    session: Session,
    task: Task,
    config: dict,
    message: ChannelMessage,
    managed_usernames: set[str],
) -> int:
    desired = quantity_with_jitter(
        int(config.get("target_comments_per_message") or 1),
        float(config.get("comment_count_jitter") or 0),
    )
    used_count = max(
        message_comment_reservation_count(session, task, message),
        _collected_managed_comment_count(session, task, message, managed_usernames),
    )
    return max(0, desired - used_count)


def _collected_managed_comment_count(
    session: Session,
    task: Task,
    message: ChannelMessage,
    managed_usernames: set[str],
) -> int:
    if not managed_usernames:
        return 0
    return int(
        session.scalar(
            select(func.count(ChannelMessageComment.id)).where(
                ChannelMessageComment.tenant_id == task.tenant_id,
                ChannelMessageComment.channel_target_id == message.channel_target_id,
                ChannelMessageComment.channel_message_id == message.id,
                func.lower(ChannelMessageComment.author_username).in_(managed_usernames),
            )
        )
        or 0
    )


def _tenant_account_usernames(session: Session, tenant_id: int) -> set[str]:
    rows = session.scalars(
        select(TgAccount.username).where(
            TgAccount.tenant_id == tenant_id,
            TgAccount.deleted_at.is_(None),
            TgAccount.username.is_not(None),
        )
    )
    return {str(value or "").strip().lstrip("@").lower() for value in rows if str(value or "").strip()}


__all__ = [
    "message_comment_reservation_count",
    "message_comment_quantities",
    "reconcile_lifetime_cap",
    "resolved_total_comment_limit",
    "total_comment_action_count",
]
