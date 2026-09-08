"""Materialize the full membership stage in the existing task start transaction."""
from sqlalchemy.orm import Session

from app.models import OperationTarget, Task

from .channel_membership import gate_channel_membership
from .channel_membership_execution import CHANNEL_TASK_TYPES, START_EPOCH_KEY
from .channel_membership_resume import resume_uncalled_channel_memberships


def prepare_channel_membership_on_start(session: Session, task: Task) -> None:
    if task.type not in CHANNEL_TASK_TYPES or task.status not in {"running", "pending"}:
        return
    config = task.type_config or {}
    target = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if target is None or target.tenant_id != task.tenant_id or target.target_type != "channel":
        raise ValueError("channel_membership_start_target_invalid")
    resume_uncalled_channel_memberships(session, task, target)
    require_send = task.type == "channel_comment" and not config.get("channel_comment_grounding_v1_enabled")
    gate_channel_membership(session, task, target, require_send=require_send, require_all_eligible=True)
    task.stats = {**dict(task.stats or {}), START_EPOCH_KEY: int(task.task_lifecycle_epoch or 1)}
