from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy.orm import Session

from app.models import CommentFulfillmentObligation, Task
from app.services._common import _now

from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import next_local_day_deadline, schedule_times
from ..payloads import PostCommentPayload
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


def prepare_comment_actions(
    session: Session,
    task: Task,
    *,
    context: Any,
    slots: list,
    payload_builder: Callable,
) -> list[PreparedCommentAction]:
    now_value = _now()
    planned_times = schedule_times(
        len(slots),
        task.pacing_config or {},
        start_at=now_value,
        deadline_at=(
            None
            if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION
            else next_local_day_deadline(now_value, task.timezone)
        ),
    )
    materialized = materialized_reply_slots(
        task,
        slots,
        planned_times,
        now_value=now_value,
    )
    prepared: list[PreparedCommentAction] = []
    for index, (slot, planned_at) in enumerate(materialized):
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
    return (
        account.id,
        adjusted_at,
        payload_builder(task, context, slot, account_id=account.id),
        slot.obligation,
    )


__all__ = ["prepare_comment_actions"]
