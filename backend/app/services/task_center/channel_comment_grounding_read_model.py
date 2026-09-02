from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentGroundingEvaluation,
    ChannelCommentGroundingSnapshot,
    ChannelMessageSourceRevision,
    CommentFulfillmentObligation,
    ExecutionAttempt,
    Task,
)
from .channel_comment_style_assignment import frozen_comment_style


def channel_comment_grounding_read_model(session: Session, task: Task) -> dict:
    if task.type != "channel_comment":
        return {}
    snapshots = list(session.scalars(
        select(ChannelCommentGroundingSnapshot)
        .where(ChannelCommentGroundingSnapshot.task_id == task.id)
        .order_by(
            ChannelCommentGroundingSnapshot.channel_message_id,
            ChannelCommentGroundingSnapshot.comment_grounding_revision,
        )
    ))
    current = _current_snapshots(snapshots)
    sources = _sources_by_id(session, current)
    obligations = list(session.scalars(
        select(CommentFulfillmentObligation)
        .where(CommentFulfillmentObligation.task_id == task.id)
        .order_by(
            CommentFulfillmentObligation.channel_message_id,
            CommentFulfillmentObligation.target_ordinal,
        )
    ))
    assignments = _assignments_by_id(session, obligations)
    actions = _actions_by_id(session, obligations)
    attempts = _attempts_by_action(session, actions)
    evaluations = _latest_evaluations(session, task.id)
    return {
        "messages": [
            _snapshot_payload(row, sources.get(row.source_revision_id))
            for row in current
        ],
        "slots": [
            _slot_payload(
                row,
                assignment=assignments.get(str(row.grounding_assignment_id or "")),
                action=actions.get(str(row.current_action_id or "")),
                attempt=attempts.get(str(row.current_action_id or "")),
                evaluations=evaluations,
            )
            for row in obligations
        ],
    }


def _current_snapshots(
    snapshots: list[ChannelCommentGroundingSnapshot],
) -> list[ChannelCommentGroundingSnapshot]:
    by_plan: dict[str, ChannelCommentGroundingSnapshot] = {}
    for row in snapshots:
        by_plan[row.comment_plan_contract_id] = row
    return sorted(
        by_plan.values(),
        key=lambda row: (row.channel_message_id, row.comment_grounding_revision),
    )


