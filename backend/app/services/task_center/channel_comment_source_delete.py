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
    ChannelMessage,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    Task,
)

from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_capacity import release_comment_capacity
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .source_pacing_release import release_source_pacing_admissions_before_gateway

DELETE_REASON = "source_deleted_before_send"
DELETE_EVENT = "source_deleted"


def settle_channel_comment_source_deleted(
    session: Session,
    message: ChannelMessage,
    *,
    occurred_at: datetime,
    evidence_hash: str,
) -> list[ChannelCommentPlanLifecycleEvent]:
    _validate_evidence_hash(evidence_hash)
    plans = list(session.scalars(
        select(ChannelCommentPlanContract).where(
            ChannelCommentPlanContract.channel_message_id == message.id,
            ChannelCommentPlanContract.contract_state.in_(("open", "terminated_source_deleted")),
        ).order_by(ChannelCommentPlanContract.id)
    ))
    events = [
        _settle_plan(
            session, plan.id,
            occurred_at=occurred_at, evidence_hash=evidence_hash,
        )
        for plan in plans
    ]
    message.comment_available = False
    return events


def _settle_plan(
    session: Session,
    plan_id: str,
    *,
    occurred_at: datetime,
    evidence_hash: str,
) -> ChannelCommentPlanLifecycleEvent:
    plan = session.scalar(
        select(ChannelCommentPlanContract)
        .where(ChannelCommentPlanContract.id == plan_id)
        .with_for_update()
    )
    if plan is None:
        raise RuntimeError("channel_comment_plan_missing_during_source_delete")
    task = session.get(Task, plan.task_id)
    if task is None:
        raise RuntimeError("channel_comment_plan_task_missing")
    lifecycle_epoch = int(task.task_lifecycle_epoch or 1)
    existing = _existing_event(
        session, plan,
        lifecycle_epoch=lifecycle_epoch, evidence_hash=evidence_hash,
    )
    if existing is not None:
        return existing
    outcomes = _settle_obligations(session, plan)
    plan.contract_state = "terminated_source_deleted"
    event = _new_event(
        plan, task, occurred_at=occurred_at,
        evidence_hash=evidence_hash, outcomes=outcomes,
    )
    session.add(event)
    session.flush()
    return event


def _settle_obligations(
    session: Session,
    plan: ChannelCommentPlanContract,
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
            outcomes.append({"ordinal": obligation.target_ordinal, "result": "identity_preserved"})
            continue
        _terminate_pre_gateway_owner(session, obligation, action)
        outcomes.append({"ordinal": obligation.target_ordinal, "result": "terminated"})
    return outcomes


def _identity_is_immutable(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> bool:
    if obligation.status in {"confirmed", "unknown"} or obligation.remote_comment_id:
        return True
    if obligation.remote_confirmed_at is not None or action is None:
        return obligation.remote_confirmed_at is not None
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


def _terminate_pre_gateway_owner(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> None:
    if action is not None:
        payload = PostCommentPayload.model_validate(action.payload)
        invalidate_comment_generation_jobs(session, action, payload, reason=DELETE_REASON)
        release_action_pacing_reservation_before_gateway(session, action)
        release_source_pacing_admissions_before_gateway(session, action)
        action.status = "cancelled"
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {**dict(action.result or {}), "error_code": DELETE_REASON}
    obligation.current_action_id = None
    obligation.status = "terminated"
    release_comment_capacity(session, obligation.id)


def _existing_event(
    session: Session,
    plan: ChannelCommentPlanContract,
    *,
    lifecycle_epoch: int,
    evidence_hash: str,
) -> ChannelCommentPlanLifecycleEvent | None:
    return session.scalar(select(ChannelCommentPlanLifecycleEvent).where(
        ChannelCommentPlanLifecycleEvent.plan_contract_id == plan.id,
        ChannelCommentPlanLifecycleEvent.lifecycle_epoch == lifecycle_epoch,
        ChannelCommentPlanLifecycleEvent.event_type == DELETE_EVENT,
        ChannelCommentPlanLifecycleEvent.evidence_hash == evidence_hash,
    ))


def _new_event(
    plan: ChannelCommentPlanContract,
    task: Task,
    *,
    occurred_at: datetime,
    evidence_hash: str,
    outcomes: list[dict],
) -> ChannelCommentPlanLifecycleEvent:
    result_hash = hashlib.sha256(json.dumps(
        outcomes, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return ChannelCommentPlanLifecycleEvent(
        tenant_id=plan.tenant_id,
        task_id=plan.task_id,
        plan_contract_id=plan.id,
        lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        event_type=DELETE_EVENT,
        occurred_at=occurred_at,
        task_revision=int(task.config_revision or 1),
        reason=DELETE_REASON,
        evidence_hash=evidence_hash,
        event_state="completed",
        result_hash=result_hash,
    )


def _validate_evidence_hash(evidence_hash: str) -> None:
    if len(evidence_hash) != 64:
        raise ValueError("channel_comment_source_delete_evidence_hash_invalid")


__all__ = ["settle_channel_comment_source_deleted"]
