from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import OperationTarget, Task

from ..account_pool import select_task_accounts
from ..channel_membership import channel_member_accounts, gate_channel_membership
from ..pacing import schedule_times
from ..payloads import ViewMessagePayload, create_view_action
from .common import adjust_for_account_hour_limit, available_channel_accounts_for_message, channel_message_payload, channel_scope, quantity_jitter_bounds, quantity_with_jitter, record_channel_capacity_warning, unplanned_channel_messages


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    channel = session.get(OperationTarget, int(config.get("target_channel_id") or 0))
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        task.last_error = "目标频道不存在"
        return 0
    gate = gate_channel_membership(session, task, channel)
    if not gate.ready:
        return gate.created
    channel, messages = channel_scope(session, task, config)
    if not channel or not messages:
        return 0
    messages = unplanned_channel_messages(session, task, "view_message", messages)
    if not messages:
        task.last_error = ""
        return 0
    target_per_message = int(config.get("target_views_per_message") or 1)
    _lower, max_target_per_message = quantity_jitter_bounds(target_per_message, float(config.get("view_count_jitter") or 0))
    account_scan_limit = max(max_target_per_message, int((task.account_config or {}).get("max_concurrent") or max_target_per_message))
    accounts = channel_member_accounts(session, task, channel, select_task_accounts(session, task.tenant_id, task.account_config or {}, limit=account_scan_limit))
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    record_channel_capacity_warning(task, "浏览", target_per_message, len(accounts))
    actions = [
        (message, account.id)
        for message in messages
        for account in available_channel_accounts_for_message(session, task, "view_message", message, accounts)[
            : quantity_with_jitter(target_per_message, float(config.get("view_count_jitter") or 0))
        ]
    ]
    if not actions:
        task.last_error = task.last_error or "没有可新增的有效浏览账号"
        return 0
    times = schedule_times(len(actions), task.pacing_config or {})
    created = 0
    for index, (message, account_id) in enumerate(actions):
        planned_at = adjust_for_account_hour_limit(session, task, account_id, "view_message", times[index], config)
        create_view_action(session, task, account_id, planned_at, ViewMessagePayload(**channel_message_payload(channel, message)))
        created += 1
    return created


__all__ = ["build_plan"]
