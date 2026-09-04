from __future__ import annotations

from typing import Any

from app.models import Task

from ..channel_payloads import PostCommentPayload
from .channel_comment_targets import (
    reply_target_label,
    reply_target_message_id,
    reply_target_text,
)
from .common import channel_message_payload


CHANNEL_COMMENT_SCENE = "channel_comment"


def build_comment_payload(
    task: Task,
    context: Any,
    slot: Any,
    *,
    account_id: int,
) -> PostCommentPayload:
    reply_target = slot.reply_target
    slot_id = f"channel-comment:{slot.message.id}:{slot.slot_index}"
    mask = context.voice_profiles.get(account_id, {})
    assignment = slot.grounding_assignment
    source_payload = channel_message_payload(context.channel, slot.message)
    if assignment is not None:
        source_payload["message_content"] = str(assignment.evidence_text or "")
    contract_fields = {
        **source_payload,
        **_assignment_fields(assignment, slot),
        **_discussion_fields(task, slot.obligation),
        **_generation_fields(task, context, slot_id=slot_id, mask=mask),
    }
    return PostCommentPayload(
        **contract_fields,
        comment_text="",
        comment_mode="reply" if reply_target else "comment",
        reply_to_message_id=reply_target_message_id(reply_target),
        reply_target_label=reply_target_label(reply_target),
        reply_target_author=reply_target_text(reply_target, "author"),
        reply_target_preview=reply_target_text(reply_target, "preview"),
        reply_target_source=reply_target_text(reply_target, "source"),
        reply_target_content_hash=reply_target_text(reply_target, "content_hash"),
        review_approved=False,
        slot_id=slot_id,
        comment_fulfillment_obligation_id=slot.obligation.id,
        comment_plan_revision=slot.obligation.comment_plan_revision,
        target_ordinal=slot.obligation.target_ordinal,
        comment_action_attempt_no=slot.obligation.action_attempt_no + 1,
        content_mix_contract_id=slot.obligation.content_mix_contract_id or "",
        comment_fallback_intent_kind=slot.obligation.fallback_intent_kind or "emergency",
    )


def _assignment_fields(
    assignment: Any,
    slot: Any,
) -> dict:
    return {
        "source_revision_id": str(
            getattr(assignment, "source_revision_id", "") or slot.source_revision_id,
        ),
        "quality_target_revision_id": str(
            getattr(assignment, "quality_target_revision_id", "")
            or slot.quality_target_revision_id,
        ),
        "grounding_assignment_id": str(getattr(assignment, "id", "") or ""),
        "grounding_snapshot_id": str(
            getattr(assignment, "grounding_snapshot_id", "")
            or slot.grounding_snapshot_id,
        ),
        "comment_grounding_revision": int(
            getattr(assignment, "comment_grounding_revision", 0)
            or slot.comment_grounding_revision,
        ),
        "grounding_evidence_hash": str(
            getattr(assignment, "evidence_hash", "")
            or slot.grounding_evidence_hash,
        ),
        "grounding_teacher_candidate_id": str(
            getattr(assignment, "teacher_candidate_id", "") or "",
        ),
        "grounding_primary_evidence_id": str(
            getattr(assignment, "primary_evidence_id", "") or "",
        ),
        "grounding_secondary_evidence_id": str(
            getattr(assignment, "secondary_evidence_id", "") or "",
        ),
        "grounding_primary_aspect_code": str(getattr(assignment, "primary_aspect_code", "") or ""),
        "grounding_primary_aspect_text": str(getattr(assignment, "primary_aspect_text", "") or ""),
        "grounding_teacher_name": str(getattr(assignment, "teacher_name", "") or ""),
        "grounding_speech_act": str(getattr(assignment, "speech_act", "") or ""),
    }


def _discussion_fields(task: Task, obligation: Any) -> dict:
    enrollment_id = str(getattr(obligation, "grounding_enrollment_id", "") or "")
    if not enrollment_id:
        return {}
    rpc_mode = str(obligation.rpc_mode or "")
    actual_peer = (
        str(obligation.channel_peer_id)
        if rpc_mode == "channel_comment_to"
        else str(obligation.discussion_peer_id)
    )
    return {
        "grounding_enrollment_id": enrollment_id,
        "discussion_group_binding_id": str(obligation.discussion_group_binding_id or ""),
        "discussion_group_binding_revision": int(obligation.discussion_group_binding_revision or 0),
        "discussion_group_identity_hash": str(obligation.discussion_group_identity_hash or ""),
        "discussion_thread_binding_id": str(obligation.discussion_thread_binding_id or ""),
        "discussion_thread_revision": int(obligation.discussion_thread_revision or 0),
        "discussion_thread_identity_hash": str(obligation.discussion_thread_identity_hash or ""),
        "discussion_peer_id": str(obligation.discussion_peer_id or ""),
        "thread_root_message_id": int(obligation.thread_root_message_id or 0),
        "rpc_mode": rpc_mode,
        "actual_target_peer": actual_peer,
        "membership_fact_id": str(obligation.membership_fact_id or ""),
        "task_config_revision": int(obligation.task_config_revision or task.config_revision),
        "task_lifecycle_epoch": int(obligation.task_lifecycle_epoch or task.task_lifecycle_epoch),
    }


def _generation_fields(
    task: Task,
    context: Any,
    *,
    slot_id: str,
    mask: dict,
) -> dict:
    rule_version = context.rule_version
    profile = context.profile_preview
    return {
        "ai_generation_id": f"{task.id}:{slot_id}",
        "ai_generation_status": "pending",
        "comment_lifecycle_state": "pending_generation",
        "rule_set_id": rule_version.rule_set_id,
        "rule_set_name": context.rule_set.name if context.rule_set else "",
        "rule_set_version_id": rule_version.id,
        "resolved_rule_set_version_id": rule_version.id,
        "rule_set_version": rule_version.version,
        "rule_binding_mode": "fixed_version" if context.config.get("rule_set_version_id") else "follow_current",
        "profile_scene": str(profile.get("profile_scene") or CHANNEL_COMMENT_SCENE),
        "profile_version": int(profile.get("profile_version") or 0),
        "profile_hit_summary": str(profile.get("profile_hit_summary") or ""),
        "profile_unavailable_reason": str(profile.get("profile_unavailable_reason") or ""),
        "account_mask_id": str(mask.get("id") or ""),
        "account_mask_version": int(mask.get("version") or 0),
        "account_mask_snapshot_hash": str(mask.get("snapshot_hash") or ""),
        "account_mask_summary": str(mask.get("summary") or ""),
        "voice_profile_contract_version": str(mask.get("contract_version") or ""),
        "mask_status": "active" if mask.get("id") else "missing",
    }


__all__ = ["build_comment_payload"]
