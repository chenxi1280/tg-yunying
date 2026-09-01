from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelCommentCapacityAllocationEpoch,
    ChannelCommentPlanContract,
    CommentFulfillmentObligation,
    Task,
    TaskCommentCapacityPeriod,
    TaskCommentCapacityReservation,
)

from .channel_comment_capacity import (
    _beijing_wall,
    _capacity_period,
    _lock_task_capacity_owner,
    reserve_comment_capacity,
)


@dataclass(frozen=True)
class _AllocationCandidate:
    obligation: CommentFulfillmentObligation
    plan: ChannelCommentPlanContract
    period: TaskCommentCapacityPeriod
    scheduled_at: datetime


def rebalance_comment_capacity_epoch(
    session: Session,
    task: Task,
    *,
    daily_cap: int,
    at: datetime,
) -> ChannelCommentCapacityAllocationEpoch:
    _lock_task_capacity_owner(session, task.id)
    candidates = _allocation_candidates(session, task, daily_cap=daily_cap, at=at)
    latest = _latest_epoch(session, task.id)
    open_hash = _open_plan_set_hash(candidates)
    immutable_hash = _immutable_usage_hash(session, task.id, at)
    current_hash = _current_allocation_hash(session, candidates)
    if _epoch_unchanged(
        latest, open_hash=open_hash,
        immutable_hash=immutable_hash, current_hash=current_hash,
    ):
        return latest
    epoch_number = int(latest.allocation_epoch if latest else 0) + 1
    _release_movable_reservations(session, candidates)
    assigned = _allocate(
        session, task, candidates, daily_cap=daily_cap, epoch=epoch_number,
    )
    epoch = _new_epoch(
        task, candidates,
        assigned=assigned, epoch=epoch_number, open_hash=open_hash,
        immutable_hash=immutable_hash, at=at,
    )
    session.add(epoch)
    _record_shortfall(task, len(candidates) - len(assigned))
    session.flush()
    return epoch


def _allocation_candidates(
    session: Session,
    task: Task,
    *,
    daily_cap: int,
    at: datetime,
) -> list[_AllocationCandidate]:
    rows = session.execute(
        select(CommentFulfillmentObligation, ChannelCommentPlanContract)
        .join(ChannelCommentPlanContract)
        .where(
            CommentFulfillmentObligation.task_id == task.id,
            CommentFulfillmentObligation.status.in_(("open", "replan_required")),
            CommentFulfillmentObligation.current_action_id.is_(None),
            CommentFulfillmentObligation.pacing_due_at.is_not(None),
            ChannelCommentPlanContract.contract_state == "open",
            ChannelCommentPlanContract.deadline_at >= at,
        )
    ).all()
    candidates = [
        _candidate(
            session, task, obligation, plan=plan, daily_cap=daily_cap, at=at,
        )
        for obligation, plan in rows
    ]
    candidates = [item for item in candidates if item is not None]
    return sorted(candidates, key=_allocation_sort_key)


def _candidate(
    session: Session,
    task: Task,
    obligation: CommentFulfillmentObligation,
    *,
    plan: ChannelCommentPlanContract,
    daily_cap: int,
    at: datetime,
) -> _AllocationCandidate | None:
    scheduled_at = max(
        _beijing_wall(obligation.pacing_due_at),
        _beijing_wall(obligation.release_not_before_at or obligation.pacing_due_at),
        _beijing_wall(at),
    )
    if scheduled_at > _beijing_wall(plan.deadline_at):
        return None
    period = _capacity_period(session, task, daily_cap=daily_cap, at=scheduled_at)
    return _AllocationCandidate(obligation, plan, period, scheduled_at)


def _allocation_sort_key(candidate: _AllocationCandidate) -> tuple:
    return (
        candidate.period.period_start_at,
        int(candidate.obligation.target_ordinal),
        candidate.plan.deadline_at,
        candidate.plan.source_published_at,
        int(candidate.plan.channel_message_id),
        candidate.obligation.id,
    )


