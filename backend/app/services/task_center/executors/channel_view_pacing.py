from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AccountPacingReservation,
    Action,
    ChannelMessage,
    ChannelViewDailyMessageTarget,
    OperationTarget,
    Task,
    TaskDayLedger,
    ViewFulfillmentObligation,
)
from app.services._common import _now

from ..account_pacing_guard import (
    AccountPacingDeadlineExceeded,
    bind_account_pacing_reservation,
    reserve_account_pacing,
)
from ..channel_fulfillment import ensure_view_obligation
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..source_pacing import (
    SourcePacingSlot,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)


@dataclass(frozen=True)
class ViewCreationContext:
    channel: OperationTarget
    config: dict
    execution_date: str
    ledger: TaskDayLedger
    targets_by_message: dict[int, ChannelViewDailyMessageTarget]


@dataclass(frozen=True)
class ViewActionRequest:
    task: Task
    message: ChannelMessage
    account_id: int
    scheduled_at: datetime
    context: ViewCreationContext
    pacing_slot_key: str = ""
    pacing_due_at: datetime | None = None
    release_not_before_at: datetime | None = None
    obligation: ViewFulfillmentObligation | None = None
    deadline_at: datetime | None = None


@dataclass(frozen=True)
class ViewPlanItem:
    message: ChannelMessage
    account_id: int
    obligation: ViewFulfillmentObligation
    source_slot: SourcePacingSlot


def create_current_view_actions(
    session: Session,
    task: Task,
    *,
    actions: list[tuple[ChannelMessage, int]],
    context: ViewCreationContext,
    action_creator: Callable[[Session, ViewActionRequest], int],
) -> int:
    items = _freeze_view_plan_items(session, task, actions=actions, context=context)
    points_by_slot = schedule_source_pacing_points(
        [item.source_slot for item in items],
        task.pacing_config or {},
        now_at=wall_datetime(_now()),
        timezone_name=task.timezone,
        seed_id=f"view:{task.id}",
    )
    if len(points_by_slot) < len(items):
        _record_stratified_shortfall(task, len(items), len(points_by_slot))
    created = 0
    for item in items:
        point = points_by_slot.get(item.source_slot.slot_key)
        if point is None:
            continue
        _freeze_view_pacing_owner(task, item, point.due_at, point.release_not_before_at)
        created += action_creator(session, ViewActionRequest(
            task,
            item.message,
            item.account_id,
            point.due_at,
            context,
            pacing_slot_key=item.source_slot.slot_key,
            pacing_due_at=point.due_at,
            release_not_before_at=point.release_not_before_at,
            obligation=item.obligation,
            deadline_at=item.source_slot.deadline_at,
        ))
    return created


def reserve_view_action_pacing(
    session: Session,
    request: ViewActionRequest,
    *,
    planned_at: datetime,
    deadline_at: datetime,
) -> tuple[datetime, AccountPacingReservation | None] | None:
    if not request.pacing_slot_key:
        return planned_at, None
    try:
        reservation = reserve_account_pacing(
            session,
            tenant_id=request.task.tenant_id,
            task_id=request.task.id,
            account_id=request.account_id,
            slot_key=request.pacing_slot_key,
            due_at=request.pacing_due_at or request.scheduled_at,
            release_not_before_at=max(
                request.release_not_before_at or request.scheduled_at,
                planned_at,
            ),
            deadline_at=deadline_at,
        )
    except AccountPacingDeadlineExceeded:
        record_view_deadline_capacity_blocker(request.task, planned_at, deadline_at)
        return None
    return reservation.effective_claim_at, reservation


def bind_view_action_pacing(
    action: Action,
    request: ViewActionRequest,
    obligation: ViewFulfillmentObligation,
    reservation: AccountPacingReservation | None,
) -> None:
    if not request.pacing_slot_key:
        return
    if reservation is None:
        raise ValueError("view_pacing_reservation_missing")
    freeze_action_pacing(action, obligation, slot_key=request.pacing_slot_key)
    bind_account_pacing_reservation(reservation, action)


