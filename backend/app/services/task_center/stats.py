from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, literal_column, select, true
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

from .config_fields import CHANNEL_DYNAMIC_TASK_TYPES
from .daily_group_target import daily_group_due_message_count, ensure_task_group_daily_target
from .datetime_compat import parse_zone, to_zone
from .dispatch_claim_types import CLAIM_WINDOW_SECONDS
from .fulfillment_takeover import (
    FULFILLMENT_TASK_TYPES,
)
from .fulfillment_retry import retry_failed_actions as _retry_failed_actions
from .hard_hourly import enabled as hard_hourly_enabled, hard_hourly_stats
from .pacing import (
    ai_next_run_after,
    fulfillment_pacing_config,
    next_run_after,
    task_pacing_anchor,
)
from .planner_backlog import planner_backlog_snapshot
from app.services.runtime_action_queries import task_action_status_counts_statement
from .hourly_stats import search_join_hourly_execution, search_rank_deboost_hourly_execution
from .targets import group_from_reference

ARCHIVED_SKIP_ERROR_CODES = {"context_expired"}
COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT = "comment_context_bound_next_run_at"
BUSINESS_MEMBERSHIP_ACTION_TYPES = [
    "ensure_channel_membership",
    "ensure_target_membership",
    "ensure_discussion_membership",
]
PLANNER_BACKLOG_STAT_KEYS = (
    "planner_backlog_blocked",
    "planner_backlog_blocked_at",
    "planner_backlog_global_pending",
    "planner_backlog_task_pending",
    "planner_backlog_oldest_age_seconds",
)
HARD_HOURLY_EXPIRED_ERROR_CODE = "hard_hourly_bucket_expired"
HARD_HOURLY_EXPIRED_ERROR_MESSAGE = "硬目标小时窗口已结束，过期补量已跳过"
AI_GENERATION_CLOSED_STATUSES = ("pending", "generating", "ready", "ai_result_persist_unknown")
SEARCH_CLICK_TASK_TYPES = {"search_join_group", "search_rank_deboost"}
AI_GENERATION_QUALITY_CODES = frozenset({
    "content_rejected",
    "duplicate_message",
    "duplicate_risk",
    "hallucination_risk",
    "quality_rejected",
    "stance_conflict",
    "template_shell_limited",
    "voice_profile_mismatch",
})


def next_run_after_task(task: Task):
    config = task.type_config or {}
    raw_pacing = task.pacing_config or {}
    fulfillment_pacing = (
        fulfillment_pacing_config(raw_pacing)
        if task.type in FULFILLMENT_TASK_TYPES
        else raw_pacing
    )
    if task.type == "channel_comment":
        materialization_next = _stats_datetime(
            task,
            COMMENT_CONTEXT_BOUND_NEXT_RUN_STAT,
        )
        if materialization_next is not None:
            return materialization_next
    if task.type == "group_ai_chat":
        hard_next = _hard_hourly_next_check_at(task)
        coverage_next = _stats_datetime(task, "daily_coverage_next_check_at")
        priority_checks = [
            value
            for value in (hard_next if hard_hourly_enabled(task) else None, coverage_next)
            if value is not None
        ]
        if priority_checks:
            return max(min(priority_checks), to_zone(_now(), parse_zone(task.timezone)))
        waiting_until = _stats_datetime(task, "idle_continuation_next_run_at")
        if waiting_until:
            return waiting_until
        return ai_next_run_after(fulfillment_pacing)
    if task.type == "search_click":
        return _next_dispatch_claim_window_start(_now())
    if task.type in CHANNEL_DYNAMIC_TASK_TYPES and (config.get("message_scope") or "latest_n") == "dynamic_new":
        interval = int(config.get("listener_interval_seconds") or 30)
        return _now() + timedelta(seconds=max(1, interval))
    timezone_name = task.timezone if task.type in SEARCH_CLICK_TASK_TYPES else None
    pacing = (
        fulfillment_pacing
        if task.type in FULFILLMENT_TASK_TYPES
        else raw_pacing
    )
    return next_run_after(pacing, timezone_name=timezone_name)


def _next_dispatch_claim_window_start(value: datetime) -> datetime:
    current_window_start = value.replace(second=0, microsecond=0)
    return current_window_start + timedelta(seconds=CLAIM_WINDOW_SECONDS)


