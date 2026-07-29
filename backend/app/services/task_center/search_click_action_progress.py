from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, Task

from .search_join_facts import (
    has_confirmed_membership_fact,
    search_join_fact_in_window,
    search_join_held_in_window,
)


def held_action_count(
    session: Session,
    task: Task,
    action_type: str,
    statuses: tuple[str, ...],
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> int:
    if task.type != "search_join_group":
        return _action_count(
            session, task, action_type, statuses,
            start_at=start_at, end_at=end_at,
        )
    actions = session.scalars(select(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status.in_((*statuses, "success")),
    ))
    return sum(
        search_join_held_in_window(
            action, start_at, end_at, "membership", statuses=statuses
        )
        for action in actions
    )


def confirmed_action_count(
    session: Session,
    task: Task,
    action_type: str,
    *,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> int:
    if task.type == "search_join_group":
        actions = session.scalars(select(Action).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type == action_type,
            Action.status == "success",
        ))
        return sum(
            search_join_fact_in_window(
                action, start_at, end_at, "membership"
            )
            for action in actions
        )
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status == "success",
    ]
    _append_time_window(filters, start_at, end_at)
    actions = session.scalars(select(Action).where(*filters))
    return sum(_has_confirmed_click_fact(task.type, action.result) for action in actions)


def _action_count(
    session: Session,
    task: Task,
    action_type: str,
    statuses: tuple[str, ...],
    *,
    start_at: datetime | None,
    end_at: datetime | None,
) -> int:
    filters = [
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == action_type,
        Action.status.in_(statuses),
    ]
    _append_time_window(filters, start_at, end_at)
    return int(session.scalar(select(func.count(Action.id)).where(*filters)) or 0)


def _append_time_window(
    filters: list,
    start_at: datetime | None,
    end_at: datetime | None,
) -> None:
    if start_at is None or end_at is None:
        return
    action_at = func.coalesce(Action.executed_at, Action.scheduled_at)
    filters.extend((action_at >= start_at, action_at < end_at))


def _has_confirmed_click_fact(task_type: str, result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if task_type == "search_join_group":
        return has_confirmed_membership_fact(result)
    if task_type == "search_rank_deboost":
        return _has_confirmed_rank_deboost_click_fact(result)
    return False


def _has_confirmed_rank_deboost_click_fact(result: dict) -> bool:
    outcomes = result.get("click_outcomes")
    if (
        result.get("execution_status") != "confirmed"
        or not isinstance(outcomes, list)
        or len(outcomes) != 1
    ):
        return False
    outcome = outcomes[0]
    if not isinstance(outcome, dict) or outcome.get("status") != "confirmed":
        return False
    identity = str(
        outcome.get("competitor_username")
        or outcome.get("competitor_peer_id")
        or ""
    ).strip()
    required = ("competitor_position", "row", "col", "dwell_seconds", "effect", "joined")
    try:
        position = int(outcome.get("competitor_position") or 0)
    except (TypeError, ValueError):
        return False
    return bool(
        identity
        and position > 0
        and all(key in outcome for key in required)
        and outcome.get("effect") == "navigate_only"
        and outcome.get("joined") is False
    )


__all__ = ["confirmed_action_count", "held_action_count"]
