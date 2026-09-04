from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    ChannelDiscussionGroupBinding,
    ChannelDiscussionThreadBinding,
    ChannelMessage,
    ChannelMessageComment,
    ChannelMessageSourceRevision,
    Task,
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateEvent,
    TelegramAuthorizationUpdateState,
    TelegramAuthorizationUpdateSubscription,
)
from app.services._common import _now
from app.timezone import as_beijing

from .engagement_update_subscriptions import ensure_task_peer_update_subscription
from .engagement_unowned_activity import observe_managed_outbound
from .channel_comment_realtime import (
    promote_human_comment_response,
    refresh_human_comment_response,
    release_deleted_human_response,
)
from .planner_wake import wake_task_planner
from .negative_outcome_events import observe_human_negative_reply


UNIFIED_ENGAGEMENT_CONTRACT = "unified_engagement_v1"
MESSAGE_EVENT_TYPES = frozenset({"message_new"})
DELETE_EVENT_TYPE = "message_delete"


def ensure_channel_comment_update_subscription(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    listener_account_id: int,
) -> bool:
    if not _uses_update_stream(task) or not _active_binding(binding):
        return False
    result = ensure_task_peer_update_subscription(
        session,
        task,
        listener_account_id=listener_account_id,
        source_peer_id=str(binding.discussion_peer_id),
    )
    task.stats = {
        **dict(task.stats or {}),
        "comment_update_stream_state": result.state.state if result.state else "blocked",
        "comment_update_stream_error": result.error,
        "comment_update_stream_route_changed": bool(result.route_changed),
        "comment_update_stream_listener_account_id": int(listener_account_id),
    }
    return result.ready


def consume_channel_comment_update_deliveries(
    session: Session,
    task: Task,
    binding: ChannelDiscussionGroupBinding,
    *,
    limit: int = 200,
) -> int:
    if not _uses_update_stream(task) or not _active_binding(binding):
        return 0
    rows = session.execute(_delivery_statement(task, limit)).all()
    changed = 0
    for delivery, event, state in rows:
        payload = dict(delivery.normalized_payload or {})
        if _apply_delivery(
            session,
            task,
            binding=binding,
            state=state,
            payload=payload,
            source_event_id=event.id,
        ):
            changed += 1
            wake_task_planner(
                session,
                task,
                reason_code="discussion_comment_update",
                not_before_at=_now(),
            )
        delivery.delivery_state = "consumed"
        _project_delivery(task, event)
    return changed


def _apply_delivery(
    session,
    task,
    *,
    binding,
    state,
    payload,
    source_event_id,
) -> bool:
    event_type = str(payload.get("event_type") or "")
    if event_type == DELETE_EVENT_TYPE:
        return _delete_comment(session, task, binding=binding, payload=payload)
    if event_type not in MESSAGE_EVENT_TYPES:
        return False
    if _managed_or_bot_sender(
        session,
        task,
        binding=binding,
        payload=payload,
        source_event_id=source_event_id,
    ):
        return False
    source = _source_for_payload(session, task, binding=binding, payload=payload)
    if source is None:
        return False
    content = str(payload.get("content") or "").strip()
    if not content:
        return False
    observe_human_negative_reply(session, task, peer_id=str(binding.discussion_peer_id), payload=payload)
    return _upsert_comment(
        session,
        task,
        source,
        listener_account_id=state.account_id,
        payload=payload,
        content=content,
    )


def _source_for_payload(session, task, *, binding, payload):
    root_ids = {
        int(value)
        for value in (
            payload.get("source_top_message_id"),
            payload.get("reply_to_message_id"),
        )
        if str(value or "").isdigit() and int(value) > 0
    }
    if not root_ids:
        return None
    target_id = int((task.type_config or {}).get("target_channel_id") or 0)
    return session.execute(
        select(ChannelMessage, ChannelDiscussionThreadBinding)
        .join(
            ChannelMessageSourceRevision,
            ChannelMessageSourceRevision.id
            == ChannelDiscussionThreadBinding.source_revision_id,
        )
        .join(
            ChannelMessage,
            ChannelMessage.current_source_revision_id
            == ChannelMessageSourceRevision.id,
        )
        .where(
            ChannelDiscussionThreadBinding.group_binding_id == binding.id,
            ChannelDiscussionThreadBinding.discussion_peer_id
            == binding.discussion_peer_id,
            ChannelDiscussionThreadBinding.thread_root_message_id.in_(root_ids),
            ChannelDiscussionThreadBinding.is_current.is_(True),
            ChannelMessage.channel_target_id == target_id,
        )
        .limit(1)
    ).first()