def refresh_task_stats(
    session: Session,
    task: Task,
    *,
    include_configured_accounts: bool = True,
    include_hard_hourly: bool = True,
) -> dict[str, Any]:
    session.flush()
    business_filter = _stats_action_filter(task)
    rows = session.execute(task_action_status_counts_statement(task, business_filter)).all()
    counts = {str(status): int(count) for status, count in rows}
    raw_skipped_count = counts.get("skipped", 0)
    archived_skipped_count = _archived_skipped_count(session, task, business_filter)
    skipped_count = max(0, raw_skipped_count - archived_skipped_count)
    accounts_used = session.scalar(select(func.count(func.distinct(Action.account_id))).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        business_filter,
        Action.account_id.is_not(None),
    )) or 0
    last_action_at = session.scalar(select(func.max(Action.executed_at)).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        business_filter,
    ))
    stats = dict(task.stats or empty_stats())
    stats = _clear_recovered_planner_backlog_stats(session, task, stats)
    stats.update(
        {
            "total_actions": max(0, sum(counts.values()) - archived_skipped_count),
            "success_count": counts.get("success", 0),
            "failure_count": counts.get("failed", 0),
            "pending_count": counts.get("pending", 0),
            "claiming_count": counts.get("claiming", 0),
            "executing_count": counts.get("executing", 0),
            "retryable_failed_count": counts.get("retryable_failed", 0),
            "unknown_after_send_count": counts.get("unknown_after_send", 0),
            "skipped_count": skipped_count,
            "raw_skipped_count": raw_skipped_count,
            "archived_skipped_count": archived_skipped_count,
            "accounts_used": int(accounts_used or 0),
            "last_action_at": last_action_at.isoformat() if last_action_at else stats.get("last_action_at"),
        }
    )
    stats = _ai_generation_stats(session, task, stats)
    stats = _daily_group_target_stats(session, task, stats)
    if task.type == "group_ai_chat":
        stats = hard_hourly_stats(session, task, _now(), stats)
    stats = _search_join_stats(session, task, stats)
    stats = _search_rank_deboost_stats(session, task, stats)
    task.stats = stats
    _refresh_runtime_summary(session, task, include_configured_accounts=include_configured_accounts)
    return stats


def _daily_group_target_stats(
    session: Session,
    task: Task,
    stats: dict[str, Any],
) -> dict[str, Any]:
    if task.type != "group_ai_chat":
        return stats
    config = task.type_config or {}
    group = group_from_reference(
        session,
        task.tenant_id,
        group_id=int(config.get("target_group_id") or 0) or None,
        operation_target_id=int(config.get("target_operation_target_id") or 0) or None,
        require_authorized=False,
    )
    if group is None:
        return stats
    timestamp = to_zone(_now(), parse_zone(task.timezone))
    target = ensure_task_group_daily_target(session, task, group, timestamp.date(), now=timestamp)
    due = daily_group_due_message_count(
        target,
        task.pacing_config or {},
        anchor_at=task_pacing_anchor(task),
        now=timestamp,
    )
    target.due_message_count = due
    updated = dict(stats)
    updated.update({
        "daily_group_target_id": target.id,
        "daily_group_configured_target": target.configured_message_target,
        "daily_group_effective_target": target.effective_message_target,
        "daily_group_due_message_count": due,
        "daily_group_confirmed_success_count": target.confirmed_message_count,
        "daily_group_frozen_account_count": target.frozen_account_count,
        "daily_group_covered_account_count": target.coverage_confirmed_account_count,
    })
    return updated


def _refresh_runtime_summary(session: Session, task: Task, *, include_configured_accounts: bool) -> None:
    from app.services.runtime_summary import refresh_task_summary

    refresh_task_summary(session, task, include_configured_accounts=include_configured_accounts)


def _ai_generation_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "group_ai_chat":
        return stats
    generation_counts = _action_json_counts(
        session,
        task,
        column=Action.payload,
        key="ai_generation_status",
        values=AI_GENERATION_CLOSED_STATUSES,
    )
    generation_failed_count = _ai_generation_failed_count(session, task)
    outcome_counts = _action_json_counts(
        session,
        task,
        column=Action.result,
        key="generation_outcome",
        values=tuple(sorted(AI_GENERATION_QUALITY_CODES | {"reply_target_stale", "reply_target_missing"})),
    )
    updated = _apply_ai_generation_counts(
        stats,
        generation_counts,
        outcome_counts,
        generation_failed_count=generation_failed_count,
    )
    updated["voice_profile_anchor_rewrite_count"] = _ai_generation_fact_count(
        session,
        task,
        _voice_profile_anchor_rewritten_condition(session),
    )
    return updated


def _ai_generation_fact_count(session: Session, task: Task, condition) -> int:
    return int(session.scalar(select(func.count()).select_from(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "send_message",
        condition,
    )) or 0)


def _ai_generation_failed_count(session: Session, task: Task) -> int:
    expression = _json_text_expression(session, column=Action.payload, key="ai_generation_status")
    return int(session.scalar(select(func.count()).select_from(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "send_message",
        expression.is_not(None),
        expression.notin_(AI_GENERATION_CLOSED_STATUSES),
    )) or 0)


def _action_json_counts(
    session: Session,
    task: Task,
    *,
    column,
    key: str,
    values: tuple[str, ...],
) -> dict[str, int]:
    expression = _json_text_expression(session, column=column, key=key)
    counts: dict[str, int] = {}
    for value in values:
        count = session.scalar(select(func.count()).select_from(Action).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type == "send_message",
            expression == value,
        )) or 0
        if count:
            counts[value] = int(count)
    return counts


def _json_text_expression(session: Session, *, column, key: str):
    if session.get_bind().dialect.name != "postgresql":
        return column[key].as_string()
    if column is Action.payload and key == "ai_generation_status":
        return literal_column("CAST(actions.payload ->> 'ai_generation_status' AS VARCHAR)")
    if column is Action.result and key == "generation_outcome":
        return literal_column("CAST(actions.result ->> 'generation_outcome' AS VARCHAR)")
    raise ValueError(f"unsupported action JSON count expression: {column.key}.{key}")


