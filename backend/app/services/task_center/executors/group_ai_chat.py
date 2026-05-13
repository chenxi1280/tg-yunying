from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, RuleSet, Task, TgGroup
from app.services._common import _now
from app.services.account_capacity import available_accounts_by_capacity, next_capacity_window
from app.services.content_filters import filter_outbound_content, looks_like_generated_template_noise, looks_like_operator_ui_content
from app.services.group_listeners import collect_group_context, recent_context_messages
from app.services.rule_engine import apply_output_policy, bound_rule_version, evaluate_input_filter

from ..account_pool import select_task_accounts
from ..ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable, generate_group_messages
from ..fingerprints import fingerprint_exists, remember_fingerprint
from ..listener_runtime import should_collect_listener
from ..pacing import schedule_times
from ..payloads import SendMessagePayload, create_send_action
from ..targets import group_from_reference
from .common import add_tokens, stats_inc


WAITING_NEW_CONTEXT_MESSAGE = "暂无新的真人上下文，等待群内新消息"
WAITING_IDLE_CONTINUATION_MESSAGE = "持续监听中，等待新消息或空闲续聊间隔"
DEFAULT_IDLE_CONTINUATION_SECONDS = 300


def build_plan(session: Session, task: Task) -> int:
    config = task.type_config or {}
    rule_version = bound_rule_version(session, task)
    rule_set = session.get(RuleSet, rule_version.rule_set_id) if rule_version else None
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=True,
    )
    if not group:
        task.last_error = "目标群不存在或未授权"
        return 0
    accounts = select_task_accounts(session, task.tenant_id, task.account_config or {}, target_group_id=group.id)
    if not accounts:
        no_cooldown_config = dict(task.account_config or {})
        no_cooldown_config["cooldown_per_account_minutes"] = 0
        cooldown_candidates = select_task_accounts(
            session,
            task.tenant_id,
            no_cooldown_config,
            target_group_id=group.id,
            limit=1,
        )
        task.last_error = "账号冷却中，等待冷却后继续执行" if cooldown_candidates else "没有可用账号，等待账号恢复后继续执行"
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
    usable_context_rows = [row for row in context_rows if not _looks_like_internal_prompt(row.content)]
    mode, ramp_ratio = ai_cycle_mode(config, task.scheduled_start)
    unprocessed_rows = [
        row
        for row in usable_context_rows
        if not fingerprint_exists(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    ]
    force_bootstrap_once = bool((task.stats or {}).get("force_bootstrap_once"))
    previous_ai_messages = _recent_ai_messages(session, task, limit=10)
    idle_continuation = False
    if not force_bootstrap_once and _should_wait_for_human_context(session, task, usable_context_rows, unprocessed_rows):
        idle_decision = _idle_continuation_decision(session, task, config)
        if idle_decision["due"]:
            idle_continuation = True
        else:
            _mark_waiting_context(
                task,
                config,
                mode,
                ramp_ratio,
                context_mode="waiting_new_context",
                next_run_at=idle_decision["next_run_at"],
            )
            return 0
    selected, turn_count = _select_cycle_accounts(accounts, config, mode, ramp_ratio, has_context=bool(usable_context_rows))
    history_parts = [f"{row.sender_name}: {row.content}" for row in usable_context_rows[-50:]]
    if idle_continuation:
        history_parts.append(_idle_continuation_history(config, group, previous_ai_messages))
    elif not usable_context_rows:
        history_parts.append(_bootstrap_history(config, group))
    history = "\n".join(history_parts)
    if rule_version:
        input_result = evaluate_input_filter(history, message_type="text", filters=rule_version.filters or {})
        if not input_result.passed:
            task.last_error = f"规则输入过滤跳过：{input_result.reason}"
            stats_inc(task, "skipped_count")
            return 0
    topic_thread = _topic_thread_summary(config, group, usable_context_rows, previous_ai_messages)
    topic_plan = _topic_plan_summary(config, group, topic_thread, turn_count)
    account_memories = _recent_account_memories(session, task, [account.id for account in selected], depth=int(config.get("account_memory_depth") or 3))
    account_profiles = account_profile_summaries(session, task, [account.id for account in selected])
    generation_config = {**config, "account_memories": account_memories, "account_profiles": account_profiles, "topic_thread": topic_thread, "topic_plan": topic_plan}
    cycle_index = _next_cycle_index(session, task)
    cycle_id = f"{task.id}:cycle:{cycle_index}"
    try:
        contents, tokens = generate_group_messages(session, task.tenant_id, generation_config, count=turn_count, target_label=group.title, history=history)
    except AiGenerationUnavailable as exc:
        task.last_error = str(exc) or AI_GENERATION_UNAVAILABLE_MESSAGE
        stats = dict(task.stats or {})
        stats["current_mode"] = mode
        stats["ramp_ratio"] = ramp_ratio
        stats["context_mode"] = _context_mode(usable_context_rows, idle_continuation)
        task.stats = stats
        return 0
    contents = [content for content in contents if not _looks_like_generated_noise(content)]
    contents = _drop_repeated_ai_messages(contents, previous_ai_messages)
    contents = contents[:turn_count]
    if not contents:
        task.last_error = AI_GENERATION_UNAVAILABLE_MESSAGE
        return 0
    add_tokens(task, tokens)
    times = schedule_times(len(contents), task.pacing_config or {})
    context_message_ids = [int(row.id) for row in usable_context_rows[-history_depth:]]
    context_snapshot_message_id = max(context_message_ids) if context_message_ids else None
    created = 0
    for index, content in enumerate(contents):
        if rule_version:
            policy_result = apply_output_policy(content, rule_version.output_checks or {}, rule_version.transforms or {})
            if not policy_result.allowed:
                stats_inc(task, "failure_count")
                continue
            content = policy_result.content
        planned_at = times[index]
        available = available_accounts_by_capacity(session, tenant_id=task.tenant_id, accounts=selected, scheduled_at=planned_at)
        account = available[index % len(available)] if available else selected[index % len(selected)]
        if not available:
            decision = next_capacity_window(
                session,
                tenant_id=task.tenant_id,
                account_ids=[item.id for item in selected],
                scheduled_at=planned_at,
            )
            if decision.defer_until:
                planned_at = decision.defer_until
        filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=group, content=content, reject_mentions=True, reject_replies=True)
        if not filtered.ok:
            stats_inc(task, "failure_count")
            continue
        create_send_action(
            session,
            task,
            account.id,
            planned_at,
            SendMessagePayload(
                chat_id=group.tg_peer_id,
                group_id=group.id,
                operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
                target_display=group.title,
                message_text=filtered.content,
                review_approved=True,
                cycle_id=cycle_id,
                turn_index=index + 1,
                account_role=_role_for_account(account.id, index, config),
                account_memory=account_memories.get(str(account.id), ""),
                account_profile=account_profiles.get(str(account.id), ""),
                topic_thread=topic_thread,
                topic_plan=topic_plan,
                intent=_intent_for_turn(index),
                context_message_ids=context_message_ids,
                context_snapshot_message_id=context_snapshot_message_id,
                context_expire_after_messages=int(config.get("context_expire_after_messages") or 0),
                ai_generation_id=cycle_id,
                ai_generation_status="success",
                ai_generation_tokens=tokens,
                ai_generation_count=len(contents),
                ai_generation_context_count=len(context_message_ids),
                ai_generation_memory_count=len(account_memories),
                rule_set_id=rule_version.rule_set_id if rule_version else None,
                rule_set_name=rule_set.name if rule_set else "",
                rule_set_version_id=rule_version.id if rule_version else None,
                resolved_rule_set_version_id=rule_version.id if rule_version else None,
                rule_set_version=rule_version.version if rule_version else None,
                rule_binding_mode="fixed_version" if rule_version and config.get("rule_set_version_id") else "follow_current" if rule_version else "",
            ),
        )
        created += 1
    for row in unprocessed_rows:
        remember_fingerprint(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    stats = dict(task.stats or {})
    stats["current_mode"] = mode
    stats["ramp_ratio"] = ramp_ratio
    stats["context_mode"] = _context_mode(usable_context_rows, idle_continuation)
    stats.pop("idle_continuation_next_run_at", None)
    stats.pop("force_bootstrap_once", None)
    task.last_error = ""
    task.stats = stats
    stats_inc(task, "total_rounds")
    return created


def _mark_waiting_context(
    task: Task,
    config: dict,
    mode: str | None = None,
    ramp_ratio: float | None = None,
    *,
    context_mode: str,
    next_run_at: datetime | None = None,
) -> None:
    resolved_mode, resolved_ratio = (mode, ramp_ratio) if mode and ramp_ratio is not None else ai_cycle_mode(config, task.scheduled_start)
    stats = dict(task.stats or {})
    stats["current_mode"] = resolved_mode
    stats["ramp_ratio"] = resolved_ratio
    stats["context_mode"] = context_mode
    if next_run_at:
        stats["idle_continuation_next_run_at"] = _naive_datetime(next_run_at).isoformat()
        task.next_run_at = _naive_datetime(next_run_at)
        task.last_error = WAITING_IDLE_CONTINUATION_MESSAGE
    else:
        stats.pop("idle_continuation_next_run_at", None)
        task.last_error = WAITING_NEW_CONTEXT_MESSAGE
    task.stats = stats


def _should_wait_for_human_context(session: Session, task: Task, usable_context_rows: list, unprocessed_rows: list) -> bool:
    return (bool(usable_context_rows) and not unprocessed_rows) or (not usable_context_rows and _has_generated_before(session, task))


def _has_generated_before(session: Session, task: Task) -> bool:
    return bool(session.scalar(select(Action.id).where(Action.task_id == task.id, Action.action_type == "send_message").limit(1)))


def _idle_continuation_decision(session: Session, task: Task, config: dict) -> dict[str, datetime | bool | None]:
    if config.get("idle_continuation_enabled") is False:
        return {"due": False, "next_run_at": None}
    last_success_at = _last_successful_ai_action_at(session, task)
    if not last_success_at:
        return {"due": False, "next_run_at": None}
    next_run_at = _naive_datetime(last_success_at) + timedelta(seconds=_idle_continuation_seconds(config))
    return {"due": _now() >= next_run_at, "next_run_at": next_run_at}


def _idle_continuation_seconds(config: dict) -> int:
    try:
        value = int(config.get("idle_continuation_seconds") or DEFAULT_IDLE_CONTINUATION_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_IDLE_CONTINUATION_SECONDS
    return max(30, value)


def _last_successful_ai_action_at(session: Session, task: Task) -> datetime | None:
    action = session.scalar(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.scheduled_at.desc(), Action.created_at.desc())
        .limit(1)
    )
    if not action:
        return None
    return _naive_datetime(action.executed_at or action.scheduled_at or action.created_at)


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


def _idle_continuation_history(config: dict, group: TgGroup, previous_ai_messages: list[str]) -> str:
    topic = str(config.get("topic_hint") or group.topic_direction or "群聊日常活跃").strip()
    recent_ai = " / ".join(_clean_topic_text(text) for text in previous_ai_messages[-3:])
    recent_ai = recent_ai.strip(" /")
    parts = [
        f"群内暂时没有新的真人消息。请围绕“{topic}”自然续聊一轮，像真实群友一样轻量推进话题。",
        "必须避免重复上一轮表达，不要提到系统、任务或 AI。",
    ]
    if recent_ai:
        parts.append(f"上一轮 AI 已说：{recent_ai}。请换一个角度承接。")
    return "\n".join(parts)


def _context_mode(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return "idle_continuation"
    return "history" if context_rows else "bootstrap"


def _topic_thread_summary(config: dict, group: TgGroup, context_rows: list, previous_ai_messages: list[str]) -> str:
    parts: list[str] = []
    topic = str(config.get("topic_hint") or group.topic_direction or "").strip()
    if topic:
        parts.append(f"主线方向：{topic[:80]}")
    recent_human = [_clean_topic_text(getattr(row, "content", "")) for row in context_rows[-3:]]
    recent_human = [text for text in recent_human if text]
    if recent_human:
        parts.append("最近真人上下文：" + " / ".join(recent_human))
    recent_ai = [_clean_topic_text(text) for text in previous_ai_messages[-3:]]
    recent_ai = [text for text in recent_ai if text]
    if recent_ai:
        parts.append("上一轮 AI 已说：" + " / ".join(recent_ai))
    if not parts:
        return ""
    return "；".join(parts)[:500]


def _topic_plan_summary(config: dict, group: TgGroup, topic_thread: str, turn_count: int) -> str:
    topic = str(config.get("topic_hint") or group.topic_direction or "群聊日常活跃").strip()
    anchors = [part.strip() for part in re.split(r"[；/]", topic_thread or "") if part.strip()]
    anchor = anchors[-1] if anchors else f"主线方向：{topic[:80]}"
    steps = [
        f"1. 承接：围绕“{anchor[:80]}”自然接一句，不重复上一轮表达。",
        f"2. 补充：给出一个和“{topic[:60]}”相关的具体信息点或轻量经验。",
        "3. 互动：抛出一个容易回答的小问题，避免营销口吻。",
        "4. 收束：简短总结并把话题带回真实用户上下文。",
    ]
    return "\n".join(steps[: max(1, min(int(turn_count or 1), len(steps)))])


def _clean_topic_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if _looks_like_internal_prompt(text) or _looks_like_generated_noise(text):
        return ""
    return text[:80]


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


def _naive_datetime(value: datetime) -> datetime:
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


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
        if content and not _looks_like_internal_prompt(content):
            messages.append(content)
    return list(reversed(messages))


def _recent_account_memories(session: Session, task: Task, account_ids: list[int], *, depth: int) -> dict[str, str]:
    if depth <= 0 or not account_ids:
        return {}
    wanted = set(account_ids)
    memories: dict[int, list[str]] = {account_id: [] for account_id in wanted}
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.account_id.in_(wanted),
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(len(wanted) * depth * 2, depth))
    )
    for action in rows:
        if action.account_id not in wanted or len(memories[action.account_id]) >= depth:
            continue
        _append_account_memory(memories, action, depth=depth)
    if any(len(items) < depth for items in memories.values()):
        cross_task_rows = session.execute(
            select(Action, Task.name)
            .join(Task, Task.id == Action.task_id)
            .where(
                Action.tenant_id == task.tenant_id,
                Action.task_id != task.id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status == "success",
                Action.account_id.in_(wanted),
                Task.tenant_id == task.tenant_id,
                Task.type == "group_ai_chat",
                Task.deleted_at.is_(None),
            )
            .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
            .limit(max(len(wanted) * depth * 3, depth))
        )
        for action, task_name in cross_task_rows:
            if action.account_id not in wanted or len(memories[action.account_id]) >= depth:
                continue
            _append_account_memory(memories, action, depth=depth, source_label=f"跨任务 {task_name}")
    return {str(account_id): "；".join(reversed(items)) for account_id, items in memories.items() if items}


