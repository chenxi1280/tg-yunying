from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ChannelMessage, Task

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
    actions: list[tuple[ChannelMessage, str]] = []
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
        actions.extend((message, contents[index] if index < len(contents) else "支持一下") for index in range(quantity))
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=len(actions))
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "回复", int(config.get("target_comments_per_message") or 1), len(accounts))
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, content) in enumerate(actions):
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
                message_content=message.content_preview,
                comment_text=content,
                review_approved=True,
            ),
        )
        created += 1
    return created


__all__ = ["build_plan"]