def _upsert_comment(
    session,
    task,
    source,
    *,
    listener_account_id,
    payload,
    content,
) -> bool:
    message, thread = source
    remote_id = int(payload.get("source_message_id") or 0)
    if remote_id <= 0 or remote_id == thread.thread_root_message_id:
        return False
    existing = _locked_comment(
        session, task, message=message, thread=thread, remote_id=remote_id,
    )
    parent_id = _comment_parent_id(payload, thread.thread_root_message_id)
    created = existing is None
    if created:
        existing = _new_comment(
            task, message, thread=thread, remote_id=remote_id,
        )
        session.add(existing)
        changed = True
    else:
        changed = existing.content_preview != content or existing.parent_comment_message_id != parent_id
    existing.parent_comment_message_id = parent_id
    existing.author_peer_id = str(payload.get("sender_peer_id") or "")
    existing.author_name = str(payload.get("sender_name") or "真人用户")
    existing.is_bot = False
    existing.content_preview = content
    existing.published_at = _sent_at(payload.get("sent_at"))
    session.flush()
    _apply_comment_intent(
        session, task, message,
        thread=thread, comment=existing, parent_id=parent_id,
        created=created, changed=changed,
    )
    _record_listener_account(task, listener_account_id)
    return changed


def _apply_comment_intent(
    session, task, message, *, thread, comment, parent_id, created, changed,
) -> None:
    if parent_id:
        _mark_parent_answered(
            session, task, message=message,
            discussion_peer_id=str(thread.discussion_peer_id), parent_id=parent_id,
        )
        return
    if created:
        promote_human_comment_response(
            session, task, message, target=_reply_target(comment), now_value=_now(),
        )
        return
    if changed:
        refresh_human_comment_response(
            session, task, message, target=_reply_target(comment), now_value=_now(),
        )


def _locked_comment(session, task, *, message, thread, remote_id):
    return session.scalar(select(ChannelMessageComment).where(
        ChannelMessageComment.tenant_id == task.tenant_id,
        ChannelMessageComment.channel_target_id == message.channel_target_id,
        ChannelMessageComment.channel_message_id == message.id,
        ChannelMessageComment.discussion_peer_id == str(thread.discussion_peer_id),
        ChannelMessageComment.comment_message_id == remote_id,
    ).with_for_update())


def _new_comment(task, message, *, thread, remote_id):
    return ChannelMessageComment(
        tenant_id=task.tenant_id,
        channel_target_id=message.channel_target_id,
        channel_message_id=message.id,
        discussion_peer_id=str(thread.discussion_peer_id),
        comment_message_id=remote_id,
    )


def _delete_comment(session, task, *, binding, payload) -> bool:
    remote_id = int(payload.get("source_message_id") or 0)
    target_id = int((task.type_config or {}).get("target_channel_id") or 0)
    comment = session.scalar(select(ChannelMessageComment).where(
        ChannelMessageComment.tenant_id == task.tenant_id,
        ChannelMessageComment.channel_target_id == target_id,
        ChannelMessageComment.discussion_peer_id == str(binding.discussion_peer_id),
        ChannelMessageComment.comment_message_id == remote_id,
    ).with_for_update())
    if comment is None:
        return False
    comment.content_preview = ""
    comment.reply_count = max(1, int(comment.reply_count or 0))
    message = session.get(ChannelMessage, comment.channel_message_id)
    if message is not None:
        release_deleted_human_response(
            session,
            task,
            message,
            remote_message_id=remote_id,
        )
    return True


