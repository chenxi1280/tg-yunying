from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import or_, select, union_all
from sqlalchemy.orm import Session

from app.models import Action, Task


@dataclass(frozen=True)
class HardHourlyAction:
    id: str
    status: str
    account_id: int | None
    scheduled_at: datetime | None
    executed_at: datetime | None


def recent_actions(session: Session, task: Task, earliest: datetime) -> list[HardHourlyAction]:
    actions_by_id: dict[str, HardHourlyAction] = {}
    for row in session.execute(recent_actions_query(task, earliest)):
        action = HardHourlyAction(
            str(row.id),
            str(row.status),
            row.account_id,
            row.scheduled_at,
            row.executed_at,
        )
        actions_by_id[action.id] = action
    return list(actions_by_id.values())


def recent_actions_query(task: Task, earliest: datetime):
    columns = (Action.id, Action.status, Action.account_id, Action.scheduled_at, Action.executed_at)
    filters = (
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.task_type == "group_ai_chat",
        Action.action_type == "send_message",
    )
    return union_all(
        select(*columns).where(*filters, Action.executed_at >= earliest),
        select(*columns).where(
            *filters,
            Action.scheduled_at >= earliest,
            or_(Action.executed_at.is_(None), Action.executed_at < earliest),
        ),
    )
