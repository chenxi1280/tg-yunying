from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, CommentFulfillmentObligation, Task
from app.services._common import _now

from ..account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    bind_account_pacing_reservation_for_slot,
    reserve_account_pacing,
)
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import next_local_day_deadline, schedule_times
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..payloads import PostCommentPayload
from ..schedule_reservation import reserve_task_schedule_times
from ..source_pacing import (
    SourcePacingSlot,
    latest_wall_datetime,
    rolling_source_window,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)
from .channel_comment_schedule import materialized_reply_slots
from .common import (
    adjust_for_account_hour_limit,
    pick_channel_account,
    stats_inc,
)


PreparedCommentAction = tuple[
    int,
    object,
    PostCommentPayload,
    CommentFulfillmentObligation,
]


def bind_prepared_comment_pacing(
    session: Session,
    task: Task,
    action: Action,
    *,
    obligation: CommentFulfillmentObligation,
    account_id: int,
) -> None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return
    slot_key = f"comment:{obligation.id}"
    freeze_action_pacing(action, obligation, slot_key=slot_key)
    bind_account_pacing_reservation_for_slot(
        session,
        tenant_id=task.tenant_id,
        account_id=account_id,
        slot_key=slot_key,
        action=action,
    )


def prepare_comment_actions(
    session: Session,
    task: Task,
    *,
    context: Any,
    slots: list,
    payload_builder: Callable,
) -> list[PreparedCommentAction]:
    now_value = _now()
    requested_count = len(slots)
    slots, planned_times, due_by_slot, release_by_slot, source_slots = _comment_schedule(
        session, task, slots=slots, config=context.config, now_at=now_value,
    )
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION and len(planned_times) < requested_count:
        _record_comment_shortfall(task, requested_count, len(planned_times))
    materialized = materialized_reply_slots(
        task,
        slots,
        planned_times,
        now_value=now_value,
    )
    prepared: list[PreparedCommentAction] = []
    for index, (slot, planned_at) in enumerate(materialized):
        slot_key = _comment_slot_key(slot)
        _freeze_comment_pacing(
            task,
            source_slots,
            slot=slot,
            due_at=due_by_slot[slot_key],
            release_not_before_at=release_by_slot[slot_key],
        )
        item = _prepared_slot(
            session,
            task,
            context=context,
            slot=slot,
            planned_at=planned_at,
            account_index=index,
            payload_builder=payload_builder,
        )
        if item is not None:
            prepared.append(item)
    return prepared


def _comment_schedule(
    session: Session,
    task: Task,
    *,
    slots: list,
    config: dict,
    now_at,
) -> tuple[list, list, dict[str, object], dict[str, object], list[SourcePacingSlot]]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        sources = _comment_source_slots(session, task, slots, config)
        points_by_slot = schedule_source_pacing_points(
            sources, task.pacing_config or {}, now_at=wall_datetime(now_at),
            timezone_name=task.timezone, seed_id=f"comment:{task.id}",
        )
        scheduled = [slot for slot in slots if _comment_slot_key(slot) in points_by_slot]
        due_by_slot = {
            key: point.due_at for key, point in points_by_slot.items()
        }
        release_by_slot = {
            key: point.release_not_before_at for key, point in points_by_slot.items()
        }
        return (
            scheduled,
            [release_by_slot[_comment_slot_key(slot)] for slot in scheduled],
            due_by_slot,
            release_by_slot,
            sources,
        )
    deadline = next_local_day_deadline(now_at, task.timezone)
    times = schedule_times(
        len(slots), task.pacing_config or {}, start_at=now_at,
        deadline_at=deadline, preserve_minimum_spacing=True,
    )
    reserved = reserve_task_schedule_times(
        session, task, "post_comment", times,
        pacing_config=task.pacing_config or {}, deadline_at=deadline,
    )
    due_by_slot = {
        _comment_slot_key(slot): due_at
        for slot, due_at in zip(slots, reserved, strict=False)
    }
    return slots, reserved, due_by_slot, dict(due_by_slot), []


def _freeze_comment_pacing(
    task: Task,
    source_slots: list[SourcePacingSlot],
    *,
    slot: Any,
    due_at,
    release_not_before_at,
) -> None:
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return
    source = _comment_source_slot(source_slots, slot)
    freeze_pacing_owner(
        slot.obligation,
        plan_hash=source_pacing_plan_hash(
            source, task.pacing_config or {}, seed_id=f"comment:{task.id}",
        ),
        slot_ordinal=source.slot_ordinal,
        plan_total=source.plan_total,
        due_at=due_at,
        release_not_before_at=release_not_before_at,
    )


