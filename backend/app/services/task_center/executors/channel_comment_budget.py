from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelMessage,
    ChannelMessageComment,
    CommentFulfillmentObligation,
    Task,
    TgAccount,
)
from app.services._common import _now

from ..ai_limits import allocate_message_budget
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import source_rolling_pacing_due
from ..pacing_quantity import deterministic_quantity_with_jitter
from ..source_pacing import rolling_source_window
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


@dataclass(frozen=True)
class MessageCommentPlanState:
    reservation_count: int
    next_slot_index: int
    managed_collected_count: int


def message_comment_quantities(
    session: Session,
    task: Task,
    config: dict,
    messages: list[ChannelMessage],
    *,
    daily_coverage_min_total: int = 0,
    total_remaining: int | None = None,
    message_states: dict[int, MessageCommentPlanState] | None = None,
) -> list[tuple[ChannelMessage, int]]:
    states = message_states if message_states is not None else load_message_comment_plan_states(session, task, messages)
    now_value = _now()
    deficits = [
        _message_comment_deficit(
            config,
            states[message.id],
            task=task,
            message=message,
            now=now_value,
        )
        for message in messages
    ]
    coverage_floor = min(max(0, int(daily_coverage_min_total or 0)), sum(deficits))
    deficits = _apply_daily_coverage_minimum(deficits, coverage_floor)
    hour_limit = _task_hour_limit(task)
    budget = _remaining_current_hour_budget(session, task, hour_limit)
    daily_cap = int(config.get("daily_comment_cap") or (task.type_config or {}).get("daily_comment_cap") or 0)
    has_daily_cap = daily_cap > 0
    if has_daily_cap:
        day_budget = _remaining_current_day_budget(session, task, daily_cap)
        budget = min(budget, day_budget) if hour_limit > 0 else day_budget
        if day_budget <= 0:
            stats = dict(task.stats or {})
            stats["daily_cap_reached"] = True
            stats["daily_cap_limit"] = daily_cap
            task.stats = stats
    if total_remaining is not None:
        total_budget = max(0, int(total_remaining or 0))
        budget = min(budget, total_budget) if (hour_limit > 0 or has_daily_cap) else total_budget
    quantities = allocate_message_budget(deficits, budget) if (hour_limit > 0 or has_daily_cap or total_remaining is not None) else deficits
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


def load_message_comment_plan_states(
    session: Session,
    task: Task,
    messages: list[ChannelMessage],
) -> dict[int, MessageCommentPlanState]:
    if not messages:
        return {}
    obligation_states = _obligation_message_plan_states(session, task, messages)
    managed_counts = _managed_collected_comment_counts(session, task, messages)
    states: dict[int, MessageCommentPlanState] = {}
    for message in messages:
        reserved, historical_count, max_slot_index = obligation_states.get(
            message.id,
            (0, 0, -1),
        )
        states[message.id] = MessageCommentPlanState(
            reservation_count=reserved,
            next_slot_index=max(historical_count, max_slot_index + 1),
            managed_collected_count=managed_counts.get(message.id, 0),
        )
    return states


def _obligation_message_plan_states(
    session: Session,
    task: Task,
    messages: list[ChannelMessage],
) -> dict[int, tuple[int, int, int]]:
    message_ids = [message.id for message in messages]
    states: dict[int, tuple[int, int, int]] = {}
    rows = session.execute(
        select(
            CommentFulfillmentObligation.channel_message_id,
            CommentFulfillmentObligation.target_ordinal,
            CommentFulfillmentObligation.status,
            Action.status,
        )
        .outerjoin(
            Action,
            Action.id == CommentFulfillmentObligation.current_action_id,
        )
        .where(
            CommentFulfillmentObligation.tenant_id == task.tenant_id,
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.channel_message_id.in_(message_ids),
        )
    )
    for message_id, ordinal, obligation_status, action_status in rows:
        reserved, historical_count, max_slot_index = states.get(
            int(message_id),
            (0, 0, -1),
        )
        states[int(message_id)] = (
            reserved + int(_obligation_reserves(obligation_status, action_status)),
            historical_count + 1,
            max(max_slot_index, int(ordinal) - 1),
        )
    return states


def _obligation_reserves(
    obligation_status: str,
    action_status: str | None,
) -> bool:
    if obligation_status in {"confirmed", "unknown"}:
        return True
    return (
        obligation_status == "pending"
        and action_status in COMMENT_RESERVATION_STATUSES
    )


def _managed_collected_comment_counts(
    session: Session,
    task: Task,
    messages: list[ChannelMessage],
) -> dict[int, int]:
    message_ids = [message.id for message in messages]
    target_ids = {message.channel_target_id for message in messages}
    normalized_account_username = func.lower(func.ltrim(func.trim(TgAccount.username), "@"))
    rows = session.execute(
        select(ChannelMessageComment.channel_message_id, func.count(func.distinct(ChannelMessageComment.id)))
        .join(
            TgAccount,
            and_(
                TgAccount.tenant_id == task.tenant_id,
                TgAccount.deleted_at.is_(None),
                TgAccount.username.is_not(None),
                func.trim(TgAccount.username) != "",
                normalized_account_username == func.lower(ChannelMessageComment.author_username),
            ),
        )
        .where(
            ChannelMessageComment.tenant_id == task.tenant_id,
            ChannelMessageComment.channel_target_id.in_(target_ids),
            ChannelMessageComment.channel_message_id.in_(message_ids),
        )
        .group_by(ChannelMessageComment.channel_message_id)
    )
    return {int(message_id): int(count) for message_id, count in rows}


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
            select(func.count(CommentFulfillmentObligation.id))
            .where(
                CommentFulfillmentObligation.tenant_id == task.tenant_id,
                CommentFulfillmentObligation.task_id == task.id,
                CommentFulfillmentObligation.status == "confirmed",
                CommentFulfillmentObligation.remote_comment_id.is_not(None),
                CommentFulfillmentObligation.remote_comment_id != "",
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


def _remaining_current_day_budget(session: Session, task: Task, daily_cap: int) -> int:
    if daily_cap <= 0:
        return 0
    return max(0, daily_cap - _current_day_comment_action_count(session, task))


def _current_day_comment_action_count(session: Session, task: Task) -> int:
    day_start = _now().replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return int(
        session.scalar(
            select(func.count(Action.id)).where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.task_type == "channel_comment",
                Action.action_type == "post_comment",
                Action.status.in_(CURRENT_HOUR_BUDGET_STATUSES),
                or_(
                    (Action.scheduled_at >= day_start) & (Action.scheduled_at < day_end),
                    (Action.executed_at >= day_start) & (Action.executed_at < day_end),
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
    config: dict,
    state: MessageCommentPlanState,
    *,
    task: Task,
    message: ChannelMessage,
    now: datetime,
) -> int:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        _period_start, deadline = rolling_source_window(task, message.created_at)
        if deadline <= now:
            return 0
    seed_id = f"comment:{task.id}:{message.id}:{task.config_revision or 1}"
    desired = deterministic_quantity_with_jitter(
        int(config.get("target_comments_per_message") or 1),
        float(config.get("comment_count_jitter") or 0),
        seed_id=seed_id,
    )
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        desired = source_rolling_pacing_due(
            desired,
            task.pacing_config or {},
            task=task,
            source_observed_at=message.created_at,
            now=now,
        )
    used_count = max(
        state.reservation_count,
        state.managed_collected_count,
    )
    return max(0, desired - used_count)


__all__ = [
    "load_message_comment_plan_states",
    "message_comment_quantities",
    "reconcile_lifetime_cap",
    "resolved_total_comment_limit",
    "total_comment_action_count",
]
