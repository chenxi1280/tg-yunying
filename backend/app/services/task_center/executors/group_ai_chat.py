from __future__ import annotations

import random
from datetime import datetime, time, timedelta
from difflib import SequenceMatcher
import re

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Action, OperationTarget, RuleSet, Task, TgGroup
from app.services._common import _now
from app.services.account_online_state import is_account_online_ready_for_planning, reconcile_runtime_online_sources
from app.services.account_capacity import (
    AccountCapacityCache,
    AccountCapacityReservation,
    available_accounts_by_capacity,
    next_capacity_window,
)
from app.services.content_filters import contains_coarse_language, filter_outbound_content, looks_like_generated_template_noise, looks_like_operator_ui_content
from app.services.group_listeners import collect_group_context, recent_context_messages
from app.services.target_learning_audit import audit_learning_profile_use
from app.services.tenant_target_profile import tenant_learning_profile_preview
from app.services.rule_engine import apply_output_policy, bound_rule_version, evaluate_input_filter
from app.services.material_rules import select_material_for_policy

from ..account_pool import DAILY_COVERAGE_SUCCESS_STATUSES, daily_account_coverage_counts, daily_uncovered_account_count, select_task_accounts
from ..ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable, generate_group_messages, generate_group_reply_messages
from ..ai_message_memory import DuplicateMessageReservation, mark_group_ai_message_result, reserve_group_ai_message
from ..account_voice_profiles import group_stance_summaries, voice_profile_prompt_details
from ..channel_membership import gate_channel_membership
from ..config_normalization import normalize_operation_target_references
from ..fingerprints import fingerprint_exists, remember_fingerprint
from ..hard_hourly import current_progress, enabled as hard_hourly_enabled, hard_schedule_times, mark_plan_result
from ..listener_runtime import should_collect_listener
from ..pacing import current_hour_rounds, operation_intensity, schedule_times
from ..payloads import SendMessagePayload, create_send_action
from ..targets import group_from_reference
from .common import add_tokens, stats_inc


WAITING_NEW_CONTEXT_MESSAGE = "暂无新的真人上下文，等待群内新消息"
WAITING_IDLE_CONTINUATION_MESSAGE = "持续监听中，等待新消息或空闲续聊间隔"
AI_QUALITY_ANCHOR_SKIP_MESSAGE = "AI 候选缺少事实锚点，已跳过本轮"
AI_QUALITY_DUPLICATE_SKIP_MESSAGE = "AI 候选语义重复风险过高，已跳过本轮"
AI_NORMAL_CANDIDATE_SHORTFALL_MESSAGE = "AI 普通发言候选不足，已跳过本轮"
ACCOUNT_CAPACITY_BLOCKED_MESSAGE = "账号容量已排满，等待账号额度恢复后继续执行"
ACCOUNT_COOLDOWN_BLOCKED_MESSAGE = "账号冷却中，等待冷却后继续执行"
ACCOUNT_UNAVAILABLE_MESSAGE = "没有可用账号，等待账号恢复后继续执行"
DEFAULT_IDLE_CONTINUATION_SECONDS = 300
DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 3600
MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS = 60
GROUP_CHAT_SCENE = "group_chat"
TARGET_HISTORY_PERMISSION_MARKERS = (
    "channelprivateerror",
    "lack permission",
    "private",
    "banned",
    "gethistoryrequest",
)

CHAT_MODE_REPLY = "reply"
CHAT_MODE_IDLE_WARMUP = "idle_warmup"
CHAT_MODE_BOOTSTRAP = "bootstrap"
AI_CHAT_ROUND_INTERVALS_SECONDS = {
    "高峰期": (20, 60),
    "正常期": (45, 120),
    "启动期": (60, 180),
    "低频期": (180, 360),
    "休眠期": (600, 1200),
    "静默期": (300, 900),
}
HARD_HOURLY_MIN_BATCH_MESSAGES = 10
HARD_HOURLY_DEFER_AI_MIN_GOAL = 100
DEFERRED_AI_HISTORY_MAX_CHARS = 1000
AI_GENERATION_REQUEST_BATCH_SIZE = 30
MAX_AI_QUALITY_GENERATION_ROUNDS = 3
LOW_RISK_EMOJI_FALLBACK_POOL = ("👍", "👌", "👀", "🤔", "😅", "😂", "🙈", "🤝", "💯", "🤣", "🫡", "🙂")
RECENT_CYCLE_SCAN_LIMIT = 200
RECENT_PLANNED_AI_STATUSES = ("pending", "claiming", "executing", "unknown_after_send")


