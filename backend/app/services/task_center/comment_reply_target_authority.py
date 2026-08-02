from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, ExecutionAttempt

from .channel_payloads import PostCommentPayload


def has_authoritative_own_history_target(
    session: Session,
    action: Action,
    payload: PostCommentPayload,
) -> bool:
    if payload.reply_target_source != "own_history" or not action.task_id:
        return False
    prior_action_id = session.scalar(
        select(Action.id)
        .join(ExecutionAttempt, ExecutionAttempt.action_id == Action.id)
        .where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status == "success",
            Action.id != action.id,
            Action.payload["channel_target_id"].as_integer() == payload.channel_target_id,
            or_(
                Action.payload["channel_message_id"].as_integer() == payload.channel_message_id,
                Action.payload["message_id"].as_integer() == payload.message_id,
            ),
            ExecutionAttempt.status == "success",
            ExecutionAttempt.remote_message_id == str(payload.reply_to_message_id or ""),
        )
        .limit(1)
    )
    return prior_action_id is not None


__all__ = ["has_authoritative_own_history_target"]
