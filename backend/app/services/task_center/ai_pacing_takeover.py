from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ExecutionAttempt,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
)

from .source_pacing import wall_datetime


BLOCKING_ACTION_STATUSES = frozenset({
    "pending",
    "claiming",
    "executing",
    "retryable_failed",
    "success",
    "unknown_after_send",
})


def release_safe_ai_pacing_owners(
    session: Session,
    task: Task,
    *,
    observed_at: datetime,
) -> int:
    if task.type != "group_ai_chat":
        return 0
    session.flush()
    rows = list(session.scalars(_safe_owner_statement(
        session,
        task,
        observed_at=observed_at,
    )))
    for row in rows:
        row.task_lifecycle_epoch = None
        if (
            row.release_not_before_at is not None
            and wall_datetime(row.release_not_before_at) <= wall_datetime(observed_at)
        ):
            row.release_not_before_at = None
    return len(rows)


def _safe_owner_statement(
    session: Session,
    task: Task,
    *,
    observed_at: datetime,
):
    action_boundary = select(Action.id).where(
        Action.primary_quantity_slot_id == TaskGroupDailyMessageSlot.id,
        or_(
            Action.status.in_(BLOCKING_ACTION_STATUSES),
            func.coalesce(
                Action.result["gateway_call_started_at"].as_string(),
                "",
            ) != "",
            func.coalesce(Action.result["remote_message_id"].as_string(), "") != "",
        ),
    ).exists()
    attempt_boundary = select(ExecutionAttempt.id).join(
        Action,
        Action.id == ExecutionAttempt.action_id,
    ).where(
        Action.primary_quantity_slot_id == TaskGroupDailyMessageSlot.id,
        or_(
            ExecutionAttempt.gateway_call_started_at.is_not(None),
            ExecutionAttempt.remote_message_id != "",
            ExecutionAttempt.status == "success",
        ),
    ).exists()
    current_ledger = select(TaskDayLedger.id).where(
        TaskDayLedger.id == TaskGroupDailyMessageSlot.task_day_ledger_id,
        TaskDayLedger.lifecycle_status == "open",
        TaskDayLedger.period_start_at <= observed_at,
        TaskDayLedger.deadline_at > observed_at,
    ).exists()
    statement = select(TaskGroupDailyMessageSlot).where(
        TaskGroupDailyMessageSlot.tenant_id == task.tenant_id,
        TaskGroupDailyMessageSlot.task_id == task.id,
        TaskGroupDailyMessageSlot.state == "open",
        TaskGroupDailyMessageSlot.pacing_due_at.is_not(None),
        TaskGroupDailyMessageSlot.task_lifecycle_epoch.is_not(None),
        current_ledger,
        ~action_boundary,
        ~attempt_boundary,
    )
    if session.get_bind().dialect.name == "sqlite":
        return statement
    return statement.with_for_update(of=TaskGroupDailyMessageSlot)


__all__ = ["release_safe_ai_pacing_owners"]