def build_plan(session: Session, task: Task) -> int:
    config = {**(task.type_config or {}), "pacing_config": task.pacing_config or {}}
    config = _canonicalized_task_config(session, task, config)
    hard_progress = current_progress(session, task, _now()) if hard_hourly_enabled(task) else {}
    hard_progress = hard_progress if int(hard_progress.get("deficit") or 0) > 0 else {}
    rule_version = bound_rule_version(session, task)
    rule_set = session.get(RuleSet, rule_version.rule_set_id) if rule_version else None
    target = session.get(OperationTarget, int(config.get("target_operation_target_id") or 0)) if int(config.get("target_operation_target_id") or 0) else None
    if target and target.tenant_id == task.tenant_id and target.target_type == "group":
        gate = gate_channel_membership(session, task, target, require_send=True)
        if not gate.ready:
            if hard_progress:
                blocker = gate.blocker_reason or "target_membership_pending"
                mark_plan_result(task, hard_progress, 0, {blocker: max(1, int(hard_progress.get("deficit") or gate.created or 1))})
            return gate.created
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )
    if not group:
        task.last_error = "目标群不存在或未授权"
        if hard_progress:
            mark_plan_result(task, hard_progress, 0, {"target_permission": max(1, int(hard_progress.get("deficit") or 1))})
        return 0
    target_label = target.title if target and target.tenant_id == task.tenant_id else group.title
    config = _with_active_conversation_targets(config, group)
    accounts = _select_accounts_for_plan(session, task, group, hard_progress, config)
    _reconcile_online_sources_for_plan(session, task, accounts)
    accounts = _online_ready_accounts(session, task, accounts, hard_progress)
    if not accounts:
        error_message, reason = _account_shortage_reason(session, task, group, hard_progress)
        if int((task.stats or {}).get("account_offline_count") or 0) > 0:
            error_message, reason = "账号在线状态不可用，等待账号恢复在线后继续执行", "account_offline"
        task.last_error = error_message
        if hard_progress:
            mark_plan_result(task, hard_progress, 0, {reason: max(1, int(hard_progress.get("deficit") or 1))})
        return 0
    history_depth = int(config.get("chat_history_depth") or 50)
    needs_context_refresh = _should_refresh_context_for_plan(session, group, history_depth, hard_progress)
    if should_collect_listener("group", group.id, window_seconds=group.listener_interval_seconds) and needs_context_refresh:
        try:
            _collect_context_with_candidate_accounts(session, task, group, _history_collect_account_ids(config, accounts))
        except Exception as exc:
            if not _is_target_history_permission_error(exc):
                raise
            if hard_progress:
                _record_history_collect_degraded(task, exc)
            else:
                task.last_error = f"监听账号无法读取目标群历史：{exc}"
                return 0
    fingerprint_source = f"{task.id}:group_ai_chat:{group.id}"
    history_rows = recent_context_messages(session, group, history_depth)
    context_rows = list(reversed(history_rows[-history_depth:]))
    usable_context_rows = _topic_relevant_context_rows(
        config,
        [row for row in context_rows if _is_human_context_row(row) and _is_usable_context_message(row.content)],
    )
    mode, ramp_ratio = ai_cycle_mode(config, task.scheduled_start)
    unprocessed_rows = [
        row
        for row in usable_context_rows
        if not fingerprint_exists(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    ]
    force_bootstrap_once = bool((task.stats or {}).get("force_bootstrap_once"))
    repeat_window = _semantic_repeat_window(config)
    previous_ai_messages = _recent_ai_messages(session, task, limit=repeat_window)
    planned_ai_messages = _recent_planned_ai_messages(session, task, limit=repeat_window)
    duplicate_baseline_messages = [*previous_ai_messages, *planned_ai_messages]
    idle_continuation = False
    if not hard_progress and not force_bootstrap_once and _should_wait_for_human_context(session, task, usable_context_rows, unprocessed_rows):
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
    cycle_index = _next_cycle_index(session, task)
    round_config = _hard_hourly_round_config(config, hard_progress)
    selected, turn_count = _select_cycle_accounts(
        accounts,
        round_config,
        mode,
        ramp_ratio,
        has_context=bool(usable_context_rows),
        cycle_index=cycle_index,
        pacing_config=task.pacing_config or {},
        daily_coverage_uncovered_count=_daily_coverage_uncovered_count(session, task, accounts, hard_progress, config),
    )
    planned_times = _schedule_times_for_plan(task, hard_progress, turn_count, mode)
    turn_count, planned_times = _limit_context_bound_turns(
        task,
        config,
        has_context=bool(usable_context_rows),
        progress=hard_progress,
        turn_count=turn_count,
        planned_times=planned_times,
    )
    selected = selected[: max(1, min(len(selected), turn_count))]
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
            if hard_progress:
                _mark_hard_blocked(task, hard_progress, "input_filter")
            return 0
    topic_thread = _topic_thread_summary(config, group, usable_context_rows, previous_ai_messages)
    topic_plan = _topic_plan_summary(config, group, topic_thread, turn_count)
    account_memories = _recent_account_memories(session, task, [account.id for account in selected], depth=int(config.get("account_memory_depth") or 3))
    account_profiles = account_profile_summaries(session, task, [account.id for account in selected])
    voice_profiles = voice_profile_prompt_details(session, tenant_id=task.tenant_id, account_ids=[account.id for account in selected])
    stance_summaries = group_stance_summaries(session, tenant_id=task.tenant_id, group_id=group.id, account_ids=[account.id for account in selected])
    profile_preview = tenant_learning_profile_preview(session, task.tenant_id, GROUP_CHAT_SCENE)
    audit_learning_profile_use(session, task, profile_preview, "AI活群任务")
    account_prompt_profiles = _account_prompt_profiles(account_profiles, voice_profiles, stance_summaries)
    generation_config = _generation_config_with_profile(round_config, account_memories, account_prompt_profiles, topic_thread, topic_plan, profile_preview)
    cycle_id = f"{task.id}:cycle:{cycle_index}"
    reply_targets = _reply_targets_for_plan(session, task, group, usable_context_rows, turn_count, config, hard_progress)
    if reply_targets is None:
        return 0
    requested_reply_count = len(reply_targets)
    normal_count = max(0, turn_count - len(reply_targets))
    defer_ai_generation = _defer_ai_generation_for_plan(config, hard_progress)
    if defer_ai_generation:
        planned_items, tokens = _deferred_ai_planned_items(normal_count), 0
    else:
        try:
            quality_items, tokens, quality_stats = _generate_quality_filled_items(
                session,
                task,
                generation_config,
                reply_targets=reply_targets,
                normal_count=normal_count,
                target_label=target_label,
                history=history,
                turn_count=turn_count,
                duplicate_baseline_messages=duplicate_baseline_messages,
                chat_mode=_chat_mode(usable_context_rows, idle_continuation),
                context_message_ids=[int(row.id) for row in usable_context_rows[-history_depth:]],
                fact_anchor_required=bool(config.get("fact_anchor_required", True)),
                low_confidence_silence_enabled=bool(config.get("low_confidence_silence_enabled", True)),
                fill_reply_shortfall_with_normal=bool(hard_progress),
                enable_quality_fallback=bool(hard_progress) or _all_accounts_daily_coverage(config),
            )
        except AiGenerationUnavailable as exc:
            task.last_error = str(exc) or AI_GENERATION_UNAVAILABLE_MESSAGE
            stats = dict(task.stats or {})
            stats["current_mode"] = mode
            stats["ramp_ratio"] = ramp_ratio
            stats["context_mode"] = _context_mode(usable_context_rows, idle_continuation)
            task.stats = stats
            if hard_progress:
                mark_plan_result(task, hard_progress, 0, {"ai_generation_unavailable": int(hard_progress.get("deficit") or 1)})
            return 0
    chat_mode = _chat_mode(usable_context_rows, idle_continuation)
    context_message_ids = [int(row.id) for row in usable_context_rows[-history_depth:]]
    if defer_ai_generation:
        quality_items, quality_stats = planned_items[:turn_count], {}
    if not quality_items:
        _mark_quality_skip(task, config, mode, ramp_ratio, _context_mode(usable_context_rows, idle_continuation), chat_mode, quality_stats)
        if hard_progress:
            mark_plan_result(task, hard_progress, 0, {"quality_filter": int(hard_progress.get("deficit") or 1)})
        return 0
    coverage_counts = _coverage_counts_for_plan(session, task, selected, round_config)
    selected = _prioritize_accounts_for_plan(selected, account_memories, coverage_counts, round_config)
    add_tokens(task, tokens)
    times = _schedule_times_for_plan(task, hard_progress, len(quality_items), mode)
    quality_items, times = _limit_context_bound_quality_schedule(
        task,
        config,
        has_context=bool(usable_context_rows),
        progress=hard_progress,
        quality_items=quality_items,
        planned_times=times,
    )
    if not quality_items:
        task.last_error = "上下文绑定计划超出有效发送窗口，等待新上下文后继续执行"
        stats_inc(task, "skipped_count")
        return 0
    context_snapshot_message_id = max(context_message_ids) if context_message_ids else None
    used_account_ids: set[int] = set()
    allow_account_repeat = bool(round_config.get("allow_account_repeat", True))
    hard_blockers: dict[str, int] = {}
    prepared_actions: list[tuple[int, datetime, SendMessagePayload]] = []
    capacity_reservations: list[AccountCapacityReservation] = []
    capacity_cache = AccountCapacityCache()
    burst_plan = _consecutive_burst_plan(round_config, len(quality_items), allow_account_repeat, cycle_id)
    burst_account = None
    created = 0
    for index, quality_item in enumerate(quality_items):
        content = str(quality_item.get("content") or "")
        deferred_ai = bool(quality_item.get("defer_ai_generation"))
        if content and rule_version:
            policy_result = apply_output_policy(content, rule_version.output_checks or {}, rule_version.transforms or {})
            if not policy_result.allowed:
                _hard_blocker_inc(hard_blockers, "content_policy", hard_progress)
                stats_inc(task, "failure_count")
                continue
            content = policy_result.content
        planned_at = times[index]
        candidate_accounts = [burst_account] if burst_account and index in burst_plan else selected
        account, planned_at = _choose_capacity_slot(
            session,
            task,
            candidate_accounts,
            planned_at,
            index,
            used_account_ids,
            allow_account_repeat,
            hard_progress,
            capacity_reservations,
            capacity_cache,
        )
        if not account:
            _hard_blocker_inc(hard_blockers, "account_capacity", hard_progress)
            stats_inc(task, "skipped_count")
            continue
        if burst_plan and index == min(burst_plan):
            burst_account = account
        used_account_ids.add(account.id)
        has_native_reply = _reply_target_message_id(quality_item) is not None
        filtered_content = content
        if content:
            filtered = filter_outbound_content(session, tenant_id=task.tenant_id, group=group, content=content, reject_mentions=True, reject_replies=not has_native_reply)
            if not filtered.ok:
                _hard_blocker_inc(hard_blockers, "content_policy", hard_progress)
                stats_inc(task, "failure_count")
                continue
            filtered_content = filtered.content
        material_result = select_material_for_policy(
            session,
            task.tenant_id,
            (rule_version.routing or {}).get("material_policy") if rule_version else {},
            context_key=f"{cycle_id}:{index}:{filtered_content or 'pending-ai'}",
            default_caption="",
        )
        if material_result.failure_reason and material_result.fallback == "skip":
            _hard_blocker_inc(hard_blockers, "content_policy", hard_progress)
            stats_inc(task, "failure_count")
            continue
        media_segments = [material_result.segment] if material_result.ok and material_result.segment else []
        coverage_payload = _coverage_payload_for_account(round_config, account.id, coverage_counts)
        act_type = _act_type_for_turn(index, quality_item)
        try:
            memory = _reserve_planned_message_memory(session, task, group, account.id, filtered_content, config)
        except DuplicateMessageReservation:
            _hard_blocker_inc(hard_blockers, "duplicate_message", hard_progress)
            stats_inc(task, "skipped_count")
            continue
        voice_profile = voice_profiles.get(account.id, {})
        prepared_actions.append(
            (
                account.id,
                planned_at,
                SendMessagePayload(
                    chat_id=group.tg_peer_id,
                    group_id=group.id,
                    operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
                    target_display=target_label,
                    message_text=filtered_content,
                    media_segments=media_segments,
                    review_approved=True,
                    cycle_id=cycle_id,
                    turn_index=index + 1,
                    account_role=_role_for_account(account.id, index, config),
                    account_memory=account_memories.get(str(account.id), ""),
                    account_profile=account_prompt_profiles.get(str(account.id), ""),
                    slot_id=_slot_id(cycle_id, index),
                    act_type=act_type,
                    account_voice_profile_version=int(voice_profile.get("version") or 0),
                    account_voice_profile_summary=str(voice_profile.get("summary") or ""),
                    stance_summary=stance_summaries.get(account.id, ""),
                    ai_message_memory_id=memory.id if memory else "",
                    rewrite_attempts=0,
                    human_quality_decision=str(quality_item.get("human_quality_decision") or "accepted"),
                    topic_direction=dict(config.get("active_topic_direction") or {}),
                    teacher_target=dict(config.get("active_teacher_target") or {}),
                    **burst_plan.get(index, {}),
                    **coverage_payload,
                    topic_thread=topic_thread,
                    topic_plan=topic_plan,
                    intent=_intent_for_turn(index),
                    chat_mode=chat_mode,
                    anchor_message_ids=context_message_ids,
                    semantic_cluster=str(quality_item.get("semantic_cluster") or ""),
                    duplicate_risk=str(quality_item.get("duplicate_risk") or ""),
                    hallucination_risk=str(quality_item.get("hallucination_risk") or ""),
                    quality_skip_reason=str(quality_item.get("quality_skip_reason") or ""),
                    context_message_ids=context_message_ids,
                    context_snapshot_message_id=context_snapshot_message_id,
                    context_expire_after_messages=int(config.get("context_expire_after_messages") or 0),
                    ai_generation_id=cycle_id,
                    ai_generation_status="pending" if deferred_ai else "success",
                    ai_generation_history=_deferred_ai_history(history) if deferred_ai else "",
                    ai_generation_tokens=tokens,
                    ai_generation_count=len(quality_items),
                    hard_hourly_target=bool(hard_progress),
                    hard_hourly_bucket=str(hard_progress.get("bucket") or ""),
                    hard_hourly_deficit_at_plan=int(hard_progress.get("deficit") or 0),
                    ai_generation_context_count=len(context_message_ids),
                    ai_generation_memory_count=len(account_memories),
                    profile_scene=str(profile_preview.get("profile_scene") or GROUP_CHAT_SCENE),
                    profile_version=int(profile_preview.get("profile_version") or 0),
                    profile_hit_summary=str(profile_preview.get("profile_hit_summary") or ""),
                    profile_unavailable_reason=str(profile_preview.get("profile_unavailable_reason") or ""),
                    rule_set_id=rule_version.rule_set_id if rule_version else None,
                    rule_set_name=rule_set.name if rule_set else "",
                    rule_set_version_id=rule_version.id if rule_version else None,
                    resolved_rule_set_version_id=rule_version.id if rule_version else None,
                    rule_set_version=rule_version.version if rule_version else None,
                    rule_binding_mode="fixed_version" if rule_version and config.get("rule_set_version_id") else "follow_current" if rule_version else "",
                    reply_to_message_id=_reply_target_message_id(quality_item),
                    reply_target_label=_reply_target_label(quality_item),
                    reply_target_author=_reply_target_text(quality_item, "author"),
                    reply_target_preview=_reply_target_text(quality_item, "preview"),
                    reply_target_source=_reply_target_text(quality_item, "source"),
                    rule_trace={
                        "material_policy": (rule_version.routing or {}).get("material_policy") if rule_version else {},
                        "material_action": material_result.action,
                        "material_id": material_result.selected.id if material_result.selected else None,
                        "material_failure_reason": material_result.failure_reason,
                    },
                ),
            )
        )
        _increment_coverage_count(round_config, account.id, coverage_counts)
        capacity_reservations.append(
            AccountCapacityReservation(account_id=account.id, scheduled_at=planned_at)
        )
    prepared_reply_count = sum(1 for _account_id, _planned_at, payload in prepared_actions if payload.reply_to_message_id)
    if prepared_reply_count < requested_reply_count:
        stats_inc(task, "reply_candidate_shortfall_count")
        if not hard_progress:
            task.last_error = "AI 引用回复候选不足，已跳过本轮"
            return 0
    for account_id, planned_at, payload in prepared_actions:
        action = create_send_action(session, task, account_id, planned_at, payload)
        if payload.ai_message_memory_id:
            mark_group_ai_message_result(session, payload.ai_message_memory_id, status="reserved", action_id=action.id)
        created += 1
    for row in unprocessed_rows:
        remember_fingerprint(session, task.tenant_id, fingerprint_source, _context_fingerprint(row))
    stats = dict(task.stats or {})
    stats["current_mode"] = mode
    stats["ramp_ratio"] = ramp_ratio
    stats["context_mode"] = _context_mode(usable_context_rows, idle_continuation)
    stats["chat_mode"] = chat_mode
    stats["reply_planned_count"] = prepared_reply_count
    if quality_stats.get("duplicate_risk"):
        stats["duplicate_risk"] = quality_stats["duplicate_risk"]
    else:
        stats.pop("duplicate_risk", None)
    if quality_stats.get("hallucination_risk"):
        stats["hallucination_risk"] = quality_stats["hallucination_risk"]
    else:
        stats.pop("hallucination_risk", None)
    if quality_stats.get("ai_generation_rounds"):
        stats["ai_generation_rounds"] = quality_stats["ai_generation_rounds"]
    if quality_stats.get("quality_fill_rounds"):
        stats["quality_fill_rounds"] = quality_stats["quality_fill_rounds"]
    if quality_stats.get("quality_fallback_count"):
        stats["quality_fallback_count"] = quality_stats["quality_fallback_count"]
    stats.pop("skip_reason", None)
    stats.pop("idle_continuation_next_run_at", None)
    stats.pop("force_bootstrap_once", None)
    task.last_error = _hard_blocked_last_error(created, hard_blockers, hard_progress)
    task.stats = stats
    if hard_progress:
        mark_plan_result(task, hard_progress, created, hard_blockers or None)
    stats_inc(task, "total_rounds")
    return created


def _canonicalized_task_config(session: Session, task: Task, config: dict) -> dict:
    normalized = normalize_operation_target_references(session, task.tenant_id, task.type, config)
    if normalized != config:
        task.type_config = {key: value for key, value in normalized.items() if key != "pacing_config"}
    return normalized


def _reply_targets_for_plan(
    session: Session,
    task: Task,
    group: TgGroup,
    usable_context_rows: list,
    turn_count: int,
    config: dict,
    hard_progress: dict[str, object],
) -> list[dict] | None:
    if hard_progress:
        return []
    reply_min = min(turn_count, int(config.get("reply_min_per_round") or 0))
    if reply_min <= 0:
        return []
    reply_target_pool = _group_reply_target_pool(session, task, group, usable_context_rows)
    if reply_min > len(reply_target_pool):
        stats_inc(task, "reply_target_shortfall_count")
        task.last_error = "可引用消息不足，等待监听到可回复消息后继续执行"
        return None
    return reply_target_pool[:reply_min]


def _hard_blocked_last_error(created: int, blockers: dict[str, int], progress: dict[str, object]) -> str:
    if created > 0 or not progress:
        return ""
    if blockers.get("account_capacity"):
        return ACCOUNT_CAPACITY_BLOCKED_MESSAGE
    return ""


def _choose_capacity_slot(
    session: Session,
    task: Task,
    selected: list,
    planned_at: datetime,
    index: int,
    used_account_ids: set[int],
    allow_repeat: bool,
    progress: dict[str, object],
    reservations: list[AccountCapacityReservation],
    capacity_cache: AccountCapacityCache,
) -> tuple[object | None, datetime]:
    if progress:
        # Hard-hourly targets are explicit quota commitments; claim/dispatch records the capacity override.
        return _choose_turn_account(selected, selected, index, used_account_ids, allow_repeat), planned_at
    candidate_limit = _capacity_candidate_limit(used_account_ids)
    available = _available_accounts_at(session, task, selected, planned_at, reservations, capacity_cache, limit=candidate_limit)
    account = _choose_turn_account(available, available, index, used_account_ids, allow_repeat)
    if account:
        return account, planned_at
    decision = next_capacity_window(
        session,
        tenant_id=task.tenant_id,
        account_ids=[item.id for item in selected],
        scheduled_at=planned_at,
        reservations=reservations,
        cache=capacity_cache,
    )
    if not decision.defer_until or _defer_crosses_hard_hour(progress, decision.defer_until):
        return None, planned_at
    deferred_available = _available_accounts_at(session, task, selected, decision.defer_until, reservations, capacity_cache, limit=candidate_limit)
    account = _choose_turn_account(deferred_available, deferred_available, index, used_account_ids, allow_repeat)
    return (account, decision.defer_until) if account else (None, planned_at)


def _capacity_candidate_limit(used_account_ids: set[int]) -> int:
    return max(1, len(used_account_ids) + 1)


def _available_accounts_at(
    session: Session,
    task: Task,
    selected: list,
    scheduled_at: datetime,
    reservations: list[AccountCapacityReservation],
    capacity_cache: AccountCapacityCache,
    *,
    limit: int | None = None,
) -> list:
    return available_accounts_by_capacity(
        session,
        tenant_id=task.tenant_id,
        accounts=selected,
        scheduled_at=scheduled_at,
        reservations=reservations,
        cache=capacity_cache,
        limit=limit,
    )


def _defer_crosses_hard_hour(progress: dict[str, object], defer_until: datetime) -> bool:
    hour_end = progress.get("hour_end") if progress else None
    return isinstance(hour_end, datetime) and defer_until >= hour_end


def _select_accounts_for_plan(
    session: Session,
    task: Task,
    group: TgGroup,
    progress: dict[str, object],
    config: dict,
) -> list:
    options = _hard_hourly_account_options(progress)
    if progress:
        options["enforce_capacity"] = False
    coverage_options = _daily_coverage_account_options(config)
    return select_task_accounts(
        session,
        task.tenant_id,
        task.account_config or {},
        target_group_id=group.id,
        daily_coverage_task_id=task.id,
        daily_coverage_action_types=("send_message",),
        **coverage_options,
        **options,
    )


def _online_ready_accounts(session: Session, task: Task, accounts: list, progress: dict[str, object]) -> list:
    ready = [
        account
        for account in accounts
        if is_account_online_ready_for_planning(session, tenant_id=task.tenant_id, account_id=account.id)
    ]
    offline_count = max(0, len(accounts) - len(ready))
    if offline_count:
        stats = dict(task.stats or {})
        stats["account_offline_count"] = offline_count
        task.stats = stats
        if progress:
            progress["account_offline_count"] = offline_count
    return ready


def _reconcile_online_sources_for_plan(session: Session, task: Task, accounts: list) -> None:
    if accounts:
        reconcile_runtime_online_sources(session, tenant_id=task.tenant_id)


def _daily_coverage_uncovered_count(
    session: Session,
    task: Task,
    accounts: list,
    progress: dict[str, object],
    config: dict,
) -> int:
    if not _all_accounts_daily_coverage(config):
        uncovered = daily_uncovered_account_count(session, task.id, ("send_message",), accounts)
        return min(uncovered, max(0, int(progress.get("deficit") or 0))) if progress else uncovered
    uncovered = daily_uncovered_account_count(
        session,
        task.id,
        ("send_message",),
        accounts,
        count_empty_as_uncovered=True,
        target_count=_coverage_target_per_account(config),
        statuses=DAILY_COVERAGE_SUCCESS_STATUSES,
    )
    if not progress:
        return uncovered
    return min(uncovered, max(0, int(progress.get("deficit") or 0)))


def _daily_coverage_account_options(config: dict) -> dict[str, object]:
    if not _all_accounts_daily_coverage(config):
        return {}
    return {
        "daily_coverage_target_count": _coverage_target_per_account(config),
        "daily_coverage_statuses": DAILY_COVERAGE_SUCCESS_STATUSES,
    }


def _all_accounts_daily_coverage(config: dict) -> bool:
    return config.get("account_coverage_mode") == "all_accounts_daily"


def _coverage_target_per_account(config: dict) -> int:
    try:
        value = int(config.get("per_account_daily_min_messages") or 1)
    except (TypeError, ValueError):
        value = 1
    return max(1, min(2, value))


def _coverage_counts_for_plan(session: Session, task: Task, accounts: list, config: dict) -> dict[int, int]:
    if not _all_accounts_daily_coverage(config):
        return {}
    return daily_account_coverage_counts(
        session,
        task.id,
        ("send_message",),
        [int(account.id) for account in accounts],
        statuses=DAILY_COVERAGE_SUCCESS_STATUSES,
    )


def _coverage_payload_for_account(config: dict, account_id: int, counts: dict[int, int]) -> dict[str, object]:
    if not _all_accounts_daily_coverage(config):
        return {}
    target = _coverage_target_per_account(config)
    completed = max(0, int(counts.get(int(account_id), 0)))
    remaining = max(0, target - completed)
    return {
        "account_coverage_mode": "all_accounts_daily",
        "coverage_window_date": _now().date().isoformat(),
        "coverage_target_per_account": target,
        "coverage_account_completed_before_action": completed,
        "coverage_account_remaining_before_action": remaining,
        "coverage_reason": "daily_account_coverage" if remaining else "",
    }


def _account_prompt_profiles(
    account_profiles: dict[str, str],
    voice_profiles: dict[int, dict[str, str | int]],
    stance_summaries: dict[int, str],
) -> dict[str, str]:
    account_ids = set(int(account_id) for account_id in account_profiles.keys())
    account_ids.update(voice_profiles.keys())
    account_ids.update(stance_summaries.keys())
    result: dict[str, str] = {}
    for account_id in account_ids:
        parts = [
            str(account_profiles.get(str(account_id)) or "").strip(),
            str((voice_profiles.get(account_id) or {}).get("summary") or "").strip(),
            f"短期立场：{stance_summaries[account_id]}" if stance_summaries.get(account_id) else "",
        ]
        result[str(account_id)] = "；".join(part for part in parts if part)
    return result


def _slot_id(cycle_id: str, index: int) -> str:
    return f"{cycle_id}:turn:{index + 1}"


def _act_type_for_turn(index: int, quality_item: dict) -> str:
    if quality_item.get("quality_fallback") == "emoji_react":
        return "emoji_react"
    if _reply_target_message_id(quality_item) is not None:
        return "context_reply"
    act_types = ("short_react", "detail_follow", "light_question", "side_comment")
    return act_types[index % len(act_types)]


def _reserve_planned_message_memory(
    session: Session,
    task: Task,
    group: TgGroup,
    account_id: int,
    content: str,
    config: dict,
):
    if not content.strip():
        return None
    return reserve_group_ai_message(
        session,
        tenant_id=task.tenant_id,
        group_id=group.id,
        task_id=task.id,
        account_id=account_id,
        raw_text=content,
        topic_direction=_active_topic_text(config, group),
        teacher_target=_active_teacher_text(config),
    )


def _increment_coverage_count(config: dict, account_id: int, counts: dict[int, int]) -> None:
    if _all_accounts_daily_coverage(config):
        counts[int(account_id)] = int(counts.get(int(account_id), 0)) + 1


def _account_shortage_reason(
    session: Session,
    task: Task,
    group: TgGroup,
    progress: dict[str, object],
) -> tuple[str, str]:
    options = _hard_hourly_account_options(progress)
    if _has_account_candidate(session, task, group, task.account_config or {}, options):
        return ACCOUNT_CAPACITY_BLOCKED_MESSAGE, "account_capacity"
    no_cooldown_config = dict(task.account_config or {})
    no_cooldown_config["cooldown_per_account_minutes"] = 0
    if _has_account_candidate(session, task, group, no_cooldown_config, options):
        return ACCOUNT_COOLDOWN_BLOCKED_MESSAGE, "account_cooldown"
    return ACCOUNT_UNAVAILABLE_MESSAGE, "account_unavailable"


def _has_account_candidate(
    session: Session,
    task: Task,
    group: TgGroup,
    account_config: dict,
    options: dict[str, object],
) -> bool:
    return bool(
        select_task_accounts(
            session,
            task.tenant_id,
            account_config,
            target_group_id=group.id,
            enforce_capacity=False,
            **options,
        )
    )


def _hard_hourly_account_options(progress: dict[str, object]) -> dict[str, object]:
    if not progress:
        return {}
    return {
        "limit": _hard_hourly_account_scan_target(progress),
        "enforce_max_concurrent": False,
    }


def _hard_hourly_account_scan_target(progress: dict[str, object]) -> int:
    goal = max(0, int(progress.get("goal") or 0))
    deficit = max(0, int(progress.get("deficit") or 0))
    return max(HARD_HOURLY_MIN_BATCH_MESSAGES, goal, deficit)


def _defer_ai_generation_for_plan(config: dict, progress: dict[str, object]) -> bool:
    if not progress or not bool(config.get("hard_hourly_defer_ai_generation", True)):
        return False
    return int(progress.get("goal") or 0) >= HARD_HOURLY_DEFER_AI_MIN_GOAL


def _deferred_ai_planned_items(count: int) -> list[dict]:
    return [{"content": "", "reply_target": None, "defer_ai_generation": True} for _index in range(max(0, int(count or 0)))]


def _deferred_ai_history(history: str) -> str:
    value = str(history or "").strip()
    return value[-DEFERRED_AI_HISTORY_MAX_CHARS:] if value else ""


def _generate_group_planned_items(
    session: Session,
    task: Task,
    config: dict,
    *,
    reply_targets: list[dict],
    normal_count: int,
    target_label: str,
    history: str,
    fill_reply_shortfall_with_normal: bool = False,
) -> tuple[list[dict], int]:
    items: list[dict] = []
    tokens = 0
    if reply_targets:
        contents, used_tokens = generate_group_reply_messages(
            session,
            task.tenant_id,
            config,
            reply_targets=reply_targets,
            target_label=target_label,
            history=history,
        )
        if len(contents) < len(reply_targets):
            stats_inc(task, "reply_candidate_shortfall_count")
            shortfall = len(reply_targets) - len(contents)
            if not fill_reply_shortfall_with_normal:
                raise AiGenerationUnavailable("AI 引用回复候选不足，已跳过本轮")
            normal_count += shortfall
        tokens += used_tokens
        items.extend({"content": content, "reply_target": target} for content, target in zip(contents, reply_targets, strict=False))
    if normal_count > 0:
        for batch_count in _normal_generation_batches(normal_count):
            contents, used_tokens = generate_group_messages(
                session,
                task.tenant_id,
                config,
                count=batch_count,
                target_label=target_label,
                history=history,
            )
            if len(contents) < batch_count:
                stats_inc(task, "normal_candidate_shortfall_count")
                raise AiGenerationUnavailable(AI_NORMAL_CANDIDATE_SHORTFALL_MESSAGE)
            tokens += used_tokens
            items.extend({"content": content, "reply_target": None} for content in contents)
    return items, tokens


def _generate_quality_filled_items(
    session: Session, task: Task, config: dict, *, reply_targets: list[dict], normal_count: int,
    target_label: str, history: str, turn_count: int, duplicate_baseline_messages: list[str],
    chat_mode: str, context_message_ids: list[int], fact_anchor_required: bool,
    low_confidence_silence_enabled: bool, fill_reply_shortfall_with_normal: bool,
    enable_quality_fallback: bool,
) -> tuple[list[dict[str, str]], int, dict[str, object]]:
    accepted: list[dict[str, str]] = []
    tokens = 0
    stats: dict[str, object] = {}
    for round_index in range(MAX_AI_QUALITY_GENERATION_ROUNDS):
        remaining = max(0, int(turn_count or 0) - len(accepted))
        if remaining <= 0:
            break
        planned_items, used_tokens = _generate_quality_round_planned_items(
            session,
            task,
            config,
            reply_targets=reply_targets,
            normal_count=normal_count,
            target_label=target_label,
            history=history,
            fill_reply_shortfall_with_normal=fill_reply_shortfall_with_normal,
            round_index=round_index,
            remaining=remaining,
        )
        tokens += used_tokens
        accepted = _accept_quality_round(
            accepted,
            planned_items,
            duplicate_baseline_messages,
            chat_mode=chat_mode,
            context_message_ids=context_message_ids,
            fact_anchor_required=fact_anchor_required,
            low_confidence_silence_enabled=low_confidence_silence_enabled,
            remaining=remaining,
            stats=stats,
        )
        stats["ai_generation_rounds"] = round_index + 1
        if len(accepted) < int(turn_count or 0):
            stats["quality_fill_rounds"] = round_index + 1
    if len(accepted) < int(turn_count or 0) and enable_quality_fallback:
        fallback = _emoji_fallback_items(int(turn_count or 0) - len(accepted), accepted)
        accepted.extend(fallback)
        stats["quality_fallback_count"] = len(fallback)
    return accepted[: max(0, int(turn_count or 0))], tokens, stats


def _generate_quality_round_planned_items(
    session: Session,
    task: Task,
    config: dict,
    *,
    reply_targets: list[dict],
    normal_count: int,
    target_label: str,
    history: str,
    fill_reply_shortfall_with_normal: bool,
    round_index: int,
    remaining: int,
) -> tuple[list[dict], int]:
    return _generate_group_planned_items(
        session,
        task,
        config,
        reply_targets=reply_targets if round_index == 0 else [],
        normal_count=normal_count if round_index == 0 else remaining,
        target_label=target_label,
        history=history,
        fill_reply_shortfall_with_normal=fill_reply_shortfall_with_normal,
    )


def _accept_quality_round(
    accepted: list[dict[str, str]],
    planned_items: list[dict],
    duplicate_baseline_messages: list[str],
    *,
    chat_mode: str,
    context_message_ids: list[int],
    fact_anchor_required: bool,
    low_confidence_silence_enabled: bool,
    remaining: int,
    stats: dict[str, object],
) -> list[dict[str, str]]:
    previous_messages = [*duplicate_baseline_messages, *[item["content"] for item in accepted]]
    clean_items = [item for item in planned_items if not _looks_like_generated_noise(item["content"])]
    clean_items = _drop_repeated_planned_items(clean_items, previous_messages)
    quality_items, quality_stats = _quality_filter_ai_messages(
        [item["content"] for item in clean_items],
        previous_messages,
        chat_mode=chat_mode,
        anchor_message_ids=context_message_ids,
        fact_anchor_required=fact_anchor_required,
        low_confidence_silence_enabled=low_confidence_silence_enabled,
        limit=remaining,
    )
    stats.update({key: value for key, value in quality_stats.items() if value})
    return [*accepted, *_attach_reply_targets(quality_items, clean_items)[:remaining]]


def _emoji_fallback_items(count: int, accepted: list[dict[str, str]]) -> list[dict[str, str]]:
    used = {item["content"] for item in accepted}
    available = [emoji for emoji in LOW_RISK_EMOJI_FALLBACK_POOL if emoji not in used]
    selected = random.sample(available, k=min(max(0, int(count or 0)), len(available)))
    return [
        {
            "content": emoji,
            "semantic_cluster": "",
            "duplicate_risk": "",
            "hallucination_risk": "",
            "quality_skip_reason": "quality_fallback",
            "quality_fallback": "emoji_react",
            "human_quality_decision": "quality_fallback",
        }
        for emoji in selected
    ]


def _normal_generation_batches(total: int) -> list[int]:
    remaining = max(0, int(total or 0))
    batches: list[int] = []
    while remaining > 0:
        batch_count = min(remaining, AI_GENERATION_REQUEST_BATCH_SIZE)
        batches.append(batch_count)
        remaining -= batch_count
    return batches


def _group_reply_target_pool(session: Session, task: Task, group: TgGroup, rows: list) -> list[dict]:
    targets = [_reply_target_from_context_row(row) for row in reversed(rows) if _reply_target_from_context_row(row)]
    targets.extend(_historical_group_reply_targets(session, task, group))
    deduped = _dedupe_reply_targets(targets)
    return _exclude_used_reply_targets(deduped, _used_group_reply_target_ids(session, task, group, _reply_message_ids(deduped)))


def _dedupe_reply_targets(targets: list[dict]) -> list[dict]:
    seen: set[int] = set()
    deduped: list[dict] = []
    for target in targets:
        message_id = int(target.get("message_id") or 0)
        if not message_id or message_id in seen:
            continue
        seen.add(message_id)
        deduped.append(target)
    return deduped


def _exclude_used_reply_targets(targets: list[dict], used_ids: set[int]) -> list[dict]:
    if not used_ids:
        return targets
    return [target for target in targets if int(target.get("message_id") or 0) not in used_ids]


def _reply_message_ids(targets: list[dict]) -> set[int]:
    return {message_id for target in targets if (message_id := int(target.get("message_id") or 0))}


def _reply_target_from_context_row(row) -> dict | None:
    message_id = _context_remote_message_id(row)
    preview = str(getattr(row, "content", "") or "").strip()
    if not message_id or not preview:
        return None
    return {
        "message_id": message_id,
        "author": str(getattr(row, "sender_name", "") or "群友").strip(),
        "preview": preview[:120],
        "source": "human_context",
    }


def _context_remote_message_id(row) -> int:
    raw = str(getattr(row, "remote_message_id", "") or "").strip()
    if raw.isdigit():
        return int(raw)
    return 0


def _historical_group_reply_targets(session: Session, task: Task, group: TgGroup, *, limit: int = 20) -> list[dict]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.tenant_id == task.tenant_id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
            Action.payload["group_id"].as_integer() == group.id,
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    return [target for action in rows if (target := _reply_target_from_action(action, group))]


def _used_group_reply_target_ids(session: Session, task: Task, group: TgGroup, candidate_ids: set[int]) -> set[int]:
    if not candidate_ids:
        return set()
    rows = session.scalars(
        select(Action.payload["reply_to_message_id"].as_integer()).where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.payload["group_id"].as_integer() == group.id,
            Action.payload["reply_to_message_id"].as_integer().in_(candidate_ids),
        )
    )
    return {int(row) for row in rows if row}


