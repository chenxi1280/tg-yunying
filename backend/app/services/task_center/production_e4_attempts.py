"""Execution receipts for diagnostics; these are not typed business facts."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from app.models import Action, ExecutionAttempt, Task

SAMPLE_LIMIT = 8
BUSINESS_ACTION_TYPES = {
    "group_ai_chat": "send_message",
    "search_click": "search_join",
    "channel_view": "view_message",
}


def iso(value):
    return value.isoformat() if value else None


def _attempt_snapshot(session, task: Task, since: datetime) -> dict[str, Any]:
    action_type = BUSINESS_ACTION_TYPES.get(task.type)
    observed_at = func.coalesce(ExecutionAttempt.after_call_at, ExecutionAttempt.created_at)
    base = (
        select(ExecutionAttempt)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            observed_at >= since,
        )
    )
    status_rows = session.execute(
        select(ExecutionAttempt.status, func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            observed_at >= since,
        )
        .group_by(ExecutionAttempt.status)
    )
    remote_success = session.scalar(
        select(func.count(ExecutionAttempt.id))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.task_id == task.id,
            Action.action_type == action_type,
            observed_at >= since,
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id != "",
        )
    )
    samples = list(
        session.scalars(
            base
            .order_by(ExecutionAttempt.created_at.desc())
            .limit(SAMPLE_LIMIT)
        )
    )
    status_counts = {str(status): int(count) for status, count in status_rows}
    return {
        "post_release_count": sum(status_counts.values()),
        "post_release_status_counts": status_counts,
        "post_release_remote_success_count": int(remote_success or 0),
        "samples": [_attempt_row(row) for row in samples],
    }


def _attempt_row(attempt: ExecutionAttempt) -> dict[str, Any]:
    return {
        "id": attempt.id,
        "action_id": attempt.action_id,
        "status": attempt.status,
        "account_id": attempt.account_id,
        "remote_message_id": attempt.remote_message_id,
        "failure_type": attempt.failure_type,
        "failure_detail": str(attempt.failure_detail or "")[:240],
        "gateway_call_started_at": iso(attempt.gateway_call_started_at),
        "after_call_at": iso(attempt.after_call_at),
    }


