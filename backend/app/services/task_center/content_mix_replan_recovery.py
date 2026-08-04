from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ContentMixCycle,
    ContentMixCycleSlot,
    ExecutionAttempt,
    Task,
    TaskGroupDailyMessageSlot,
)

from .daily_coverage import release_coverage_reservation
from .daily_coverage_planning import MAX_DAILY_COVERAGE_PLAN_BATCH


REPLAN_TERMINAL_ACTION_STATUSES = frozenset({
    "failed",
    "retryable_failed",
    "skipped",
})
TERMINAL_PRE_GATEWAY_ERRORS = frozenset({
    "ai_generation_deadline_budget_exhausted",
})


def recover_stale_pending_content_mix_slots(
    session: Session,
    task: Task,
) -> int:
    rows = _stale_pending_rows(session, task)
    recovered = 0
    for cycle_slot, action, quantity_slot in rows:
        if not _take_over_slot(session, cycle_slot, action):
            continue
        _release_coverage(session, action)
        quantity_slot.state = "open"
        recovered += 1
    return recovered


def _stale_pending_rows(
    session: Session,
    task: Task,
) -> list[tuple[ContentMixCycleSlot, Action, TaskGroupDailyMessageSlot]]:
    gateway_started = select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == Action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).exists()
    statement = (
        select(ContentMixCycleSlot, Action, TaskGroupDailyMessageSlot)
        .join(ContentMixCycle, ContentMixCycle.id == ContentMixCycleSlot.cycle_id)
        .join(Action, Action.id == ContentMixCycleSlot.current_action_id)
        .join(
            TaskGroupDailyMessageSlot,
            TaskGroupDailyMessageSlot.id
            == ContentMixCycleSlot.primary_quantity_slot_id,
        )
        .where(
            ContentMixCycle.task_id == task.id,
            ContentMixCycleSlot.slot_state == "pending",
            Action.status.in_(REPLAN_TERMINAL_ACTION_STATUSES),
            ~gateway_started,
        )
        .order_by(ContentMixCycle.cycle_seq, ContentMixCycleSlot.slot_index)
        .limit(MAX_DAILY_COVERAGE_PLAN_BATCH)
    )
    return list(session.execute(statement))


def _take_over_slot(
    session: Session,
    cycle_slot: ContentMixCycleSlot,
    action: Action,
) -> bool:
    result = dict(action.result or {})
    terminal_reason = str(result.get("error_code") or action.status)
    if terminal_reason in TERMINAL_PRE_GATEWAY_ERRORS:
        return False
    changed = session.execute(
        update(ContentMixCycleSlot)
        .where(
            ContentMixCycleSlot.id == cycle_slot.id,
            ContentMixCycleSlot.slot_state == "pending",
            ContentMixCycleSlot.current_action_id == action.id,
        )
        .values(
            slot_state="replan_required",
            current_action_id=None,
            terminal_reason=terminal_reason,
        )
    ).rowcount
    return changed == 1


def _release_coverage(session: Session, action: Action) -> None:
    payload = dict(action.payload or {})
    coverage_id = str(payload.get("coverage_ledger_id") or "")
    if not coverage_id:
        return
    result = dict(action.result or {})
    release_coverage_reservation(
        session,
        coverage_id,
        action.id,
        blocker_code=str(result.get("error_code") or action.status),
        blocker_detail=str(result.get("error_message") or ""),
    )


__all__ = ["recover_stale_pending_content_mix_slots"]