def _payload_int(action: Action, key: str) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    raw = str(payload.get(key) or "").strip()
    return int(raw) if raw.isdigit() else 0


def _reply_target_from_action(action: Action, group: TgGroup) -> dict | None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    result = action.result if isinstance(action.result, dict) else {}
    raw_id = str(result.get("remote_message_id") or result.get("message_id") or "").strip()
    content = str(payload.get("message_text") or "").strip()
    if not raw_id.isdigit() or not content:
        return None
    return {
        "message_id": int(raw_id),
        "author": str(payload.get("account_role") or group.title or "历史账号").strip(),
        "preview": content[:120],
        "source": "own_history",
    }


def _drop_repeated_planned_items(items: list[dict], previous_messages: list[str]) -> list[dict]:
    normal_contents = [item["content"] for item in items if not item.get("reply_target")]
    remaining = _drop_repeated_ai_messages(normal_contents, previous_messages)
    accepted: list[dict] = []
    for item in items:
        if item.get("reply_target"):
            accepted.append(item)
            continue
        if not remaining or item["content"] != remaining[0]:
            continue
        accepted.append(item)
        remaining.pop(0)
    return accepted


def _attach_reply_targets(quality_items: list[dict[str, str]], planned_items: list[dict]) -> list[dict]:
    by_content: dict[str, dict | None] = {item["content"]: item.get("reply_target") for item in planned_items}
    return [{**item, "reply_target": by_content.get(item["content"])} for item in quality_items]


