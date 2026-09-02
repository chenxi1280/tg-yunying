from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentGroundingSnapshot,
    ChannelCommentPlanContract,
    ChannelCommentPlanLifecycleEvent,
    ChannelCommentQualityTargetRevision,
    ChannelMessage,
    CommentFulfillmentObligation,
    TaskCommentCapacityReservation,
)
from sqlalchemy import select

from .channel_payloads import PostCommentPayload


def comment_grounding_send_blocker(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> str:
    if _source_deleted(session, action, payload):
        return "source_deleted_before_send"
    if not payload.source_revision_id:
        return ""
    message = session.get(ChannelMessage, int(payload.channel_message_id or 0))
    if message is None or not message.comment_available:
        return "source_unavailable_before_send"
    if message.current_source_revision_id != payload.source_revision_id:
        return "source_revision_superseded_before_gateway"
    capacity_blocker = _capacity_send_blocker(session, action)
    if capacity_blocker:
        return capacity_blocker
    quality_blocker = _quality_target_blocker(session, action, payload)
    if quality_blocker:
        return quality_blocker
    if not payload.grounding_assignment_id:
        return ""
    assignment = session.get(
        ChannelCommentGroundingAssignment,
        payload.grounding_assignment_id,
    )
    valid = bool(
        assignment
        and assignment.tenant_id == action.tenant_id
        and assignment.source_revision_id == payload.source_revision_id
        and assignment.quality_target_revision_id == payload.quality_target_revision_id
        and assignment.target_ordinal == payload.target_ordinal
        and assignment.assignment_state == "active"
        and assignment.grounding_snapshot_id == payload.grounding_snapshot_id
        and assignment.comment_grounding_revision == payload.comment_grounding_revision
        and assignment.teacher_candidate_id == payload.grounding_teacher_candidate_id
        and assignment.primary_evidence_id == payload.grounding_primary_evidence_id
        and assignment.secondary_evidence_id == payload.grounding_secondary_evidence_id
    )
    if not valid:
        return "grounding_assignment_superseded"
    return _grounding_snapshot_blocker(
        session, action, payload, assignment=assignment,
    )


def _grounding_snapshot_blocker(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
    *,
    assignment: ChannelCommentGroundingAssignment,
) -> str:
    snapshot = session.get(ChannelCommentGroundingSnapshot, payload.grounding_snapshot_id)
    if not snapshot or snapshot.tenant_id != action.tenant_id:
        return "grounding_snapshot_missing"
    if (
        snapshot.source_revision_id != payload.source_revision_id
        or snapshot.comment_plan_contract_id != assignment.plan_contract_id
        or snapshot.source_content_hash != payload.grounding_evidence_hash
        or snapshot.source_state not in {"ready", "minimal"}
    ):
        return "grounding_snapshot_stale"
    evidence = next(
        (
            row for row in snapshot.aspect_evidence_json
            if row.get("evidence_id") == payload.grounding_primary_evidence_id
        ),
        None,
    )
    if evidence is None:
        return "grounding_primary_evidence_missing"
    return _temporal_evidence_blocker(evidence)


def _temporal_evidence_blocker(evidence: dict) -> str:
    valid_until = str(evidence.get("valid_until") or "")
    if not valid_until:
        return ""
    from datetime import datetime
    from app.services._common import _now

    return (
        "temporal_evidence_expired"
        if _now() > datetime.fromisoformat(valid_until)
        else ""
    )


def _quality_target_blocker(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> str:
    if not payload.quality_target_revision_id:
        return "quality_target_revision_missing"
    target = session.get(
        ChannelCommentQualityTargetRevision, payload.quality_target_revision_id,
    )
    obligation = session.get(
        CommentFulfillmentObligation,
        payload.comment_fulfillment_obligation_id,
    )
    if target is None or obligation is None or target.target_state != "frozen":
        return "quality_target_revision_invalid"
    if target.tenant_id != action.tenant_id or target.plan_contract_id != obligation.plan_contract_id:
        return "quality_target_revision_scope_invalid"
    components = [
        row for row in target.component_targets_json
        if int(payload.target_ordinal) in row.get("owned_ordinal_ids", [])
    ]
    if len(components) != 1:
        return "quality_target_component_owner_invalid"
    if payload.grounding_assignment_id:
        return ""
    planned = int(payload.target_ordinal) in components[0].get(
        "planned_fallback_ordinal_ids", [],
    )
    if payload.comment_fallback_intent_kind == "planned" and planned:
        return _planned_fallback_snapshot_blocker(
            session, action, payload, obligation=obligation, component=components[0],
        )
    return "grounding_assignment_missing"


def _planned_fallback_snapshot_blocker(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
    *,
    obligation: CommentFulfillmentObligation,
    component: dict,
) -> str:
    snapshot = session.get(ChannelCommentGroundingSnapshot, payload.grounding_snapshot_id)
    valid = bool(
        snapshot
        and snapshot.tenant_id == action.tenant_id
        and snapshot.source_revision_id == payload.source_revision_id
        and snapshot.comment_plan_contract_id == obligation.plan_contract_id
        and snapshot.comment_grounding_revision == payload.comment_grounding_revision
        and snapshot.source_content_hash == payload.grounding_evidence_hash
        and component.get("grounding_snapshot_id") == payload.grounding_snapshot_id
        and int(component.get("comment_grounding_revision") or 0)
        == payload.comment_grounding_revision
    )
    return "" if valid else "grounding_snapshot_stale"


def _source_deleted(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> bool:
    if not payload.channel_message_id:
        return False
    event_id = session.scalar(
        select(ChannelCommentPlanLifecycleEvent.id)
        .join(
            ChannelCommentPlanContract,
            ChannelCommentPlanContract.id == ChannelCommentPlanLifecycleEvent.plan_contract_id,
        )
        .where(
            ChannelCommentPlanLifecycleEvent.task_id == action.task_id,
            ChannelCommentPlanLifecycleEvent.event_type == "source_deleted",
            ChannelCommentPlanContract.channel_message_id == int(payload.channel_message_id),
        )
        .limit(1)
    )
    return event_id is not None


def _capacity_send_blocker(session: Session, action: Action) -> str:
    reservation = session.scalar(select(TaskCommentCapacityReservation).where(
        TaskCommentCapacityReservation.action_id == action.id,
    ))
    if reservation is None:
        return "comment_capacity_reservation_missing"
    if reservation.reservation_state != "action_reserved":
        return "comment_capacity_reservation_not_action_reserved"
    return ""


__all__ = ["comment_grounding_send_blocker"]
