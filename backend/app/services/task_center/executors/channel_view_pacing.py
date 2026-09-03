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
from ..datetime_compat import utc_storage_as_beijing_wall
from ..pacing_persistence import freeze_action_pacing, freeze_pacing_owner
from ..source_pacing import (
    SourcePacingSlot,
    latest_wall_datetime,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
    wall_datetime,
)
from ..source_capacity_plans import apply_source_capacity_plan
from ..source_owner_cursor import attach_owner_history, pacing_source_key_hash


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


def effective_channel_view_pacing_config(task: Task) -> dict:
    cfg = dict(task.pacing_config or {})
    mode = cfg.get("mode") or "template"
    if mode == "fixed":
        return cfg
    profile = dict(cfg.get("operation_profile") or {})
    if not profile.get("manual_override"):
        profile["hourly_activity_curve"] = [1] * 24
        cfg["operation_profile"] = profile
    return cfg


def create_current_view_actions(
    session: Session,
    task: Task,
    *,
    actions: list[tuple[ChannelMessage, int]],
    context: ViewCreationContext,
    action_creator: Callable[[Session, ViewActionRequest], int],
) -> int:
    items = _freeze_view_plan_items(session, task, actions=actions, context=context)
    items = _attach_view_owner_history(session, task, items)
    points_by_slot = schedule_source_pacing_points(
        [item.source_slot for item in items],
        effective_channel_view_pacing_config(task),
        now_at=wall_datetime(_now()),
        timezone_name=task.timezone,
        seed_id=f"view:{task.id}",
    )
    points_by_slot, capacity_slots = apply_source_capacity_plan(
        session,
        task,
        [item.source_slot for item in items],
        points=points_by_slot,
        pacing_domain="view",
    )
    capacity_by_key = {slot.slot_key: slot for slot in capacity_slots}
    items = [
        ViewPlanItem(
            item.message,
            item.account_id,
            item.obligation,
            capacity_by_key.get(item.source_slot.slot_key, item.source_slot),
        )
        for item in items
    ]
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
            release_not_before_at=latest_wall_datetime(
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
        source_identity=source.owner_identity,
    )
    if source.source_capacity_plan_hash:
        item.obligation.source_capacity_plan_hash = source.source_capacity_plan_hash
        item.obligation.source_capacity_slot_ordinal = source.source_capacity_slot_ordinal


def _attach_view_owner_history(
    session: Session,
    task: Task,
    items: list[ViewPlanItem],
) -> list[ViewPlanItem]:
    slots = attach_owner_history(
        session,
        task,
        [item.source_slot for item in items],
        owner_model=ViewFulfillmentObligation,
        config=task.pacing_config or {},
        seed_id=f"view:{task.id}",
    )
    return [
        ViewPlanItem(item.message, item.account_id, item.obligation, slots[index])
        for index, item in enumerate(items)
    ]


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
        utc_storage_as_beijing_wall(context.ledger.period_start_at),
        utc_storage_as_beijing_wall(context.ledger.planning_anchor_at),
        wall_datetime(target.accrual_anchor_at),
    )
    deadline = min(
        utc_storage_as_beijing_wall(context.ledger.deadline_at),
        utc_storage_as_beijing_wall(target.active_until),
    )
    period_key = (
        str(obligation.pacing_period_key)
        if obligation.pacing_due_at is not None and obligation.pacing_period_key
        else f"{context.ledger.id}:message:{message.id}"
    )
    slot = SourcePacingSlot(
        source_key=f"{context.ledger.id}:{message.id}",
        slot_key=f"view:{task.id}:{context.ledger.id}:{message.id}:{account_id}",
        slot_ordinal=ordinal,
        plan_total=(
            int(obligation.pacing_plan_total)
            if obligation.pacing_due_at is not None and obligation.pacing_plan_total
            else max(int(target.effective_target_snapshot), ordinal + 1)
        ),
        period_start_at=period_start,
        deadline_at=deadline,
        release_not_before_at=obligation.release_not_before_at,
        frozen_due_at=obligation.pacing_due_at,
        owner_id=obligation.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=period_key,
        pacing_source_key_hash=pacing_source_key_hash(context.channel.tg_peer_id),
        source_capacity_plan_hash=obligation.source_capacity_plan_hash,
        source_capacity_slot_ordinal=obligation.source_capacity_slot_ordinal,
    )
    return ViewPlanItem(message, account_id, obligation, slot)


__all__ = [
    "ViewActionRequest",
    "ViewCreationContext",
    "bind_view_action_pacing",
    "create_current_view_actions",
    "effective_channel_view_pacing_config",
    "record_view_deadline_capacity_blocker",
    "reserve_view_action_pacing",
]