def _reply_target_message_id(item: dict) -> int | None:
    target = item.get("reply_target") if isinstance(item, dict) else None
    return int(target.get("message_id")) if isinstance(target, dict) and target.get("message_id") else None


def _reply_target_label(item: dict) -> str:
    message_id = _reply_target_message_id(item)
    return f"回复消息 #{message_id}" if message_id else ""


def _reply_target_text(item: dict, key: str) -> str:
    target = item.get("reply_target") if isinstance(item, dict) else None
    return str(target.get(key) or "") if isinstance(target, dict) else ""


def _choose_turn_account(available: list, selected: list, index: int, used_account_ids: set[int], allow_repeat: bool):
    candidates = available or selected
    for account in candidates:
        if account.id not in used_account_ids:
            return account
    if not allow_repeat:
        return None
    return candidates[index % len(candidates)] if candidates else None


def _schedule_times_for_plan(task: Task, progress: dict[str, object], total: int, mode: str) -> list[datetime]:
    return _hard_hourly_schedule(task, progress, total) or _round_schedule_times(total, task.pacing_config or {}, mode)


def _limit_context_bound_turns(
    task: Task,
    config: dict,
    *,
    has_context: bool,
    progress: dict[str, object],
    turn_count: int,
    planned_times: list[datetime],
) -> tuple[int, list[datetime]]:
    if not _requires_context_bound_window(config, has_context, progress):
        _clear_context_bound_limit_stats(task)
        return turn_count, planned_times
    window_seconds = _context_bound_schedule_window_seconds(config)
    cutoff = _now() + timedelta(seconds=window_seconds)
    allowed_count = len([time_item for time_item in planned_times if _naive_datetime(time_item) <= cutoff])
    limited_count = max(1, min(int(turn_count or 1), allowed_count))
    _record_context_bound_limit_stats(task, turn_count, limited_count, window_seconds)
    return limited_count, planned_times[:limited_count]


