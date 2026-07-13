from __future__ import annotations

from sqlalchemy import func, select, true

from app.models import Action, Task


def task_action_status_counts_statement(task: Task, business_filter=None):
    action_filter = business_filter if business_filter is not None else true()
    return (
        select(Action.status, func.count())
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            action_filter,
        )
        .group_by(Action.status)
    )


__all__ = ["task_action_status_counts_statement"]
