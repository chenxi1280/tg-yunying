from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GroupBotAdmission, GroupBotRequiredChannelFollow, Task, TaskGroupBotAdmission
from app.services._common import _now

from .payloads import (
    GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
    GroupBotConfirmationButtonPayload,
    GroupBotRequiredChannelFollowPayload,
    create_group_bot_confirmation_button_action,
    create_group_bot_required_channel_follow_action,
)

CONFIRMATION_ACTION_TYPE = "group_bot_confirmation_button"
OPEN_ACTION_STATUSES = frozenset({"pending", "claiming", "executing"})
STOPPED_ACTION_CODES = frozenset({"task_stopped"})


def rearm_stopped_admission_actions(session: Session, *, task: Task) -> int:
    if task.type != "group_ai_chat":
        return 0
    return (
        _rearm_follow_actions(session, task)
        + _rearm_confirmation_actions(session, task)
        + _rearm_task_scoped_actions(session, task)
    )


def _rearm_task_scoped_actions(session: Session, task: Task) -> int:
    if task.fulfillment_contract_version != "fact_first_v3":
        return 0
    actions = session.scalars(select(Action).where(
        Action.task_id == task.id,
        Action.action_type.in_((GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE, CONFIRMATION_ACTION_TYPE)),
        Action.status == "skipped",
    )).all()
    count = 0
    for old_action in actions:
        if not _eligible_stopped_action(old_action, task, old_action.action_type):
            continue
        payload = old_action.payload if isinstance(old_action.payload, dict) else {}
        admission_id = str(payload.get("task_group_bot_admission_id") or "")
        if not admission_id or _has_open_task_requirement(session, old_action, admission_id):
            continue
        admission = session.get(TaskGroupBotAdmission, admission_id)
        if admission is None or int(admission.version or 1) != int(payload.get("admission_version") or 1):
            continue
        replacement = _create_task_replacement(session, task, old_action, payload)
        if replacement is not None:
            count += 1
    return count


def _has_open_task_requirement(session: Session, old_action: Action, admission_id: str) -> bool:
    key = str((old_action.payload or {}).get("requirement_action_key") or "")
    if not key:
        return False
    return session.scalar(select(Action.id).where(
        Action.task_id == old_action.task_id,
        Action.account_id == old_action.account_id,
        Action.action_type == old_action.action_type,
        Action.status.in_(OPEN_ACTION_STATUSES),
        Action.payload["task_group_bot_admission_id"].as_string() == admission_id,
        Action.payload["requirement_action_key"].as_string() == key,
        Action.id != old_action.id,
    ).limit(1)) is not None


def _create_task_replacement(session: Session, task: Task, old_action: Action, payload: dict) -> Action | None:
    from .payloads import (
        GroupBotConfirmationButtonPayload,
        GroupBotRequiredChannelFollowPayload,
        create_group_bot_confirmation_button_action,
        create_group_bot_required_channel_follow_action,
    )

    replacement_payload = {
        **payload,
        "replan_attempt": int(payload.get("replan_attempt") or 0) + 1,
    }
    scheduled_at = _now()
    if old_action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE:
        parsed = GroupBotRequiredChannelFollowPayload.model_validate(replacement_payload)
        return create_group_bot_required_channel_follow_action(
            session, task, int(old_action.account_id), scheduled_at, parsed, flush=True,
        )
    parsed = GroupBotConfirmationButtonPayload.model_validate(replacement_payload)
    return create_group_bot_confirmation_button_action(
        session, task, int(old_action.account_id), scheduled_at, parsed, flush=True,
    )


def _rearm_follow_actions(session: Session, task: Task) -> int:
    count = 0
    pairs = session.execute(
        select(GroupBotRequiredChannelFollow, Action)
        .join(Action, Action.id == GroupBotRequiredChannelFollow.action_id)
        .where(
            GroupBotRequiredChannelFollow.status == "pending",
            Action.task_id == task.id,
            Action.action_type == GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE,
            Action.status == "skipped",
        )
    ).all()
    for row, old_action in pairs:
        if not _eligible_stopped_action(old_action, task, GROUP_BOT_CHANNEL_FOLLOW_ACTION_TYPE):
            continue
        payload = GroupBotRequiredChannelFollowPayload.model_validate(old_action.payload)
        if not _follow_binding_is_current(session, row, payload):
            continue
        action = create_group_bot_required_channel_follow_action(
            session, task, payload.admission_bound_account_id, _now(), payload, flush=True
        )
        row.action_id = str(action.id)
        count += 1
    return count


def _rearm_confirmation_actions(session: Session, task: Task) -> int:
    actions = session.scalars(
        select(Action).where(Action.task_id == task.id, Action.action_type == CONFIRMATION_ACTION_TYPE)
    ).all()
    open_keys = {_confirmation_key(action.payload) for action in actions if action.status in OPEN_ACTION_STATUSES}
    count = 0
    for old_action in actions:
        if not _eligible_stopped_action(old_action, task, CONFIRMATION_ACTION_TYPE):
            continue
        payload = GroupBotConfirmationButtonPayload.model_validate(old_action.payload)
        key = _confirmation_key(payload)
        if key in open_keys or not _confirmation_binding_is_current(session, payload):
            continue
        create_group_bot_confirmation_button_action(
            session, task, payload.admission_bound_account_id, _now(), payload, flush=True
        )
        open_keys.add(key)
        count += 1
    return count


def _eligible_stopped_action(action: Action | None, task: Task, action_type: str) -> bool:
    if action is None or action.task_id != task.id or action.action_type != action_type:
        return False
    return action.status == "skipped" and str((action.result or {}).get("error_code") or "") in STOPPED_ACTION_CODES


def _follow_binding_is_current(
    session: Session,
    row: GroupBotRequiredChannelFollow,
    payload: GroupBotRequiredChannelFollowPayload,
) -> bool:
    admission = session.get(GroupBotAdmission, payload.admission_id)
    return bool(
        admission
        and row.admission_id == admission.id
        and row.channel_ref == payload.channel_ref
        and admission.admission_version == payload.admission_version
    )


def _confirmation_binding_is_current(session: Session, payload: GroupBotConfirmationButtonPayload) -> bool:
    admission = session.get(GroupBotAdmission, payload.admission_id)
    return bool(
        admission
        and admission.admission_version == payload.admission_version
        and str(admission.source_message_id or "") == payload.source_message_id
    )


def _confirmation_key(payload: Any) -> tuple[int, int, str]:
    parsed = GroupBotConfirmationButtonPayload.model_validate(payload)
    return parsed.admission_id, parsed.admission_version, parsed.source_message_id


__all__ = ["rearm_stopped_admission_actions"]