def _limit_context_bound_quality_schedule(
    task: Task,
    config: dict,
    *,
    has_context: bool,
    progress: dict[str, object],
    quality_items: list[dict[str, str]],
    planned_times: list[datetime],
) -> tuple[list[dict[str, str]], list[datetime]]:
    if not _requires_context_bound_window(config, has_context, progress):
        return quality_items, planned_times
    window_seconds = _context_bound_schedule_window_seconds(config)
    cutoff = _now() + timedelta(seconds=window_seconds)
    allowed_count = len([item for item in planned_times if _naive_datetime(item) <= cutoff])
    limited_count = min(len(quality_items), allowed_count)
    _record_context_bound_limit_stats(
        task,
        int((task.stats or {}).get("context_bound_requested_turns") or len(quality_items)),
        limited_count,
        window_seconds,
    )
    return quality_items[:limited_count], planned_times[:limited_count]


def _requires_context_bound_window(config: dict, has_context: bool, progress: dict[str, object]) -> bool:
    return bool(has_context) and not progress and int(config.get("context_expire_after_messages") or 0) > 0


def _context_bound_schedule_window_seconds(config: dict) -> int:
    try:
        value = int(config.get("context_bound_schedule_window_seconds") or DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS)
    except (TypeError, ValueError):
        value = DEFAULT_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS
    return max(MIN_CONTEXT_BOUND_SCHEDULE_WINDOW_SECONDS, value)


