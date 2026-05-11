from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.models import Task

from . import channel_comment, channel_like, channel_view, group_ai_chat, group_relay
from .common import reached_daily_action_limit


class TaskExecutor(Protocol):
    def build_plan(self, session: Session, task: Task) -> int: ...


EXECUTORS: dict[str, TaskExecutor] = {
    "group_ai_chat": group_ai_chat,
    "group_relay": group_relay,
    "channel_view": channel_view,
    "channel_like": channel_like,
    "channel_comment": channel_comment,
}


def build_task_plan(session: Session, task: Task) -> int:
    executor = EXECUTORS.get(task.type)
    if not executor:
        task.status = "failed"
        task.last_error = f"未知任务类型: {task.type}"
        return 0
    return executor.build_plan(session, task)


__all__ = ["EXECUTORS", "build_task_plan", "reached_daily_action_limit"]
