from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, FailureType, MessageTask, TaskStatus, TgGroup
from app.services._common import _now
from app.timezone import as_beijing, beijing_day_bounds


GROUP_SEND_SLOT_STATUSES = ("before_call", "gateway_call_started", "success", "result_unknown")


@dataclass(frozen=True)
class GroupSendSlotBlock:
    failure_type: str
    detail: str
    retry_after_seconds: int


def group_send_slot_block(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
) -> GroupSendSlotBlock | None:
    now_value = _beijing_now()
    attempts = _same_day_group_attempts(session, action=action, group=group, now_value=now_value)
    legacy_count = _legacy_group_send_count(session, action=action, group=group, now_value=now_value)
    if len(attempts) + legacy_count >= int(group.daily_limit or 0):
        _day_start, day_end = beijing_day_bounds(now_value)
        return GroupSendSlotBlock(
            FailureType.SLOWMODE.value,
            f"群当日发送已达上限 {group.daily_limit}",
            max(1, int((day_end - now_value).total_seconds())),
        )
    last_slot_at = _latest_group_slot_at(session, action=action, group=group)
    cooldown = int(group.group_cooldown_seconds or 0)
    if cooldown > 0 and last_slot_at is not None:
        elapsed = (now_value - last_slot_at).total_seconds()
        if elapsed < cooldown:
            retry_after = max(1, int(cooldown - elapsed))
            return GroupSendSlotBlock(
                FailureType.SLOWMODE.value,
                f"群冷却中，还需等待 {retry_after} 秒",
                retry_after,
            )
    return None


def _same_day_group_attempts(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
    now_value: datetime,
) -> list[ExecutionAttempt]:
    day_start, day_end = beijing_day_bounds(now_value)
    return list(session.scalars(
        select(ExecutionAttempt)
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.tenant_id == action.tenant_id,
            Action.action_type == "send_message",
            Action.payload["group_id"].as_integer() == group.id,
            ExecutionAttempt.status.in_(GROUP_SEND_SLOT_STATUSES),
            ExecutionAttempt.before_call_at >= day_start,
            ExecutionAttempt.before_call_at < day_end,
        )
    ))


def _legacy_group_send_count(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
    now_value: datetime,
) -> int:
    day_start, day_end = beijing_day_bounds(now_value)
    filters = (
        MessageTask.tenant_id == action.tenant_id,
        MessageTask.group_id == group.id,
        MessageTask.status == TaskStatus.SENT.value,
        MessageTask.sent_at.is_not(None),
        MessageTask.sent_at >= day_start,
        MessageTask.sent_at < day_end,
    )
    count = session.scalar(select(func.count(MessageTask.id)).where(*filters)) or 0
    return int(count)


def _latest_group_slot_at(session: Session, *, action: Action, group: TgGroup) -> datetime | None:
    attempt_at = session.scalar(
        select(func.max(ExecutionAttempt.before_call_at))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.tenant_id == action.tenant_id,
            Action.action_type == "send_message",
            Action.payload["group_id"].as_integer() == group.id,
            ExecutionAttempt.status.in_(GROUP_SEND_SLOT_STATUSES),
        )
    )
    legacy_at = session.scalar(
        select(func.max(MessageTask.sent_at)).where(
            MessageTask.tenant_id == action.tenant_id,
            MessageTask.group_id == group.id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
        )
    )
    values = [as_beijing(attempt_at), as_beijing(legacy_at)]
    return max((value for value in values if value is not None), default=None)


def _beijing_now() -> datetime:
    return as_beijing(_now()) or _now()


__all__ = ["GroupSendSlotBlock", "group_send_slot_block"]