def _voice_profile_anchor_rewritten_condition(session: Session):
    if session.get_bind().dialect.name == "postgresql":
        return literal_column("CAST(actions.result ->> 'voice_profile_anchor_rewritten' AS BOOLEAN) IS TRUE")
    return literal_column("JSON_EXTRACT(actions.result, '$.voice_profile_anchor_rewritten') IS 1")


def _apply_ai_generation_counts(
    stats: dict[str, Any],
    generation_counts: dict[str, int],
    outcome_counts: dict[str, int],
    *,
    generation_failed_count: int,
) -> dict[str, Any]:
    quality_counts = {
        code: count
        for code, count in outcome_counts.items()
        if code in AI_GENERATION_QUALITY_CODES
    }
    updated = dict(stats)
    updated.update({
        "generation_pending_count": generation_counts.get("pending", 0),
        "generation_claimed_count": generation_counts.get("generating", 0),
        "generation_ready_count": generation_counts.get("ready", 0),
        "generation_persist_unknown_count": generation_counts.get("ai_result_persist_unknown", 0),
        "generation_failed_count": generation_failed_count,
        "quality_rejected_count": sum(quality_counts.values()),
        "quality_rejection_counts": quality_counts,
        "reply_target_stale_count": outcome_counts.get("reply_target_stale", 0),
        "reply_target_missing_count": outcome_counts.get("reply_target_missing", 0),
        "gateway_unknown_count": int(updated.get("unknown_after_send_count") or 0),
    })
    return updated


def _search_join_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "search_join_group":
        return stats
    updated = dict(stats)
    search_join_stats = dict(updated.get("search_join_stats") or {})
    previous_hourly = dict(search_join_stats.get("hourly_execution") or {})
    current_hourly = search_join_hourly_execution(session, task, _now())
    search_join_stats["hourly_execution"] = {**previous_hourly, **current_hourly}
    updated["search_join_stats"] = search_join_stats
    return updated


def _search_rank_deboost_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "search_rank_deboost":
        return stats
    updated = dict(stats)
    deboost_stats = dict(updated.get("search_rank_deboost_stats") or {})
    previous_hourly = dict(deboost_stats.get("hourly_execution") or {})
    current_hourly = search_rank_deboost_hourly_execution(session, task, _now())
    deboost_stats["hourly_execution"] = {**previous_hourly, **current_hourly}
    updated["search_rank_deboost_stats"] = deboost_stats
    return updated


def _stats_action_filter(task: Task):
    if task.type == "target_admission_retry":
        return true()
    return Action.action_type.notin_(BUSINESS_MEMBERSHIP_ACTION_TYPES)


def clear_planner_backlog_stats(stats: dict[str, Any]) -> dict[str, Any]:
    updated = dict(stats or {})
    for key in PLANNER_BACKLOG_STAT_KEYS:
        updated.pop(key, None)
    return updated


def _clear_recovered_planner_backlog_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if not stats.get("planner_backlog_blocked"):
        return stats
    if planner_backlog_snapshot(session, task)["blocked"]:
        return stats
    return clear_planner_backlog_stats(stats)


def _archived_skipped_count(session: Session, task: Task, business_filter) -> int:
    if task.type != "group_ai_chat":
        return 0
    count = session.scalar(
        select(func.count(Action.id)).where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            business_filter,
            Action.action_type == "send_message",
            Action.status == "skipped",
            Action.result["error_code"].as_string().in_(ARCHIVED_SKIP_ERROR_CODES),
        )
    )
    return int(count or 0)


def retry_failed_actions(
    session: Session,
    task: Task,
    *,
    limit: int = 100,
) -> int:
    return _retry_failed_actions(
        session,
        task,
        limit=limit,
        now_value=_now(),
    )


def empty_stats() -> dict[str, Any]:
    return {
        "total_rounds": 0,
        "total_actions": 0,
        "success_count": 0,
        "failure_count": 0,
        "accounts_used": 0,
        "accounts_banned": 0,
        "started_at": None,
        "last_action_at": None,
        "estimated_completion": None,
    }


def _stats_datetime(task: Task, key: str) -> datetime | None:
    stats = task.stats or {}
    if not isinstance(stats, dict):
        return None
    value = stats.get(key)
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return to_zone(parsed, parse_zone(task.timezone))


def _hard_hourly_next_check_at(task: Task) -> datetime | None:
    checkpoint = (
        to_zone(task.hard_hourly_next_check_at, parse_zone(task.timezone))
        if task.hard_hourly_next_check_at is not None
        else None
    )
    if checkpoint is not None:
        return checkpoint
    checkpoint = _stats_datetime(task, "hard_hourly_next_check_at")
    if checkpoint is not None:
        task.hard_hourly_next_check_at = checkpoint
    return checkpoint
__all__ = ["empty_stats", "next_run_after_task", "refresh_task_stats", "retry_failed_actions", "search_rank_deboost_hourly_execution"]
