from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChannelMessage, ChannelMessageComment, Task

from ..account_pool import select_task_accounts
from ..ai_generator import generate_channel_comments
from ..pacing import schedule_times
from ..payloads import PostCommentPayload, create_comment_action
from .common import add_tokens, adjust_for_account_hour_limit, channel_message_payload, channel_scope, pick_channel_account, quantity_with_jitter, record_channel_capacity_warning, stats_inc, unplanned_channel_messages


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    channel, messages = channel_scope(session, task, config)
    if not channel or not messages:
        return 0
    messages = unplanned_channel_messages(session, task, "post_comment", messages)
    if not messages:
        task.last_error = ""
        return 0
    actions: list[tuple[ChannelMessage, str, int | None]] = []
    requested_reply_targets = [int(item) for item in config.get("reply_to_message_ids") or [] if int(item or 0) > 0]
    comment_mode = config.get("comment_mode") or "comment"
    reply_targets = _valid_reply_targets(session, task, channel.id, messages, requested_reply_targets)
    if comment_mode in {"reply", "mixed"} and requested_reply_targets and not reply_targets:
        task.last_error = "回复对象不属于当前频道消息，请先采集评论后重新选择"
        return 0
    for message in messages:
        quantity = quantity_with_jitter(int(config.get("target_comments_per_message") or 1), float(config.get("comment_count_jitter") or 0))
        contents, tokens = generate_channel_comments(
            session,
            task.tenant_id,
            config,
            count=quantity,
            message_content=message.content_preview or message.message_url,
            target_label=channel.title,
        )
        add_tokens(task, tokens)
        actions.extend((message, contents[index] if index < len(contents) else "支持一下", _reply_target_for_index(comment_mode, reply_targets, index)) for index in range(quantity))
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=len(actions))
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "回复", int(config.get("target_comments_per_message") or 1), len(accounts))
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, content, reply_to_message_id) in enumerate(actions):
        planned_at = times[index]
        account = pick_channel_account(session, task, accounts, "post_comment", planned_at, config, index)
        if not account:
            stats_inc(task, "failure_count")
            continue
        planned_at = adjust_for_account_hour_limit(session, task, account.id, "post_comment", planned_at, config)
        create_comment_action(
            session,
            task,
            account.id,
            planned_at,
            PostCommentPayload(
                **channel_message_payload(channel, message),
                comment_text=content,
                comment_mode="reply" if reply_to_message_id else "comment",
                reply_to_message_id=reply_to_message_id,
                reply_target_label=f"回复消息 #{reply_to_message_id}" if reply_to_message_id else "",
                review_approved=True,
            ),
        )
        created += 1
    return created


def _reply_target_for_index(comment_mode: str, reply_targets: list[int], index: int) -> int | None:
    if not reply_targets:
        return None
    if comment_mode == "reply":
        return reply_targets[index % len(reply_targets)]
    if comment_mode == "mixed" and index % 2 == 1:
        return reply_targets[(index // 2) % len(reply_targets)]
    return None


def _valid_reply_targets(session: Session, task: Task, channel_target_id: int, messages: list[ChannelMessage], requested_ids: list[int]) -> list[int]:
    if not requested_ids:
        return []
    channel_message_ids = [message.id for message in messages]
    if not channel_message_ids:
        return []
    valid_ids = set(
        session.scalars(
            select(ChannelMessageComment.comment_message_id).where(
                ChannelMessageComment.tenant_id == task.tenant_id,
                ChannelMessageComment.channel_target_id == channel_target_id,
                ChannelMessageComment.channel_message_id.in_(channel_message_ids),
                ChannelMessageComment.comment_message_id.in_(requested_ids),
            )
        )
    )
    seen: set[int] = set()
    filtered: list[int] = []
    for target_id in requested_ids:
        if target_id in valid_ids and target_id not in seen:
            filtered.append(target_id)
            seen.add(target_id)
    return filtered


__all__ = ["build_plan"]
