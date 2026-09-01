from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    Task,
    TaskCommentCapacityReservation,
)

from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_capacity import (
    mark_comment_capacity_gateway_hold,
    release_comment_capacity,
)
from .channel_comment_capacity_allocation import rebalance_comment_capacity_epoch
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .datetime_compat import compare_datetimes
from .source_pacing_release import release_source_pacing_admissions_before_gateway

PAUSE_EVENT = "pause"
PAUSE_REASON = "task_paused_by_operator"
PAUSE_ACTION_REASON = "task_paused_before_gateway"


def pause_channel_comment_plans(
    session: Session,
    task: Task,
    *,
    occurred_at: datetime,
) -> list[ChannelCommentPlanLifecycleEvent]:
    if task.type != "channel_comment" or task.status != "paused":
        raise ValueError("channel_comment_pause_task_state_invalid")
    plans = list(session.scalars(
        select(ChannelCommentPlanContract.id).where(
            ChannelCommentPlanContract.task_id == task.id,
            ChannelCommentPlanContract.contract_state == "open",
        ).order_by(ChannelCommentPlanContract.id)
    ))
    if not plans:
        return []
    events = [
        _pause_plan(session, task, plan_id=plan_id, occurred_at=occurred_at)
        for plan_id in plans
    ]
    rebalance_comment_capacity_epoch(
        session, task,
        daily_cap=int((task.type_config or {}).get("daily_comment_cap") or 0),
        at=occurred_at,
    )
    return events


def _pause_plan(
    session: Session,
    task: Task,
    *,
    plan_id: str,
    occurred_at: datetime,
) -> ChannelCommentPlanLifecycleEvent:
    plan = session.scalar(
        select(ChannelCommentPlanContract)
        .where(ChannelCommentPlanContract.id == plan_id)
        .with_for_update()
    )
    if plan is None:
        raise RuntimeError("channel_comment_plan_missing_during_pause")
    evidence_hash = _event_evidence_hash(task, plan, PAUSE_EVENT)
    existing = _existing_event(
        session, task, plan=plan, evidence_hash=evidence_hash,
    )
    if existing is not None:
        return existing
    outcomes = _pause_obligations(session, plan, occurred_at=occurred_at)
    event = _new_event(
        task, plan, occurred_at=occurred_at,
        evidence_hash=evidence_hash, outcomes=outcomes,
    )
    session.add(event)
    session.flush()
    return event


def _pause_obligations(
    session: Session,
    plan: ChannelCommentPlanContract,
    *,
    occurred_at: datetime,
) -> list[dict]:
    obligations = list(session.scalars(
        select(CommentFulfillmentObligation).where(
            CommentFulfillmentObligation.plan_contract_id == plan.id,
        ).order_by(CommentFulfillmentObligation.target_ordinal)
    ))
    outcomes = []
    for obligation in obligations:
        action = _bound_action(session, obligation)
        if _identity_is_immutable(session, obligation, action):
            _hold_gateway_capacity(session, action)
            outcomes.append({"ordinal": obligation.target_ordinal, "result": "identity_preserved"})
            continue
        _pause_pre_gateway_owner(session, obligation, action)
        deadline_passed = compare_datetimes(occurred_at, plan.deadline_at) > 0
        status = "missed_task_paused" if deadline_passed else "paused_unallocated"
        obligation.status = status
        outcomes.append({"ordinal": obligation.target_ordinal, "result": status})
    return outcomes


def _identity_is_immutable(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> bool:
    if obligation.status in {"confirmed", "unknown"} or obligation.remote_comment_id:
        return True
    if obligation.remote_confirmed_at is not None:
        return True
    if action is None:
        return False
    if action.status in {"success", "unknown_after_send"}:
        return True
    return session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)) is not None


def _bound_action(
    session: Session,
    obligation: CommentFulfillmentObligation,
) -> Action | None:
    if not obligation.current_action_id:
        return None
    action = session.get(Action, obligation.current_action_id)
    if action is None:
        raise RuntimeError("channel_comment_obligation_action_missing")
    return action


def _pause_pre_gateway_owner(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> None:
    if action is not None:
        payload = PostCommentPayload.model_validate(action.payload)
        invalidate_comment_generation_jobs(
            session, action, payload, reason=PAUSE_ACTION_REASON,
        )
        release_action_pacing_reservation_before_gateway(session, action)
        release_source_pacing_admissions_before_gateway(session, action)
        action.status = "cancelled"
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {**dict(action.result or {}), "error_code": PAUSE_ACTION_REASON}
    obligation.current_action_id = None
    release_comment_capacity(session, obligation.id)


def _hold_gateway_capacity(session: Session, action: Action | None) -> None:
    if action is None:
        return
    reservation = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.action_id == action.id,
    ))
    if reservation is not None and reservation.reservation_state == "action_reserved":
        mark_comment_capacity_gateway_hold(session, action.id)


def _existing_event(
    session: Session,
    task: Task,
    *,
    plan: ChannelCommentPlanContract,
    evidence_hash: str,
) -> ChannelCommentPlanLifecycleEvent | None:
    return session.scalar(select(ChannelCommentPlanLifecycleEvent).where(
        ChannelCommentPlanLifecycleEvent.plan_contract_id == plan.id,
        ChannelCommentPlanLifecycleEvent.lifecycle_epoch == int(task.task_lifecycle_epoch or 1),
        ChannelCommentPlanLifecycleEvent.event_type == PAUSE_EVENT,
        ChannelCommentPlanLifecycleEvent.evidence_hash == evidence_hash,
    ))


def _new_event(
    task: Task,
    plan: ChannelCommentPlanContract,
    *,
    occurred_at: datetime,
    evidence_hash: str,
    outcomes: list[dict],
) -> ChannelCommentPlanLifecycleEvent:
    result_hash = _hash(outcomes)
    return ChannelCommentPlanLifecycleEvent(
        tenant_id=task.tenant_id, task_id=task.id, plan_contract_id=plan.id,
        lifecycle_epoch=int(task.task_lifecycle_epoch or 1), event_type=PAUSE_EVENT,
        occurred_at=occurred_at, task_revision=int(task.config_revision or 1),
        reason=PAUSE_REASON, evidence_hash=evidence_hash,
        event_state="completed", result_hash=result_hash,
    )


def _event_evidence_hash(
    task: Task,
    plan: ChannelCommentPlanContract,
    event_type: str,
) -> str:
    return _hash([task.id, plan.id, int(task.task_lifecycle_epoch or 1), event_type])


def _hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = ["pause_channel_comment_plans"]