def _mark_parent_answered(
    session, task, *, message, discussion_peer_id, parent_id,
) -> None:
    parent = session.scalar(select(ChannelMessageComment).where(
        ChannelMessageComment.tenant_id == task.tenant_id,
        ChannelMessageComment.channel_target_id == message.channel_target_id,
        ChannelMessageComment.channel_message_id == message.id,
        ChannelMessageComment.discussion_peer_id == discussion_peer_id,
        ChannelMessageComment.comment_message_id == parent_id,
    ).with_for_update())
    if parent is not None:
        parent.reply_count = max(1, int(parent.reply_count or 0))


def _comment_parent_id(payload: dict, thread_root_id: int) -> int | None:
    reply_id = int(payload.get("reply_to_message_id") or 0)
    return reply_id if reply_id > 0 and reply_id != thread_root_id else None


def _managed_or_bot_sender(
    session: Session,
    task: Task,
    *,
    binding,
    payload: dict,
    source_event_id: str,
) -> bool:
    if bool(payload.get("sender_is_bot", False)):
        return True
    return observe_managed_outbound(
        session,
        tenant_id=task.tenant_id,
        canonical_peer_id=str(binding.discussion_peer_id),
        payload=payload,
        action_class="authored_comment",
        source_event_id=source_event_id,
    )


def _reply_target(comment: ChannelMessageComment) -> dict:
    content = str(comment.content_preview or "").strip()
    return {
        "message_id": int(comment.comment_message_id),
        "channel_message_id": int(comment.channel_message_id),
        "author": str(comment.author_name or "读者").strip(),
        "preview": content[:120],
        "content_hash": hashlib.sha256(content.encode()).hexdigest(),
        "source": "channel_comment",
        "reply_count": int(comment.reply_count or 0),
    }


def _delivery_statement(task: Task, limit: int):
    return (
        select(
            TelegramAuthorizationUpdateDelivery,
            TelegramAuthorizationUpdateEvent,
            TelegramAuthorizationUpdateState,
        )
        .join(
            TelegramAuthorizationUpdateEvent,
            TelegramAuthorizationUpdateEvent.id
            == TelegramAuthorizationUpdateDelivery.update_event_id,
        )
        .join(
            TelegramAuthorizationUpdateSubscription,
            TelegramAuthorizationUpdateSubscription.id
            == TelegramAuthorizationUpdateDelivery.subscription_id,
        )
        .join(
            TelegramAuthorizationUpdateState,
            TelegramAuthorizationUpdateState.id
            == TelegramAuthorizationUpdateEvent.authorization_update_state_id,
        )
        .where(
            TelegramAuthorizationUpdateDelivery.task_id == task.id,
            TelegramAuthorizationUpdateDelivery.delivery_state == "pending",
            TelegramAuthorizationUpdateSubscription.task_epoch
            == int(task.task_lifecycle_epoch or 1),
            TelegramAuthorizationUpdateSubscription.state == "active",
        )
        .order_by(
            TelegramAuthorizationUpdateEvent.ingress_order_no,
            TelegramAuthorizationUpdateDelivery.normalized_item_index,
        )
        .limit(max(1, int(limit)))
        .with_for_update()
    )


def _project_delivery(task: Task, event: TelegramAuthorizationUpdateEvent) -> None:
    task.stats = {
        **dict(task.stats or {}),
        "comment_update_stream_last_ingress_order_no": event.ingress_order_no,
        "comment_update_stream_last_event_at": _now().isoformat(),
    }


def _record_listener_account(task: Task, account_id: int) -> None:
    task.stats = {
        **dict(task.stats or {}),
        "comment_update_stream_listener_account_id": int(account_id),
    }


def _sent_at(value) -> datetime:
    parsed = datetime.fromisoformat(str(value)) if value else _now()
    return as_beijing(parsed)


def _uses_update_stream(task: Task) -> bool:
    return bool(
        task.type == "channel_comment"
        and task.status in {"pending", "running"}
        and (task.type_config or {}).get("engagement_contract_version")
        == UNIFIED_ENGAGEMENT_CONTRACT
    )


def _active_binding(binding: ChannelDiscussionGroupBinding) -> bool:
    return bool(
        binding
        and binding.is_current
        and binding.binding_status == "active"
        and binding.discussion_peer_id
    )


__all__ = [
    "consume_channel_comment_update_deliveries",
    "ensure_channel_comment_update_subscription",
]