def _assignments_by_id(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> dict[str, ChannelCommentGroundingAssignment]:
    ids = {str(row.grounding_assignment_id) for row in obligations if row.grounding_assignment_id}
    if not ids:
        return {}
    rows = session.scalars(select(ChannelCommentGroundingAssignment).where(
        ChannelCommentGroundingAssignment.id.in_(ids),
    ))
    return {row.id: row for row in rows}


def _sources_by_id(
    session: Session,
    snapshots: list[ChannelCommentGroundingSnapshot],
) -> dict[str, ChannelMessageSourceRevision]:
    ids = {row.source_revision_id for row in snapshots}
    if not ids:
        return {}
    rows = session.scalars(select(ChannelMessageSourceRevision).where(
        ChannelMessageSourceRevision.id.in_(ids),
    ))
    return {row.id: row for row in rows}


def _actions_by_id(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> dict[str, Action]:
    ids = {str(row.current_action_id) for row in obligations if row.current_action_id}
    if not ids:
        return {}
    return {row.id: row for row in session.scalars(select(Action).where(Action.id.in_(ids)))}


def _attempts_by_action(
    session: Session,
    actions: dict[str, Action],
) -> dict[str, ExecutionAttempt]:
    if not actions:
        return {}
    rows = session.scalars(
        select(ExecutionAttempt)
        .where(ExecutionAttempt.action_id.in_(actions))
        .order_by(ExecutionAttempt.action_id, ExecutionAttempt.attempt_no)
    )
    return {row.action_id: row for row in rows}


def _latest_evaluations(
    session: Session,
    task_id: str,
) -> dict[str, ChannelCommentGroundingEvaluation]:
    rows = session.scalars(
        select(ChannelCommentGroundingEvaluation)
        .where(ChannelCommentGroundingEvaluation.task_id == task_id)
        .order_by(
            ChannelCommentGroundingEvaluation.action_id,
            ChannelCommentGroundingEvaluation.created_at,
            ChannelCommentGroundingEvaluation.id,
        )
    )
    latest: dict[str, ChannelCommentGroundingEvaluation] = {}
    for row in rows:
        latest[row.action_id] = row
    return latest


def _snapshot_payload(
    row: ChannelCommentGroundingSnapshot,
    source: ChannelMessageSourceRevision | None,
) -> dict:
    return {
        "snapshot_id": row.id,
        "plan_contract_id": row.comment_plan_contract_id,
        "channel_message_id": row.channel_message_id,
        "source_message_id": row.source_remote_message_id,
        "source_revision_id": row.source_revision_id,
        "comment_grounding_revision": row.comment_grounding_revision,
        "content_route": row.content_route,
        "grounding_contract_version": row.grounding_contract_version,
        "extractor_version": row.extractor_version,
        "source_content_hash": row.source_content_hash,
        "source_published_at": source.source_published_at if source else None,
        "source_observed_at": source.source_observed_at if source else None,
        "telegram_edit_date": source.telegram_edit_date if source else None,
        "source_state": row.source_state,
        "teacher_state": row.teacher_state,
        "teacher_candidates": list(row.teacher_candidates_json or []),
        "aspect_evidence": list(row.aspect_evidence_json or []),
        "evidence_blocks": list(row.evidence_blocks_json or []),
        "groundable_capacity_count": row.groundable_capacity_count,
        "created_at": row.created_at,
    }


def _slot_payload(
    obligation: CommentFulfillmentObligation,
    *,
    assignment: ChannelCommentGroundingAssignment | None,
    action: Action | None,
    attempt: ExecutionAttempt | None,
    evaluations: dict[str, ChannelCommentGroundingEvaluation],
) -> dict:
    payload = dict(action.payload or {}) if action is not None else {}
    evaluation = evaluations.get(action.id) if action is not None else None
    remote_fact = dict(
        (attempt.result_snapshot or {}).get("channel_comment_remote_fact") or {}
    ) if attempt is not None else {}
    style = _slot_style(assignment, obligation)
    return {
        "obligation_id": obligation.id,
        "action_id": action.id if action is not None else "",
        "channel_message_id": obligation.channel_message_id,
        "target_ordinal": obligation.target_ordinal,
        "relation_kind": obligation.relation_kind,
        "obligation_status": obligation.status,
        "action_status": action.status if action is not None else "unmaterialized",
        "lifecycle_state": _slot_lifecycle(obligation, action, payload),
        "content_source": str(payload.get("content_source") or ""),
        "grounding_snapshot_id": str(payload.get("grounding_snapshot_id") or ""),
        "grounding_assignment_id": assignment.id if assignment is not None else "",
        "assignment_version": assignment.assignment_version if assignment is not None else 0,
        "assignment_state": assignment.assignment_state if assignment is not None else "",
        "supersedes_assignment_id": (
            assignment.supersedes_assignment_id if assignment is not None else ""
        ),
        "teacher_name": assignment.teacher_name if assignment is not None else "",
        "primary_evidence_id": (
            assignment.primary_evidence_id if assignment is not None else ""
        ),
        "primary_aspect": assignment.primary_aspect_text if assignment is not None else "",
        "speech_act": assignment.speech_act if assignment is not None else "",
        "length_tier": style.length_tier if style is not None else "",
        "persona_key": style.persona_key if style is not None else "",
        "fallback_kind": str(payload.get("comment_fallback_kind") or ""),
        "accepted_content_hash": str(payload.get("accepted_content_hash") or ""),
        "fallback_content_hash": str(payload.get("fallback_content_hash") or ""),
        "outbound_content_hash": str(remote_fact.get("outbound_content_hash") or ""),
        "remote_comment_id": str(obligation.remote_comment_id or ""),
        "evaluation": _evaluation_payload(evaluation),
    }


def _slot_style(
    assignment: ChannelCommentGroundingAssignment | None,
    obligation: CommentFulfillmentObligation,
):
    if assignment is None or not assignment.grounding_snapshot_id:
        return None
    return frozen_comment_style(
        assignment.grounding_snapshot_id, obligation.target_ordinal,
    )


def _slot_lifecycle(
    obligation: CommentFulfillmentObligation,
    action: Action | None,
    payload: dict,
) -> str:
    if obligation.status == "confirmed":
        return (
            "remote_confirmed_fallback"
            if payload.get("content_source") in {
                "comment_unicode_emoji_fallback", "comment_image_meme_fallback",
            }
            else "remote_confirmed_grounded"
        )
    if action is not None:
        return str(payload.get("comment_lifecycle_state") or action.status)
    return "planned_fallback" if obligation.fallback_intent_kind == "planned" else "assignment_ready"


def _evaluation_payload(row: ChannelCommentGroundingEvaluation | None) -> dict:
    if row is None:
        return {}
    return {
        "evaluation_id": row.id,
        "generation_attempt_id": row.generation_attempt_id,
        "candidate_content_hash": row.candidate_content_hash,
        "reviewer_model": row.semantic_reviewer_model,
        "reviewer_prompt_version": row.semantic_reviewer_prompt_version,
        "claim_results": list(row.claim_results_json or []),
        "primary_aspect_result": row.primary_aspect_result,
        "reply_relation_result": row.reply_relation_result,
        "final_result": row.final_result,
        "created_at": row.created_at,
    }


__all__ = ["channel_comment_grounding_read_model"]