def record_view_deadline_capacity_blocker(
    task: Task,
    planned_at: datetime,
    deadline_at: datetime,
) -> None:
    stats = dict(task.stats or {})
    key = "channel_view_deadline_capacity_defer_count"
    stats[key] = int(stats.get(key) or 0) + 1
    stats["channel_view_deadline_capacity_defer"] = {
        "planned_at": planned_at.isoformat(),
        "deadline_at": deadline_at.isoformat(),
        "reason_code": "account_capacity_after_ledger_deadline",
    }
    task.stats = stats
    task.last_error = "账号容量可用时刻已越过当前浏览任务日截止时间，未创建跨日 Action"


def _freeze_view_pacing_owner(
    task: Task,
    item: ViewPlanItem,
    due_at: datetime,
    release_not_before_at: datetime,
) -> None:
    source = item.source_slot
    freeze_pacing_owner(
        item.obligation,
        plan_hash=source_pacing_plan_hash(
            source,
            task.pacing_config or {},
            seed_id=f"view:{task.id}",
        ),
        slot_ordinal=source.slot_ordinal,
        plan_total=source.plan_total,
        due_at=due_at,
        release_not_before_at=release_not_before_at,
    )


def _record_stratified_shortfall(task: Task, requested: int, scheduled: int) -> None:
    stats = dict(task.stats or {})
    stats["pacing_schedule_shortfall_count"] = (
        int(stats.get("pacing_schedule_shortfall_count") or 0) + requested - scheduled
    )
    stats["pacing_schedule_shortfall"] = {
        "reason_code": "pacing_capacity_shortfall",
        "requested": requested,
        "scheduled": scheduled,
    }
    task.stats = stats
    if scheduled == 0:
        task.last_error = "当前日截止前无合法节奏窗口可安排浏览义务，形成 pacing shortfall"


def _freeze_view_plan_items(
    session: Session,
    task: Task,
    *,
    actions: list[tuple[ChannelMessage, int]],
    context: ViewCreationContext,
) -> list[ViewPlanItem]:
    next_ordinals: dict[int, int] = {}
    items: list[ViewPlanItem] = []
    for message, account_id in actions:
        obligation = ensure_view_obligation(session, context.ledger, message, account_id)
        if obligation.pacing_slot_ordinal is None:
            next_ordinal = _next_view_ordinal(
                session, context, message, cached=next_ordinals.get(message.id),
            )
            obligation.pacing_slot_ordinal = next_ordinal
            next_ordinals[message.id] = next_ordinal + 1
        items.append(_view_plan_item(task, context, message, account_id, obligation))
    return items


def _next_view_ordinal(
    session: Session,
    context: ViewCreationContext,
    message: ChannelMessage,
    *,
    cached: int | None,
) -> int:
    if cached is not None:
        return cached
    current_max = session.scalar(select(func.max(ViewFulfillmentObligation.pacing_slot_ordinal)).where(
        ViewFulfillmentObligation.task_day_ledger_id == context.ledger.id,
        ViewFulfillmentObligation.channel_message_id == message.id,
    ))
    return int(current_max) + 1 if current_max is not None else 0


def _view_plan_item(
    task: Task,
    context: ViewCreationContext,
    message: ChannelMessage,
    account_id: int,
    obligation: ViewFulfillmentObligation,
) -> ViewPlanItem:
    target = context.targets_by_message[message.id]
    ordinal = int(obligation.pacing_slot_ordinal)
    period_start = max(
        wall_datetime(context.ledger.period_start_at),
        wall_datetime(context.ledger.planning_anchor_at),
        wall_datetime(target.accrual_anchor_at),
    )
    deadline = min(
        wall_datetime(context.ledger.deadline_at),
        wall_datetime(target.active_until),
    )
    slot = SourcePacingSlot(
        source_key=f"{context.ledger.id}:{message.id}",
        slot_key=f"view:{task.id}:{context.ledger.id}:{message.id}:{account_id}",
        slot_ordinal=ordinal,
        plan_total=max(int(target.effective_target_snapshot), ordinal + 1),
        period_start_at=period_start,
        deadline_at=deadline,
        release_not_before_at=obligation.release_not_before_at,
    )
    return ViewPlanItem(message, account_id, obligation, slot)


__all__ = [
    "ViewActionRequest",
    "ViewCreationContext",
    "bind_view_action_pacing",
    "create_current_view_actions",
    "record_view_deadline_capacity_blocker",
    "reserve_view_action_pacing",
]
