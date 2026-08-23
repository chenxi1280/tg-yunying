from __future__ import annotations

from typing import Protocol

from sqlalchemy.orm import Session

from app.models import Task

from ..fulfillment_activation import CURRENT_CONTRACT_VERSION

from . import channel_comment, channel_like, channel_view, group_ai_chat, group_membership_admission, group_relay, search_click, search_join_group, search_rank_deboost, target_admission_retry


class TaskExecutor(Protocol):
    def build_plan(self, session: Session, task: Task) -> int: ...


EXECUTORS: dict[str, TaskExecutor] = {
    "group_ai_chat": group_ai_chat,
    "group_relay": group_relay,
    "group_membership_admission": group_membership_admission,
    "target_admission_retry": target_admission_retry,
    "channel_view": channel_view,
    "channel_like": channel_like,
    "channel_comment": channel_comment,
    "search_click": search_click,
    "search_join_group": search_join_group,
    "search_rank_deboost": search_rank_deboost,
}


def build_task_plan(session: Session, task: Task) -> int:
    executor = EXECUTORS.get(task.type)
    if not executor:
        task.status = "failed"
        task.last_error = f"未知任务类型: {task.type}"
        return 0
    if (
        task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
        and task.type in {"channel_like", "channel_view"}
    ):
        from ..channel_fulfillment import cancel_superseded_channel_actions

        cancel_superseded_channel_actions(session, task)
    return executor.build_plan(session, task)


def prepare_open_actions_for_planning(session: Session, task: Task) -> int:
    executor = EXECUTORS.get(task.type)
    prepare = getattr(executor, "prepare_open_actions_for_planning", None)
    if not prepare:
        return 0
    return int(prepare(session, task) or 0)


def requires_planning_with_open_actions(session: Session, task: Task) -> bool:
    executor = EXECUTORS.get(task.type)
    requires = getattr(executor, "requires_planning_with_open_actions", None)
    return bool(requires and requires(session, task))


__all__ = [
    "EXECUTORS",
    "build_task_plan",
    "prepare_open_actions_for_planning",
    "requires_planning_with_open_actions",
]
