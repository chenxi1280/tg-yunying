from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TaskGroupBotAdmission


def persist_unique_observation(
    session: Session,
    row: TaskGroupBotAdmission,
) -> tuple[TaskGroupBotAdmission, bool]:
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
    except IntegrityError:
        existing = session.scalar(select(TaskGroupBotAdmission).where(
            TaskGroupBotAdmission.task_id == row.task_id,
            TaskGroupBotAdmission.account_id == row.account_id,
            TaskGroupBotAdmission.target_group_id == row.target_group_id,
        ))
        if existing is None:
            raise
        return existing, False
    return row, True


__all__ = ["persist_unique_observation"]
