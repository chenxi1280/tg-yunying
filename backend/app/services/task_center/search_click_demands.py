from __future__ import annotations

from collections import Counter
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    SearchClickFulfillmentObligation,
    SearchClickTaskFairnessState,
    Task,
    TaskDayLedger,
)

from .search_click_assignment_solver import SearchClickDemand
from .search_click_dispatch_allocation import SearchClickFulfillmentUnit


def search_click_demands(
    session: Session,
    units: tuple[SearchClickFulfillmentUnit, ...],
) -> tuple[SearchClickDemand, ...]:
    obligation_ids = [unit.obligation_id for unit in units]
    rows = session.execute(
        select(SearchClickFulfillmentObligation, TaskDayLedger, Task)
        .join(
            TaskDayLedger,
            TaskDayLedger.id
            == SearchClickFulfillmentObligation.task_day_ledger_id,
        )
        .join(Task, Task.id == TaskDayLedger.task_id)
        .where(SearchClickFulfillmentObligation.id.in_(obligation_ids))
    ).all()
    remaining = _remaining_by_task(session, {task.id for _, _, task in rows})
    result = []
    for obligation, ledger, task in rows:
        state = _fairness_state(session, task.id)
        result.append(SearchClickDemand(
            obligation_id=obligation.id,
            task_id=task.id,
            task_remaining_count=remaining.get(task.id, 1),
            task_deadline_at=_task_deadline(task, ledger),
            task_last_opportunity_at=state.last_click_opportunity_at,
            persistent_task_cursor=state.persistent_task_cursor,
            task_cursor_version=state.cursor_version,
        ))
    return tuple(result)


def record_task_opportunities(
    session: Session,
    task_ids: list[str],
    *,
    now_value: datetime,
) -> None:
    for task_id, count in sorted(Counter(task_ids).items()):
        state = _fairness_state(session, task_id)
        state.persistent_task_cursor += count
        state.cursor_version += 1
        state.last_click_opportunity_at = now_value


def _remaining_by_task(
    session: Session,
    task_ids: set[str],
) -> dict[str, int]:
    if not task_ids:
        return {}
    rows = session.execute(
        select(TaskDayLedger.task_id, func.count(SearchClickFulfillmentObligation.id))
        .join(
            SearchClickFulfillmentObligation,
            SearchClickFulfillmentObligation.task_day_ledger_id
            == TaskDayLedger.id,
        )
        .where(
            TaskDayLedger.task_id.in_(task_ids),
            TaskDayLedger.lifecycle_status == "open",
            SearchClickFulfillmentObligation.status.in_(
                ("open", "assigned", "action_bound", "claiming", "executing")
            ),
        )
        .group_by(TaskDayLedger.task_id)
    ).all()
    return {task_id: int(count) for task_id, count in rows}


def _fairness_state(
    session: Session,
    task_id: str,
) -> SearchClickTaskFairnessState:
    state = session.get(SearchClickTaskFairnessState, task_id)
    if state is None:
        state = SearchClickTaskFairnessState(task_id=task_id)
        session.add(state)
        session.flush()
    return state


def _task_deadline(task: Task, ledger: TaskDayLedger) -> datetime:
    values = [_naive(ledger.deadline_at)]
    if task.scheduled_end is not None:
        values.append(_naive(task.scheduled_end))
    return min(values)


def _naive(value: datetime) -> datetime:
    return value.replace(tzinfo=None) if value.tzinfo else value


__all__ = ["record_task_opportunities", "search_click_demands"]
