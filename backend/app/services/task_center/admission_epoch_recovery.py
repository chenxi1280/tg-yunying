from __future__ import annotations

from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, Task, TaskMembershipAdmissionItem
from app.services._common import _now, audit

from .targets import group_from_reference


ADMISSION_ACTION_TYPES = frozenset({
    "ensure_channel_membership",
    "ensure_target_membership",
    "invite_group_account",
})


def replan_stale_admission_actions(session: Session, *, task: Task) -> int:
    if task.type != "group_ai_chat" or task.status not in {"pending", "running"}:
        return 0
    if not _task_target_is_canonical(session, task):
        return 0
    actions = list(session.scalars(_stale_action_statement(task)))
    replacements = 0
    for old_action in actions:
        if _has_attempt(session, old_action.id):
            continue
        if not _action_target_is_current(old_action, task):
            continue
        replacement = _existing_replacement(session, old_action, task)
        if replacement is None:
            replacement = _clone_for_current_epoch(old_action, task)
            _skip_and_rebind(session, old_action, replacement, task)
            session.add(replacement)
            session.flush()
            replacements += 1
        else:
            _skip_and_rebind(session, old_action, replacement, task)
    if replacements:
        audit(
            session,
            tenant_id=task.tenant_id,
            actor="system:admission-epoch-recovery",
            action="重建旧生命周期AI活群准入动作",
            target_type="task",
            target_id=task.id,
            detail=f"count={replacements};epoch={int(task.task_lifecycle_epoch or 1)}",
        )
    return replacements


def _task_target_is_canonical(session: Session, task: Task) -> bool:
    config = dict(task.type_config or {})
    target_id = int(config.get("target_operation_target_id") or 0)
    group_id = int(config.get("target_group_id") or 0)
    return group_from_reference(
        session,
        task.tenant_id,
        group_id=group_id or None,
        operation_target_id=target_id or None,
        require_authorized=False,
    ) is not None


def _action_target_is_current(action: Action, task: Task) -> bool:
    config = dict(task.type_config or {})
    payload = dict(action.payload or {})
    target_id = int(config.get("target_operation_target_id") or 0)
    group_id = int(config.get("target_group_id") or 0)
    if action.action_type == "invite_group_account":
        return (
            int(payload.get("operation_target_id") or 0) == target_id
            and int(payload.get("group_id") or 0) == group_id
        )
    return int(payload.get("channel_target_id") or 0) == target_id


def _stale_action_statement(task: Task):
    return (
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.action_type.in_(ADMISSION_ACTION_TYPES),
            Action.status == "pending",
            Action.task_lifecycle_epoch != int(task.task_lifecycle_epoch or 1),
        )
        .order_by(Action.created_at, Action.id)
        .with_for_update(skip_locked=True)
    )


def _has_attempt(session: Session, action_id: str) -> bool:
    return session.scalar(
        select(ExecutionAttempt.id)
        .where(ExecutionAttempt.action_id == action_id)
        .limit(1)
    ) is not None


def _existing_replacement(
    session: Session,
    old_action: Action,
    task: Task,
) -> Action | None:
    key = _replacement_key(old_action, task)
    return session.scalar(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.action_dedupe_key == key,
        )
        .limit(1)
    )


def _clone_for_current_epoch(old_action: Action, task: Task) -> Action:
    return Action(
        id=str(uuid4()),
        tenant_id=old_action.tenant_id,
        task_id=old_action.task_id,
        task_type=old_action.task_type,
        action_type=old_action.action_type,
        account_id=old_action.account_id,
        scheduled_at=_now(),
        plan_batch_key=f"{old_action.plan_batch_key or task.id}:epoch:{task.task_lifecycle_epoch}",
        action_dedupe_key=_replacement_key(old_action, task),
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        execution_lane=old_action.execution_lane,
        obligation_type=old_action.obligation_type,
        obligation_id=old_action.obligation_id,
        materialization_version=int(old_action.materialization_version or 1) + 1,
        payload=dict(old_action.payload or {}),
        result={"replanned_from_action_id": old_action.id},
        status="pending",
    )


def _replacement_key(old_action: Action, task: Task) -> str:
    return (
        f"{task.tenant_id}:{task.id}:admission-epoch-replan:"
        f"{old_action.id}:{int(task.task_lifecycle_epoch or 1)}"
    )


def _skip_and_rebind(
    session: Session,
    old_action: Action,
    replacement: Action,
    task: Task,
) -> None:
    old_action.status = "skipped"
    old_action.executed_at = _now()
    old_action.result = {
        **dict(old_action.result or {}),
        "success": False,
        "error_code": "stale_lifecycle_epoch_replanned",
        "old_task_lifecycle_epoch": int(old_action.task_lifecycle_epoch or 1),
        "current_task_lifecycle_epoch": int(task.task_lifecycle_epoch or 1),
        "replacement_action_id": replacement.id,
    }
    _rebind_membership_items(session, old_action, replacement)


def _rebind_membership_items(
    session: Session,
    old_action: Action,
    replacement: Action,
) -> None:
    rows = session.scalars(
        select(TaskMembershipAdmissionItem).where(
            TaskMembershipAdmissionItem.task_id == old_action.task_id,
        )
    )
    for item in rows:
        if item.membership_action_id == old_action.id:
            item.membership_action_id = replacement.id
        if item.rescue_action_id == old_action.id:
            item.rescue_action_id = replacement.id


__all__ = ["replan_stale_admission_actions"]
