"""Allow only the started channel admission stage before a scheduled main task."""
from sqlalchemy import and_, or_

from app.models import Action, Task


CHANNEL_TASK_TYPES = frozenset({"channel_view", "channel_like", "channel_comment"})
MEMBERSHIP_ACTION_TYPES = frozenset({"ensure_target_membership", "ensure_channel_membership"})
START_EPOCH_KEY = "channel_membership_start_epoch"


def task_allows_action_execution(task, action: Action) -> bool:
    if task.status == "running":
        return True
    return _started_pending_channel(task) and _membership_matches_task(task, action)


def _started_pending_channel(task) -> bool:
    return (
        task.status == "pending"
        and task.type in CHANNEL_TASK_TYPES
        and task.deleted_at is None
        and task.retired_at is None
        and (task.stats or {}).get(START_EPOCH_KEY) == task.task_lifecycle_epoch
    )


def _membership_matches_task(task, action: Action) -> bool:
    config = task.type_config or {}
    payload = action.payload or {}
    return (
        action.task_type == task.type
        and action.tenant_id == task.tenant_id
        and action.action_type in MEMBERSHIP_ACTION_TYPES
        and action.task_lifecycle_epoch == task.task_lifecycle_epoch
        and payload.get("channel_target_id") is not None
        and payload.get("channel_target_id") == config.get("target_channel_id")
    )


def task_action_execution_condition():
    return or_(Task.status == "running", and_(
        Task.status == "pending",
        Task.type.in_(CHANNEL_TASK_TYPES),
        Action.task_type == Task.type,
        Action.tenant_id == Task.tenant_id,
        Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
        Task.deleted_at.is_(None),
        Task.retired_at.is_(None),
        Task.stats[START_EPOCH_KEY].as_integer() == Task.task_lifecycle_epoch,
        Action.task_lifecycle_epoch == Task.task_lifecycle_epoch,
        Action.payload["channel_target_id"].as_integer() == Task.type_config["target_channel_id"].as_integer(),
    ))
