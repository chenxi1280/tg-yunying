from __future__ import annotations

import random

from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, Task, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.group_listeners import collect_group_context, recent_context_messages

from ..account_pool import select_task_accounts
from ..ai_generator import generate_group_messages
from ..fingerprints import fingerprint_exists, remember_fingerprint
from ..pacing import schedule_times
from ..payloads import SendMessagePayload, create_send_action
from .common import add_tokens, stats_inc


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    group = session.get(TgGroup, int(config.get("target_group_id") or 0))
    if not group or group.tenant_id != task.tenant_id or group.auth_status != GroupAuthStatus.AUTHORIZED.value:
        task.last_error = "目标群不存在或未授权"
        return 0
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, target_group_id=group.id)
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    history_fetch_account_id = int(config.get("history_fetch_account_id") or 0)
    available_account_ids = {account.id for account in accounts}
    collect_account_id = history_fetch_account_id if history_fetch_account_id in available_account_ids else accounts[0].id
    collect_group_context(session, group, [collect_account_id])
    fingerprint_source = f"{task.id}:group_ai_chat:{group.id}"
    history_depth = int(config.get("chat_history_depth") or 50)
    history_rows = recent_context_messages(session, group, history_depth)
    context_rows = list(reversed(history_rows[-history_depth:]))
    unprocessed_rows = [
        row
        for row in context_rows
        if not fingerprint_exists(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    ]
    if not unprocessed_rows:
        task.last_error = ""
        return 0
    jitter = float(config.get("participation_jitter") or 0)
    rate = float(config.get("participation_rate") or 0.6)
    desired = max(1, round(len(accounts) * rate * random.uniform(max(0.1, 1 - jitter), 1 + jitter)))
    selected = accounts[: min(desired, len(accounts))]
    messages_per_round = int(config.get("messages_per_round") or 1)
    history = "\n".join(f"{row.sender_name}: {row.content}" for row in context_rows[-50:])
    contents, tokens = generate_group_messages(session, task.tenant_id, config, count=len(selected) * messages_per_round, target_label=group.title, history=history)
    add_tokens(task, tokens)
    times = schedule_times(len(contents), task.pacing_config or {})
    created = 0
    for index, content in enumerate(contents):
        account = selected[index % len(selected)]
        filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=group, content=content, reject_mentions=True, reject_replies=True)
        if not filtered.ok:
            stats_inc(task, "failure_count")
            continue
        create_send_action(
            session,
            task,
            account.id,
            times[index],
            SendMessagePayload(
                chat_id=group.tg_peer_id,
                group_id=group.id,
                target_display=group.title,
                message_text=filtered.content,
                review_approved=True,
            ),
        )
        created += 1
    for row in unprocessed_rows:
        remember_fingerprint(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    stats_inc(task, "total_rounds")
    return created


def _context_fingerprint(row) -> str:
    return f"context:{row.id}:{row.remote_message_id}"


__all__ = ["build_plan"]
