from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select, true
from sqlalchemy.orm import Session

from app.models import Action, Task
from app.services._common import _now

from .config_fields import CHANNEL_DYNAMIC_TASK_TYPES
from .hard_hourly import enabled as hard_hourly_enabled, hard_hourly_stats
from .pacing import ai_next_run_after, next_run_after
from .planner_backlog import hard_hourly_payload_expired, planner_backlog_snapshot
from app.services.runtime_action_queries import task_action_status_counts_statement
from .hourly_stats import search_join_hourly_execution, search_rank_deboost_hourly_execution

ARCHIVED_SKIP_ERROR_CODES = {"context_expired"}
DEFAULT_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_AUTO_RETRY_STATUSES = ("failed", "retryable_failed")
TARGET_ADMISSION_DEFAULT_MAX_RETRIES = 1
TARGET_ADMISSION_DEFAULT_RETRY_DELAY_SECONDS = 30
BUSINESS_MEMBERSHIP_ACTION_TYPES = ["ensure_channel_membership", "ensure_target_membership"]
PLANNER_BACKLOG_STAT_KEYS = (
    "planner_backlog_blocked",
    "planner_backlog_blocked_at",
    "planner_backlog_global_pending",
    "planner_backlog_task_pending",
    "planner_backlog_oldest_age_seconds",
)
HARD_HOURLY_EXPIRED_ERROR_CODE = "hard_hourly_bucket_expired"
HARD_HOURLY_EXPIRED_ERROR_MESSAGE = "硬目标小时窗口已结束，过期补量已跳过"
AI_GROUP_TERMINAL_QUALITY_ERRORS = frozenset({"duplicate_message", "ai_message_memory_missing"})
AI_GROUP_TERMINAL_GENERATION_STATUSES = frozenset({"duplicate_rejected"})
AI_GENERATION_OPEN_STATUSES = frozenset({"pending", "generating"})
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
    if task.type == "group_ai_chat":
        hard_next = _stats_datetime(task, "hard_hourly_next_check_at")
        coverage_next = _stats_datetime(task, "daily_coverage_next_check_at")
        priority_checks = [
            value
            for value in (hard_next if hard_hourly_enabled(task) else None, coverage_next)
            if value is not None
        ]
        if priority_checks:
            return max(min(priority_checks), _now())
        waiting_until = _stats_datetime(task, "idle_continuation_next_run_at")
        if waiting_until:
            return waiting_until
        return ai_next_run_after(task.pacing_config or {})
    if task.type in CHANNEL_DYNAMIC_TASK_TYPES and (config.get("message_scope") or "latest_n") == "dynamic_new":
        interval = int(config.get("listener_interval_seconds") or 30)
        return utc_now_naive() + timedelta(seconds=max(1, interval))
    return next_run_after(task.pacing_config or {})


def refresh_task_stats(
    session: Session,
    task: Task,
    *,
    include_configured_accounts: bool = True,
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
    stats = hard_hourly_stats(session, task, _now(), stats)
    stats = _search_join_stats(session, task, stats)
    stats = _search_rank_deboost_stats(session, task, stats)
    task.stats = stats
    _refresh_runtime_summary(session, task, include_configured_accounts=include_configured_accounts)
    return stats


def _refresh_runtime_summary(session: Session, task: Task, *, include_configured_accounts: bool) -> None:
    from app.services.runtime_summary import refresh_task_summary

    refresh_task_summary(session, task, include_configured_accounts=include_configured_accounts)


def _ai_generation_stats(session: Session, task: Task, stats: dict[str, Any]) -> dict[str, Any]:
    if task.type != "group_ai_chat":
        return stats
    generation_counts = _action_json_counts(
        session,
        task,
        Action.payload["ai_generation_status"].as_string(),
    )
    outcome_counts = _action_json_counts(
        session,
        task,
        Action.result["generation_outcome"].as_string(),
    )
    updated = _apply_ai_generation_counts(stats, generation_counts, outcome_counts)
    updated["voice_profile_anchor_rewrite_count"] = _ai_generation_fact_count(
        session,
        task,
        Action.result["voice_profile_anchor_rewritten"].as_boolean().is_(True),
    )
    return updated


def _ai_generation_fact_count(session: Session, task: Task, condition) -> int:
    return int(session.scalar(select(func.count()).select_from(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.action_type == "send_message",
        condition,
    )) or 0)


def _action_json_counts(session: Session, task: Task, expression) -> dict[str, int]:
    rows = session.execute(
        select(expression, func.count())
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type == "send_message",
        )
        .group_by(expression)
    ).all()
    return {str(code or ""): int(count) for code, count in rows if code}