def _record_comment_shortfall(task: Task, requested: int, scheduled: int) -> None:
    """来源滚动窗口内无合法节奏窗口时守恒可见：不压缩追量，记录 typed shortfall。"""
    stats = dict(task.stats or {})
    stats["pacing_schedule_shortfall_count"] = int(stats.get("pacing_schedule_shortfall_count") or 0) + (requested - scheduled)
    stats["pacing_schedule_shortfall"] = {
        "reason_code": "pacing_capacity_shortfall",
        "requested": requested,
        "scheduled": scheduled,
    }
    task.stats = stats
    if scheduled == 0:
        task.last_error = "来源滚动窗口内无合法节奏窗口可安排评论义务，形成 pacing shortfall"


def _comment_source_slots(
    session: Session,
    task: Task,
    slots: list,
    config: dict,
) -> list[SourcePacingSlot]:
    target = int(config.get("target_comments_per_message") or 1)
    jitter = float(config.get("comment_count_jitter") or 0)
    max_target = max(1, round(target * (1 + jitter)))
    totals: dict[int, int] = {}
    result: list[SourcePacingSlot] = []
    for slot in slots:
        message_id = int(slot.obligation.channel_message_id)
        if message_id not in totals:
            frozen_max = session.scalar(select(func.max(CommentFulfillmentObligation.target_ordinal)).where(
                CommentFulfillmentObligation.task_id == task.id,
                CommentFulfillmentObligation.channel_message_id == message_id,
            )) or 0
            totals[message_id] = max(max_target, int(frozen_max))
        period_start, deadline = rolling_source_window(task, slot.message.created_at)
        result.append(SourcePacingSlot(
            source_key=str(message_id),
            slot_key=_comment_slot_key(slot),
            slot_ordinal=int(slot.obligation.target_ordinal) - 1,
            plan_total=totals[message_id],
            period_start_at=period_start,
            deadline_at=deadline,
            release_not_before_at=slot.obligation.release_not_before_at,
        ))
    return result


def _comment_source_slot(source_slots: list[SourcePacingSlot], slot: Any) -> SourcePacingSlot:
    slot_key = _comment_slot_key(slot)
    return next(source for source in source_slots if source.slot_key == slot_key)


def _comment_slot_key(slot: Any) -> str:
    return f"comment:{slot.obligation.id}"


def _prepared_slot(
    session: Session,
    task: Task,
    *,
    context: Any,
    slot: Any,
    planned_at: Any,
    account_index: int,
    payload_builder: Callable,
) -> PreparedCommentAction | None:
    account = pick_channel_account(
        session,
        task,
        context.accounts,
        "post_comment",
        planned_at,
        context.config,
        account_index,
    )
    if not account:
        stats_inc(task, "failure_count")
        return None
    adjusted_at = adjust_for_account_hour_limit(
        session,
        task,
        account.id,
        "post_comment",
        planned_at,
        context.config,
    )
    adjusted_at = _comment_effective_time(
        session, task, slot=slot, account_id=account.id,
        planned_at=planned_at, adjusted_at=adjusted_at,
    )
    if adjusted_at is None:
        return None
    return (
        account.id,
        adjusted_at,
        payload_builder(task, context, slot, account_id=account.id),
        slot.obligation,
    )


def _comment_effective_time(
    session: Session,
    task: Task,
    *,
    slot: Any,
    account_id: int,
    planned_at,
    adjusted_at,
):
    if task.fulfillment_contract_version != CURRENT_CONTRACT_VERSION:
        return adjusted_at
    due_at = slot.obligation.pacing_due_at or planned_at
    _period_start, deadline = rolling_source_window(task, slot.message.created_at)
    try:
        reservation = reserve_account_pacing(
            session, tenant_id=task.tenant_id, task_id=task.id,
            account_id=account_id, slot_key=_comment_slot_key(slot), due_at=due_at,
            release_not_before_at=latest_wall_datetime(
                slot.obligation.release_not_before_at or due_at,
                adjusted_at,
            ),
            deadline_at=deadline,
        )
    except AccountPacingDeadlineExceeded:
        stats_inc(task, "account_timeline_conflict")
        return None
    return reservation.effective_claim_at


__all__ = ["bind_prepared_comment_pacing", "prepare_comment_actions"]
