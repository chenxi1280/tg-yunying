from __future__ import annotations

from collections import defaultdict
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Action,
    ChannelCommentGroundingAssignment,
    ChannelCommentPlanContract,
    ChannelCommentQualityTargetRevision,
    CommentFulfillmentObligation,
    Task,
)
from app.services._common import _now
from .channel_comment_quality_target import quality_target_projection


TERMINAL_PRIORITY = ("missed", "terminated", "blocked", "at_risk", "evaluating")


def channel_comment_acceptance(session: Session, task: Task) -> dict:
    plans = list(session.scalars(select(ChannelCommentPlanContract).where(
        ChannelCommentPlanContract.task_id == task.id,
    )))
    if not plans:
        return _empty_acceptance(task)
    obligations = _obligations(session, plans)
    actions = _actions_by_id(session, obligations)
    assignments = _assignments_by_id(session, obligations)
    targets = _quality_targets(session, plans)
    by_message = {}
    for plan in plans:
        rows = [row for row in obligations if row.plan_contract_id == plan.id]
        by_message[int(plan.channel_message_id)] = _plan_acceptance(
            plan, rows, actions, assignments=assignments,
            quality_target=targets.get(plan.current_quality_target_revision_id or ""),
        )
    return _aggregate_acceptance(by_message)


def _empty_acceptance(task: Task) -> dict:
    status = "evaluating" if task.status in {"draft", "running", "paused"} else "not_applicable"
    return {
        "acceptance_status": status,
        "quantity_status": status,
        "content_mix_status": status,
        "grounding_quality_status": status,
        "message_acceptance": {},
    }


def _obligations(
    session: Session,
    plans: list[ChannelCommentPlanContract],
) -> list[CommentFulfillmentObligation]:
    plan_ids = [plan.id for plan in plans]
    return list(session.scalars(select(CommentFulfillmentObligation).where(
        CommentFulfillmentObligation.plan_contract_id.in_(plan_ids),
    )))


