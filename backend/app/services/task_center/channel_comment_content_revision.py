from __future__ import annotations

import hashlib
import json
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentContentRevisionOperation,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
)

from .channel_comment_capacity import release_comment_capacity
from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_plan_contract import grounding_assignment_content
from .channel_payloads import PostCommentPayload
from .comment_generation_job import invalidate_comment_generation_jobs
from .target_lifecycle import action_has_gateway_started


def reconcile_channel_comment_source_edit(
    session: Session,
    message: ChannelMessage,
    source: ChannelMessageSourceRevision,
    *,
    at: datetime,
) -> list[ChannelCommentContentRevisionOperation]:
    if source.channel_message_id != message.id or source.tenant_id != message.tenant_id:
        raise ValueError("channel_comment_source_revision_scope_mismatch")
    plans = list(session.scalars(
        select(ChannelCommentPlanContract).where(
            ChannelCommentPlanContract.channel_message_id == message.id,
            ChannelCommentPlanContract.contract_state == "open",
            ChannelCommentPlanContract.deadline_at >= at,
        ).order_by(ChannelCommentPlanContract.id)
    ))
    return [_reconcile_plan(session, plan, source) for plan in plans]


def _reconcile_plan(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
) -> ChannelCommentContentRevisionOperation:
    locked = session.scalar(
        select(ChannelCommentPlanContract)
        .where(ChannelCommentPlanContract.id == plan.id)
        .with_for_update()
    )
    if locked is None:
        raise RuntimeError("channel_comment_plan_missing_during_source_edit")
    existing = _existing_operation(session, locked.id, source.id)
    if existing is not None:
        return existing
    outcomes = _migrate_assignments(session, locked, source)
    operation = _new_operation(session, locked, source, outcomes)
    session.add(operation)
    session.flush()
    return operation


def _migrate_assignments(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
) -> list[dict]:
    assignments = list(session.scalars(
        select(ChannelCommentGroundingAssignment).where(
            ChannelCommentGroundingAssignment.plan_contract_id == plan.id,
            ChannelCommentGroundingAssignment.assignment_state == "active",
            ChannelCommentGroundingAssignment.source_revision_id != source.id,
        ).order_by(ChannelCommentGroundingAssignment.target_ordinal)
    ))
    obligations = _obligations_by_ordinal(session, plan.id)
    outcomes = []
    for assignment in assignments:
        obligation = obligations.get(int(assignment.target_ordinal))
        if obligation is None:
            raise RuntimeError("channel_comment_assignment_obligation_missing")
        outcome = _migrate_assignment(session, assignment, obligation, source)
        outcomes.append(outcome)
    return outcomes


def _migrate_assignment(
    session: Session,
    assignment: ChannelCommentGroundingAssignment,
    obligation: CommentFulfillmentObligation,
    source: ChannelMessageSourceRevision,
) -> dict:
    action = _bound_action(session, obligation)
    if _identity_is_immutable(session, obligation, action):
        return {"ordinal": assignment.target_ordinal, "result": "identity_preserved"}
    _terminate_pre_gateway_owner(session, obligation, action)
    successor = _append_successor(session, assignment, source)
    obligation.grounding_assignment_id = successor.id
    return {"ordinal": assignment.target_ordinal, "result": "successor", "id": successor.id}


def _identity_is_immutable(
    session: Session,
    obligation: CommentFulfillmentObligation,
    action: Action | None,
) -> bool:
    if (
        obligation.status == "confirmed"
        or obligation.remote_comment_id
        or obligation.remote_confirmed_at
    ):
        return True
    if action is None:
        return False
    return action.status == "success" or action_has_gateway_started(session, action)


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
        invalidate_comment_generation_jobs(
            session, action, payload,
            reason="source_revision_superseded_before_gateway",
        )
        action.status = "cancelled"
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {
            **dict(action.result or {}),
            "error_code": "source_revision_superseded_before_gateway",
        }
        release_action_pacing_reservation_before_gateway(session, action)
    obligation.current_action_id = None
    if obligation.status != "paused_unallocated":
        obligation.status = "replan_required"
    release_comment_capacity(session, obligation.id)


def _append_successor(
    session: Session,
    assignment: ChannelCommentGroundingAssignment,
    source: ChannelMessageSourceRevision,
) -> ChannelCommentGroundingAssignment:
    assignment.assignment_state = "superseded"
    session.flush()
    successor = ChannelCommentGroundingAssignment(
        tenant_id=assignment.tenant_id,
        plan_contract_id=assignment.plan_contract_id,
        source_revision_id=source.id,
        target_ordinal=assignment.target_ordinal,
        assignment_version=int(assignment.assignment_version) + 1,
        supersedes_assignment_id=assignment.id,
        **grounding_assignment_content(source, int(assignment.target_ordinal)),
        assignment_state="active",
    )
    session.add(successor)
    session.flush()
    return successor


def _obligations_by_ordinal(
    session: Session,
    plan_contract_id: str,
) -> dict[int, CommentFulfillmentObligation]:
    rows = session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id == plan_contract_id,
    ))
    return {int(row.target_ordinal): row for row in rows}


def _existing_operation(
    session: Session,
    plan_contract_id: str,
    source_revision_id: str,
) -> ChannelCommentContentRevisionOperation | None:
    return session.scalar(select(ChannelCommentContentRevisionOperation).where(
        ChannelCommentContentRevisionOperation.plan_contract_id == plan_contract_id,
        ChannelCommentContentRevisionOperation.to_source_revision_id == source_revision_id,
    ))


def _new_operation(
    session: Session,
    plan: ChannelCommentPlanContract,
    source: ChannelMessageSourceRevision,
    outcomes: list[dict],
) -> ChannelCommentContentRevisionOperation:
    previous = session.scalar(select(ChannelMessageSourceRevision).where(
        ChannelMessageSourceRevision.channel_message_id == source.channel_message_id,
        ChannelMessageSourceRevision.source_revision == int(source.source_revision) - 1,
    ))
    version = session.scalar(select(func.count(ChannelCommentContentRevisionOperation.id)).where(
        ChannelCommentContentRevisionOperation.plan_contract_id == plan.id,
    ))
    result_hash = hashlib.sha256(
        json.dumps(outcomes, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return ChannelCommentContentRevisionOperation(
        tenant_id=plan.tenant_id,
        task_id=plan.task_id,
        plan_contract_id=plan.id,
        from_source_revision_id=(previous.id if previous else plan.source_revision_id),
        to_source_revision_id=source.id,
        operation_version=int(version or 0) + 1,
        operation_state="completed",
        result_hash=result_hash,
    )


__all__ = ["reconcile_channel_comment_source_edit"]
