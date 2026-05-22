from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.models import Action, Task


class SendMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = ""
    group_id: int | None = None
    operation_target_id: int | None = None
    target_display: str = ""
    message_text: str = Field(min_length=1)
    original_text: str = ""
    review_approved: bool = False
    cycle_id: str = ""
    turn_index: int | None = None
    account_role: str = ""
    account_memory: str = ""
    account_profile: str = ""
    topic_thread: str = ""
    topic_plan: str = ""
    intent: str = ""
    chat_mode: str = ""
    anchor_message_ids: list[int] = Field(default_factory=list)
    semantic_cluster: str = ""
    duplicate_risk: str = ""
    hallucination_risk: str = ""
    quality_skip_reason: str = ""
    context_message_ids: list[int] = Field(default_factory=list)
    context_snapshot_message_id: int | None = None
    context_expire_after_messages: int = 0
    ai_generation_id: str = ""
    ai_generation_status: str = ""
    ai_generation_tokens: int = 0
    ai_generation_count: int = 0
    ai_generation_context_count: int = 0
    ai_generation_memory_count: int = 0
    relay_batch_id: str = ""
    relay_event_id: str = ""
    source_group_id: int | None = None
    source_operation_target_id: int | None = None
    source_info: str = ""
    source_group_title: str = ""
    source_sender_name: str = ""
    source_sender_peer_id: str = ""
    source_sender_username: str = ""
    source_sender_role: str = ""
    source_is_bot: bool = False
    source_filter_reason: str = ""
    source_remote_message_id: str = ""
    source_message_type: str = ""
    source_sent_at: datetime | None = None
    source_media_asset_ids: list[str] = Field(default_factory=list)
    waiting_source_media_asset_ids: list[str] = Field(default_factory=list)
    waiting_source_media_versions: dict[str, int] = Field(default_factory=dict)
    material_cache_wait_until: str = ""
    media_segments: list[dict[str, Any]] = Field(default_factory=list)
    album_segment_results: list[dict[str, Any]] = Field(default_factory=list)
    rule_set_id: int | None = None
    rule_set_name: str = ""
    rule_set_version_id: int | None = None
    resolved_rule_set_version_id: int | None = None
    rule_set_version: int | None = None
    rule_binding_mode: str = ""
    rule_trace: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def require_destination(self) -> "SendMessagePayload":
        if self.group_id is None and not self.chat_id.strip():
            raise ValueError("send_message action requires group_id or chat_id")
        return self


class ViewMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    channel_target_id: int | None = None
    channel_message_id: int | None = None
    message_id: int = Field(ge=1)
    target_display: str = ""
    message_content: str = ""


class LikeMessagePayload(ViewMessagePayload):
    reaction_emoji: str = Field(default="👍", min_length=1, max_length=32)


class PostCommentPayload(ViewMessagePayload):
    message_content: str = ""
    comment_text: str = Field(min_length=1)
    comment_mode: str = "comment"
    reply_to_message_id: int | None = None
    reply_target_label: str = ""
    review_approved: bool = False
    rule_set_id: int | None = None
    rule_set_name: str = ""
    rule_set_version_id: int | None = None
    resolved_rule_set_version_id: int | None = None
    rule_set_version: int | None = None
    rule_binding_mode: str = ""


class EnsureChannelMembershipPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    channel_id: str = Field(min_length=1)
    channel_target_id: int
    target_type: str = "channel"
    target_display: str = ""
    target_username: str = ""
    invite_link: str = ""
    require_send: bool = False


PAYLOAD_MODELS = {
    "ensure_channel_membership": EnsureChannelMembershipPayload,
    "ensure_target_membership": EnsureChannelMembershipPayload,
    "send_message": SendMessagePayload,
    "view_message": ViewMessagePayload,
    "like_message": LikeMessagePayload,
    "post_comment": PostCommentPayload,
}


def validate_action_payload(action_type: str, payload: dict[str, Any]) -> BaseModel:
    model = PAYLOAD_MODELS.get(action_type)
    if not model:
        raise ValueError(f"未知 action_type: {action_type}")
    return model(**(payload or {}))


def _create_action(session: Session, task: Task, action_type: str, account_id: int | None, scheduled_at: datetime, payload: BaseModel) -> Action:
    payload_data = payload.model_dump(mode="json")
    plan_batch_key = _plan_batch_key(task, scheduled_at)
    action_dedupe_key = _action_dedupe_key(task, plan_batch_key, action_type, account_id, payload_data)
    action = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type=action_type,
        account_id=account_id,
        scheduled_at=scheduled_at,
        plan_batch_key=plan_batch_key,
        action_dedupe_key=action_dedupe_key,
        status="pending",
        payload=payload_data,
        result={},
    )
    session.add(action)
    session.flush()
    return action


def _plan_batch_key(task: Task, scheduled_at: datetime) -> str:
    configured = (task.stats or {}).get("current_plan_batch_key") if isinstance(task.stats, dict) else ""
    if configured:
        return str(configured)
    slot = scheduled_at.isoformat() if hasattr(scheduled_at, "isoformat") else str(scheduled_at)
    return f"{task.id}:{slot}"


def _action_dedupe_key(task: Task, plan_batch_key: str, action_type: str, account_id: int | None, payload_data: dict[str, Any]) -> str:
    business_parts = {
        "action_type": action_type,
        "account_id": account_id,
        "payload": payload_data,
    }
    digest = hashlib.sha256(json.dumps(business_parts, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")).hexdigest()
    return f"{task.tenant_id}:{plan_batch_key}:{digest}"


def create_send_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: SendMessagePayload) -> Action:
    return _create_action(session, task, "send_message", account_id, scheduled_at, payload)


def create_membership_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: EnsureChannelMembershipPayload) -> Action:
    return _create_action(session, task, "ensure_target_membership", account_id, scheduled_at, payload)


def create_view_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: ViewMessagePayload) -> Action:
    return _create_action(session, task, "view_message", account_id, scheduled_at, payload)


def create_like_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: LikeMessagePayload) -> Action:
    return _create_action(session, task, "like_message", account_id, scheduled_at, payload)


def create_comment_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: PostCommentPayload) -> Action:
    return _create_action(session, task, "post_comment", account_id, scheduled_at, payload)


def payload_error_message(exc: ValidationError | ValueError) -> str:
    if isinstance(exc, ValidationError):
        return "; ".join(".".join(str(part) for part in error["loc"]) + ": " + error["msg"] for error in exc.errors())
    return str(exc)


__all__ = [
    "EnsureChannelMembershipPayload",
    "LikeMessagePayload",
    "PostCommentPayload",
    "SendMessagePayload",
    "ViewMessagePayload",
    "create_comment_action",
    "create_like_action",
    "create_membership_action",
    "create_send_action",
    "create_view_action",
    "payload_error_message",
    "validate_action_payload",
]
