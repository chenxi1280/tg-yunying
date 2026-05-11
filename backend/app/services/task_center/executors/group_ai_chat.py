from __future__ import annotations

import random
from datetime import datetime, time

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Action, GroupAuthStatus, Task, TgGroup
from app.services._common import _now
from app.services.content_filters import filter_outbound_content
from app.services.group_listeners import collect_group_context, recent_context_messages

from ..account_pool import select_task_accounts
from ..ai_generator import generate_group_messages
from ..fingerprints import fingerprint_exists, remember_fingerprint
from ..listener_runtime import should_collect_listener
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
    if should_collect_listener("group", group.id, window_seconds=group.listener_interval_seconds):
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
    mode, ramp_ratio = ai_cycle_mode(config, task.scheduled_start)
    selected, turn_count = _select_cycle_accounts(accounts, config, mode, ramp_ratio, has_context=bool(context_rows))
    history_parts = [f"{row.sender_name}: {row.content}" for row in context_rows[-50:]]
    if not context_rows:
        history_parts.append(_bootstrap_history(config, group))
    previous_ai_messages = _recent_ai_messages(session, task, limit=10)
    if previous_ai_messages:
        history_parts.extend(f"上一轮AI发言: {content}" for content in previous_ai_messages)
    history = "\n".join(history_parts)
    contents, tokens = generate_group_messages(session, task.tenant_id, config, count=turn_count, target_label=group.title, history=history)
    add_tokens(task, tokens)
    times = schedule_times(len(contents), task.pacing_config or {})
    cycle_index = _next_cycle_index(session, task)
    cycle_id = f"{task.id}:cycle:{cycle_index}"
    context_message_ids = [int(row.id) for row in context_rows[-history_depth:]]
    context_snapshot_message_id = max(context_message_ids) if context_message_ids else None
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
                cycle_id=cycle_id,
                turn_index=index + 1,
                account_role=_role_for_turn(index),
                intent=_intent_for_turn(index),
                context_message_ids=context_message_ids,
                context_snapshot_message_id=context_snapshot_message_id,
                context_expire_after_messages=int(config.get("context_expire_after_messages") or 0),
            ),
        )
        created += 1
    for row in unprocessed_rows:
        remember_fingerprint(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    stats = dict(task.stats or {})
    stats["current_mode"] = mode
    stats["ramp_ratio"] = ramp_ratio
    stats["context_mode"] = "history" if context_rows else "bootstrap"
    task.last_error = ""
    task.stats = stats
    stats_inc(task, "total_rounds")
    return created


def _select_cycle_accounts(accounts: list, config: dict, mode: str, ramp_ratio: float, *, has_context: bool) -> tuple[list, int]:
    if str(config.get("messages_per_round_mode") or "auto") == "manual":
        jitter = float(config.get("participation_jitter") or 0)
        rate = float(config.get("participation_rate") or 0.6) * ramp_ratio
        desired = max(1, round(len(accounts) * rate * random.uniform(max(0.1, 1 - jitter), 1 + jitter)))
        if mode == "静默期":
            desired = min(desired, int(config.get("silent_max_accounts") or 5))
        selected = accounts[: min(desired, len(accounts))]
        messages_per_round = int(config.get("messages_per_round") or 1)
        if mode == "静默期":
            messages_per_round = min(messages_per_round, int(config.get("silent_messages_per_round") or 1))
        return selected, max(1, len(selected) * messages_per_round)
    limit = 2 if mode == "静默期" else 5
    if not has_context:
        limit = min(limit, 3)
    selected = accounts[: min(limit, len(accounts))]
    return selected, max(1, len(selected))


def _bootstrap_history(config: dict, group: TgGroup) -> str:
    topic = str(config.get("topic_hint") or group.topic_direction or "").strip()
    if not topic:
        topic = "围绕群内日常交流自然开场，轻松抛出一个大家容易接上的话题"
    return f"当前群暂无可用历史消息。请以“{topic}”为方向，生成自然开场，不要提到系统、任务或 AI。"


def ai_cycle_mode(config: dict, scheduled_start: datetime | None = None, now: datetime | None = None) -> tuple[str, float]:
    current = now or _now()
    mode = "正常期"
    if config.get("silent_mode_enabled", True) and _in_time_window(current.time(), str(config.get("silent_start") or "23:00"), str(config.get("silent_end") or "08:00")):
        mode = "静默期"
    ramp_minutes = int(config.get("ramp_up_minutes") or 0)
    if ramp_minutes <= 0:
        return mode, 1.0
    start = scheduled_start or current.replace(hour=0, minute=0, second=0, microsecond=0)
    if start.tzinfo is not None:
        start = start.replace(tzinfo=None)
    elapsed_minutes = max(0.0, (current - start).total_seconds() / 60)
    if elapsed_minutes >= ramp_minutes:
        return mode, 1.0
    start_ratio = float(config.get("ramp_start_ratio") or 0.3)
    ratio = min(1.0, max(start_ratio, start_ratio + (1 - start_ratio) * (elapsed_minutes / max(ramp_minutes, 1))))
    return ("启动期" if mode == "正常期" else mode), round(ratio, 3)


def _in_time_window(current: time, start_raw: str, end_raw: str) -> bool:
    start = _parse_time(start_raw, time(23, 0))
    end = _parse_time(end_raw, time(8, 0))
    return start <= current < end if start < end else current >= start or current < end


def _parse_time(value: str, fallback: time) -> time:
    try:
        hour, minute = [int(part) for part in value.split(":", 1)]
        return time(hour=hour, minute=minute)
    except (TypeError, ValueError):
        return fallback


def _context_fingerprint(row) -> str:
    return f"context:{row.id}:{row.remote_message_id}"


def _recent_ai_messages(session: Session, task: Task, *, limit: int) -> list[str]:
    messages: list[str] = []
    rows = session.scalars(
        select(Action)
        .where(Action.task_id == task.id, Action.task_type == "group_ai_chat", Action.status == "success")
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        content = str(payload.get("message_text") or "").strip()
        if content:
            messages.append(content)
    return list(reversed(messages))


def _next_cycle_index(session: Session, task: Task) -> int:
    max_index = 0
    rows = session.scalars(select(Action.payload).where(Action.task_id == task.id, Action.task_type == "group_ai_chat"))
    prefix = f"{task.id}:cycle:"
    for payload in rows:
        if not isinstance(payload, dict):
            continue
        cycle_id = str(payload.get("cycle_id") or "")
        if not cycle_id.startswith(prefix):
            continue
        try:
            max_index = max(max_index, int(cycle_id.removeprefix(prefix)))
        except ValueError:
            continue
    return max_index + 1


def _role_for_turn(index: int) -> str:
    roles = ["引导型账号", "补充型账号", "提问型账号", "总结型账号", "轻松闲聊型账号"]
    return roles[index % len(roles)]


def _intent_for_turn(index: int) -> str:
    intents = ["回应上下文", "补充信息", "引出讨论", "轻量总结", "承接话题"]
    return intents[index % len(intents)]


__all__ = ["ai_cycle_mode", "build_plan"]
