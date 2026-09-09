from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountPacingReservation, OperationTarget, ReactionFulfillmentObligation, Task
from ..fulfillment_activation import CURRENT_CONTRACT_VERSION
from ..pacing import next_local_day_deadline, schedule_times
from ..pacing_persistence import PacingOwnerImmutableConflict
from ..schedule_reservation import reserve_task_schedule_times
from ..source_capacity_plans import apply_source_capacity_plan
from ..source_owner_cursor import attach_owner_history, pacing_source_key_hash
from ..source_pacing import SourcePacingPoint, SourcePacingSlot, rolling_source_window, schedule_source_pacing_points, wall_datetime
from .channel_like_types import LikePlanItem


def _like_due_by_slot(
    session: Session,
    task: Task,
    *,
    channel: OperationTarget,
    actions: list[LikePlanItem],
    owners: dict[str, object],
    now_at,
) -> dict[str, object]:
    if task.fulfillment_contract_version == CURRENT_CONTRACT_VERSION:
        return _source_like_due_by_slot(session, task, channel=channel,
            actions=actions, owners=owners, now_at=now_at)
    deadline = next_local_day_deadline(now_at, task.timezone)
    times = schedule_times(
        len(actions), task.pacing_config or {}, start_at=now_at,
        deadline_at=deadline, preserve_minimum_spacing=True,
    )
    times = reserve_task_schedule_times(
        session, task, "like_message", times,
        pacing_config=task.pacing_config or {}, deadline_at=deadline,
    )
    return {
        _like_slot_key(task, item): due_at
        for item, due_at in zip(actions, times, strict=False)
    }


def _source_like_due_by_slot(session, task, *, channel, actions, owners, now_at):
    restored = _reserved_owner_points(session, task, owners=owners, now_at=now_at)
    source_hash = pacing_source_key_hash(channel.tg_peer_id)
    all_slots = [
        _like_source_slot(
            task,
            item,
            owner=owners[_like_slot_key(task, item)],
            source_hash=source_hash,
        )
        for item in actions
    ]
    slots = [slot for slot in all_slots if slot.slot_key not in restored]
    slots = attach_owner_history(
        session,
        task,
        slots,
        owner_model=ReactionFulfillmentObligation,
        config=task.pacing_config or {},
        seed_id=f"like:{task.id}",
        allow_plan_total_overrun=True,
    )
    points = schedule_source_pacing_points(
        slots,
        task.pacing_config or {},
        now_at=wall_datetime(now_at),
        timezone_name=task.timezone,
        seed_id=f"like:{task.id}",
    )
    points.update(restored)
    slots.extend(slot for slot in all_slots if slot.slot_key in restored)
    points, slots = apply_source_capacity_plan(
        session,
        task,
        slots,
        points=points,
        pacing_domain="reaction",
    )
    for slot in slots:
        owner = owners[slot.slot_key]
        owner.source_capacity_plan_hash = slot.source_capacity_plan_hash
        owner.source_capacity_slot_ordinal = slot.source_capacity_slot_ordinal
    return points


def _reserved_owner_points(session, task, *, owners, now_at):
    reservations = session.scalars(select(AccountPacingReservation).where(
        AccountPacingReservation.tenant_id == task.tenant_id,
        AccountPacingReservation.task_id == task.id,
        AccountPacingReservation.pacing_slot_key.in_(owners),
        AccountPacingReservation.state == "reserved",
        AccountPacingReservation.action_id.is_(None),
    ))
    points = {}
    for reservation in reservations:
        owner = owners[reservation.pacing_slot_key]
        if (owner.status != "open" or owner.current_action_id is not None
                or owner.pacing_due_at is None or owner.release_not_before_at is None):
            continue
        if (reservation.source_deadline_at is None
                or wall_datetime(reservation.source_deadline_at) <= wall_datetime(now_at)):
            continue
        if (reservation.account_id != owner.account_id
                or wall_datetime(reservation.due_at) != wall_datetime(owner.pacing_due_at)):
            raise PacingOwnerImmutableConflict("reaction_replan_reservation_identity_mismatch")
        points[reservation.pacing_slot_key] = SourcePacingPoint(
            wall_datetime(owner.pacing_due_at), wall_datetime(owner.release_not_before_at))
    return points


def _like_slot_key(task: Task, item: LikePlanItem) -> str:
    return f"like:{task.id}:{item.message.id}:{item.account_id}"


def _like_source_slot(
    task: Task,
    item: LikePlanItem,
    *,
    owner: ReactionFulfillmentObligation,
    source_hash: str,
) -> SourcePacingSlot:
    period_start, deadline = rolling_source_window(task, item.message.created_at)
    pacing_ordinal = (
        int(owner.pacing_slot_ordinal)
        if owner.pacing_slot_ordinal is not None
        else item.slot_ordinal
    )
    return SourcePacingSlot(
        source_key=str(item.message.id),
        slot_key=_like_slot_key(task, item),
        slot_ordinal=pacing_ordinal,
        plan_total=(
            int(owner.pacing_plan_total)
            if owner.pacing_due_at is not None and owner.pacing_plan_total
            else item.plan_total
        ),
        period_start_at=period_start,
        deadline_at=deadline,
        release_not_before_at=owner.release_not_before_at,
        frozen_due_at=owner.pacing_due_at,
        owner_id=owner.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=f"message:{item.message.id}",
        pacing_source_key_hash=source_hash,
        source_capacity_plan_hash=owner.source_capacity_plan_hash,
        source_capacity_slot_ordinal=owner.source_capacity_slot_ordinal,
    )