def _record_context_bound_limit_stats(task: Task, requested: int, planned: int, window_seconds: int) -> None:
    stats = dict(task.stats or {})
    stats["context_bound_requested_turns"] = int(requested or 0)
    stats["context_bound_planned_turns"] = int(planned or 0)
    stats["context_bound_schedule_window_seconds"] = int(window_seconds or 0)
    task.stats = stats


def _clear_context_bound_limit_stats(task: Task) -> None:
    stats = dict(task.stats or {})
    for key in ("context_bound_requested_turns", "context_bound_planned_turns", "context_bound_schedule_window_seconds"):
        stats.pop(key, None)
    task.stats = stats


def _round_schedule_times(total: int, pacing_config: dict, mode: str) -> list[datetime]:
    if not (pacing_config or {}).get("operation_profile"):
        return schedule_times(total, pacing_config or {})
    lo, hi = AI_CHAT_ROUND_INTERVALS_SECONDS.get(mode, AI_CHAT_ROUND_INTERVALS_SECONDS["正常期"])
    hourly_cap = int((pacing_config or {}).get("max_actions_per_hour") or 0)
    if hourly_cap > 0:
        min_gap = max(1, (3600 + hourly_cap - 1) // hourly_cap)
        lo = max(lo, min_gap)
        hi = max(hi, lo)
    return schedule_times(
        total,
        {"mode": "fixed", "interval_seconds_min": lo, "interval_seconds_max": hi, "jitter_percent": 20},
        start_at=_now(),
    )


def _hard_hourly_round_config(config: dict, progress: dict[str, object]) -> dict:
    if not progress:
        return config
    updated = dict(config)
    updated["messages_per_round_mode"] = "manual"
    updated["messages_per_round"] = _hard_hourly_batch_size(config, progress)
    updated["allow_account_repeat"] = True
    return updated


def _hard_hourly_batch_size(config: dict, progress: dict[str, object]) -> int:
    deficit = max(1, int(progress.get("deficit") or 1))
    return deficit


def _hard_hourly_schedule(task: Task, progress: dict[str, object], total: int) -> list[datetime]:
    if not progress:
        return []
    return hard_schedule_times(
        total,
        task,
        _now(),
        target_total=int(progress.get("deficit") or total),
    )


def _history_collect_account_ids(config: dict, accounts: list) -> list[int]:
    account_ids = [int(account.id) for account in accounts]
    preferred = int(config.get("history_fetch_account_id") or 0)
    if preferred not in account_ids:
        return account_ids
    return [preferred, *[account_id for account_id in account_ids if account_id != preferred]]


def _should_refresh_context_for_plan(_session: Session, _group: TgGroup, _history_depth: int, progress: dict[str, object]) -> bool:
    if progress:
        # Hard-hourly planning must not wait on synchronous history fetches; listener workers refresh context separately.
        return False
    return True


def _collect_context_with_candidate_accounts(session: Session, task: Task, group: TgGroup, account_ids: list[int]) -> int:
    failed_ids: list[int] = []
    last_error: Exception | None = None
    for account_id in account_ids:
        try:
            inserted = collect_group_context(session, group, [account_id], create_source_media=False, learning_scene=None)
        except Exception as exc:
            if not _is_target_history_permission_error(exc):
                raise
            failed_ids.append(account_id)
            last_error = exc
            continue
        _record_history_collect_recovery(task, failed_ids, account_id)
        return inserted
    if last_error:
        _record_history_collect_recovery(task, failed_ids, None)
        raise last_error
    return 0


def _record_history_collect_recovery(task: Task, failed_ids: list[int], success_id: int | None) -> None:
    stats = dict(task.stats or {})
    stats.pop("history_fetch_degraded", None)
    stats.pop("history_fetch_degraded_reason", None)
    if not failed_ids:
        task.stats = stats
        return
    stats["history_fetch_failed_account_ids"] = failed_ids
    if success_id is None:
        stats.pop("history_fetch_fallback_account_id", None)
    else:
        stats["history_fetch_fallback_account_id"] = success_id
    task.stats = stats


def _record_history_collect_degraded(task: Task, exc: Exception) -> None:
    stats = dict(task.stats or {})
    stats["history_fetch_degraded"] = True
    stats["history_fetch_degraded_reason"] = str(exc)
    task.stats = stats


def _hard_blocker_inc(blockers: dict[str, int], reason: str, progress: dict[str, object]) -> None:
    if not progress:
        return
    blockers[reason] = int(blockers.get(reason) or 0) + 1


def _mark_hard_blocked(task: Task, progress: dict[str, object], reason: str) -> None:
    mark_plan_result(task, progress, 0, {reason: max(1, int(progress.get("deficit") or 1))})


def _is_target_history_permission_error(exc: Exception) -> bool:
    text = f"{exc.__class__.__name__} {exc}".lower()
    return any(marker in text for marker in TARGET_HISTORY_PERMISSION_MARKERS)


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
    stats["chat_mode"] = "waiting_new_context"
    stats.pop("skip_reason", None)
    stats.pop("duplicate_risk", None)
    stats.pop("hallucination_risk", None)
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


def _semantic_repeat_window(config: dict) -> int:
    try:
        value = int(config.get("semantic_repeat_window") or 10)
    except (TypeError, ValueError):
        value = 10
    return max(1, min(100, value))


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


def _select_cycle_accounts(
    accounts: list,
    config: dict,
    mode: str,
    ramp_ratio: float,
    *,
    has_context: bool,
    cycle_index: int = 1,
    pacing_config: dict | None = None,
    daily_coverage_uncovered_count: int = 0,
) -> tuple[list, int]:
    coverage_count = max(0, min(int(daily_coverage_uncovered_count or 0), len(accounts)))
    rotated_accounts = accounts if coverage_count else _rotate_accounts(accounts, cycle_index)
    if str(config.get("messages_per_round_mode") or "auto") == "manual":
        messages_per_round = _manual_messages_per_round(config, mode)
        desired = _desired_participant_count(rotated_accounts, config, mode, ramp_ratio)
        turn_count = _manual_turn_count(desired, messages_per_round)
        participant_count = _manual_participant_count(desired, turn_count, len(rotated_accounts), config)
        selected = rotated_accounts[:participant_count]
        if not bool(config.get("allow_account_repeat", True)):
            turn_count = min(turn_count, len(selected))
        return selected, max(1, turn_count)
    turn_count = _auto_messages_per_round(config, mode, has_context, pacing_config or {})
    desired = _desired_participant_count(rotated_accounts, config, mode, ramp_ratio)
    selected_count = min(max(turn_count, desired), len(rotated_accounts))
    selected = rotated_accounts[:selected_count]
    if not bool(config.get("allow_account_repeat", True)):
        turn_count = min(turn_count, len(selected))
    return selected, max(1, turn_count)


def _auto_messages_per_round(config: dict, mode: str, has_context: bool, pacing_config: dict) -> int:
    hourly_cap = int((pacing_config or {}).get("max_actions_per_hour") or 0)
    if hourly_cap > 0:
        rounds = current_hour_rounds(pacing_config or {}, _now())
        base = max(1, (hourly_cap + max(1, rounds) - 1) // max(1, rounds))
    else:
        base = 2 if mode == "静默期" else 5
    if mode == "静默期":
        base = min(base, int(config.get("silent_messages_per_round") or 1))
    if not has_context:
        base = min(base, 3)
    return max(1, base)


def _manual_messages_per_round(config: dict, mode: str) -> int:
    messages_per_round = int(config.get("messages_per_round") or 1)
    if mode == "静默期":
        messages_per_round = min(messages_per_round, int(config.get("silent_messages_per_round") or 1))
    return max(1, messages_per_round)


def _manual_turn_count(desired: int, messages_per_round: int) -> int:
    if messages_per_round == 1:
        return max(1, desired)
    return max(1, messages_per_round)


def _desired_participant_count(accounts: list, config: dict, mode: str, ramp_ratio: float) -> int:
    jitter = float(config.get("participation_jitter") or 0)
    rate = float(config.get("participation_rate") or 0.6)
    desired = max(1, round(len(accounts) * rate * random.uniform(max(0.1, 1 - jitter), 1 + jitter)))
    if mode == "静默期":
        desired = min(desired, int(config.get("silent_max_accounts") or 5))
    return min(desired, len(accounts))


def _manual_participant_count(desired: int, turn_count: int, account_count: int, config: dict) -> int:
    if account_count <= 0:
        return 0
    spread_count = min(turn_count, account_count)
    participant_count = max(desired, spread_count)
    if not bool(config.get("allow_account_repeat", True)):
        participant_count = max(participant_count, spread_count)
    return min(participant_count, account_count)


def _rotate_accounts(accounts: list, cycle_index: int) -> list:
    if len(accounts) <= 1:
        return accounts
    offset = (max(1, int(cycle_index or 1)) - 1) % len(accounts)
    return accounts[offset:] + accounts[:offset]


def _prioritize_account_memory(accounts: list, account_memories: dict[str, str]) -> list:
    if len(accounts) <= 1 or not account_memories:
        return accounts
    return sorted(accounts, key=lambda account: 0 if account_memories.get(str(account.id)) else 1)


def _prioritize_accounts_for_plan(
    accounts: list,
    account_memories: dict[str, str],
    coverage_counts: dict[int, int],
    config: dict,
) -> list:
    if len(accounts) <= 1:
        return accounts
    if not _all_accounts_daily_coverage(config):
        return _prioritize_account_memory(accounts, account_memories)
    target = _coverage_target_per_account(config)
    return sorted(
        accounts,
        key=lambda account: (
            0 if max(0, target - int(coverage_counts.get(int(account.id), 0))) > 0 else 1,
            int(coverage_counts.get(int(account.id), 0)),
            0 if account_memories.get(str(account.id)) else 1,
        ),
    )


def _with_active_conversation_targets(config: dict, group: TgGroup) -> dict:
    topic = _choose_topic_direction(config, group)
    teacher = _choose_teacher_target(config)
    return {**config, "active_topic_direction": topic, "active_teacher_target": teacher}


def _choose_topic_direction(config: dict, group: TgGroup) -> dict:
    directions = [item for item in config.get("topic_directions") or [] if str(item.get("title") or "").strip()]
    if directions:
        total = sum(max(0.01, float(item.get("weight") or 1)) for item in directions)
        marker = random.random() * total
        cursor = 0.0
        for item in directions:
            cursor += max(0.01, float(item.get("weight") or 1))
            if marker <= cursor:
                return dict(item)
    fallback = str(group.topic_direction or "群聊日常活跃").strip()
    return {"title": fallback, "description": "", "weight": 1}


def _choose_teacher_target(config: dict) -> dict:
    teachers = [item for item in config.get("teacher_targets") or [] if str(item.get("name") or "").strip()]
    if not teachers:
        return {}
    return dict(sorted(teachers, key=lambda item: int(item.get("priority") or 1), reverse=True)[0])


def _active_topic_text(config: dict, group: TgGroup) -> str:
    topic = config.get("active_topic_direction") or _choose_topic_direction(config, group)
    title = str(topic.get("title") or "").strip()
    description = str(topic.get("description") or "").strip()
    return f"{title}：{description}" if title and description else title


def _active_teacher_text(config: dict) -> str:
    teacher = config.get("active_teacher_target") or {}
    name = str(teacher.get("name") or "").strip()
    description = str(teacher.get("description") or "").strip()
    return f"{name}：{description}" if name and description else name


def _consecutive_burst_plan(config: dict, count: int, allow_repeat: bool, cycle_id: str) -> dict[int, dict]:
    if not allow_repeat or not bool(config.get("consecutive_message_enabled")):
        return {}
    minimum = int(config.get("consecutive_message_min") or 2)
    maximum = int(config.get("consecutive_message_max") or minimum)
    if count < minimum or random.random() > float(config.get("consecutive_message_probability") or 0):
        return {}
    size = min(count, random.randint(minimum, maximum))
    burst_id = f"{cycle_id}:burst:1"
    return {index: {"burst_id": burst_id, "burst_index": index + 1, "burst_size": size} for index in range(size)}


def _bootstrap_history(config: dict, group: TgGroup) -> str:
    topic = _active_topic_text(config, group)
    if not topic:
        topic = "围绕群内日常交流自然开场，轻松抛出一个大家容易接上的话题"
    teacher = _active_teacher_text(config)
    suffix = f"，讨论老师参考“{teacher}”" if teacher else ""
    return f"当前群暂无可用历史消息。请以“{topic}”为方向{suffix}，生成自然开场，不要提到系统、任务或 AI。"


def _idle_continuation_history(config: dict, group: TgGroup, previous_ai_messages: list[str]) -> str:
    topic = _active_topic_text(config, group) or "群聊日常活跃"
    teacher = _active_teacher_text(config)
    recent_ai = " / ".join(_clean_topic_text(text) for text in previous_ai_messages[-3:])
    recent_ai = recent_ai.strip(" /")
    parts = [
        f"群内暂时没有新的真人消息。请围绕“{topic}”补一句具体小事，像群友随手回消息。",
        "必须避免重复上一轮表达，不要提到系统、任务或 AI。",
    ]
    if teacher:
        parts.append(f"讨论老师参考：{teacher}。")
    if recent_ai:
        parts.append(f"上一轮 AI 已说：{recent_ai}。请避开原句，只接一个轻量问题或泛化观察，不要编具体经历、到场感受、位置或回访。")
    return "\n".join(parts)


def _context_mode(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return "idle_continuation"
    return "history" if context_rows else "bootstrap"


def _chat_mode(context_rows: list, idle_continuation: bool) -> str:
    if idle_continuation:
        return CHAT_MODE_IDLE_WARMUP
    return CHAT_MODE_REPLY if context_rows else CHAT_MODE_BOOTSTRAP


def _mark_quality_skip(
    task: Task,
    config: dict,
    mode: str,
    ramp_ratio: float,
    context_mode: str,
    chat_mode: str,
    quality_stats: dict[str, str],
) -> None:
    stats = dict(task.stats or {})
    stats["current_mode"] = mode
    stats["ramp_ratio"] = ramp_ratio
    stats["context_mode"] = context_mode
    stats["chat_mode"] = chat_mode
    stats["skip_reason"] = quality_stats.get("skip_reason") or "quality_gate"
    if quality_stats.get("duplicate_risk"):
        stats["duplicate_risk"] = quality_stats["duplicate_risk"]
    if quality_stats.get("hallucination_risk"):
        stats["hallucination_risk"] = quality_stats["hallucination_risk"]
    task.stats = stats
    if quality_stats.get("skip_reason") == "hallucination_risk":
        task.last_error = AI_QUALITY_ANCHOR_SKIP_MESSAGE
    elif quality_stats.get("skip_reason") == "duplicate_risk":
        task.last_error = AI_QUALITY_DUPLICATE_SKIP_MESSAGE
    else:
        task.last_error = AI_GENERATION_UNAVAILABLE_MESSAGE


def _topic_thread_summary(config: dict, group: TgGroup, context_rows: list, previous_ai_messages: list[str]) -> str:
    parts: list[str] = []
    topic = _active_topic_text(config, group)
    if topic:
        parts.append(f"主线方向：{topic[:80]}")
    teacher = _active_teacher_text(config)
    if teacher:
        parts.append(f"讨论老师：{teacher[:80]}")
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
    topic = _active_topic_text(config, group) or "群聊日常活跃"
    teacher = _active_teacher_text(config)
    anchors = [part.strip() for part in re.split(r"[；/]", topic_thread or "") if part.strip()]
    anchor = anchors[-1] if anchors else f"主线方向：{topic[:80]}"
    teacher_hint = f"，讨论老师参考“{teacher[:40]}”" if teacher else ""
    steps = [
        f"1. 贴近现场：从“{anchor[:80]}”里挑一个最像真人会接的点{teacher_hint}，短句承接。",
        f"2. 补充一点生活化细节：只给一个和“{topic[:60]}”相关的小信息或亲身口吻，不像科普。",
        "3. 轻轻问一句：问题要小、具体、容易回，不要问“大家怎么看”。",
        "4. 收到一个具体细节上：把内容放回上一条真人上下文，别总结成公告。",
        "5. 换个小细节：如果前面已经有人接话，就从反应、吐槽或经历切入。",
    ]
    return "\n".join(steps[: max(1, min(int(turn_count or 1), len(steps)))])


def _generation_config_with_profile(
    config: dict,
    account_memories: dict,
    account_profiles: dict,
    topic_thread: str,
    topic_plan: str,
    profile_preview: dict,
) -> dict:
    target_profile = str(profile_preview.get("profile_hit_summary") or "").strip()
    return {
        **config,
        "account_memories": account_memories,
        "account_profiles": account_profiles,
        "topic_thread": topic_thread,
        "topic_plan": topic_plan,
        "target_profile_style": target_profile,
        "target_learning_profile": profile_preview,
    }


def _clean_topic_text(value: str) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if _looks_like_internal_prompt(text) or _looks_like_generated_noise(text):
        return ""
    return text[:80]


def ai_cycle_mode(config: dict, scheduled_start: datetime | None = None, now: datetime | None = None) -> tuple[str, float]:
    current = now or _now()
    mode, ratio, _intensity = operation_intensity(config.get("pacing_config") or config, current)
    if (config.get("pacing_config") or {}).get("operation_profile") or config.get("operation_profile"):
        return mode, round(ratio, 3)
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
        .where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status == "success",
        )
        .order_by(Action.executed_at.desc().nullslast(), Action.created_at.desc())
        .limit(max(1, int(limit)))
    )
    for action in rows:
        payload = action.payload if isinstance(action.payload, dict) else {}
        content = str(payload.get("message_text") or "").strip()
        if content and not _looks_like_internal_prompt(content):
            messages.append(content)
    return list(reversed(messages))


def _recent_planned_ai_messages(session: Session, task: Task, *, limit: int) -> list[str]:
    messages: list[str] = []
    rows = session.scalars(
        select(Action)
        .where(
            Action.task_id == task.id,
            Action.task_type == "group_ai_chat",
            Action.action_type == "send_message",
            Action.status.in_(RECENT_PLANNED_AI_STATUSES),
        )
        .order_by(Action.created_at.desc())
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
        "刚看到大家提到",
        "刚看到有人聊这个",
        "看大家聊",
        "顺着这个话题说",
        "这个点挺有意思",
        "这个点我也留意到了",
        "可以继续聊聊",
        "有经验的朋友也可以补充",
        "这个话题",
        "自然接一句",
        "换个角度",
        "轻量推进",
        "值得讨论",
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


def _is_usable_context_message(content: str) -> bool:
    text = re.sub(r"\s+", " ", str(content or "")).strip()
    compact = re.sub(r"\s+", "", text)
    if _looks_like_internal_prompt(text):
        return False
    if contains_coarse_language(text):
        return False
    if compact.isdigit():
        return False
    if len(compact) <= 8 and len(set(compact)) <= 2:
        return False
    return True


def _is_human_context_row(row) -> bool:
    return not bool(getattr(row, "is_bot", False))


def _topic_relevant_context_rows(config: dict, rows: list) -> list:
    active_topic = config.get("active_topic_direction") or {}
    topic_parts = [
        str(active_topic.get("title") or ""),
        str(active_topic.get("description") or ""),
        _active_teacher_text(config),
    ]
    topic = " ".join(part.strip() for part in topic_parts if part.strip())
    if not topic or not rows:
        return rows
    keywords = _topic_keywords(topic)
    if not keywords:
        return rows
    matched = [row for row in rows if any(keyword in str(getattr(row, "content", "") or "") for keyword in keywords)]
    return matched or rows


def _topic_keywords(topic: str) -> set[str]:
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", " ", topic).strip()
    parts = [part for part in re.split(r"\s+", cleaned) if len(part) >= 2]
    keywords = set(parts)
    for part in parts:
        if re.fullmatch(r"[\u4e00-\u9fff]{3,}", part):
            keywords.update(part[index:index + 2] for index in range(0, len(part) - 1))
    return {keyword for keyword in keywords if keyword}


def _drop_repeated_ai_messages(contents: list[str], previous_messages: list[str]) -> list[str]:
    accepted: list[str] = []
    seen_starts: set[str] = set()
    for content in contents:
        normalized = _normalize_for_similarity(content)
        if not normalized:
            continue
        cluster = _semantic_cluster(content)
        start = normalized[:8]
        if start in seen_starts:
            continue
        if any(_is_similarity_duplicate(normalized, cluster, previous, threshold=0.62) for previous in previous_messages):
            continue
        if any(_is_similarity_duplicate(normalized, cluster, existing, threshold=0.68) for existing in accepted):
            continue
        seen_starts.add(start)
        accepted.append(content)
    return accepted


def _is_similarity_duplicate(normalized: str, cluster: str, previous: str, *, threshold: float) -> bool:
    previous_normalized = _normalize_for_similarity(previous)
    if not previous_normalized:
        return False
    if cluster and cluster == _semantic_cluster(previous):
        return False
    return _similarity(normalized, previous_normalized) >= threshold


def _quality_filter_ai_messages(
    contents: list[str],
    previous_messages: list[str],
    *,
    chat_mode: str,
    anchor_message_ids: list[int],
    fact_anchor_required: bool,
    low_confidence_silence_enabled: bool,
    limit: int,
) -> tuple[list[dict[str, str]], dict[str, str]]:
    accepted: list[dict[str, str]] = []
    accepted_clusters: set[str] = set()
    previous_clusters = {_semantic_cluster(message) for message in previous_messages}
    previous_clusters.discard("")
    stats: dict[str, str] = {}
    for content in contents:
        cluster = _semantic_cluster(content)
        item = {
            "content": content,
            "semantic_cluster": cluster,
            "duplicate_risk": "",
            "hallucination_risk": "",
            "quality_skip_reason": "",
        }
        if cluster and (cluster in accepted_clusters or cluster in previous_clusters):
            stats["duplicate_risk"] = "semantic_cluster"
            stats["skip_reason"] = stats.get("skip_reason") or "duplicate_risk"
            continue
        if fact_anchor_required and _has_unanchored_idle_fact(content, chat_mode=chat_mode, anchor_message_ids=anchor_message_ids):
            stats["hallucination_risk"] = "high"
            stats["skip_reason"] = "hallucination_risk"
            continue
        if low_confidence_silence_enabled and chat_mode == CHAT_MODE_BOOTSTRAP and _looks_like_fact_claim(content):
            stats["hallucination_risk"] = "low_confidence_bootstrap"
            stats["skip_reason"] = "hallucination_risk"
            continue
        accepted.append(item)
        if cluster:
            accepted_clusters.add(cluster)
        if len(accepted) >= max(1, int(limit or 1)):
            break
    return accepted, stats


def _semantic_cluster(content: str) -> str:
    text = _normalize_for_similarity(content)
    cluster_markers = [
        ("photo_real_match", ("照片准", "照片没p", "照片没修", "没照骗", "真人没差", "本人也差不多", "见面没翻车")),
        ("stable_attitude", ("态度稳", "不催", "不敷衍", "没催", "没加价", "挺省心")),
        ("early_location", ("位置提前", "提前发位置", "发了位置", "没绕路", "没绕远", "跑冤枉路")),
        ("revisit_feedback", ("结束后问", "问反馈", "回访", "下次安排", "下次约不约", "下次啥时候")),
        ("time_punctual", ("准时到", "准点", "时间卡得准", "没干等", "没让我等", "没放鸽子")),
        ("fixed_shell_bonus", ("这点加分", "这点挺加分", "挺加分", "这个加分")),
    ]
    for cluster, markers in cluster_markers:
        if any(_normalize_for_similarity(marker) in text for marker in markers):
            return cluster
    return ""


def _has_unanchored_idle_fact(content: str, *, chat_mode: str, anchor_message_ids: list[int]) -> bool:
    if chat_mode not in {CHAT_MODE_IDLE_WARMUP, CHAT_MODE_BOOTSTRAP}:
        return False
    text = _normalize_for_similarity(content)
    fact_markers = (
        "走之前",
        "结束后",
        "回访",
        "准时到",
        "准点",
        "没让我等",
        "没干等",
        "位置提前",
        "提前发位置",
        "发了位置",
        "穿着",
        "照片里一样",
        "上次那个",
        "我上次",
        "之前约过",
        "路过",
    )
    normalized_markers = [_normalize_for_similarity(marker) for marker in fact_markers]
    if not any(marker and marker in text for marker in normalized_markers):
        return False
    return True


def _looks_like_fact_claim(content: str) -> bool:
    text = _normalize_for_similarity(content)
    markers = (
        "我上次",
        "之前",
        "上次那个",
        "结束后",
        "走之前",
        "位置",
        "照片",
        "准时",
        "回访",
        "没让我等",
    )
    return any(_normalize_for_similarity(marker) in text for marker in markers)


def _normalize_for_similarity(content: str) -> str:
    return re.sub(r"[\s，。！？!?、,.；;：:\"'“”‘’（）()\[\]【】]+", "", (content or "").lower())


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    return SequenceMatcher(None, left, right).ratio()


def _next_cycle_index(session: Session, task: Task) -> int:
    max_index = 0
    rows = session.scalars(
        select(Action.payload["cycle_id"].as_string())
        .where(Action.task_id == task.id, Action.task_type == "group_ai_chat", Action.action_type == "send_message")
        .order_by(Action.created_at.desc())
        .limit(RECENT_CYCLE_SCAN_LIMIT)
    )
    prefix = f"{task.id}:cycle:"
    for cycle_id in rows:
        cycle_id = str(cycle_id or "")
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
