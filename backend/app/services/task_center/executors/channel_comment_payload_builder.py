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
    return PostCommentPayload(
        **source_payload,
        **_assignment_fields(
            assignment, slot.source_revision_id, slot.quality_target_revision_id,
        ),
        **_generation_fields(
            task, context, slot_id=slot_id, mask=mask,
        ),
        comment_text="",
        comment_mode="reply" if reply_target else "comment",
        reply_to_message_id=reply_target_message_id(reply_target),
        reply_target_label=reply_target_label(reply_target),
        reply_target_author=reply_target_text(reply_target, "author"),
        reply_target_preview=reply_target_text(reply_target, "preview"),
        reply_target_source=reply_target_text(reply_target, "source"),
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
    source_revision_id: str,
    quality_target_revision_id: str,
) -> dict:
    return {
        "source_revision_id": str(
            getattr(assignment, "source_revision_id", "") or source_revision_id,
        ),
        "quality_target_revision_id": str(
            getattr(assignment, "quality_target_revision_id", "")
            or quality_target_revision_id,
        ),
        "grounding_assignment_id": str(getattr(assignment, "id", "") or ""),
        "grounding_evidence_hash": str(getattr(assignment, "evidence_hash", "") or ""),
        "grounding_primary_aspect_code": str(getattr(assignment, "primary_aspect_code", "") or ""),
        "grounding_primary_aspect_text": str(getattr(assignment, "primary_aspect_text", "") or ""),
        "grounding_teacher_name": str(getattr(assignment, "teacher_name", "") or ""),
        "grounding_speech_act": str(getattr(assignment, "speech_act", "") or ""),
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
