from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import GroupAuthStatus, Task, TgGroup
from app.services.content_filters import filter_outbound_content
from app.services.group_listeners import collect_group_context, recent_context_messages

from ..account_pool import select_task_accounts
from ..ai_generator import rewrite_relay_content
from ..fingerprints import is_duplicate, remember_fingerprint
from ..pacing import schedule_times
from ..payloads import SendMessagePayload, create_send_action
from .common import add_tokens, stats_inc


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    target = session.get(TgGroup, int(config.get("target_group_id") or 0))
    if not target or target.tenant_id != task.tenant_id or target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        task.last_error = "目标群不存在或未授权"
        return 0
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, target_group_id=target.id)
    if not accounts:
        task.last_error = "没有可用账号，等待账号恢复后继续执行"
        return 0
    candidate_actions: list[tuple[int, str, str, str]] = []
    monitor_account_ids = [int(account_id) for account_id in config.get("monitor_account_ids") or []]
    for item in [item for item in config.get("source_groups") or [] if item.get("is_active", True)]:
        source = session.get(TgGroup, int(item.get("group_id") or 0))
        if not source or source.tenant_id != task.tenant_id:
            continue
        collect_group_context(session, source, monitor_account_ids or None)
        source_fingerprint_key = f"{task.id}:relay:{source.id}"
        for message in reversed(recent_context_messages(session, source, source.listener_context_limit)):
            if not passes_relay_filters(message.content, message.sender_peer_id, message.message_type, config.get("filters") or {}):
                continue
            if is_duplicate(session, task.tenant_id, source_fingerprint_key, message.content, window_minutes=int(config.get("dedup_window_minutes") or 60)):
                continue
            rewritten, tokens = rewrite_relay_content(session, task.tenant_id, config, message.content, target_label=target.title)
            add_tokens(task, tokens)
            filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=target, content=rewritten, reject_mentions=True, reject_replies=True)
            if not filtered.ok:
                stats_inc(task, "failure_count")
                remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
                continue
            candidate_actions.append((source.id, message.content, filtered.content, f"{source.title} / {message.sender_name}"))
            remember_fingerprint(session, task.tenant_id, source_fingerprint_key, message.content)
    if not candidate_actions:
        return 0
    times = schedule_times(len(candidate_actions), task.pacing_config or {})
    created = 0
    for index, (_source_id, original, content, source_info) in enumerate(candidate_actions):
        account = accounts[index % len(accounts)]
        create_send_action(
            session,
            task,
            account.id,
            times[index],
            SendMessagePayload(
                chat_id=target.tg_peer_id,
                group_id=target.id,
                target_display=target.title,
                message_text=content,
                original_text=original,
                review_approved=True,
            ),
        )
        created += 1
    stats_inc(task, "total_rounds")
    return created


def passes_relay_filters(content: str, sender_id: str, message_type: str, filters: dict) -> bool:
    text = content or ""
    whitelist = [str(item).lower() for item in filters.get("keyword_whitelist") or [] if str(item).strip()]
    blacklist = [str(item).lower() for item in filters.get("keyword_blacklist") or [] if str(item).strip()]
    if whitelist and not any(item in text.lower() for item in whitelist):
        return False
    if blacklist and any(item in text.lower() for item in blacklist):
        return False
    if filters.get("min_message_length") and len(text) < int(filters["min_message_length"]):
        return False
    if filters.get("max_message_length") and len(text) > int(filters["max_message_length"]):
        return False
    if sender_id and sender_id in {str(item) for item in filters.get("blocked_user_ids") or []}:
        return False
    allowed = {str(item) for item in filters.get("allowed_media_types") or []}
    if allowed and message_type not in allowed:
        return False
    is_text = message_type in {"text", "文本", ""}
    if filters.get("only_with_media") and is_text:
        return False
    if filters.get("only_text") and not is_text:
        return False
    return True


__all__ = ["build_plan", "passes_relay_filters"]