def _allocate(
    session: Session,
    task: Task,
    candidates: list[_AllocationCandidate],
    *,
    daily_cap: int,
    epoch: int,
) -> list[str]:
    assigned: list[str] = []
    for candidate in candidates:
        row = reserve_comment_capacity(
            session, task, candidate.obligation,
            scheduled_at=candidate.scheduled_at, daily_cap=daily_cap,
            allocation_epoch=epoch,
        )
        if row is not None:
            assigned.append(candidate.obligation.id)
    return assigned


def _release_movable_reservations(
    session: Session,
    candidates: list[_AllocationCandidate],
) -> None:
    obligation_ids = [item.obligation.id for item in candidates]
    if not obligation_ids:
        return
    rows = session.scalars(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.obligation_id.in_(obligation_ids),
        TaskCommentCapacityReservation.reservation_state == "plan_reserved",
    ))
    for row in rows:
        row.reservation_state = "released"
    session.flush()


def _latest_epoch(
    session: Session,
    task_id: str,
) -> ChannelCommentCapacityAllocationEpoch | None:
    return session.scalar(
        select(ChannelCommentCapacityAllocationEpoch)
        .where(ChannelCommentCapacityAllocationEpoch.task_id == task_id)
        .order_by(ChannelCommentCapacityAllocationEpoch.allocation_epoch.desc())
        .limit(1)
    )


def _epoch_unchanged(
    latest,
    *,
    open_hash: str,
    immutable_hash: str,
    current_hash: str,
) -> bool:
    return bool(
        latest
        and latest.open_plan_set_hash == open_hash
        and latest.immutable_usage_hash == immutable_hash
        and latest.allocation_result_hash == current_hash
    )


def _new_epoch(
    task: Task,
    candidates: list[_AllocationCandidate],
    *,
    assigned: list[str],
    epoch: int,
    open_hash: str,
    immutable_hash: str,
    at: datetime,
) -> ChannelCommentCapacityAllocationEpoch:
    horizon = [item.scheduled_at for item in candidates] or [at]
    return ChannelCommentCapacityAllocationEpoch(
        tenant_id=task.tenant_id, task_id=task.id, allocation_epoch=epoch,
        horizon_start_at=min(horizon), horizon_end_at=max(horizon),
        open_plan_set_hash=open_hash, immutable_usage_hash=immutable_hash,
        allocation_result_hash=_stable_hash(sorted(assigned)),
    )


def _open_plan_set_hash(candidates: list[_AllocationCandidate]) -> str:
    return _stable_hash([
        (
            item.plan.id, item.obligation.id, item.obligation.target_ordinal,
            item.scheduled_at, item.plan.deadline_at, item.plan.source_published_at,
        )
        for item in candidates
    ])


def _immutable_usage_hash(session: Session, task_id: str, at: datetime) -> str:
    rows = session.execute(select(
        TaskCommentCapacityReservation.id,
        TaskCommentCapacityReservation.reservation_state,
        TaskCommentCapacityReservation.scheduled_for_at,
        TaskCommentCapacityReservation.action_id,
    ).where(
        TaskCommentCapacityReservation.task_id == task_id,
        TaskCommentCapacityReservation.reservation_state.in_({
            "action_reserved", "gateway_hold", "confirmed", "plan_reserved",
        }),
    ).order_by(TaskCommentCapacityReservation.id)).all()
    immutable = [row for row in rows if row[1] != "plan_reserved" or row[2] < at]
    return _stable_hash(immutable)


def _current_allocation_hash(
    session: Session,
    candidates: list[_AllocationCandidate],
) -> str:
    obligation_ids = [item.obligation.id for item in candidates]
    if not obligation_ids:
        return _stable_hash([])
    assigned = session.scalars(select(TaskCommentCapacityReservation.obligation_id).where(
        TaskCommentCapacityReservation.obligation_id.in_(obligation_ids),
        TaskCommentCapacityReservation.reservation_state == "plan_reserved",
    ))
    return _stable_hash(sorted(assigned))


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _record_shortfall(task: Task, shortfall: int) -> None:
    stats = dict(task.stats or {})
    if shortfall:
        stats["daily_cap_unallocated"] = shortfall
    else:
        stats.pop("daily_cap_unallocated", None)
    task.stats = stats


__all__ = ["rebalance_comment_capacity_epoch"]
