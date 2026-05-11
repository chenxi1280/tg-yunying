from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy.orm import Session

from app.models import Action, Task


class SendMessagePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chat_id: str = ""
    group_id: int | None = None
    target_display: str = ""
    message_text: str = Field(min_length=1)
    original_text: str = ""
    review_approved: bool = False
    cycle_id: str = ""
    turn_index: int | None = None
    account_role: str = ""
    intent: str = ""
    context_message_ids: list[int] = Field(default_factory=list)
    context_snapshot_message_id: int | None = None
    context_expire_after_messages: int = 0
    relay_batch_id: str = ""
    relay_event_id: str = ""
    source_group_id: int | None = None
    source_info: str = ""
    rule_set_id: int | None = None
    rule_set_version_id: int | None = None

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


class LikeMessagePayload(ViewMessagePayload):
    reaction_emoji: str = Field(default="👍", min_length=1, max_length=32)


class PostCommentPayload(ViewMessagePayload):
    message_content: str = ""
    comment_text: str = Field(min_length=1)
    review_approved: bool = False


PAYLOAD_MODELS = {
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
    action = Action(
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type=action_type,
        account_id=account_id,
        scheduled_at=scheduled_at,
        status="pending",
        payload=payload.model_dump(mode="json"),
        result={},
    )
    session.add(action)
    session.flush()
    return action


def create_send_action(session: Session, task: Task, account_id: int | None, scheduled_at: datetime, payload: SendMessagePayload) -> Action:
    return _create_action(session, task, "send_message", account_id, scheduled_at, payload)


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
    "LikeMessagePayload",
    "PostCommentPayload",
    "SendMessagePayload",
    "ViewMessagePayload",
    "create_comment_action",
    "create_like_action",
    "create_send_action",
    "create_view_action",
    "payload_error_message",
    "validate_action_payload",
]
