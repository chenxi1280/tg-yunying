from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import and_, case, func, select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt, FailureType, MessageTask, TaskStatus, TgGroup
from app.services._common import _now
from app.services.task_center.daily_coverage_schedule import active_window_bounds
from app.timezone import as_beijing, beijing_day_bounds


GROUP_SEND_SLOT_STATUSES = ("before_call", "gateway_call_started", "success", "result_unknown")

SEND_LIMIT_MODE_LEGACY_GROUP_SLOT = "legacy_group_slot"
SEND_LIMIT_MODE_ACCOUNT_ONLY = "account_only"
SEND_LIMIT_MODE_ACCOUNT_ONLY_WITH_GROUP_DAILY_LIMIT = "account_only_with_group_daily_limit"


@dataclass(frozen=True)
class GroupSendSlotBlock:
    failure_type: str
    detail: str
    retry_after_seconds: int


@dataclass(frozen=True)
class GroupSendSlotSummary:
    same_day_count: int
    latest_at: datetime | None


def group_send_slot_block(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
) -> GroupSendSlotBlock | None:
    """Backward-compatible entry: active window + group policy for the group's mode."""
    now_value = _beijing_now()
    window = active_window_block(group, now_value)
    if window is not None:
        return window
    return group_policy_block(session, action=action, group=group, now_value=now_value)


def active_window_block(group: TgGroup, now: datetime | None = None) -> GroupSendSlotBlock | None:
    now_value = as_beijing(now) if now is not None else _beijing_now()
    if now_value is None:
        now_value = _beijing_now()
    active_window_retry_at = _next_group_active_window_start(group, now_value)
    if active_window_retry_at is not None:
        return GroupSendSlotBlock(
            FailureType.SLOWMODE.value,
            f"群不在活动时段 {group.active_window}，延后至 {active_window_retry_at.isoformat()}",
            max(1, int((active_window_retry_at - now_value).total_seconds())),
        )
    return None


def group_policy_block(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
    now_value: datetime | None = None,
) -> GroupSendSlotBlock | None:
    """Apply group daily/cooldown policy according to send_limit_mode.

    Uses aggregated attempt summaries (index-friendly) rather than loading attempt rows.
    """
    now_local = now_value or _beijing_now()
    mode = str(getattr(group, "send_limit_mode", None) or SEND_LIMIT_MODE_LEGACY_GROUP_SLOT)
    enforce_daily = mode in {
        SEND_LIMIT_MODE_LEGACY_GROUP_SLOT,
        SEND_LIMIT_MODE_ACCOUNT_ONLY_WITH_GROUP_DAILY_LIMIT,
    }
    enforce_cooldown = mode == SEND_LIMIT_MODE_LEGACY_GROUP_SLOT
    attempt_summary = _group_attempt_summary(session, action=action, group=group, now_value=now_local)
    legacy_summary = _legacy_group_send_summary(session, action=action, group=group, now_value=now_local)
    if enforce_daily:
        if attempt_summary.same_day_count + legacy_summary.same_day_count >= int(group.daily_limit or 0):
            retry_at = _next_daily_group_window_start(group, now_local)
            return GroupSendSlotBlock(
                FailureType.SLOWMODE.value,
                f"群当日发送已达上限 {group.daily_limit}",
                max(1, int((retry_at - now_local).total_seconds())),
            )
    if enforce_cooldown:
        last_slot_at = _latest_group_slot_at(attempt_summary.latest_at, legacy_summary.latest_at)
        cooldown = int(group.group_cooldown_seconds or 0)
        if cooldown > 0 and last_slot_at is not None:
            elapsed = (now_local - last_slot_at).total_seconds()
            if elapsed < cooldown:
                retry_after = max(1, int(cooldown - elapsed))
                return GroupSendSlotBlock(
                    FailureType.SLOWMODE.value,
                    f"群冷却中，还需等待 {retry_after} 秒",
                    retry_after,
                )
    return None


def _group_attempt_summary(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
    now_value: datetime,
) -> GroupSendSlotSummary:
    day_start, day_end = beijing_day_bounds(now_value)
    same_day_count = func.coalesce(
        func.sum(
            case(
                (
                    and_(
                        ExecutionAttempt.before_call_at >= day_start,
                        ExecutionAttempt.before_call_at < day_end,
                    ),
                    1,
                ),
                else_=0,
            )
        ),
        0,
    )
    count, latest_at = session.execute(
        select(same_day_count, func.max(ExecutionAttempt.before_call_at))
        .join(Action, Action.id == ExecutionAttempt.action_id)
        .where(
            Action.tenant_id == action.tenant_id,
            Action.action_type == "send_message",
            Action.payload["group_id"].as_integer() == group.id,
            ExecutionAttempt.status.in_(GROUP_SEND_SLOT_STATUSES),
        )
    ).one()
    return GroupSendSlotSummary(same_day_count=int(count or 0), latest_at=as_beijing(latest_at))


def _legacy_group_send_summary(
    session: Session,
    *,
    action: Action,
    group: TgGroup,
    now_value: datetime,
) -> GroupSendSlotSummary:
    day_start, day_end = beijing_day_bounds(now_value)
    same_day_count = func.coalesce(
        func.sum(
            case(
                (and_(MessageTask.sent_at >= day_start, MessageTask.sent_at < day_end), 1),
                else_=0,
            )
        ),
        0,
    )
    count, latest_at = session.execute(
        select(same_day_count, func.max(MessageTask.sent_at)).where(
            MessageTask.tenant_id == action.tenant_id,
            MessageTask.group_id == group.id,
            MessageTask.status == TaskStatus.SENT.value,
            MessageTask.sent_at.is_not(None),
        )
    ).one()
    return GroupSendSlotSummary(same_day_count=int(count or 0), latest_at=as_beijing(latest_at))


def _latest_group_slot_at(*values: datetime | None) -> datetime | None:
    return max((value for value in values if value is not None), default=None)


def _beijing_now() -> datetime:
    return as_beijing(_now()) or _now()


def _next_daily_group_window_start(group: TgGroup, now_value: datetime) -> datetime:
    _day_start, next_day_start = beijing_day_bounds(now_value)
    window_start, _window_end = active_window_bounds(group.active_window, next_day_start.date())
    return window_start


def _next_group_active_window_start(group: TgGroup, now_value: datetime) -> datetime | None:
    current_start, current_end = active_window_bounds(group.active_window, now_value.date())
    previous_start, previous_end = active_window_bounds(group.active_window, now_value.date() - timedelta(days=1))
    if previous_start <= now_value < previous_end or current_start <= now_value < current_end:
        return None
    if now_value < current_start:
        return current_start
    next_start, _next_end = active_window_bounds(group.active_window, now_value.date() + timedelta(days=1))
    return next_start


__all__ = [
    "GroupSendSlotBlock",
    "SEND_LIMIT_MODE_ACCOUNT_ONLY",
    "SEND_LIMIT_MODE_ACCOUNT_ONLY_WITH_GROUP_DAILY_LIMIT",
    "SEND_LIMIT_MODE_LEGACY_GROUP_SLOT",
    "active_window_block",
    "group_policy_block",
    "group_send_slot_block",
]
