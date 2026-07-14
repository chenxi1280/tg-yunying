from __future__ import annotations

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, ChannelMessageComment, Task


def valid_reply_targets(
    session: Session,
    task: Task,
    channel_target_id: int,
    messages: list[ChannelMessage],
    requested_ids: list[int],
) -> list[dict]:
    if not requested_ids:
        return []
    channel_message_ids = [message.id for message in messages]
    if not channel_message_ids:
        return []
    comments = session.scalars(
        select(ChannelMessageComment).where(
            ChannelMessageComment.tenant_id == task.tenant_id,
            ChannelMessageComment.channel_target_id == channel_target_id,
            ChannelMessageComment.channel_message_id.in_(channel_message_ids),
            ChannelMessageComment.comment_message_id.in_(requested_ids),
        )
    )
    by_id = {int(comment.comment_message_id): _target_from_comment(comment) for comment in comments}
    seen: set[int] = set()
    filtered: list[dict] = []
    for target_id in requested_ids:
        if target_id in by_id and target_id not in seen:
            filtered.append(by_id[target_id])
            seen.add(target_id)
    return filtered


def message_reply_targets(
    session: Session,
    task: Task,
    channel_target_id: int,
    message: ChannelMessage,
    *,
    limit: int = 20,
) -> list[dict]:
    used_ids = _used_reply_target_ids(session, task, channel_target_id, message)
    limit_value = max(1, int(limit))
    query = select(ChannelMessageComment).where(
        ChannelMessageComment.tenant_id == task.tenant_id,
        ChannelMessageComment.channel_target_id == channel_target_id,
        ChannelMessageComment.channel_message_id == message.id,
    )
    if used_ids:
        query = query.where(~ChannelMessageComment.comment_message_id.in_(used_ids))
    comments = session.scalars(
        query.order_by(ChannelMessageComment.created_at.asc(), ChannelMessageComment.id.asc()).limit(limit_value)
    )
    targets = [_target_from_comment(comment) for comment in comments]
    targets.extend(_historical_targets(session, task, channel_target_id, message, limit_value + len(used_ids)))
    return _exclude_used_targets(_dedupe_targets(targets), used_ids)


def reply_target_message_id(target: dict | None) -> int | None:
    return int(target.get("message_id")) if target and target.get("message_id") else None


def reply_target_label(target: dict | None) -> str:
    message_id = reply_target_message_id(target)
    return f"回复消息 #{message_id}" if message_id else ""


def reply_target_text(target: dict | None, key: str) -> str:
    return str(target.get(key) or "") if target else ""


def _dedupe_targets(targets: list[dict]) -> list[dict]:
    seen: set[int] = set()
    deduped: list[dict] = []
    for target in targets:
        message_id = int(target.get("message_id") or 0)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        deduped.append(target)
    return deduped


def _exclude_used_targets(targets: list[dict], used_ids: set[int]) -> list[dict]:
    if not used_ids:
        return targets
    return [target for target in targets if int(target.get("message_id") or 0) not in used_ids]


def _used_reply_target_ids(
    session: Session,
    task: Task,
    channel_target_id: int,
    message: ChannelMessage,
) -> set[int]:
    actions = session.scalars(
        select(Action).where(
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
        )
    )
    used_ids: set[int] = set()
    for action in actions:
        if _payload_int(action, "channel_target_id") != channel_target_id:
            continue
        if not _same_channel_message(action, message):
            continue
        reply_to_message_id = _payload_int(action, "reply_to_message_id")
        if reply_to_message_id:
            used_ids.add(reply_to_message_id)
    return used_ids


def _same_channel_message(action: Action, message: ChannelMessage) -> bool:
    channel_message_id = _payload_int(action, "channel_message_id")
    message_id = _payload_int(action, "message_id")
    return channel_message_id == message.id or message_id == message.message_id


def _payload_int(action: Action, key: str) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw = str(payload.get(key) or "").strip()
    return int(raw) if raw.isdigit() else 0


def _historical_targets(
    session: Session,
    task: Task,
    channel_target_id: int,
    message: ChannelMessage,
    limit: int,
) -> list[dict]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "channel_comment",
            Action.action_type == "post_comment",
            Action.status == "success",
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
            or_(
                Action.payload["channel_message_id"].as_integer() == message.id,
                Action.payload["message_id"].as_integer() == message.message_id,
            ),
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return [target for action in rows if (target := _target_from_action(action))]


def _target_from_action(action: Action) -> dict | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    result = action.result if isinstance(action.result, dict) else {}
    raw_id = str(result.get("telegram_msg_id") or result.get("remote_message_id") or "").strip()
    content = str(payload.get("comment_text") or "").strip()
    if not raw_id.isdigit() or not content:
        return None
    return {
        "message_id": int(raw_id),
        "channel_message_id": _payload_int(action, "channel_message_id"),
        "author": str(payload.get("account_role") or "历史评论账号").strip(),
        "preview": content[:120],
        "source": "own_history",
    }


def _target_from_comment(comment: ChannelMessageComment) -> dict:
    return {
        "message_id": int(comment.comment_message_id),
        "channel_message_id": int(comment.channel_message_id),
        "author": str(comment.author_name or "读者").strip(),
        "preview": str(comment.content_preview or "").strip()[:120],
        "source": "channel_comment",
    }


__all__ = [
    "message_reply_targets",
    "reply_target_label",
    "reply_target_message_id",
    "reply_target_text",
    "valid_reply_targets",
]