def _append_account_memory(memories: dict[int, list[str]], action: Action, *, depth: int, source_label: str = "") -> None:
    if action.account_id not in memories or len(memories[action.account_id]) >= depth:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    content = str(payload.get("message_text") or "").strip()
    if not content or _looks_like_internal_prompt(content):
        return
    role = str(payload.get("account_role") or "").strip()
    intent = str(payload.get("intent") or "").strip()
    label = " / ".join(part for part in [source_label, role, intent] if part)
    memories[action.account_id].append(f"{label}: {content[:80]}" if label else content[:80])


def account_profile_summaries(session: Session, task: Task, account_ids: list[int], *, recent_limit: int = 5) -> dict[str, str]:
    if not account_ids:
        return {}
    wanted = {int(account_id) for account_id in account_ids if account_id}
    if not wanted:
        return {}
    totals = dict(
        session.execute(
            select(Action.account_id, func.count(Action.id))
            .join(Task, Task.id == Action.task_id)
            .where(
                Action.tenant_id == task.tenant_id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status == "success",
                Action.account_id.in_(wanted),
                Task.tenant_id == task.tenant_id,
                Task.type == "group_ai_chat",
                Task.deleted_at.is_(None),
            )
            .group_by(Action.account_id)
        ).all()
    )
    if not totals:
        return {}
    rows = session.execute(
        select(Action, Task.name)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.account_id.in_(wanted),
            Task.tenant_id == task.tenant_id,
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
        )
        .order_by(Action.account_id.asc(), Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(len(wanted) * recent_limit * 3, recent_limit))
    )
    profiles: dict[int, dict[str, object]] = {
        account_id: {"roles": {}, "intents": {}, "tasks": set(), "messages": []}
        for account_id in wanted
        if int(totals.get(account_id) or 0) > 0
    }
    for action, task_name in rows:
        if action.account_id not in profiles:
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        item = profiles[action.account_id]
        task_names = item["tasks"]
        if isinstance(task_names, set) and task_name:
            task_names.add(str(task_name))
        _profile_count(item["roles"], str(payload.get("account_role") or "").strip())
        _profile_count(item["intents"], str(payload.get("intent") or "").strip())
        messages = item["messages"]
        content = str(payload.get("message_text") or "").strip()
        if isinstance(messages, list) and content and not _looks_like_internal_prompt(content) and len(messages) < recent_limit:
            messages.append(content[:60])
    result: dict[str, str] = {}
    for account_id, item in profiles.items():
        roles = _top_profile_values(item["roles"])
        intents = _top_profile_values(item["intents"])
        tasks = item["tasks"] if isinstance(item["tasks"], set) else set()
        messages = item["messages"] if isinstance(item["messages"], list) else []
        parts = [
            f"历史成功发言 {int(totals.get(account_id) or 0)} 次",
            f"关联任务 {len(tasks)} 个" if tasks else "",
            f"常用角色：{'、'.join(roles)}" if roles else "",
            f"常见意图：{'、'.join(intents)}" if intents else "",
            f"近期表达：{' / '.join(messages[:2])}" if messages else "",
        ]
        result[str(account_id)] = "；".join(part for part in parts if part)
    return result


