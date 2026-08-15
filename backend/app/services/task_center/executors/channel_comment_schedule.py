from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now


DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 300
MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 60
COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT = "comment_context_bound_next_run_at"


def materialized_reply_slots(
    task: Task,
    slots: list,
    planned_times: list[datetime],
    *,
    now_value: datetime,
) -> list[tuple[object, datetime]]:
    cutoff = now_value + timedelta(seconds=_window_seconds(task.type_config or {}))
    selected: list[tuple[object, datetime]] = []
    deferred_times: list[datetime] = []
    for slot, planned_at in zip(slots, planned_times, strict=False):
        if not slot.reply_target:
            selected.append((slot, planned_at))
            continue
        if int(slot.obligation.action_attempt_no or 0) > 0:
            selected.append((slot, now_value))
            continue
        if planned_at <= cutoff:
            selected.append((slot, planned_at))
            continue
        deferred_times.append(planned_at)
    _record_next_materialization(task, deferred_times)
    return selected


def accelerate_future_replacements(
    session: Session,
    task: Task,
    *,
    now_value: datetime,
) -> int:
    """wake 存在 replacement 的任务，但禁止把 future Action 改写为 now。

    deterministic_stratified_v1 合同：replacement 由 obligation 的新 intent
    revision 在原 pacing release 约束内重建；本函数只提前唤醒任务进入重规划，
    不做通用 future→now rewrite（scheduled_at 保持原值）。
    """
    actions = session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.action_type == "post_comment",
            Action.status == "pending",
            Action.scheduled_at > now_value,
        )
    )
    replacements = [action for action in actions if _attempt_no(action) > 1]
    if replacements:
        _wake_task(task, now_value)
    return len(replacements)


def wake_tasks_with_future_replacements(
    session: Session,
    *,
    now_value: datetime,
) -> int:
    actions = session.scalars(
        select(Action).where(
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status == "pending",
            Action.scheduled_at > now_value,
        )
    )
    task_ids = {action.task_id for action in actions if action.task_id and _attempt_no(action) > 1}
    tasks = session.scalars(
        select(Task).where(
            Task.id.in_(task_ids),
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
    ) if task_ids else []
    recovered = 0
    for task in tasks:
        _wake_task(task, now_value)
        recovered += 1
    return recovered


def prepare_open_actions_for_planning(session: Session, task: Task) -> int:
    return accelerate_future_replacements(session, task, now_value=_now())


def wake_deferred_comment_replacements(session: Session) -> int:
    return wake_tasks_with_future_replacements(session, now_value=_now())


def wake_comment_replan(task: Task, *, now_value: datetime) -> None:
    _wake_task(task, now_value)


def reply_minimum_for_mode(comment_mode: str, quantity: int, config: dict) -> int:
    if comment_mode not in {"reply", "mixed"}:
        return 0
    return min(quantity, int(config.get("reply_min_per_message") or 0))


def _window_seconds(config: dict) -> int:
    try:
        value = int(
            config.get("context_bound_schedule_window_seconds")
            or DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS
        )
    except (TypeError, ValueError):
        value = DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS
    return max(MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS, value)


def _record_next_materialization(task: Task, deferred_times: list[datetime]) -> None:
    stats = dict(task.stats or {})
    if deferred_times:
        stats[COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT] = min(deferred_times).isoformat()
    else:
        stats.pop(COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT, None)
    task.stats = stats


def _attempt_no(action: Action) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return int(payload.get("comment_action_attempt_no") or 0)


def _wake_task(task: Task, now_value: datetime) -> None:
    stats = dict(task.stats or {})
    stats.pop(COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT, None)
    task.stats = stats
    task.next_run_at = now_value


__all__ = [
    "COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT",
    "accelerate_future_replacements",
    "materialized_reply_slots",
    "prepare_open_actions_for_planning",
    "reply_minimum_for_mode",
    "wake_comment_replan",
    "wake_deferred_comment_replacements",
    "wake_tasks_with_future_replacements",
]
