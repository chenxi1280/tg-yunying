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
)

from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_capacity import release_comment_capacity
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .source_pacing_release import release_source_pacing_admissions_before_gateway


CHANGE_EVENT = "discussion_changed"
CHANGE_REASON = "discussion_identity_changed_pre_gateway"


def fence_group_binding_change(
    session: Session,
    superseded_binding_id: str,
    successor_binding_id: str,
    *,
    occurred_at: datetime,
) -> int:
    return _fence_matching_plans(
        session,
        ChannelCommentPlanContract.discussion_group_binding_id == superseded_binding_id,
        evidence_parts=("group", superseded_binding_id, successor_binding_id),
        occurred_at=occurred_at,
    )


def fence_thread_binding_change(
    session: Session,
    superseded_thread_id: str,
    successor_thread_id: str,
    *,
    occurred_at: datetime,
) -> int:
    return _fence_matching_plans(
        session,
        ChannelCommentPlanContract.discussion_thread_binding_id == superseded_thread_id,
        evidence_parts=("thread", superseded_thread_id, successor_thread_id),
        occurred_at=occurred_at,
    )


def _fence_matching_plans(
    session: Session,
    plan_filter,
    *,
    evidence_parts: tuple[str, str, str],
    occurred_at: datetime,
) -> int:
    plan_ids = list(session.scalars(select(ChannelCommentPlanContract.id).where(
        plan_filter,
        ChannelCommentPlanContract.contract_state.in_(("open", "discussion_changed_reconcile")),
    )))
    evidence_hash = _stable_hash(evidence_parts)
    for plan_id in plan_ids:
        _fence_plan(
            session, plan_id,
            evidence_hash=evidence_hash, occurred_at=occurred_at,
        )
    return len(plan_ids)


def _fence_plan(
    session: Session,
    plan_id: str,
    *,
    evidence_hash: str,
    occurred_at: datetime,
) -> None:
    plan = session.scalar(select(ChannelCommentPlanContract).where(
        ChannelCommentPlanContract.id == plan_id,
    ).with_for_update())
    if plan is None:
        raise RuntimeError("channel_comment_discussion_change_plan_missing")
    task = session.get(Task, plan.task_id)
    if task is None:
        raise RuntimeError("channel_comment_discussion_change_task_missing")
    if _existing_event(session, plan, task, evidence_hash=evidence_hash):
        return
    outcomes = _fence_obligations(session, plan)
    plan.contract_state = (
        "discussion_changed_reconcile"
        if any(item["result"] == "identity_preserved" for item in outcomes)
        else "terminated_discussion_changed"
    )
    session.add(_change_event(
        plan, task,
        outcomes=outcomes, evidence_hash=evidence_hash, occurred_at=occurred_at,
    ))
    session.flush()


def _fence_obligations(
    session: Session,
    plan: ChannelCommentPlanContract,
) -> list[dict]:
    obligations = list(session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id == plan.id,
    ).order_by(CommentFulfillmentObligation.target_ordinal)))
    outcomes = []
    for obligation in obligations:
        action = session.get(Action, obligation.current_action_id) if obligation.current_action_id else None
        if _remote_identity_started(session, obligation, action):
            outcomes.append({"ordinal": obligation.target_ordinal, "result": "identity_preserved"})
            continue
        _terminate_pre_gateway(session, obligation, action)
        outcomes.append({"ordinal": obligation.target_ordinal, "result": "terminated"})
    return outcomes


def _remote_identity_started(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> bool:
    if obligation.status in {"confirmed", "unknown"} or obligation.remote_confirmed_at:
        return True
    if action is None:
        return False
    if action.status in {"success", "unknown_after_send"}:
        return True
    return session.scalar(select(ExecutionAttempt.id).where(
        ExecutionAttempt.action_id == action.id,
        ExecutionAttempt.gateway_call_started_at.is_not(None),
    ).limit(1)) is not None


def _terminate_pre_gateway(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> None:
    if action is not None:
        payload = PostCommentPayload.model_validate(action.payload)
        invalidate_comment_generation_jobs(session, action, payload, reason=CHANGE_REASON)
        release_action_pacing_reservation_before_gateway(session, action)
        release_source_pacing_admissions_before_gateway(session, action)
        action.status = "cancelled"
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {**dict(action.result or {}), "error_code": CHANGE_REASON}
    obligation.current_action_id = None
    obligation.status = "terminated"
    release_comment_capacity(session, obligation.id)


def _existing_event(session: Session, plan, task, *, evidence_hash: str):
    return session.scalar(select(ChannelCommentPlanLifecycleEvent).where(
        ChannelCommentPlanLifecycleEvent.plan_contract_id == plan.id,
        ChannelCommentPlanLifecycleEvent.lifecycle_epoch == task.task_lifecycle_epoch,
        ChannelCommentPlanLifecycleEvent.event_type == CHANGE_EVENT,
        ChannelCommentPlanLifecycleEvent.evidence_hash == evidence_hash,
    ))


def _change_event(plan, task, *, outcomes, evidence_hash, occurred_at):
    return ChannelCommentPlanLifecycleEvent(
        tenant_id=plan.tenant_id,
        task_id=plan.task_id,
        plan_contract_id=plan.id,
        lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        event_type=CHANGE_EVENT,
        occurred_at=occurred_at,
        task_revision=int(task.config_revision or 1),
        reason=CHANGE_REASON,
        evidence_hash=evidence_hash,
        event_state="completed",
        result_hash=_stable_hash(outcomes),
    )


def _stable_hash(value: object) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


__all__ = ["fence_group_binding_change", "fence_thread_binding_change"]