def _profile_count(container: object, value: str) -> None:
    if not value or not isinstance(container, dict):
        return
    container[value] = int(container.get(value, 0) or 0) + 1


def _top_profile_values(container: object, *, limit: int = 3) -> list[str]:
    if not isinstance(container, dict):
        return []
    return [
        str(key)
        for key, _count in sorted(container.items(), key=lambda item: (-int(item[1] or 0), str(item[0])))[:limit]
        if str(key).strip()
    ]


def _looks_like_internal_prompt(content: str) -> bool:
    text = content or ""
    markers = (
        "当前群暂无可用历史消息",
        "不要提到系统、任务或 AI",
        "不要提到系统、任务或AI",
        "生成自然开场",
        "刚看到大家提到“刚看到大家提到",
        "[已撤回的内部提示词",
    )
    return (
        any(marker in text for marker in markers)
        or looks_like_generated_template_noise(text)
        or looks_like_operator_ui_content(text)
    )


def _looks_like_generated_noise(content: str) -> bool:
    text = content or ""
    if _looks_like_internal_prompt(text):
        return True
    return text.count("“") + text.count("”") >= 4


def _drop_repeated_ai_messages(contents: list[str], previous_messages: list[str]) -> list[str]:
    accepted: list[str] = []
    seen_starts: set[str] = set()
    for content in contents:
        normalized = _normalize_for_similarity(content)
        if not normalized:
            continue
        start = normalized[:8]
        if start in seen_starts:
            continue
        if any(_similarity(normalized, _normalize_for_similarity(previous)) >= 0.62 for previous in previous_messages):
            continue
        if any(_similarity(normalized, _normalize_for_similarity(existing)) >= 0.68 for existing in accepted):
            continue
        seen_starts.add(start)
        accepted.append(content)
    return accepted


def _normalize_for_similarity(content: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;：:\"'“”‘’（）()\[\]【】]+", "", (content or "").lower())


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


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


def _role_for_account(account_id: int, index: int, config: dict) -> str:
    personas = config.get("account_personas") if isinstance(config.get("account_personas"), dict) else {}
    role = personas.get(str(account_id)) or personas.get(account_id)
    if role:
        return str(role)
    return _role_for_turn(index)


def _role_for_turn(index: int) -> str:
    roles = ["引导型账号", "补充型账号", "提问型账号", "总结型账号", "轻松闲聊型账号"]
    return roles[index % len(roles)]


def _intent_for_turn(index: int) -> str:
    intents = ["回应上下文", "补充信息", "引出讨论", "轻量总结", "承接话题"]
    return intents[index % len(intents)]


__all__ = ["ai_cycle_mode", "build_plan"]
