from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ViewMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    channel_target_id: int | None = None
    target_reference_revision: int | None = None
    target_reference_snapshot: dict[str, str] = Field(default_factory=dict)
    channel_message_id: int | None = None
    message_id: int = Field(ge=1)
    target_display: str = ""
    message_content: str = ""
    execution_date: str = ""
    daily_view_target: int | None = None
    total_view_target: int | None = None
    task_day_ledger_id: str = ""
    view_fulfillment_obligation_id: str = ""


class LikeMessagePayload(ViewMessagePayload):
    reaction_emoji: str = Field(default="👍", min_length=1, max_length=32)
    reaction_contract_version: int = 0
    reaction_fulfillment_obligation_id: str = ""


class PostCommentPayload(ViewMessagePayload):
    message_content: str = ""
    comment_text: str = ""
    accepted_content_text: str = ""
    accepted_content_hash: str = ""
    fallback_content_text: str = ""
    fallback_content_hash: str = ""
    quality_contract_version: str = ""
    comment_mode: str = "comment"
    reply_to_message_id: int | None = None
    reply_target_label: str = ""
    reply_target_author: str = ""
    reply_target_preview: str = ""
    reply_target_source: str = ""
    review_approved: bool = False
    slot_id: str = ""
    comment_fulfillment_obligation_id: str = ""
    comment_plan_revision: int = 0
    target_ordinal: int = 0
    comment_action_attempt_no: int = 0
    content_mix_contract_id: str = ""
    source_revision_id: str = ""
    quality_target_revision_id: str = ""
    grounding_assignment_id: str = ""
    grounding_snapshot_id: str = ""
    comment_grounding_revision: int = 0
    grounding_evidence_hash: str = ""
    grounding_teacher_candidate_id: str = ""
    grounding_primary_evidence_id: str = ""
    grounding_secondary_evidence_id: str = ""
    grounding_primary_aspect_code: str = ""
    grounding_primary_aspect_text: str = ""
    grounding_teacher_name: str = ""
    grounding_speech_act: str = ""
    grounding_enrollment_id: str = ""
    discussion_group_binding_id: str = ""
    discussion_group_binding_revision: int = 0
    discussion_group_identity_hash: str = ""
    discussion_thread_binding_id: str = ""
    discussion_thread_revision: int = 0
    discussion_thread_identity_hash: str = ""
    discussion_peer_id: str = ""
    thread_root_message_id: int = 0
    rpc_mode: str = ""
    actual_target_peer: str = ""
    membership_fact_id: str = ""
    task_config_revision: int = 0
    task_lifecycle_epoch: int = 0
    ai_generation_id: str = ""
    ai_generation_status: str = ""
    comment_lifecycle_state: str = "unresolved"
    ai_generation_attempt_id: str = ""
    ai_generation_request_id: str = ""
    ai_generation_claim_owner: str = ""
    ai_generation_claim_token: str = ""
    generation_job_id: str = ""
    ai_generation_attempt_history: list[dict[str, Any]] = Field(default_factory=list)
    comment_generation_attempts: list[dict[str, Any]] = Field(default_factory=list)
    ai_generation_result_cache: dict[str, Any] = Field(default_factory=dict)
    ai_generation_tokens: int = 0
    rule_set_id: int | None = None
    rule_set_name: str = ""
    rule_set_version_id: int | None = None
    resolved_rule_set_version_id: int | None = None
    rule_set_version: int | None = None
    rule_binding_mode: str = ""
    profile_scene: str = ""
    profile_version: int = 0
    profile_hit_summary: str = ""
    profile_unavailable_reason: str = ""
    account_mask_id: str = ""
    account_mask_version: int = 0
    account_mask_snapshot_hash: str = ""
    account_mask_summary: str = ""
    voice_profile_contract_version: str = ""
    mask_status: str = ""
    # Humanized interaction / speaker rotation audit fields
    conversation_surface: str = ""
    conversation_key: str = ""
    speaker_selection_reason: str = ""
    content_source: str = ""
    quality_fallback: str = ""
    fallback_reason: str = ""
    deterministic_fallback_reason: str = ""
    comment_fallback_kind: str = ""
    comment_fallback_intent_kind: str = "emergency"
    comment_media_segment: dict[str, Any] = Field(default_factory=dict)
    comment_fallback_selection: dict[str, Any] = Field(default_factory=dict)
    planned_normal_text_emoji: str = "unresolved"
    human_quality_decision: str = ""

    @model_validator(mode="after")
    def validate_comment_state(self) -> "PostCommentPayload":
        if self.comment_fallback_intent_kind not in {"planned", "emergency"}:
            raise ValueError("comment_fallback_intent_kind_invalid")
        pending_statuses = {
            "pending", "generating", "ai_result_persist_unknown", "provider_result_unknown",
        }
        ready_content = self.comment_text.strip() or self.comment_media_segment
        if not ready_content and self.ai_generation_status not in pending_statuses:
            raise ValueError("post_comment action requires comment_text unless AI generation is pending")
        reply_meta = any(
            [self.reply_target_label, self.reply_target_author, self.reply_target_preview, self.reply_target_source]
        )
        if self.comment_mode == "reply" and not self.reply_to_message_id:
            raise ValueError("引用评论 action 缺少 reply_to_message_id")
        if reply_meta and not self.reply_to_message_id:
            raise ValueError("引用评论 action 缺少 reply_to_message_id")
        self._validate_discussion_identity()
        self._validate_grounding_lifecycle()
        return self

    def _validate_discussion_identity(self) -> None:
        if not self.grounding_enrollment_id:
            return
        required = (
            self.discussion_group_binding_id,
            self.discussion_group_identity_hash,
            self.discussion_thread_binding_id,
            self.discussion_thread_identity_hash,
            self.discussion_peer_id,
            self.actual_target_peer,
            self.membership_fact_id,
        )
        if not all(required) or not self.thread_root_message_id:
            raise ValueError("channel_comment_discussion_identity_incomplete")
        if self.rpc_mode == "channel_comment_to" and self.reply_to_message_id:
            raise ValueError("channel_comment_rpc_identity_conflict")
        if self.rpc_mode == "discussion_reply_to" and not self.reply_to_message_id:
            raise ValueError("channel_comment_reply_identity_missing")
        if self.rpc_mode not in {"channel_comment_to", "discussion_reply_to"}:
            raise ValueError("channel_comment_rpc_mode_invalid")

    def _validate_grounding_lifecycle(self) -> None:
        if not self.grounding_enrollment_id:
            return
        if not (
            self.grounding_snapshot_id
            and self.comment_grounding_revision > 0
            and self.grounding_evidence_hash
        ):
            raise ValueError("channel_comment_grounding_snapshot_identity_incomplete")
        ready_states = {"quality_accepted", "fallback_ready"}
        waiting_states = {
            "pending_generation", "generation_claimed", "provider_result_unknown",
            "generation_result_persist_unknown", "quality_wait", "reply_quality_shortfall",
        }
        if self.ai_generation_status == "ready":
            if self.comment_lifecycle_state not in ready_states:
                raise ValueError("channel_comment_generation_lifecycle_invalid")
            return
        if self.comment_lifecycle_state not in waiting_states:
            raise ValueError("channel_comment_generation_lifecycle_invalid")


__all__ = ["LikeMessagePayload", "PostCommentPayload", "ViewMessagePayload"]
