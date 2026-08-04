from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import TaskGroupBotAdmission


def find_observation(
    session: Session,
    *,
    task_id: str,
    group_id: int,
    account_id: int,
) -> TaskGroupBotAdmission | None:
    return session.scalar(select(TaskGroupBotAdmission).where(
        TaskGroupBotAdmission.task_id == task_id,
        TaskGroupBotAdmission.target_group_id == group_id,
        TaskGroupBotAdmission.account_id == account_id,
    ))


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


__all__ = ["find_observation", "persist_unique_observation"]