def _actions_by_id(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> dict[str, Action]:
    action_ids = [row.current_action_id for row in obligations if row.current_action_id]
    if not action_ids:
        return {}
    return {
        action.id: action
        for action in session.scalars(select(Action).where(Action.id.in_(action_ids)))
    }


def _assignments_by_id(
    session: Session,
    obligations: list[CommentFulfillmentObligation],
) -> dict[str, ChannelCommentGroundingAssignment]:
    ids = [row.grounding_assignment_id for row in obligations if row.grounding_assignment_id]
    if not ids:
        return {}
    return {
        row.id: row
        for row in session.scalars(select(ChannelCommentGroundingAssignment).where(
            ChannelCommentGroundingAssignment.id.in_(ids),
        ))
    }


def _quality_targets(
    session: Session,
    plans: list[ChannelCommentPlanContract],
) -> dict[str, ChannelCommentQualityTargetRevision]:
    ids = [row.current_quality_target_revision_id for row in plans if row.current_quality_target_revision_id]
    if not ids:
        return {}
    return {
        row.id: row
        for row in session.scalars(select(ChannelCommentQualityTargetRevision).where(
            ChannelCommentQualityTargetRevision.id.in_(ids),
        ))
    }


def _plan_acceptance(
    plan: ChannelCommentPlanContract,
    obligations: list[CommentFulfillmentObligation],
    actions: dict[str, Action],
    *,
    assignments: dict[str, ChannelCommentGroundingAssignment],
    quality_target: ChannelCommentQualityTargetRevision | None,
) -> dict:
    confirmed = _distinct_confirmed(obligations)
    content = _content_counts(confirmed, actions)
    grounding = _grounding_counts(confirmed, actions, assignments)
    target_projection = _target_projection(plan, quality_target)
    target_projection = _assignment_projection(
        target_projection, quality_target, obligations, assignments,
    )
    deadline_passed = _deadline_passed(plan.deadline_at)
    target = int(plan.required_distinct_account_count)
    if plan.contract_state in {"terminated_by_operator", "terminated_source_deleted"}:
        quantity_status = content_status = grounding_status = "terminated"
    else:
        quantity_status = _target_status(
            len(confirmed), target, deadline_passed=deadline_passed,
        )
        content_status = _content_status(
            plan, content, target_projection=target_projection,
            confirmed=len(confirmed), deadline_passed=deadline_passed,
        )
        grounding_status = _grounding_status(
            grounding, target_projection=target_projection,
            content=content, deadline_passed=deadline_passed,
        )
    return {
        "acceptance_status": _combined_status(
            quantity_status, content_status, grounding_status,
        ),
        "quantity_status": quantity_status,
        "content_mix_status": content_status,
        "grounding_quality_status": grounding_status,
        "quantity_target_count": target,
        "quantity_confirmed_count": len(confirmed),
        "planned_fallback_confirmed_count": content["planned_fallback"],
        "unplanned_fallback_confirmed_count": content["emergency_fallback"],
        "grounded_remote_confirmed_count": grounding["grounded"],
        "teacher_remote_covered_count": grounding["teacher_covered"],
        "primary_aspect_remote_covered_count": grounding["aspect_covered"],
        "deadline_at": plan.deadline_at,
        **target_projection,
    }


def _distinct_confirmed(
    obligations: list[CommentFulfillmentObligation],
) -> list[CommentFulfillmentObligation]:
    result = []
    seen_accounts: set[int] = set()
    for row in sorted(obligations, key=lambda item: item.target_ordinal):
        account_id = int(row.account_id or 0)
        if not row.remote_comment_id or not account_id or account_id in seen_accounts:
            continue
        result.append(row)
        seen_accounts.add(account_id)
    return result


def _content_counts(
    confirmed: list[CommentFulfillmentObligation],
    actions: dict[str, Action],
) -> dict[str, int]:
    counts = defaultdict(int)
    for row in confirmed:
        action = actions.get(str(row.current_action_id or ""))
        payload = action.payload if action and isinstance(action.payload, dict) else {}
        source = str(payload.get("content_source") or "")
        if source in {
            "comment_unicode_emoji_fallback", "comment_image_meme_fallback",
        }:
            counts[f"{row.fallback_intent_kind}_fallback"] += 1
        elif source == "normal":
            counts["normal"] += 1
        else:
            counts["unproven"] += 1
    return counts


def _grounding_counts(
    confirmed: list[CommentFulfillmentObligation],
    actions: dict[str, Action],
    assignments: dict[str, ChannelCommentGroundingAssignment],
) -> dict[str, int]:
    teachers: set[str] = set()
    aspects: set[str] = set()
    grounded = 0
    for row in confirmed:
        action = actions.get(str(row.current_action_id or ""))
        assignment = assignments.get(str(row.grounding_assignment_id or ""))
        if not _grounded_action(action, assignment):
            continue
        grounded += 1
        text = str((action.payload or {}).get("comment_text") or "")
        if assignment.teacher_name and assignment.teacher_name in text:
            teachers.add(assignment.teacher_name)
        if _aspect_realized(text, assignment.primary_aspect_text):
            aspects.add(assignment.primary_aspect_code)
    return {
        "grounded": grounded,
        "teacher_covered": len(teachers),
        "aspect_covered": len(aspects),
    }


def _grounded_action(
    action: Action | None,
    assignment: ChannelCommentGroundingAssignment | None,
) -> bool:
    if action is None or assignment is None:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    audit = dict((action.result or {}).get("comment_quality_audit") or {})
    semantic = dict(audit.get("two_stage_evaluator_evidence") or {}).get("semantic_review")
    return bool(
        payload.get("content_source") == "normal"
        and payload.get("grounding_assignment_id") == assignment.id
        and payload.get("grounding_evidence_hash") == assignment.evidence_hash
        and payload.get("quality_target_revision_id") == assignment.quality_target_revision_id
        and isinstance(semantic, dict)
        and semantic.get("decision") == "pass"
    )


def _aspect_realized(content: str, aspect_text: str) -> bool:
    terms = [item.strip() for item in aspect_text.split("/") if len(item.strip()) >= 2]
    return bool(terms and any(term in content for term in terms))


def _deadline_passed(deadline: datetime) -> bool:
    return deadline.replace(tzinfo=None) <= _now().replace(tzinfo=None)


def _target_status(confirmed: int, target: int, *, deadline_passed: bool) -> str:
    if confirmed >= target:
        return "met"
    return "missed" if deadline_passed else "at_risk"


def _content_status(
    plan: ChannelCommentPlanContract,
    content: dict[str, int],
    *,
    target_projection: dict,
    confirmed: int,
    deadline_passed: bool,
) -> str:
    if content["emergency_fallback"]:
        return "missed" if deadline_passed else "blocked"
    complete = bool(
        confirmed >= int(plan.required_distinct_account_count)
        and content["planned_fallback"] >= int(
            target_projection["planned_fallback_target_count"],
        )
        and not content["unproven"]
    )
    return _target_status(int(complete), 1, deadline_passed=deadline_passed)


def _grounding_status(
    grounding: dict[str, int],
    *,
    target_projection: dict,
    content: dict[str, int],
    deadline_passed: bool,
) -> str:
    if target_projection["quality_target_revision_state"] != "frozen":
        return "missed" if deadline_passed else "blocked"
    if target_projection["quality_target_unassigned_ordinal_count"]:
        return "missed" if deadline_passed else "blocked"
    required = int(target_projection["grounding_required_count"])
    if content["emergency_fallback"]:
        return "missed" if deadline_passed else "blocked"
    complete = bool(
        grounding["grounded"] >= required
        and content["planned_fallback"] >= int(
            target_projection["planned_fallback_target_count"],
        )
        and grounding["teacher_covered"] >= int(target_projection["teacher_required_count"])
        and grounding["aspect_covered"] >= int(
            target_projection["primary_aspect_required_count"],
        )
    )
    return _target_status(int(complete), 1, deadline_passed=deadline_passed)


def _combined_status(*statuses: str) -> str:
    if all(status in {"met", "not_applicable"} for status in statuses):
        return "met"
    for status in TERMINAL_PRIORITY:
        if status in statuses:
            return status
    return "evaluating"


def _aggregate_acceptance(by_message: dict[int, dict]) -> dict:
    rows = list(by_message.values())
    keys = (
        "quantity_target_count", "quantity_confirmed_count",
        "planned_fallback_target_count", "planned_fallback_confirmed_count",
        "unplanned_fallback_confirmed_count", "grounding_required_count",
        "grounded_remote_confirmed_count", "teacher_required_count",
        "teacher_remote_covered_count", "primary_aspect_required_count",
        "primary_aspect_remote_covered_count",
        "applicable_grounding_ordinal_count", "unadjusted_grounding_target_count",
        "groundable_capacity_count", "quality_target_unassigned_ordinal_count",
        "quality_target_not_applicable_ordinal_count",
        "quality_target_shortfall_ordinal_count",
    )
    result = {key: sum(int(row.get(key) or 0) for row in rows) for key in keys}
    for dimension in (
        "quantity_status", "content_mix_status", "grounding_quality_status",
    ):
        result[dimension] = _combined_status(*(row[dimension] for row in rows))
    result["acceptance_status"] = _combined_status(
        result["quantity_status"],
        result["content_mix_status"],
        result["grounding_quality_status"],
    )
    result["message_acceptance"] = by_message
    result.update(_aggregate_quality_revision(rows))
    return result


def _target_projection(
    plan: ChannelCommentPlanContract,
    target: ChannelCommentQualityTargetRevision | None,
) -> dict:
    if target is not None:
        return quality_target_projection(target)
    return {
        "quality_target_current_revision": 0,
        "quality_target_effective_revision": 0,
        "quality_target_revision_state": "missing",
        "quality_target_component_count": 0,
        "quality_target_component_set_hash": "",
        "quality_target_unassigned_ordinal_count": int(plan.required_distinct_account_count),
        "quality_target_not_applicable_ordinal_count": 0,
        "quality_target_shortfall_ordinal_count": int(plan.required_distinct_account_count),
        "applicable_grounding_ordinal_count": int(plan.required_distinct_account_count),
        "unadjusted_grounding_target_count": int(plan.grounding_required_count),
        "groundable_capacity_count": 0,
        "grounding_required_count": int(plan.grounding_required_count),
        "planned_fallback_target_count": int(plan.planned_fallback_count),
        "teacher_required_count": 0,
        "primary_aspect_required_count": 0,
        "semantic_capacity_state": "unproven",
    }


def _assignment_projection(
    projection: dict,
    target: ChannelCommentQualityTargetRevision | None,
    obligations: list[CommentFulfillmentObligation],
    assignments: dict[str, ChannelCommentGroundingAssignment],
) -> dict:
    if target is None:
        return projection
    required = {
        int(value)
        for component in target.component_targets_json
        for value in component.get("grounding_ordinal_ids", [])
    }
    assigned = {
        int(row.target_ordinal)
        for row in obligations
        if (
            (assignment := assignments.get(str(row.grounding_assignment_id or "")))
            and assignment.assignment_state == "active"
        )
    }
    missing = len(required - assigned)
    return {
        **projection,
        "quality_target_unassigned_ordinal_count": (
            int(projection["quality_target_unassigned_ordinal_count"]) + missing
        ),
        "quality_target_not_applicable_ordinal_count": 0,
        "quality_target_shortfall_ordinal_count": missing,
    }


def _aggregate_quality_revision(rows: list[dict]) -> dict:
    revisions = {int(row["quality_target_current_revision"]) for row in rows}
    states = {str(row["quality_target_revision_state"]) for row in rows}
    capacity_states = {str(row["semantic_capacity_state"]) for row in rows}
    revision = max(revisions, default=0)
    return {
        "quality_target_current_revision": revision,
        "quality_target_effective_revision": revision,
        "quality_target_revision_state": states.pop() if len(states) == 1 else "mixed",
        "semantic_capacity_state": (
            capacity_states.pop() if len(capacity_states) == 1 else "mixed"
        ),
    }


__all__ = ["channel_comment_acceptance"]
