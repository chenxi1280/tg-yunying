"""Keep an action's resource contract tied to its original work identity."""
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentPlanContract,
    CommentFulfillmentObligation,
    ReactionFulfillmentObligation,
    Task,
    TaskAccountGroupBindingSetRevision,
    TaskDayLedger,
    TaskGroupDailyTarget,
    ViewFulfillmentObligation,
)
from app.timezone import as_beijing

from .engagement_binding import ENGAGEMENT_TASK_TYPES, UNIFIED_ENGAGEMENT_CONTRACT_VERSION


def action_uses_unified_contract(session: Session, action: Action) -> bool:
    if action.task_type not in ENGAGEMENT_TASK_TYPES:
        return False
    task = session.get(Task, action.task_id)
    if task is None:
        raise ValueError("engagement_task_missing")
    if (task.tenant_id, task.type) != (action.tenant_id, action.task_type):
        raise ValueError("engagement_task_owner_mismatch")
    binding = _first_binding(session, action)
    if binding is None:
        return (
            int(task.task_lifecycle_epoch or 1) == int(action.task_lifecycle_epoch or 1)
            and (task.type_config or {}).get("engagement_contract_version")
            == UNIFIED_ENGAGEMENT_CONTRACT_VERSION
        )
    created_at = min(_work_creation_times(session, action))
    return created_at >= as_beijing(binding.effective_from)


def _first_binding(session: Session, action: Action):
    return session.scalar(select(TaskAccountGroupBindingSetRevision).where(
        TaskAccountGroupBindingSetRevision.tenant_id == action.tenant_id,
        TaskAccountGroupBindingSetRevision.task_id == action.task_id,
        TaskAccountGroupBindingSetRevision.task_lifecycle_epoch
        == int(action.task_lifecycle_epoch or 1),
    ).order_by(TaskAccountGroupBindingSetRevision.binding_set_revision).limit(1))


def _work_creation_times(session: Session, action: Action) -> list:
    times = [_created_at(action)]
    payload = action.payload or {}
    references = (
        ("daily_group_target_id", TaskGroupDailyTarget),
        ("comment_fulfillment_obligation_id", CommentFulfillmentObligation),
        ("reaction_fulfillment_obligation_id", ReactionFulfillmentObligation),
        ("view_fulfillment_obligation_id", ViewFulfillmentObligation),
    )
    for key, model in references:
        owner_id = str(payload.get(key) or "")
        if not owner_id:
            continue
        owner = _owned_row(session, action, model=model, owner_id=owner_id)
        times.append(_created_at(owner))
        times.extend(_parent_creation_times(session, action, owner=owner))
    return times


def _parent_creation_times(session: Session, action: Action, *, owner) -> list:
    references = (
        ("plan_contract_id", ChannelCommentPlanContract),
        ("task_day_ledger_id", TaskDayLedger),
    )
    times = []
    for key, model in references:
        parent_id = str(getattr(owner, key, None) or "")
        if parent_id:
            parent = _owned_row(session, action, model=model, owner_id=parent_id)
            times.append(_created_at(parent))
    return times


def _owned_row(session: Session, action: Action, *, model, owner_id):
    owner = session.get(model, owner_id)
    if owner is None:
        raise ValueError("engagement_action_work_owner_missing")
    if owner.tenant_id != action.tenant_id:
        raise ValueError("engagement_action_work_owner_mismatch")
    task_id = getattr(owner, "task_id", None)
    epoch = getattr(owner, "task_lifecycle_epoch", None)
    account_id = getattr(owner, "account_id", None)
    if task_id is not None and task_id != action.task_id:
        raise ValueError("engagement_action_work_owner_mismatch")
    if epoch is not None and int(epoch) != int(action.task_lifecycle_epoch or 1):
        raise ValueError("engagement_action_work_epoch_mismatch")
    if account_id is not None and account_id != action.account_id:
        raise ValueError("engagement_action_work_account_mismatch")
    return owner


def _created_at(owner):
    if owner.created_at is None:
        raise ValueError("engagement_action_work_time_missing")
    return as_beijing(owner.created_at)
