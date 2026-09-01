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
    ChannelCommentQualityTargetRevision,
    ChannelMessage,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
)

from .channel_comment_capacity import release_comment_capacity
from .account_pacing_release import release_action_pacing_reservation_before_gateway
from .channel_comment_quality_target import (
    append_quality_target_revision,
    quality_assignment_content,
    target_component_for_ordinal,
)
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
    obligations = _ordered_obligations(session, locked.id)
    immutable = _immutable_ordinals(session, obligations)
    target = append_quality_target_revision(
        session, locked, source, immutable_ordinals=immutable,
    )
    outcomes = _migrate_obligations(
        session, obligations, source=source, target=target, immutable=immutable,
    )
    operation = _new_operation(session, locked, source, target, outcomes)
    session.add(operation)
    session.flush()
    return operation


def _migrate_obligations(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
    *,
    source: ChannelMessageSourceRevision,
    target: ChannelCommentQualityTargetRevision,
    immutable: set[int],
) -> list[dict]:
    outcomes = []
    for obligation in obligations:
        ordinal = int(obligation.target_ordinal)
        if ordinal in immutable:
            outcomes.append({"ordinal": ordinal, "result": "identity_preserved"})
            continue
        outcomes.append(_migrate_obligation(
            session, obligation, source=source, target=target,
        ))
    return outcomes


def _migrate_obligation(
    session: Session,
    obligation: CommentFulfillmentObligation,
    *,
    source: ChannelMessageSourceRevision,
    target: ChannelCommentQualityTargetRevision,
) -> dict:
    action = _bound_action(session, obligation)
    assignment = _bound_assignment(session, obligation)
    _terminate_pre_gateway_owner(session, obligation, action)
    component = target_component_for_ordinal(target, int(obligation.target_ordinal))
    if int(obligation.target_ordinal) not in component["grounding_ordinal_ids"]:
        _supersede_assignment(session, assignment)
        obligation.grounding_assignment_id = None
        obligation.fallback_intent_kind = "planned"
        return {"ordinal": obligation.target_ordinal, "result": "planned_fallback"}
    successor = _append_successor(
        session, assignment, source=source, target=target, component=component,
        ordinal=int(obligation.target_ordinal),
    )
    obligation.grounding_assignment_id = successor.id
    obligation.fallback_intent_kind = "emergency"
    return {"ordinal": obligation.target_ordinal, "result": "successor", "id": successor.id}


def _immutable_ordinals(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> set[int]:
    return {
        int(row.target_ordinal)
        for row in obligations
        if _identity_is_immutable(session, row, _bound_action(session, row))
    }


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


def _bound_assignment(
    session: Session,
    obligation: CommentFulfillmentObligation,
) -> ChannelCommentGroundingAssignment | None:
    if not obligation.grounding_assignment_id:
        return None
    assignment = session.get(
        ChannelCommentGroundingAssignment, obligation.grounding_assignment_id,
    )
    if assignment is None:
        raise RuntimeError("channel_comment_obligation_assignment_missing")
    return assignment


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
    assignment: ChannelCommentGroundingAssignment | None,
    *,
    source: ChannelMessageSourceRevision,
    target: ChannelCommentQualityTargetRevision,
    component: dict,
    ordinal: int,
) -> ChannelCommentGroundingAssignment:
    _supersede_assignment(session, assignment)
    successor = ChannelCommentGroundingAssignment(
        tenant_id=target.tenant_id,
        plan_contract_id=target.plan_contract_id,
        source_revision_id=source.id,
        target_ordinal=ordinal,
        assignment_version=(int(assignment.assignment_version) + 1 if assignment else 1),
        supersedes_assignment_id=(assignment.id if assignment else None),
        quality_target_revision_id=target.id,
        quality_component_key=component["quality_component_key"],
        **quality_assignment_content(source, component, ordinal),
        assignment_state="active",
    )
    session.add(successor)
    session.flush()
    return successor


def _supersede_assignment(
    session: Session,
    assignment: ChannelCommentGroundingAssignment | None,
) -> None:
    if assignment is None:
        return
    assignment.assignment_state = "superseded"
    session.flush()


def _ordered_obligations(
    session: Session,
    plan_contract_id: str,
) -> list[CommentFulfillmentObligation]:
    return list(session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id == plan_contract_id,
    ).order_by(CommentFulfillmentObligation.target_ordinal)))


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
    target: ChannelCommentQualityTargetRevision,
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
        json.dumps(
            {"quality_target_revision_id": target.id, "outcomes": outcomes},
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
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
