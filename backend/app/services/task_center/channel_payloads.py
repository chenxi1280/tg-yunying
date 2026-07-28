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


class LikeMessagePayload(ViewMessagePayload):
    reaction_emoji: str = Field(default="👍", min_length=1, max_length=32)


class PostCommentPayload(ViewMessagePayload):
    message_content: str = ""
    comment_text: str = ""
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
    ai_generation_id: str = ""
    ai_generation_status: str = ""
    ai_generation_attempt_id: str = ""
    ai_generation_request_id: str = ""
    ai_generation_claim_owner: str = ""
    ai_generation_claim_token: str = ""
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
    planned_normal_text_emoji: str = "unresolved"
    human_quality_decision: str = ""

    @model_validator(mode="after")
    def validate_comment_state(self) -> "PostCommentPayload":
        pending_statuses = {"pending", "generating", "ai_result_persist_unknown"}
        if not self.comment_text.strip() and self.ai_generation_status not in pending_statuses:
            raise ValueError("post_comment action requires comment_text unless AI generation is pending")
        reply_meta = any(
            [self.reply_target_label, self.reply_target_author, self.reply_target_preview, self.reply_target_source]
        )
        if self.comment_mode == "reply" and not self.reply_to_message_id:
            raise ValueError("引用评论 action 缺少 reply_to_message_id")
        if reply_meta and not self.reply_to_message_id:
            raise ValueError("引用评论 action 缺少 reply_to_message_id")
        return self


__all__ = ["LikeMessagePayload", "PostCommentPayload", "ViewMessagePayload"]
