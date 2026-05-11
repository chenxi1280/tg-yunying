from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import ChannelMessage, Task

from ..account_pool import select_task_accounts
from ..pacing import schedule_times
from ..payloads import LikeMessagePayload, create_like_action
from .common import adjust_for_account_hour_limit, available_channel_accounts_for_message, channel_message_payload, channel_scope, quantity_with_jitter, record_channel_capacity_warning


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    channel, messages = channel_scope(session, task, config)
    if not channel or not messages:
        return 0
    reactions = config.get("allowed_reactions") or ["👍"]
    target_per_message = int(config.get("target_likes_per_message") or 1)
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=target_per_message)
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "点赞", target_per_message, len(accounts))
    actions: list[tuple[ChannelMessage, int, str]] = []
    for message in messages:
        available_accounts = available_channel_accounts_for_message(session, task, "like_message", message, accounts)
        quantity = min(quantity_with_jitter(target_per_message, float(config.get("like_count_jitter") or 0)), len(available_accounts))
        actions.extend((message, available_accounts[index].id, reactions[index % len(reactions)]) for index in range(quantity))
    if not actions:
        task.last_error = task.last_error or "没有可新增的有效点赞账号"
        return 0
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, account_id, reaction) in enumerate(actions):
        planned_at = times[index]
        planned_at = adjust_for_account_hour_limit(session, task, account_id, "like_message", planned_at, config)
        create_like_action(
            session,
            task,
            account_id,
            planned_at,
            LikeMessagePayload(**channel_message_payload(channel, message), reaction_emoji=reaction),
        )
        created += 1
    return created


__all__ = ["build_plan"]