def _apply_ai_generation_counts(
    stats: dict[str, Any],
    generation_counts: dict[str, int],
    outcome_counts: dict[str, int],
) -> dict[str, Any]:
    quality_counts = {
        code: count
        for code, count in outcome_counts.items()
        if code in AI_GENERATION_QUALITY_CODES
    }
    closed_statuses = AI_GENERATION_OPEN_STATUSES | {"ready", "ai_result_persist_unknown"}
    updated = dict(stats)
    updated.update({
        "generation_pending_count": generation_counts.get("pending", 0),
        "generation_claimed_count": generation_counts.get("generating", 0),
        "generation_ready_count": generation_counts.get("ready", 0),
        "generation_persist_unknown_count": generation_counts.get("ai_result_persist_unknown", 0),
        "generation_failed_count": sum(
            count
            for status, count in generation_counts.items()
            if status and status not in closed_statuses
        ),
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


def retry_failed_actions(session: Session, task: Task, *, limit: int = 100) -> int:
    policy = task.failure_policy or {}
    max_retries = _max_retries_for_task(task, policy)
    if max_retries <= 0:
        return 0
    retry_delay = _retry_delay_seconds_for_task(task, policy)
    backoff = policy.get("retry_backoff") or "none"
    count = 0
    query = select(Action).where(
        Action.tenant_id == task.tenant_id,
        Action.task_id == task.id,
        Action.status.in_(_auto_retry_statuses(task)),
        Action.retry_count < max_retries,
    ).order_by(Action.scheduled_at.asc(), Action.id.asc()).limit(max(1, int(limit)))
    for action in session.scalars(query):
        previous_result = dict(action.result or {})
        if _is_terminal_ai_quality_failure(action, previous_result):
            continue
        now_value = _now()
        if _skip_expired_hard_hourly_retry(action, previous_result, now_value):
            count += 1
            continue
        action.retry_count += 1
        delay = retry_delay
        if backoff == "linear":
            delay *= action.retry_count
        elif backoff == "exponential":
            delay *= 2 ** max(0, action.retry_count - 1)
        action.status = "pending"
        action.scheduled_at = now_value + timedelta(seconds=delay)
        action.executed_at = None
        action.lease_owner = ""
        action.lease_expires_at = None
        action.result = {
            "retry_scheduled": True,
            "retry_count": int(action.retry_count or 0),
            "retry_after_seconds": max(0, int(delay)),
            "last_failure": previous_result,
        }
        count += 1
    return count


def _is_terminal_ai_quality_failure(action: Action, previous_result: dict[str, Any]) -> bool:
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    error_code = str(previous_result.get("error_code") or previous_result.get("failure_type") or "")
    generation_status = str(payload.get("ai_generation_status") or "")
    quality_reason = str(payload.get("quality_skip_reason") or previous_result.get("quality_skip_reason") or "")
    return (
        error_code in AI_GROUP_TERMINAL_QUALITY_ERRORS
        or generation_status in AI_GROUP_TERMINAL_GENERATION_STATUSES
        or quality_reason in AI_GROUP_TERMINAL_QUALITY_ERRORS
    )


def _auto_retry_statuses(task: Task) -> tuple[str, ...]:
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_AUTO_RETRY_STATUSES
    return DEFAULT_AUTO_RETRY_STATUSES


def _max_retries_for_task(task: Task, policy: dict[str, Any]) -> int:
    if policy.get("max_retries") is not None:
        return int(policy.get("max_retries") or 0)
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_DEFAULT_MAX_RETRIES
    return 0


def _retry_delay_seconds_for_task(task: Task, policy: dict[str, Any]) -> int:
    if policy.get("retry_delay_seconds") is not None:
        return int(policy["retry_delay_seconds"])
    if task.type == "target_admission_retry":
        return TARGET_ADMISSION_DEFAULT_RETRY_DELAY_SECONDS
    return 60


def _skip_expired_hard_hourly_retry(action: Action, previous_result: dict[str, Any], now_value: datetime) -> bool:
    if not _hard_hourly_bucket_expired(action, now_value):
        return False
    action.status = "skipped"
    action.executed_at = now_value
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        "success": False,
        "error_code": HARD_HOURLY_EXPIRED_ERROR_CODE,
        "error_message": HARD_HOURLY_EXPIRED_ERROR_MESSAGE,
        "validation_stage": "hard_hourly_retry_recovery",
        "auto_check": "过期补量跳过",
        "previous_result": previous_result,
    }
    return True


def _hard_hourly_bucket_expired(action: Action, now_value: datetime) -> bool:
    if action.action_type != "send_message":
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    return hard_hourly_payload_expired(payload, now_value)


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


def utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
    return _naive_datetime(parsed)


def _naive_datetime(value):
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


__all__ = ["empty_stats", "next_run_after_task", "refresh_task_stats", "retry_failed_actions", "search_rank_deboost_hourly_execution", "utc_now_naive"]
