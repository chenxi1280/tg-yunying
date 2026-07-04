from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, ChannelMessage, ExecutionAttempt, FailureType, MessageFingerprint, OperationIssue, OperationPlanTaskLink, OperationTarget, ReviewQueue, RuntimeMetricSnapshot, RuleSet, RuleSetVersion, Task, TaskRuntimeSummary, TgAccount, TgGroup, WorkerHeartbeat
from app.schemas.task_center import (
    ChannelCapacityCheckRequest,
    ChannelCommentConfig,
    ChannelCommentTaskCreate,
    ChannelCommentTaskConfigUpdate,
    ChannelCommentTaskPreviewRequest,
    ChannelLikeConfig,
    ChannelLikeTaskCreate,
    ChannelLikeTaskConfigUpdate,
    ChannelViewConfig,
    ChannelViewTaskCreate,
    ChannelViewTaskConfigUpdate,
    GroupAIChatConfig,
    GroupAIChatTaskCreate,
    GroupAIChatTaskConfigUpdate,
    GroupAIChatTaskPreviewRequest,
    GroupMembershipAdmissionTaskCreate,
    PacingConfig,
    GroupRelayConfig,
    GroupRelayTaskCreate,
    GroupRelayTaskConfigUpdate,
    RecommendTaskAccountsRequest,
    SearchJoinGroupTaskCreate,
    SearchJoinGroupTaskConfigUpdate,
    TaskPrecheckRequest,
    TaskRetryRequest,
    TaskSettingsUpdate,
    TaskSourceFilterOverrideRequest,
    TaskUpdate,
)
from app.services._common import _now, audit, gateway, normalize_list_filter
from app.services.developer_apps import credentials_for_account
from app.timezone import as_beijing

from .account_pool import select_task_accounts
from .ai_act_types import canonical_ai_group_act_type
from .ai_generator import AiGenerationUnavailable, generate_channel_comments, generate_group_messages
from .channel_membership import (
    ACTION_TYPE as TARGET_MEMBERSHIP_ACTION_TYPE,
    LEGACY_ACTION_TYPE as LEGACY_MEMBERSHIP_ACTION_TYPE,
    channel_membership_summary,
    mark_channel_membership_joined,
)
from .dispatcher import claim_actions, dispatch_action, due_actions, recover_expired_claims, recover_expired_hard_hourly_actions
from .executors import build_task_plan, prepare_open_actions_for_planning
from .details import (
    _accounts_by_id,
    _ai_account_profiles,
    _ai_cycles,
    _ai_generation_records,
    _ai_quality_funnel,
    _channel_subtask_status,
    _groups_by_target_id,
    _latest_attempts_by_action,
    _membership_item_payload,
    _membership_items,
    _membership_action_blocked,
    _membership_action_failed,
    _membership_action_succeeded,
    _membership_phase,
    _message_groups,
    _relay_batches,
    _relay_recent_sources,
    _stats_with_account_coverage,
    _task_payload,
    _verification_tasks_by_group_account,
)
from .fingerprints import content_fingerprint
from .heartbeat import record_worker_heartbeat
from .listener_runtime import drain_listener_runtime, invalidate_listener_collect
from .membership_fast_track import fast_track_pending_hard_hourly_memberships
from .membership_admission import (
    list_membership_admission_items_page,
    mark_membership_admission_manual_handled,
    membership_admission_summary,
    membership_admission_failure_rows,
    retry_failed_membership_admission_items,
    retry_membership_admission_item,
    retry_membership_admission_rescue,
)
from .membership_recovery_gate import recover_missing_hard_hourly_memberships
from .review import expire_reviews
from .reviews import ReviewStateError, approve_review, list_reviews, reject_review
from .precheck import run_precheck_task_creation
from .profile_batch_projection import delete_profile_batch_task, get_profile_batch_task_detail, is_profile_batch_task_id, list_profile_batch_tasks
from app.services.task_runtime_stage import derive_task_runtime_stage
from .stats import clear_planner_backlog_stats, empty_stats, next_run_after_task, planner_backlog_snapshot, refresh_task_stats, retry_failed_actions
from .utils import as_int as _as_int, as_int_list as _as_int_list
from .runtime_retention import cleanup_runtime_details, cleanup_runtime_metric_snapshots_if_due
from app.services.tenant_target_profile import tenant_learning_profile_preview
from app.services.source_media import WAITING_MATERIAL_CACHE, expire_waiting_source_media_actions, wake_waiting_actions_for_source_media
from app.services.account_online_projection import task_account_online_summary
from app.services.runtime_summary import clear_task_runtime_artifacts, reconcile_stale_operation_issues

_empty_stats = empty_stats
_next_run_after_task = next_run_after_task
_retry_failed_actions = retry_failed_actions
CHANNEL_COMMENT_SCENE = "channel_comment"
GROUP_CHAT_SCENE = "group_chat"
GROUP_PREVIEW_CANDIDATE_SHORTFALL_MESSAGE = "AI 普通发言候选不足，无法生成完整预览"
CHANNEL_PREVIEW_CANDIDATE_SHORTFALL_MESSAGE = "AI 评论候选不足，无法生成完整预览"
OPEN_PLAN_ACTION_STATUSES = {"pending", "claiming", "executing", "retryable_failed"}
MEMBERSHIP_PENDING_STATUSES = {"pending", "claiming", "executing", "retryable_failed"}
MEMBERSHIP_UNKNOWN_STATUSES = {"unknown_after_send"}
MEMBERSHIP_ACTION_TYPES = {TARGET_MEMBERSHIP_ACTION_TYPE, LEGACY_MEMBERSHIP_ACTION_TYPE}
TARGET_PERMISSION_MARKERS = (
    "lack permission",
    "banned",
    "private",
    "sendmessagerequest",
    "chatwriteforbidden",
    "userbanned",
    "该账号不可向此群发送",
    "群无权限",
    "账号不可发言",
    "缓存频道不可访问",
)
COMMENT_UNAVAILABLE_MARKERS = (
    "评论区不可用",
    "无法解析到评论区",
    "comment_unavailable",
    "msgidinvalid",
    "discussion",
)
ACCOUNT_AUTH_MARKERS = ("session", "auth key", "auth_key", "unauthorized", "重新登录", "账号没有可用 session", "session 已失效")
RATE_LIMIT_MARKERS = ("floodwait", "too many requests", "slowmode", "慢速模式", "冷却")
HARD_HOURLY_WAKE_MIN_SCAN = 20
HARD_HOURLY_RECOVERY_MIN_BATCH = 1000
HARD_HOURLY_RECOVERY_LIMIT_MULTIPLIER = 20


from .config_fields import (
    CHANNEL_DYNAMIC_TASK_TYPES,
    COMMON_CREATE_FIELDS,
    COMMON_SETTINGS_FIELDS,
    GROUP_AI_LEGACY_RUNTIME_FIELDS,
    GROUP_RELAY_LEGACY_CREATE_FIELDS,
    SEARCH_JOIN_PACING_FIELDS,
    TYPE_SETTINGS_FIELDS,
)
from .hard_hourly import current_progress as hard_hourly_current_progress, enabled as hard_hourly_enabled, requires_planning as hard_hourly_requires_planning
from .config_normalization import (
    apply_default_rule_binding,
    apply_default_slang_config,
    normalize_operation_target_references,
    pacing_config_payload,
    validate_rule_binding,
    validated_type_config,
)


def create_group_ai_chat_task(session: Session, tenant_id: int, payload: GroupAIChatTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "group_ai_chat", payload, actor)


def create_group_relay_task(session: Session, tenant_id: int, payload: GroupRelayTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "group_relay", payload, actor)


def create_group_membership_admission_task(session: Session, tenant_id: int, payload: GroupMembershipAdmissionTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "group_membership_admission", payload, actor)


def create_channel_view_task(session: Session, tenant_id: int, payload: ChannelViewTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_view", payload, actor)


def create_channel_like_task(session: Session, tenant_id: int, payload: ChannelLikeTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_like", payload, actor)


def create_channel_comment_task(session: Session, tenant_id: int, payload: ChannelCommentTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_comment", payload, actor)


def create_search_join_group_task(session: Session, tenant_id: int, payload: SearchJoinGroupTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "search_join_group", payload, actor)


def create_and_start_group_ai_chat_task(session: Session, tenant_id: int, payload: GroupAIChatTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "group_ai_chat", payload, actor)


def create_and_start_group_relay_task(session: Session, tenant_id: int, payload: GroupRelayTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "group_relay", payload, actor)


def create_and_start_group_membership_admission_task(session: Session, tenant_id: int, payload: GroupMembershipAdmissionTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "group_membership_admission", payload, actor)


def create_and_start_channel_view_task(session: Session, tenant_id: int, payload: ChannelViewTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_view", payload, actor)


def create_and_start_channel_like_task(session: Session, tenant_id: int, payload: ChannelLikeTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_like", payload, actor)


def create_and_start_channel_comment_task(session: Session, tenant_id: int, payload: ChannelCommentTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_comment", payload, actor)


def create_and_start_search_join_group_task(session: Session, tenant_id: int, payload: SearchJoinGroupTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "search_join_group", payload, actor)


def _new_task(session: Session, tenant_id: int, task_type: str, payload) -> Task:
    raw_type_config = payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS, exclude_unset=True)
    raw_type_config = normalize_operation_target_references(session, tenant_id, task_type, raw_type_config)
    raw_type_config = apply_default_slang_config(session, tenant_id, task_type, raw_type_config)
    raw_type_config = apply_default_rule_binding(session, tenant_id, task_type=task_type, config=raw_type_config)
    type_config = validated_type_config(task_type, raw_type_config)
    validate_rule_binding(session, tenant_id, type_config)
    task = Task(
        tenant_id=tenant_id,
        name=payload.name,
        type=task_type,
        status="draft",
        priority=payload.priority,
        timezone=payload.timezone,
        scheduled_start=payload.scheduled_start,
        scheduled_end=payload.scheduled_end,
        max_duration_hours=payload.max_duration_hours,
        account_config=payload.account_config.model_dump(mode="json"),
        pacing_config=pacing_config_payload(payload.pacing_config),
        failure_policy=payload.failure_policy.model_dump(mode="json"),
        type_config=type_config,
        stats=empty_stats(),
    )
    session.add(task)
    session.flush()
    return task


def _create_task(session: Session, tenant_id: int, task_type: str, payload, actor: str) -> Task:
    task = _new_task(session, tenant_id, task_type, payload)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    session.commit()
    session.refresh(task)
    return task


def _create_and_start_task(session: Session, tenant_id: int, task_type: str, payload, actor: str) -> Task:
    _assert_precheck_allows_start(session, tenant_id, task_type, payload.model_dump(mode="json"))
    task = _new_task(session, tenant_id, task_type, payload)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    _mark_task_started(task)
    audit(session, tenant_id=tenant_id, actor=actor, action="启动任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def _task_payload_with_runtime_summary(session: Session, task: Task, summary: TaskRuntimeSummary | None) -> dict[str, Any]:
    payload = _task_payload(session, task, include_detail_search=True)
    if not summary:
        return payload
    stats = dict(payload.get("stats") or {})
    stats.update(
        {
            "total_actions": summary.planned_count,
            "success_count": summary.success_count,
            "failure_count": summary.failed_count,
            "pending_count": summary.pending_count,
            "oldest_pending_at": summary.oldest_pending_at,
            "latest_failure_type": summary.latest_failure_type,
            "runtime_summary_updated_at": summary.updated_at,
        }
    )
    payload["stats"] = stats
    payload["runtime_stage"] = derive_task_runtime_stage(task, summary=summary)
    return payload


def list_tasks(session: Session, tenant_id: int, task_type: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    type_filter = normalize_list_filter(task_type)
    status_filter = normalize_list_filter(status)
    stmt = select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    if type_filter:
        stmt = stmt.where(Task.type == type_filter)
    if status_filter:
        stmt = stmt.where(Task.status == status_filter)
    tasks = list(session.scalars(stmt.order_by(Task.priority.asc(), Task.created_at.desc())))
    summaries = _task_runtime_summaries(session, tenant_id)
    task_rows = [_task_payload_with_runtime_summary(session, task, summaries.get(task.id)) for task in tasks]
    return [*task_rows, *list_profile_batch_tasks(session, tenant_id, type_filter, status_filter)]


def _task_runtime_summaries(session: Session, tenant_id: int) -> dict[str, TaskRuntimeSummary]:
    rows = session.scalars(
        select(TaskRuntimeSummary)
        .join(Task, Task.id == TaskRuntimeSummary.task_id)
        .where(TaskRuntimeSummary.tenant_id == tenant_id, Task.deleted_at.is_(None))
    )
    return {summary.task_id: summary for summary in rows}


def get_task_detail(session: Session, tenant_id: int, task_id: str) -> dict[str, Any]:
    if is_profile_batch_task_id(task_id):
        return get_profile_batch_task_detail(session, tenant_id, task_id)
    task = _get_task(session, tenant_id, task_id)
    return _task_summary_detail(session, tenant_id, task)


def refresh_task_detail_stats(session: Session, tenant_id: int, task_id: str) -> dict[str, Any]:
    task = _get_task(session, tenant_id, task_id)
    return _stats_with_account_coverage(session, task, refresh_task_stats(session, task))


def _task_summary_detail(session: Session, tenant_id: int, task: Task) -> dict[str, Any]:
    task_summary = session.scalar(select(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == tenant_id, TaskRuntimeSummary.task_id == task.id))
    operation_plan_links = list(session.scalars(select(OperationPlanTaskLink).where(OperationPlanTaskLink.tenant_id == tenant_id, OperationPlanTaskLink.task_id == task.id)))
    membership_phase = _summary_membership_phase(session, task)
    stats = _stats_with_account_coverage(session, task, _summary_stats(task, membership_phase))
    admission_phase = membership_admission_summary(session, task)
    task_payload = _task_payload(session, task, actions=[], include_detail_search=False)
    task_payload["runtime_stage"] = derive_task_runtime_stage(task, actions=[], membership_phase=membership_phase, summary=task_summary)
    ai_quality_actions = _ai_quality_actions(session, task) if task.type == "group_ai_chat" else []
    return {
        "task": task_payload,
        "actions": [],
        "stats": stats,
        "task_runtime_summary": task_summary,
        "operation_plan_links": operation_plan_links,
        "accounts": [],
        "membership_phase": membership_phase,
        "membership_accounts": [],
        "membership_admission_phase": admission_phase,
        "membership_admission_items": [],
        "message_groups": [],
        "ai_cycles": [],
        "ai_generation_records": _ai_generation_records(ai_quality_actions),
        "ai_account_profiles": [],
        "ai_quality_funnel": _ai_quality_funnel(ai_quality_actions, task.stats if isinstance(task.stats, dict) else {}),
        "account_online_summary": task_account_online_summary(session, task) if task.type in {"group_ai_chat", "group_relay"} else {},
        "relay_batches": [],
        "recent_relay_sources": _relay_recent_sources(session, task) if task.type == "group_relay" else [],
        "learning_profile_preview": _task_learning_profile_preview(session, task),
    }


def _ai_quality_actions(session: Session, task: Task) -> list[Action]:
    return list(
        session.scalars(
            select(Action)
            .where(
                Action.tenant_id == task.tenant_id,
                Action.task_id == task.id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
            )
            .order_by(Action.created_at.desc())
        )
    )


def _summary_membership_phase(session: Session, task: Task) -> dict[str, Any]:
    stats = task.stats if isinstance(task.stats, dict) else {}
    if stats and any(key.startswith("membership_") for key in stats):
        return _membership_phase(task, None)
    return _lightweight_membership_phase(session, task)


def _summary_stats(task: Task, membership_phase: dict[str, Any]) -> dict[str, Any]:
    stats = dict(task.stats or empty_stats())
    if int(stats.get("total_actions") or 0) > 0:
        return stats
    total = int((membership_phase.get("summary") or {}).get("action_count") or 0)
    if total <= 0:
        return stats
    stats.update(
        {
            "total_actions": total,
            "success_count": int(membership_phase.get("success_count") or 0),
            "failure_count": int(membership_phase.get("failed_count") or 0),
            "pending_count": int(membership_phase.get("pending_account_count") or 0),
            "executing_count": int(membership_phase.get("running_count") or 0),
            "unknown_after_send_count": int(membership_phase.get("unknown_after_send_count") or 0),
        }
    )
    return stats


def _lightweight_membership_phase(session: Session, task: Task) -> dict[str, Any]:
    rows = session.scalars(
        select(Action)
        .where(
            Action.tenant_id == task.tenant_id,
            Action.task_id == task.id,
            Action.action_type.in_([TARGET_MEMBERSHIP_ACTION_TYPE, LEGACY_MEMBERSHIP_ACTION_TYPE]),
        )
    ).all()
    success = sum(1 for action in rows if _membership_action_succeeded(action))
    pending = sum(1 for action in rows if action.status in MEMBERSHIP_PENDING_STATUSES)
    failed = sum(1 for action in rows if _membership_action_failed(action))
    unknown = sum(1 for action in rows if action.status in MEMBERSHIP_UNKNOWN_STATUSES)
    total = len(rows)
    running = sum(1 for action in rows if action.status in {"claiming", "executing"})
    blocked = sum(1 for action in rows if _membership_action_blocked(action)) + unknown
    stage = "membership_running" if pending else "membership_blocked" if blocked else "membership_ready"
    status = "partial_success" if success and (pending or blocked) else "pending" if pending else "blocked" if unknown else "failed" if failed else "completed"
    return {
        "stage": stage,
        "status": status,
        "progress_percent": round((success + failed) * 100 / total) if total else 100,
        "current_phase": "排队中" if pending else "等待人工确认" if unknown else "等待人工处理" if failed else "已完成",
        "warnings": [],
        "summary": {"action_count": total, "success_account_count": success, "unknown_after_send_count": unknown},
        "ready_account_count": success,
        "pending_account_count": pending,
        "running_account_count": running,
        "success_account_count": success,
        "failed_account_count": failed,
        "unknown_after_send_count": unknown,
        "blocked_account_count": blocked,
        "failed_count": failed,
        "running_count": running,
        "success_count": success,
    }


def _task_learning_profile_preview(session: Session, task: Task) -> dict[str, Any]:
    if task.type == "group_ai_chat":
        return tenant_learning_profile_preview(session, task.tenant_id, GROUP_CHAT_SCENE)
    if task.type == "channel_comment":
        return tenant_learning_profile_preview(session, task.tenant_id, CHANNEL_COMMENT_SCENE)
    return {}


def update_task(session: Session, tenant_id: int, task_id: str, payload: TaskUpdate, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    raw_data = payload.model_dump(exclude_unset=True)
    data = payload.model_dump(exclude_unset=True, mode="json")
    for field in ["name", "priority", "timezone", "scheduled_start", "scheduled_end", "max_duration_hours"]:
        if field in raw_data:
            setattr(task, field, raw_data[field])
    for field in ["account_config", "pacing_config", "failure_policy"]:
        if field in data and data[field] is not None:
            setattr(task, field, _pacing_payload_for_task(task, raw_data[field]) if field == "pacing_config" else data[field])
    task.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def update_task_settings(session: Session, tenant_id: int, task_id: str, payload: TaskSettingsUpdate, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    raw_data = payload.model_dump(exclude_unset=True)
    data = payload.model_dump(exclude_unset=True, mode="json")
    type_fields = TYPE_SETTINGS_FIELDS.get(task.type)
    if type_fields is None:
        raise ValueError(f"unknown task type: {task.type}")
    type_updates = {key: value for key, value in data.items() if key not in COMMON_SETTINGS_FIELDS}
    invalid = sorted(set(type_updates) - type_fields)
    if invalid:
        raise ValueError(f"这些字段不能用于 {task.type} 任务: {', '.join(invalid)}")
    for field in ["name", "priority", "timezone", "scheduled_start", "scheduled_end", "max_duration_hours"]:
        if field in raw_data:
            setattr(task, field, raw_data[field])
    for field in ["account_config", "pacing_config", "failure_policy"]:
        if field in data and data[field] is not None:
            setattr(task, field, _pacing_payload_for_task(task, raw_data[field]) if field == "pacing_config" else data[field])
    if task.type == "group_ai_chat" and "pacing_config" in data and not type_updates:
        next_config = dict(task.type_config or {})
        for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
            next_config.pop(field, None)
        task.type_config = validated_type_config(task.type, next_config)
    if type_updates:
        next_config = dict(task.type_config or {})
        next_config.update(type_updates)
        if task.type == "group_ai_chat":
            for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
                if field not in type_updates:
                    next_config.pop(field, None)
        next_config = normalize_operation_target_references(session, tenant_id, task.type, next_config)
        task.type_config = validated_type_config(task.type, next_config)
    _clear_unfinished_plan(session, task)
    if task.status not in {"completed", "failed"}:
        now = _now()
        scheduled_start = _naive_datetime(task.scheduled_start)
        task.status = "pending" if scheduled_start and scheduled_start > now else "running"
        task.next_run_at = scheduled_start if task.status == "pending" else now
    task.last_error = ""
    task.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新任务中心任务配置", target_type="task", target_id=task.id, detail=task.type)
    refresh_task_stats(session, task)
    session.commit()
    session.refresh(task)
    return task


def add_task_source_filter_override(session: Session, tenant_id: int, task_id: str, payload: TaskSourceFilterOverrideRequest, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type != "group_relay":
        raise ValueError("来源过滤覆盖仅支持群转发任务")

    next_config = dict(task.type_config or {})
    if payload.sender_peer_id:
        next_config["excluded_sender_peer_ids"] = _append_unique_string(next_config.get("excluded_sender_peer_ids"), payload.sender_peer_id)
    if payload.sender_username:
        next_config["excluded_sender_usernames"] = _append_unique_string(next_config.get("excluded_sender_usernames"), payload.sender_username)
    if payload.sender_name:
        next_config["excluded_sender_names"] = _append_unique_string(next_config.get("excluded_sender_names"), payload.sender_name)

    next_config = normalize_operation_target_references(session, tenant_id, task.type, next_config)
    next_config = apply_default_rule_binding(session, tenant_id, task_type=task.type, config=next_config)
    validate_rule_binding(session, tenant_id, next_config)
    task.type_config = validated_type_config(task.type, next_config)
    _clear_unfinished_plan(session, task)
    task.last_error = ""
    task.updated_at = _now()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="添加任务来源过滤覆盖",
        target_type="task",
        target_id=task.id,
        detail=_source_filter_override_detail(payload),
    )
    refresh_task_stats(session, task)
    session.commit()
    session.refresh(task)
    return task


def _append_unique_string(current: Any, value: str) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in [*(current if isinstance(current, list) else []), value]:
        text = str(item or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _source_filter_override_detail(payload: TaskSourceFilterOverrideRequest) -> str:
    parts = [
        f"sender_peer_id={payload.sender_peer_id or '-'}",
        f"sender_username={payload.sender_username or '-'}",
        f"sender_name={payload.sender_name or '-'}",
        f"source_action_id={payload.source_action_id or '-'}",
        f"source_action={payload.source_action or '-'}",
        f"reason={payload.reason}",
    ]
    return "; ".join(parts)


def update_group_ai_chat_config(session: Session, tenant_id: int, task_id: str, payload: GroupAIChatTaskConfigUpdate, actor: str) -> Task:
    return _update_type_config(session, tenant_id, task_id, "group_ai_chat", payload, actor)


def update_group_relay_config(session: Session, tenant_id: int, task_id: str, payload: GroupRelayTaskConfigUpdate, actor: str) -> Task:
    return _update_type_config(session, tenant_id, task_id, "group_relay", payload, actor)


def update_channel_view_config(session: Session, tenant_id: int, task_id: str, payload: ChannelViewTaskConfigUpdate, actor: str) -> Task:
    return _update_type_config(session, tenant_id, task_id, "channel_view", payload, actor)


def update_channel_like_config(session: Session, tenant_id: int, task_id: str, payload: ChannelLikeTaskConfigUpdate, actor: str) -> Task:
    return _update_type_config(session, tenant_id, task_id, "channel_like", payload, actor)


def update_channel_comment_config(session: Session, tenant_id: int, task_id: str, payload: ChannelCommentTaskConfigUpdate, actor: str) -> Task:
    return _update_type_config(session, tenant_id, task_id, "channel_comment", payload, actor)


def update_search_join_group_config(session: Session, tenant_id: int, task_id: str, payload: SearchJoinGroupTaskConfigUpdate, actor: str) -> Task:
    update_data = payload.model_dump(mode="json", exclude_unset=True)
    pacing_data = update_data.pop("pacing_config", None)
    task = _apply_type_config_data(session, tenant_id, task_id, "search_join_group", update_data, actor)
    if pacing_data is not None:
        task.pacing_config = pacing_config_payload(pacing_data)
        task.updated_at = _now()
    session.commit()
    session.refresh(task)
    return task


def start_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    _assert_precheck_allows_start(session, tenant_id, task.type, _task_create_payload_for_precheck(task))
    _mark_task_started(task)
    audit(session, tenant_id=tenant_id, actor=actor, action="启动任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def pause_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    task.status = "paused"
    task.next_run_at = None
    audit(session, tenant_id=tenant_id, actor=actor, action="暂停任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def resume_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    return start_task(session, tenant_id, task_id, actor)


def stop_task(session: Session, tenant_id: int, task_id: str, actor: str, reason: str = "") -> Task:
    task = _get_task(session, tenant_id, task_id)
    task.status = "stopped"
    task.next_run_at = None
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")):
        action.status = "skipped"
        action.result = {"success": False, "error_code": "task_stopped", "error_message": "任务已停止"}
        action.executed_at = _now()
    refresh_task_stats(session, task)
    audit(session, tenant_id=tenant_id, actor=actor, action="停止任务中心任务", target_type="task", target_id=task.id, detail=reason)
    session.commit()
    session.refresh(task)
    return task


def delete_task(session: Session, tenant_id: int, task_id: str, actor: str, reason: str = "") -> None:
    if is_profile_batch_task_id(task_id):
        delete_profile_batch_task(session, tenant_id, task_id, actor=actor, reason=reason)
        return
    task = _get_task(session, tenant_id, task_id)
    now = _now()
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status.in_(["pending", "executing"]))):
        action.status = "skipped"
        action.result = {"success": False, "error_code": "task_deleted", "error_message": "任务已删除"}
        action.executed_at = now
    refresh_task_stats(session, task)
    task.status = "deleted"
    task.next_run_at = None
    task.deleted_at = now
    task.deleted_by = actor
    task.delete_reason = reason
    task.updated_at = now
    clear_task_runtime_artifacts(session, task, reason="任务删除后自动解决关联告警", actor=actor)
    audit(session, tenant_id=tenant_id, actor=actor, action="删除任务中心任务", target_type="task", target_id=task.id, detail=reason)
    session.commit()


def retry_task(session: Session, tenant_id: int, task_id: str, payload: TaskRetryRequest, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    stmt = select(Action).where(Action.task_id == task.id)
    if payload.failed_only:
        stmt = stmt.where(Action.status.in_(["failed", "unknown_after_send", "skipped"]))
    for action in session.scalars(stmt):
        if payload.failed_only and not _action_should_retry(task, action):
            continue
        action.status = "pending"
        action.retry_count = 0
        action.scheduled_at = _now()
        action.executed_at = None
        action.result = {}
    task.status = "running"
    task.next_run_at = _now()
    task.last_error = ""
    audit(session, tenant_id=tenant_id, actor=actor, action="重试任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def _action_should_retry(task: Task, action: Action) -> bool:
    if action.status in {"failed", "unknown_after_send"}:
        return True
    if task.type != "target_admission_retry":
        return False
    result = action.result or {}
    return (
        action.action_type in MEMBERSHIP_ACTION_TYPES
        and result.get("error_code") == "membership_permission_denied"
        and result.get("membership_status") == "permission_denied"
    )


def reset_task(session: Session, tenant_id: int, task_id: str, actor: str, reason: str = "") -> Task:
    task = _get_task(session, tenant_id, task_id)
    now = _now()
    stats = empty_stats()
    stats["started_at"] = now.isoformat()
    if task.type == "group_ai_chat":
        stats["force_bootstrap_once"] = True
    task.stats = stats
    _clear_unfinished_plan(session, task)
    _clear_group_ai_context_fingerprints(session, task)
    _invalidate_task_listener_cache(task)
    task.status = "pending" if task.scheduled_start and task.scheduled_start > now else "running"
    task.next_run_at = task.scheduled_start if task.status == "pending" else now
    task.last_error = ""
    task.updated_at = now
    refresh_task_stats(session, task)
    audit(session, tenant_id=tenant_id, actor=actor, action="重置任务中心任务", target_type="task", target_id=task.id, detail=reason)
    session.commit()
    session.refresh(task)
    return task


def _clear_group_ai_context_fingerprints(session: Session, task: Task) -> None:
    if task.type != "group_ai_chat":
        return
    session.execute(
        delete(MessageFingerprint).where(
            MessageFingerprint.tenant_id == task.tenant_id,
            MessageFingerprint.source_group_id.like(f"{task.id}:group_ai_chat:%"),
        )
    )


def list_actions(session: Session, tenant_id: int, task_id: str | None = None, status: str | None = None) -> list[Action]:
    stmt = select(Action).where(Action.tenant_id == tenant_id)
    if task_id:
        stmt = stmt.where(Action.task_id == task_id)
    if status:
        stmt = stmt.where(Action.status == status)
    return list(session.scalars(stmt.order_by(Action.scheduled_at.desc(), Action.created_at.desc()).limit(500)))


def list_actions_page(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    status: str | None = None,
    action_type: str | None = None,
    account_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    sort_by: str = "scheduled_at",
    sort_order: str = "desc",
) -> tuple[list[dict[str, Any]], int]:
    filters = [Action.tenant_id == tenant_id, Action.task_id == task_id]
    _append_action_status_filter(filters, status)
    if action_type:
        filters.append(Action.action_type == action_type)
    if account_id is not None:
        filters.append(Action.account_id == account_id)
    total = session.scalar(select(func.count(Action.id)).where(*filters)) or 0
    sort_columns = {
        "scheduled_at": Action.scheduled_at,
        "executed_at": Action.executed_at,
        "created_at": Action.created_at,
        "status": Action.status,
        "action_type": Action.action_type,
        "account_id": Action.account_id,
    }
    sort_column = sort_columns.get(sort_by, Action.scheduled_at)
    order_expr = sort_column.asc() if sort_order == "asc" else sort_column.desc()
    actions = list(
        session.scalars(
            select(Action)
            .where(*filters)
            .order_by(order_expr, Action.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return _action_payloads_with_issue_rollup(session, tenant_id, actions), int(total)


def list_ai_cycles_page(session: Session, tenant_id: int, task_id: str, *, page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    cycle_id = Action.payload["cycle_id"].as_string()
    actions, total = _list_actions_for_group_page(session, tenant_id, task_id, [cycle_id.is_not(None)], [cycle_id], page=page, page_size=page_size)
    return _ai_cycles(actions), total


def list_message_groups_page(session: Session, tenant_id: int, task_id: str, *, page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    task = _get_task(session, tenant_id, task_id)
    channel_target_id = Action.payload["channel_target_id"].as_integer()
    channel_message_id = Action.payload["channel_message_id"].as_integer()
    message_id = Action.payload["message_id"].as_integer()
    filters = [
        message_id.is_not(None),
        Action.payload["channel_id"].as_string().is_not(None),
    ]
    key_exprs = [channel_target_id, channel_message_id, message_id, Action.action_type]
    actions, total = _list_actions_for_group_page(session, tenant_id, task_id, filters, key_exprs, page=page, page_size=page_size)
    return _message_groups(session, task, actions), total


def list_relay_batches_page(session: Session, tenant_id: int, task_id: str, *, page: int = 1, page_size: int = 20) -> tuple[list[dict[str, Any]], int]:
    batch_id = Action.payload["relay_batch_id"].as_string()
    actions, total = _list_actions_for_group_page(session, tenant_id, task_id, [batch_id.is_not(None)], [batch_id], page=page, page_size=page_size)
    return _relay_batches(actions), total


def _list_actions_for_group_page(
    session: Session,
    tenant_id: int,
    task_id: str,
    extra_filters: list[Any],
    key_exprs: list[Any],
    *,
    page: int,
    page_size: int,
) -> tuple[list[Action], int]:
    _get_task(session, tenant_id, task_id)
    filters = [Action.tenant_id == tenant_id, Action.task_id == task_id, *extra_filters]
    page_keys, total = _group_page_keys(session, filters, key_exprs, page=page, page_size=page_size)
    if not page_keys:
        return [], total
    key_filter = _group_key_filter(key_exprs, page_keys)
    actions = list(
        session.scalars(
            select(Action)
            .where(*filters, key_filter)
            .order_by(Action.scheduled_at.desc(), Action.created_at.desc())
        )
    )
    return actions, int(total)


def _group_page_keys(session: Session, filters: list[Any], key_exprs: list[Any], *, page: int, page_size: int) -> tuple[list[tuple], int]:
    labels = [expr.label(f"key_{index}") for index, expr in enumerate(key_exprs)]
    grouped = (
        select(*labels, func.max(Action.scheduled_at).label("latest_scheduled"), func.max(Action.created_at).label("latest_created"))
        .where(*filters)
        .group_by(*key_exprs)
        .subquery()
    )
    total = session.scalar(select(func.count()).select_from(grouped)) or 0
    rows = session.execute(
        select(*[grouped.c[f"key_{index}"] for index in range(len(key_exprs))])
        .order_by(grouped.c.latest_scheduled.desc(), grouped.c.latest_created.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return [tuple(row) for row in rows], int(total)


def _group_key_filter(key_exprs: list[Any], keys: list[tuple]) -> Any:
    return or_(*[and_(*[_value_matches(expr, value) for expr, value in zip(key_exprs, key, strict=True)]) for key in keys])


def _value_matches(expr: Any, value: Any) -> Any:
    return expr.is_(None) if value is None else expr == value


def _append_action_status_filter(filters: list[Any], status: str | None) -> None:
    if status == "planned":
        filters.append(Action.status.in_(OPEN_PLAN_ACTION_STATUSES))
        return
    if status == "executed":
        filters.append(Action.status.notin_(OPEN_PLAN_ACTION_STATUSES))
        return
    if status:
        filters.append(Action.status == status)


def list_membership_items_page(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    status: str | None = None,
    phase: str | None = None,
    account_id: int | None = None,
    manual_required: bool | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[dict[str, Any]], int]:
    task = _get_task(session, tenant_id, task_id)
    if phase is None and manual_required is None:
        actions, total = _list_membership_actions_page(
            session,
            tenant_id,
            task.id,
            status=status,
            account_id=account_id,
            page=page,
            page_size=page_size,
        )
        return _membership_page_payloads(session, task, actions), total
    return _filtered_membership_items_page(
        session,
        task,
        status=status,
        account_id=account_id,
        phase=phase,
        manual_required=manual_required,
        page=page,
        page_size=page_size,
    )


def _filtered_membership_items_page(
    session: Session,
    task: Task,
    *,
    status: str | None,
    account_id: int | None,
    phase: str | None,
    manual_required: bool | None,
    page: int,
    page_size: int,
) -> tuple[list[dict[str, Any]], int]:
    chunk_size = max(page_size, 100)
    action_page = 1
    total_matches = 0
    selected: list[dict[str, Any]] = []
    start = (page - 1) * page_size
    while True:
        actions, action_total = _list_membership_actions_page(
            session, task.tenant_id, task.id, status=status, account_id=account_id, page=action_page, page_size=chunk_size
        )
        if not actions:
            return selected, total_matches
        for row in _membership_page_payloads(session, task, actions):
            if not _membership_row_matches(row, phase, manual_required):
                continue
            if start <= total_matches < start + page_size:
                selected.append(row)
            total_matches += 1
        if action_page * chunk_size >= action_total:
            return selected, total_matches
        action_page += 1


def _membership_row_matches(row: dict[str, Any], phase: str | None, manual_required: bool | None) -> bool:
    if phase and row.get("phase") != phase:
        return False
    if manual_required is not None and bool(row.get("manual_required")) != manual_required:
        return False
    return True


def _list_membership_actions_page(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    status: str | None = None,
    account_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
) -> tuple[list[Action], int]:
    filters = _membership_action_filters(tenant_id, task_id, status=status, account_id=account_id)
    total = session.scalar(select(func.count(Action.id)).where(*filters)) or 0
    rows = list(
        session.scalars(
            select(Action)
            .where(*filters)
            .order_by(Action.scheduled_at.desc(), Action.account_id.asc(), Action.created_at.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return rows, int(total)


def _membership_page_payloads(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    accounts = _accounts_by_id(session, actions)
    groups = _groups_by_target_id(session, task.tenant_id, actions)
    verifications = _verification_tasks_by_group_account(session, task.tenant_id, groups, actions)
    attempts = _latest_attempts_by_action(session, actions)
    return [_membership_item_payload(action, accounts, groups, verifications, attempts) for action in actions]


def _list_membership_actions(
    session: Session,
    tenant_id: int,
    task_id: str,
    *,
    status: str | None = None,
    account_id: int | None = None,
) -> list[Action]:
    filters = _membership_action_filters(tenant_id, task_id, status=status, account_id=account_id)
    return list(session.scalars(select(Action).where(*filters).order_by(Action.scheduled_at.desc(), Action.created_at.desc())))


def _membership_action_filters(
    tenant_id: int,
    task_id: str,
    *,
    status: str | None = None,
    account_id: int | None = None,
) -> list[Any]:
    filters: list[Any] = [
        Action.tenant_id == tenant_id,
        Action.task_id == task_id,
        Action.action_type.in_([TARGET_MEMBERSHIP_ACTION_TYPE, LEGACY_MEMBERSHIP_ACTION_TYPE]),
    ]
    if status:
        filters.append(Action.status == status)
    if account_id is not None:
        filters.append(Action.account_id == account_id)
    return filters


def list_action_attempts(session: Session, tenant_id: int, task_id: str, action_id: str) -> list[ExecutionAttempt]:
    action = session.get(Action, action_id)
    if not action or action.tenant_id != tenant_id or action.task_id != task_id:
        raise ValueError("action not found")
    return list(
        session.scalars(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.tenant_id == tenant_id, ExecutionAttempt.action_id == action_id)
            .order_by(ExecutionAttempt.attempt_no.asc(), ExecutionAttempt.created_at.asc())
        )
    )


def generate_group_ai_chat_preview(session: Session, tenant_id: int, payload: GroupAIChatTaskPreviewRequest) -> dict[str, list[str]]:
    config = GroupAIChatConfig(**payload.model_dump(mode="json", exclude={"count"})).model_dump(mode="json")
    contents, _ = generate_group_messages(session, tenant_id, config, count=payload.count, target_label="群组", history="")
    if len(contents) < payload.count:
        raise AiGenerationUnavailable(GROUP_PREVIEW_CANDIDATE_SHORTFALL_MESSAGE)
    return {"previews": contents[: payload.count]}


def generate_channel_comment_preview(session: Session, tenant_id: int, payload: ChannelCommentTaskPreviewRequest) -> dict[str, list[str]]:
    config = ChannelCommentConfig(**payload.model_dump(mode="json", exclude={"count", "message_content"})).model_dump(mode="json")
    contents, _ = generate_channel_comments(session, tenant_id, config, count=payload.count, message_content=payload.message_content or "频道消息内容示例", target_label="频道")
    if len(contents) < payload.count:
        raise AiGenerationUnavailable(CHANNEL_PREVIEW_CANDIDATE_SHORTFALL_MESSAGE)
    return {"previews": contents[: payload.count]}


def recommend_accounts(session: Session, tenant_id: int, payload: RecommendTaskAccountsRequest) -> list[dict[str, Any]]:
    accounts = select_task_accounts(
        session,
        tenant_id,
        payload.model_dump(mode="json"),
        target_group_id=payload.target_group_id,
        limit=payload.limit,
    )
    return [{"id": item.id, "display_name": item.display_name, "username": item.username, "status": item.status, "reason": "可用账号"} for item in accounts]


def check_channel_capacity(session: Session, tenant_id: int, payload: ChannelCapacityCheckRequest) -> dict[str, Any]:
    accounts = select_task_accounts(
        session,
        tenant_id,
        payload.account_config.model_dump(mode="json"),
        limit=payload.target_per_message,
    )
    effective_count = len(accounts)
    membership_summary: dict[str, Any] = {}
    if payload.target_channel_id:
        channel = session.get(OperationTarget, int(payload.target_channel_id))
        if channel and channel.tenant_id == tenant_id and channel.target_type == "channel":
            membership_summary = channel_membership_summary(session, tenant_id, channel, payload.account_config.model_dump(mode="json"), candidates=accounts)
            effective_count = int(membership_summary.get("joined_account_count") or 0) + int(membership_summary.get("need_join_account_count") or 0)
    action_label = {"channel_view": "浏览", "channel_like": "点赞", "channel_comment": "评论"}.get(payload.task_type, "互动")
    will_shortfall = payload.target_per_message > effective_count
    warning = ""
    if will_shortfall:
        warning = f"每条消息目标{action_label} {payload.target_per_message}，当前参与账号 {effective_count} 个；任务会继续运行，账号恢复或增加后继续补计划。"
    return {
        "effective_account_count": effective_count,
        "target_per_message": payload.target_per_message,
        "max_effective_per_message": effective_count,
        "will_shortfall": will_shortfall,
        "warning_message": warning,
        "membership_summary": membership_summary,
    }


def precheck_task_creation(session: Session, tenant_id: int, payload: TaskPrecheckRequest) -> dict[str, Any]:
    return run_precheck_task_creation(
        session,
        tenant_id,
        payload,
        normalize_operation_target_references=normalize_operation_target_references,
        validated_type_config=validated_type_config,
        validate_rule_binding=validate_rule_binding,
    )


def _assert_precheck_allows_start(session: Session, tenant_id: int, task_type: str, payload: dict[str, Any]) -> None:
    result = precheck_task_creation(session, tenant_id, TaskPrecheckRequest(task_type=task_type, payload=payload))
    if result.get("decision") == "block":
        reasons = result.get("blockers") or result.get("risk_hits") or ["任务预检阻塞"]
        if task_type in {"channel_view", "channel_like", "channel_comment"} and set(str(item) for item in reasons) <= {"没有匹配账号", "no_available_account"}:
            return
        raise ValueError("；".join(str(item) for item in reasons if item))


def _task_create_payload_for_precheck(task: Task) -> dict[str, Any]:
    return {
        "name": task.name,
        "priority": task.priority,
        "timezone": task.timezone,
        "scheduled_start": task.scheduled_start,
        "scheduled_end": task.scheduled_end,
        "max_duration_hours": task.max_duration_hours,
        "account_config": task.account_config or {},
        "pacing_config": task.pacing_config or {},
        "failure_policy": task.failure_policy or {},
        **(task.type_config or {}),
    }
def drain_task_center(session_factory, limit: int = 100) -> int:
    processed = 0
    with session_factory() as session:
        record_worker_heartbeat(session, metadata={"limit": limit})
        session.commit()
    processed += _drain_task_listener(session_factory, limit=limit, process_type=None)
    recovery_count, _ = _drain_task_recovery(session_factory, limit=limit, process_type=None)
    processed += recovery_count
    planner_count, future_open_action_task_ids = _drain_task_planner(session_factory, limit=limit, process_type=None)
    processed += planner_count
    processed += _drain_task_dispatcher(session_factory, limit=limit, exclude_task_ids=future_open_action_task_ids, process_type=None)
    return processed


def drain_task_listener(session_factory, limit: int = 100) -> int:
    return _drain_task_listener(session_factory, limit=limit, process_type="listener")


def _drain_task_listener(session_factory, *, limit: int, process_type: str | None) -> int:
    result = drain_listener_runtime(session_factory, limit=limit)
    if process_type:
        with session_factory() as session:
            record_worker_heartbeat(
                session,
                process_type=process_type,
                metadata={"limit": limit, "source_count": result.source_count, "processed_count": result.processed_count},
            )
            session.commit()
    return result.processed_count


def drain_task_recovery(session_factory, limit: int = 100) -> int:
    processed, _ = _drain_task_recovery(session_factory, limit=limit, process_type="recovery")
    return processed


def _drain_task_recovery(session_factory, *, limit: int, process_type: str | None) -> tuple[int, set[int]]:
    processed = 0
    touched_tenant_ids: set[int] = set()
    with session_factory() as session:
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
        processed += recover_expired_claims(session)
        processed += recover_expired_hard_hourly_actions(session, limit=_hard_hourly_recovery_limit(limit))
        processed += fast_track_pending_hard_hourly_memberships(session, limit=_hard_hourly_recovery_limit(limit))
        processed += recover_missing_hard_hourly_memberships(session, limit=_hard_hourly_recovery_limit(limit))
        processed += fast_track_pending_hard_hourly_memberships(session, limit=_hard_hourly_recovery_limit(limit))
        processed += _recover_continuous_task_states(session)
        processed += _recover_stale_executing_actions(session)
        processed += expire_reviews(session)
        settings = get_settings()
        if settings.enable_runtime_retention_cleanup:
            processed += cleanup_runtime_details(session, retention_days=settings.runtime_detail_retention_days)
            processed += cleanup_runtime_metric_snapshots_if_due(
                session,
                retention_days=settings.runtime_metric_retention_days,
                batch_size=settings.runtime_metric_retention_batch_size,
                interval_seconds=settings.runtime_metric_cleanup_interval_seconds,
            )
        processed += expire_waiting_source_media_actions(session, limit=max(10, limit))
        tenant_ids = list(session.scalars(select(Action.tenant_id).where(Action.status == WAITING_MATERIAL_CACHE).distinct()))
        touched_tenant_ids.update(int(tenant_id) for tenant_id in tenant_ids)
        for tenant_id in tenant_ids:
            processed += wake_waiting_actions_for_source_media(session, tenant_id=tenant_id, limit=max(10, limit))
        session.commit()
    return processed, touched_tenant_ids


def _hard_hourly_recovery_limit(limit: int) -> int:
    return max(HARD_HOURLY_RECOVERY_MIN_BATCH, int(limit or 0) * HARD_HOURLY_RECOVERY_LIMIT_MULTIPLIER)


def drain_task_planner(session_factory, limit: int = 100) -> int:
    processed, _ = _drain_task_planner(session_factory, limit=limit, process_type="planner")
    return processed


def _drain_task_planner(session_factory, *, limit: int, process_type: str | None) -> tuple[int, set[str]]:
    processed = 0
    with session_factory() as session:
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
        _activate_pending_tasks(session)
        hard_hourly_task_ids = _wake_hard_hourly_tasks(session, limit=limit)
        task_ids = list(
            session.scalars(
                select(Task.id)
                .where(Task.status == "running", (Task.next_run_at.is_(None)) | (Task.next_run_at <= _now()))
                .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
                .limit(max(1, limit))
            )
        )
        task_ids = _merge_planner_task_ids(hard_hourly_task_ids, task_ids, limit)
        session.commit()
    future_open_action_task_ids: set[str] = set()
    for task_id in task_ids:
        with session_factory() as session:
            _refresh_planner_heartbeat(session, process_type, limit, task_id=task_id)
            task = session.get(Task, task_id)
            if not task or task.status != "running":
                continue
            if _check_stop_conditions(session, task):
                session.commit()
                continue
            processed += retry_failed_actions(session, task)
            prepared_open_actions = prepare_open_actions_for_planning(session, task)
            processed += prepared_open_actions
            has_open_actions, open_actions_are_future = _open_actions_state(session, task)
            if task.type == "group_ai_chat" and has_open_actions and not hard_hourly_requires_planning(session, task, _now()):
                if open_actions_are_future:
                    future_open_action_task_ids.add(task.id)
                refresh_task_stats(session, task)
                session.commit()
                continue
            if _planning_backlog_blocked(session, task):
                refresh_task_stats(session, task)
                session.commit()
                continue
            created = build_task_plan(session, task)
            refresh_task_stats(session, task)
            task.next_run_at = next_run_after_task(task)
            session.commit()
            processed += created
    return processed, future_open_action_task_ids


def _refresh_planner_heartbeat(session: Session, process_type: str | None, limit: int, *, task_id: str | None = None) -> None:
    if not process_type:
        return
    metadata = {"limit": limit, "phase": "task"}
    if task_id:
        metadata["task_id"] = task_id
    record_worker_heartbeat(session, process_type=process_type, metadata=metadata)
    session.commit()


def drain_task_dispatcher(session_factory, limit: int = 100) -> int:
    return _drain_task_dispatcher(session_factory, limit=limit, exclude_task_ids=None, process_type="dispatcher")


def _drain_task_dispatcher(session_factory, *, limit: int, exclude_task_ids: set[str] | None, process_type: str | None) -> int:
    with session_factory() as session:
        dialect_name = session.bind.dialect.name if session.bind else ""
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
            session.commit()
        claimed = claim_actions(session, limit=max(10, limit), exclude_task_ids=exclude_task_ids)
        action_ids = [action.id for action in claimed]
    if not action_ids:
        return 0
    concurrency = 1 if dialect_name == "sqlite" else _dispatcher_concurrency()
    if concurrency <= 1 or len(action_ids) == 1:
        return sum(_dispatch_claimed_action(session_factory, action_id) for action_id in action_ids)
    processed = 0
    with ThreadPoolExecutor(max_workers=min(concurrency, len(action_ids)), thread_name_prefix="task-dispatcher") as executor:
        futures = [executor.submit(_dispatch_claimed_action, session_factory, action_id) for action_id in action_ids]
        for future in as_completed(futures):
            processed += int(future.result() or 0)
    return processed


def _dispatcher_concurrency() -> int:
    settings = get_settings()
    configured = max(1, int(settings.dispatcher_concurrency or 1))
    db_budget = max(1, int(settings.db_pool_size or 1) + int(settings.db_max_overflow or 0) - 2)
    return max(1, min(configured, db_budget))


def _dispatch_claimed_action(session_factory, action_id: str) -> int:
    with session_factory() as session:
        action = session.get(Action, action_id)
        if not action or action.status != "executing":
            return 0
        if not dispatch_action(session, action):
            session.commit()
            return 0
        refresh = session.get(Task, action.task_id)
        if refresh:
            refresh_task_stats(session, refresh)
        session.commit()
        return 1


def drain_task_metrics(session_factory, limit: int = 100) -> int:
    now_value = _now()
    rows: list[RuntimeMetricSnapshot] = []
    with session_factory() as session:
        record_worker_heartbeat(session, process_type="metrics", metadata={"limit": limit})
        statuses = dict(session.execute(select(Action.status, func.count()).select_from(Action).group_by(Action.status)).all())
        oldest_pending = session.scalar(select(func.min(Action.scheduled_at)).where(Action.status == "pending"))
        oldest_pending_age = int((now_value - _naive_datetime(oldest_pending)).total_seconds()) if oldest_pending else 0
        minute_cutoff = now_value - timedelta(minutes=1)
        recent_statuses = dict(
            session.execute(
                select(Action.status, func.count())
                .select_from(Action)
                .where(Action.executed_at >= minute_cutoff)
                .group_by(Action.status)
            ).all()
        )
        created_last_minute = session.scalar(select(func.count()).select_from(Action).where(Action.created_at >= minute_cutoff)) or 0
        heartbeat_cutoff = now_value - timedelta(minutes=2)
        active_workers = session.scalar(select(func.count(WorkerHeartbeat.worker_id)).where(WorkerHeartbeat.last_seen_at >= heartbeat_cutoff)) or 0
        stale_workers = session.scalar(select(func.count(WorkerHeartbeat.worker_id)).where(WorkerHeartbeat.last_seen_at < heartbeat_cutoff)) or 0
        metrics = {
            "actions.pending.count": int(statuses.get("pending") or 0),
            "actions.claiming.count": int(statuses.get("claiming") or 0),
            "actions.executing.count": int(statuses.get("executing") or 0),
            "actions.success.count": int(statuses.get("success") or 0),
            "actions.failed.count": int(statuses.get("failed") or 0),
            "actions.skipped.count": int(statuses.get("skipped") or 0),
            "actions.unknown_after_send.count": int(statuses.get("unknown_after_send") or 0),
            "actions.created.per_minute": int(created_last_minute or 0),
            "actions.success.per_minute": int(recent_statuses.get("success") or 0),
            "actions.failed.per_minute": int(recent_statuses.get("failed") or 0),
            "actions.skipped.per_minute": int(recent_statuses.get("skipped") or 0),
            "actions.oldest_pending_age_seconds": max(0, oldest_pending_age),
            "worker.active.count": int(active_workers or 0),
            "worker.stale.count": int(stale_workers or 0),
        }
        for name, value in metrics.items():
            rows.append(
                RuntimeMetricSnapshot(
                    captured_at=now_value,
                    metric_name=name,
                    dimension_type="global",
                    dimension_id="all",
                    metric_value=int(value),
                    tags={"worker_role": "metrics"},
                )
            )
        session.add_all(rows)
        for tenant_id in session.scalars(select(OperationIssue.tenant_id).where(OperationIssue.status == "open").distinct()):
            reconcile_stale_operation_issues(session, int(tenant_id))
        session.commit()
    return len(rows)


def _planning_backlog_blocked(session: Session, task: Task) -> bool:
    snapshot = planner_backlog_snapshot(session, task)
    now_value = _now()
    if hard_hourly_requires_planning(session, task, now_value):
        task.stats = clear_planner_backlog_stats(dict(task.stats or {}))
        return False
    if not snapshot["blocked"]:
        task.stats = clear_planner_backlog_stats(dict(task.stats or {}))
        return False
    stats = dict(task.stats or {})
    stats["planner_backlog_blocked"] = True
    stats["planner_backlog_blocked_at"] = now_value.isoformat()
    stats["planner_backlog_global_pending"] = int(snapshot["global_pending"])
    stats["planner_backlog_task_pending"] = int(snapshot["task_pending"])
    stats["planner_backlog_oldest_age_seconds"] = int(snapshot["oldest_age_seconds"])
    task.stats = stats
    interval = max(10, min(300, int((task.pacing_config or {}).get("interval_seconds") or 30)))
    task.next_run_at = now_value + timedelta(seconds=interval)
    return True


def _recover_continuous_task_states(session: Session) -> int:
    now = _now()
    recovered = 0
    stale_ai_errors = ("暂无群上下文", "等待监听采集")
    for task in session.scalars(
        select(Task).where(
            Task.type == "group_ai_chat",
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
    ):
        action_count = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id)) or 0
        if action_count:
            continue
        last_error = task.last_error or ""
        if not any(text in last_error for text in stale_ai_errors):
            continue
        task.last_error = ""
        task.next_run_at = now
        task.updated_at = now
        recovered += 1
    for task in session.scalars(
        select(Task).where(
            Task.type.in_(CHANNEL_DYNAMIC_TASK_TYPES),
            Task.status == "completed",
            Task.scheduled_end.is_(None),
            Task.deleted_at.is_(None),
        )
    ):
        config = task.type_config or {}
        if (config.get("message_scope") or "dynamic_new") == "specific":
            continue
        task.status = "running"
        task.next_run_at = now
        task.last_error = ""
        task.updated_at = now
        recovered += 1
    return recovered


def _recover_stale_executing_actions(session: Session, *, timeout_minutes: int = 30) -> int:
    now = _now()
    cutoff = now - timedelta(minutes=max(1, int(timeout_minutes or 30)))
    heartbeat_cutoff = now - timedelta(minutes=2)
    stale_worker_ids = set(
        session.scalars(
            select(WorkerHeartbeat.worker_id).where(
                WorkerHeartbeat.last_seen_at < heartbeat_cutoff,
            )
        )
    )
    recovery_conditions = [
        and_(Action.lease_expires_at.is_not(None), Action.lease_expires_at <= now),
        and_(Action.lease_expires_at.is_(None), Action.scheduled_at <= cutoff),
    ]
    if stale_worker_ids:
        recovery_conditions.append(Action.lease_owner.in_(stale_worker_ids))
    recovered = 0
    rows = session.execute(
        select(Action, Task)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "executing",
            or_(*recovery_conditions),
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        .order_by(Action.scheduled_at.asc())
    ).all()
    for action, task in rows:
        previous_result = dict(action.result or {})
        previous_lease_owner = action.lease_owner or ""
        previous_lease_expires_at = action.lease_expires_at
        recovery_reason = "stale_worker" if previous_lease_owner in stale_worker_ids else "lease_expired" if previous_lease_expires_at else "execution_timeout"
        latest_attempt = session.scalar(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.action_id == action.id)
            .order_by(ExecutionAttempt.attempt_no.desc())
            .limit(1)
        )
        gateway_started = bool(latest_attempt and latest_attempt.gateway_call_started_at and latest_attempt.status not in {"success", "failed", "call_not_started"})
        if gateway_started and _recover_unknown_membership_action(session, action, task, latest_attempt, now):
            recovered += 1
            continue
        action.status = "unknown_after_send" if gateway_started else "failed"
        action.executed_at = now
        action.lease_owner = ""
        action.lease_expires_at = None
        action.result = {
            "success": False,
            "error_code": "unknown_after_send" if gateway_started else "execution_timeout",
            "error_message": "执行项已进入 TG 调用边界但本地结果未知，需人工或补偿确认" if gateway_started else "执行项长时间处于执行中，已由投递守护标记为超时",
            "validation_stage": "execution_recovery",
            "auto_check": "结果未知" if gateway_started else "超时恢复",
            "recovery_reason": recovery_reason,
            "recovered_at": now.isoformat(),
            "previous_lease_owner": previous_lease_owner,
            "previous_lease_expires_at": previous_lease_expires_at.isoformat() if previous_lease_expires_at else "",
        }
        if previous_result:
            action.result["previous_result"] = previous_result
        if latest_attempt:
            latest_attempt.status = "result_unknown" if gateway_started else "call_not_started"
            latest_attempt.after_call_at = now
            latest_attempt.result_snapshot = dict(action.result or {})
        task.last_error = action.result["error_message"]
        stats = dict(task.stats or {})
        recovered_action_ids = list(stats.get("stale_executing_recovered_action_ids") or [])
        recovered_action_ids.append(action.id)
        stats["last_error"] = task.last_error
        stats["stale_executing_recovered_at"] = now.isoformat()
        stats["stale_executing_last_action_id"] = action.id
        stats["stale_executing_last_lease_owner"] = previous_lease_owner
        stats["stale_executing_last_recovery_reason"] = recovery_reason
        stats["stale_executing_recovered_action_ids"] = recovered_action_ids[-20:]
        stats["recovered_execution_timeout_count"] = int(stats.get("recovered_execution_timeout_count") or 0) + 1
        if gateway_started:
            stats["unknown_after_send_count"] = int(stats.get("unknown_after_send_count") or 0) + 1
        stats["last_recovery_stage"] = "execution_recovery"
        task.stats = stats
        recovered += 1
    recovered += _recover_existing_unknown_membership_actions(session, now)
    return recovered


def _recover_existing_unknown_membership_actions(session: Session, now: datetime) -> int:
    recovered = 0
    reprobed_identities: set[tuple[int, int, str]] = set()
    rows = session.execute(
        select(Action, Task)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "unknown_after_send",
            Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        .order_by(Action.executed_at.asc().nullsfirst(), Action.scheduled_at.asc())
    ).all()
    for action, task in rows:
        result = dict(action.result or {})
        if result.get("unknown_membership_reprobe_status") == "failed":
            continue
        identity = _unknown_membership_reprobe_identity(action)
        if identity in reprobed_identities:
            continue
        reprobed_identities.add(identity)
        latest_attempt = session.scalar(
            select(ExecutionAttempt)
            .where(ExecutionAttempt.action_id == action.id)
            .order_by(ExecutionAttempt.attempt_no.desc())
            .limit(1)
        )
        if _recover_unknown_membership_action(session, action, task, latest_attempt, now):
            recovered += 1
            continue
        action.result = {
            **result,
            "unknown_membership_reprobe_status": "failed",
            "unknown_membership_reprobe_at": now.isoformat(),
        }
    return recovered


def _unknown_membership_reprobe_identity(action: Action) -> tuple[int, int, str]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return (
        int(action.account_id or 0),
        _as_int(payload.get("channel_target_id")),
        str(payload.get("channel_id") or ""),
    )


def _recover_unknown_membership_action(
    session: Session,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
) -> bool:
    if action.action_type not in MEMBERSHIP_ACTION_TYPES or not action.account_id:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id = _as_int(payload.get("channel_target_id"))
    channel_id = str(payload.get("channel_id") or "")
    if not channel_target_id or not channel_id:
        return False
    account = session.get(TgAccount, action.account_id)
    if account is None or account.deleted_at is not None:
        return False
    credentials = credentials_for_account(session, account)
    result = gateway.probe_target_capabilities(
        account.id,
        channel_id,
        str(payload.get("target_type") or "channel"),
        account.session_ciphertext,
        credentials,
    )
    if not result.ok:
        return False
    label = "可发言" if payload.get("require_send") else "已关注"
    mark_channel_membership_joined(session, action.tenant_id, channel_target_id, account.id, permission_label=label)
    _mark_membership_action_recovered(action, task, latest_attempt, now, result.detail or "补偿复检已满足目标准入")
    return True


def _mark_membership_action_recovered(
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    detail: str,
) -> None:
    action.status = "success"
    action.executed_at = now
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = {
        "success": True,
        "error_code": "",
        "error_message": "",
        "auto_check": "补偿复检成功",
        "validation_stage": "execution_recovery_reprobe",
        "membership_status": "recovered_after_unknown",
        "detail": detail,
    }
    if latest_attempt:
        latest_attempt.status = "success"
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result)
    task.last_error = ""


def _activate_pending_tasks(session: Session) -> None:
    for task in session.scalars(select(Task).where(Task.status == "pending", (Task.scheduled_start.is_(None)) | (Task.scheduled_start <= _now()))):
        task.status = "running"
        task.next_run_at = _now()


def _merge_planner_task_ids(primary: list[str], secondary: list[str], limit: int) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    target_count = max(max(1, limit), len(primary))
    for task_id in [*primary, *secondary]:
        if task_id in seen:
            continue
        merged.append(task_id)
        seen.add(task_id)
        if len(merged) >= target_count:
            break
    return merged


def _wake_hard_hourly_tasks(session: Session, *, limit: int) -> list[str]:
    now = _now()
    target_count = max(HARD_HOURLY_WAKE_MIN_SCAN, max(1, limit))
    candidates = sorted(
        (
            candidate
            for task in session.scalars(_hard_hourly_wake_query())
            if (candidate := _hard_hourly_due_candidate(session, task, now)) is not None
        ),
        key=lambda candidate: candidate[0],
    )
    selected = [task for _sort_key, task in candidates[:target_count]]
    for task in selected:
        next_run_at = _naive_datetime(task.next_run_at)
        if next_run_at is None or next_run_at > now:
            task.next_run_at = now
    return [task.id for task in selected]


def _hard_hourly_wake_query():
    return (
        select(Task)
        .where(
            Task.status == "running",
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
        )
        .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
    )


def _hard_hourly_due_for_planner(session: Session, task: Task, now: datetime) -> bool:
    return _hard_hourly_due_candidate(session, task, now) is not None


def _hard_hourly_due_candidate(session: Session, task: Task, now: datetime):
    if not hard_hourly_enabled(task):
        return None
    progress = hard_hourly_current_progress(session, task, now)
    if int(progress.get("deficit") or 0) <= 0:
        return None
    next_check_at = _hard_hourly_next_check_at(task)
    if next_check_at is not None and next_check_at > now:
        return None
    return (_hard_hourly_due_sort_key(task, progress, next_check_at), task)


def _hard_hourly_due_sort_key(task: Task, progress: dict[str, Any], next_check_at: datetime | None):
    next_run_at = _naive_datetime(task.next_run_at) or datetime.min
    created_at = _naive_datetime(task.created_at) or datetime.min
    return (
        next_check_at or datetime.min,
        -int(progress.get("deficit") or 0),
        int(task.priority or 0),
        next_run_at,
        created_at,
    )


def _hard_hourly_next_check_at(task: Task) -> datetime | None:
    stats = task.stats if isinstance(task.stats, dict) else {}
    value = stats.get("hard_hourly_next_check_at")
    if not value:
        return None
    try:
        return as_beijing(datetime.fromisoformat(str(value)))
    except ValueError:
        return None


def _check_stop_conditions(session: Session, task: Task) -> bool:
    now = _now()
    scheduled_end = _naive_datetime(task.scheduled_end)
    if scheduled_end and scheduled_end <= now:
        task.status = "completed"
        task.next_run_at = None
        refresh_task_stats(session, task)
        return True
    return False


def _mark_task_started(task: Task) -> None:
    now = _now()
    scheduled_start = _naive_datetime(task.scheduled_start)
    task.status = "pending" if scheduled_start and scheduled_start > now else "running"
    task.next_run_at = scheduled_start if task.status == "pending" else now
    stats = dict(task.stats or empty_stats())
    stats["started_at"] = stats.get("started_at") or now.isoformat()
    if task.type == "group_ai_chat":
        stats["force_bootstrap_once"] = True
    task.stats = stats
    task.last_error = ""


def _update_type_config(session: Session, tenant_id: int, task_id: str, expected_type: str, payload, actor: str) -> Task:
    update_data = payload.model_dump(mode="json", exclude_unset=True)
    return _update_type_config_data(session, tenant_id, task_id, expected_type, update_data, actor)


def _update_type_config_data(session: Session, tenant_id: int, task_id: str, expected_type: str, update_data: dict[str, Any], actor: str) -> Task:
    task = _apply_type_config_data(session, tenant_id, task_id, expected_type, update_data, actor)
    session.commit()
    session.refresh(task)
    return task


def _apply_type_config_data(session: Session, tenant_id: int, task_id: str, expected_type: str, update_data: dict[str, Any], actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type != expected_type:
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    next_config = {**(task.type_config or {}), **update_data}
    next_config = normalize_operation_target_references(session, tenant_id, expected_type, next_config)
    next_config = apply_default_rule_binding(session, tenant_id, task_type=expected_type, config=next_config)
    next_config = validated_type_config(expected_type, next_config)
    validate_rule_binding(session, tenant_id, next_config)
    task.type_config = next_config
    _clear_unfinished_plan(session, task)
    if task.status not in {"completed", "failed"}:
        now = _now()
        scheduled_start = _naive_datetime(task.scheduled_start)
        task.status = "pending" if scheduled_start and scheduled_start > now else "running"
        task.next_run_at = scheduled_start if task.status == "pending" else now
    task.last_error = ""
    task.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新任务类型配置", target_type="task", target_id=task.id, detail=expected_type)
    return task


def _pacing_payload_for_task(task: Task, pacing_config: Any) -> dict[str, Any]:
    if task.type != "search_join_group":
        raw_data = pacing_config.model_dump(mode="json", exclude_unset=True) if hasattr(pacing_config, "model_dump") else pacing_config
        raw_data = dict(raw_data or {})
        if raw_data.get("hourly_jitter_percent") == raw_data.get("jitter_percent"):
            raw_data.pop("hourly_jitter_percent", None)
        if (SEARCH_JOIN_PACING_FIELDS - {"max_actions_per_day"}).intersection(raw_data or {}):
            raise ValueError("search_join_group 专属 pacing 字段不能用于其他任务类型")
        return pacing_config_payload(PacingConfig.model_validate(raw_data))
    return pacing_config_payload(pacing_config)


def _get_task(session: Session, tenant_id: int, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task or task.tenant_id != tenant_id or task.deleted_at is not None:
        raise ValueError("task not found")
    return task


def _clear_unfinished_plan(session: Session, task: Task) -> None:
    pending_actions = list(
        session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending"))
    )
    pending_action_ids = [action.id for action in pending_actions]
    if pending_action_ids:
        _clear_pending_relay_fingerprints(session, task, pending_actions)
        session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.action_id.in_(pending_action_ids)))
        attempted_action_ids = set(session.scalars(select(ExecutionAttempt.action_id).where(ExecutionAttempt.action_id.in_(pending_action_ids))))
        _skip_attempted_pending_actions(pending_actions, attempted_action_ids)
        deletable_action_ids = [action_id for action_id in pending_action_ids if action_id not in attempted_action_ids]
        if deletable_action_ids:
            session.execute(delete(Action).where(Action.id.in_(deletable_action_ids)))
    _supersede_active_plan_actions(session, task)
    session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.status == "pending"))


def _skip_attempted_pending_actions(pending_actions: list[Action], attempted_action_ids: set[str]) -> None:
    now = _now()
    for action in pending_actions:
        if action.id not in attempted_action_ids:
            continue
        action.status = "skipped"
        action.executed_at = now
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {"success": False, "error_code": "plan_superseded", "error_message": "任务配置已更新，旧执行计划已废弃"}


def _supersede_active_plan_actions(session: Session, task: Task) -> None:
    now = _now()
    active_statuses = sorted(OPEN_PLAN_ACTION_STATUSES - {"pending"})
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status.in_(active_statuses))):
        action.status = "skipped"
        action.executed_at = now
        action.lease_owner = ""
        action.lease_expires_at = None
        action.claim_owner = ""
        action.claim_token = ""
        action.claim_expires_at = None
        action.result = {"success": False, "error_code": "plan_superseded", "error_message": "任务配置已更新，旧执行计划已废弃"}


def _clear_pending_relay_fingerprints(session: Session, task: Task, pending_actions: list[Action]) -> None:
    for action in pending_actions:
        if action.task_type != "group_relay" or action.action_type != "send_message":
            continue
        payload = action.payload if isinstance(action.payload, dict) else {}
        source_group_id = payload.get("source_group_id")
        target_group_id = payload.get("group_id")
        original_text = str(payload.get("original_text") or "").strip()
        if not source_group_id or not target_group_id or not original_text:
            continue
        session.execute(
            delete(MessageFingerprint).where(
                MessageFingerprint.tenant_id == task.tenant_id,
                MessageFingerprint.source_group_id == f"{task.id}:relay:{source_group_id}:target:{target_group_id}",
                MessageFingerprint.fingerprint == content_fingerprint(original_text),
            )
        )


def _invalidate_task_listener_cache(task: Task) -> None:
    config = task.type_config or {}
    if task.type in {"channel_view", "channel_like", "channel_comment"}:
        target_channel_id = int(config.get("target_channel_id") or 0)
        if target_channel_id:
            invalidate_listener_collect("channel", target_channel_id)
        return
    if task.type == "group_ai_chat":
        target_group_id = int(config.get("target_group_id") or 0)
        if target_group_id:
            invalidate_listener_collect("group", target_group_id)
        return
    if task.type == "group_relay":
        for item in config.get("source_groups") or []:
            if isinstance(item, dict) and item.get("group_id"):
                invalidate_listener_collect("group", int(item["group_id"]))


def _action_payloads_with_issue_rollup(session: Session, tenant_id: int, actions: list[Action]) -> list[dict[str, Any]]:
    if not actions:
        return []
    action_ids = [action.id for action in actions]
    task_ids = {action.task_id for action in actions}
    account_ids = sorted({int(action.account_id) for action in actions if action.account_id})
    accounts = {account.id: account for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))} if account_ids else {}
    issues = list(
        session.scalars(
            select(OperationIssue).where(
                OperationIssue.tenant_id == tenant_id,
                OperationIssue.status.in_(["open", "acknowledged"]),
                or_(OperationIssue.representative_action_id.in_(action_ids), OperationIssue.source_task_id.in_(task_ids)),
            )
        )
    )
    direct_issue = {issue.representative_action_id: issue for issue in issues if issue.representative_action_id}
    issue_by_task_failure: dict[tuple[str, str], OperationIssue] = {}
    for issue in issues:
        if issue.source_task_id and issue.failure_type:
            issue_by_task_failure.setdefault((issue.source_task_id, issue.failure_type), issue)
    return [
        _action_payload(action, direct_issue.get(action.id) or issue_by_task_failure.get((action.task_id, _action_failure_type(action))), accounts.get(int(action.account_id or 0)))
        for action in actions
    ]


class _ActionPayload(dict):
    def __getattr__(self, key: str) -> Any:
        try:
            return self[key]
        except KeyError as exc:
            raise AttributeError(key) from exc


def _action_payload(action: Action, issue: OperationIssue | None = None, account: TgAccount | None = None) -> dict[str, Any]:
    result = action.result or {}
    failure_type = _action_failure_type(action)
    failure_reason = _action_failure_reason(action)
    return _ActionPayload({
        "id": action.id,
        "tenant_id": action.tenant_id,
        "task_id": action.task_id,
        "task_type": action.task_type,
        "action_type": action.action_type,
        "account_id": action.account_id,
        "account_display_name": account.display_name if account else "",
        "account_username": account.username if account else "",
        "scheduled_at": action.scheduled_at,
        "executed_at": action.executed_at,
        "status": action.status,
        "payload": _observable_action_payload(action),
        "result": result,
        "retry_count": action.retry_count,
        "failure_type": failure_type,
        "failure_reason": failure_reason,
        "failure_diagnosis": _action_failure_diagnosis(action, failure_type, failure_reason),
        "raw_error": str(result.get("raw_error") or result.get("raw_response") or result.get("exception") or ""),
        "trace_id": str(result.get("trace_id") or result.get("request_id") or ""),
        "operation_issue_id": issue.id if issue else "",
        "operation_issue_status": issue.status if issue else "",
        "operation_issue_rolled_up": bool(issue),
        "created_at": action.created_at,
    })


def _observable_action_payload(action: Action) -> dict[str, Any]:
    payload = dict(action.payload or {})
    if action.task_type == "group_ai_chat" and action.action_type == "send_message" and payload.get("act_type"):
        payload["act_type"] = canonical_ai_group_act_type(str(payload["act_type"]))
    return payload


def _action_failure_type(action: Action) -> str:
    result = action.result or {}
    return str(result.get("error_code") or result.get("failure_type") or "")


def _action_failure_reason(action: Action) -> str:
    result = action.result or {}
    return str(result.get("error_message") or result.get("failure_reason") or result.get("detail") or "")


def _action_failure_diagnosis(action: Action, failure_type: str, failure_reason: str) -> dict[str, str]:
    if action.status not in {"failed", "retryable_failed", "skipped"} and not failure_type and not failure_reason:
        return {}
    if action.action_type == "post_comment":
        diagnosis = _channel_comment_failure_diagnosis(failure_type)
        if diagnosis:
            return diagnosis
    text = _action_failure_text(action, failure_type, failure_reason)
    if _has_failure_marker(text, COMMENT_UNAVAILABLE_MARKERS) or failure_type == FailureType.COMMENT_UNAVAILABLE.value:
        return _comment_unavailable_diagnosis()
    if _has_failure_marker(text, TARGET_PERMISSION_MARKERS) or failure_type in _target_permission_types():
        return _target_permission_diagnosis()
    if _has_failure_marker(text, ACCOUNT_AUTH_MARKERS) or failure_type in _account_auth_types():
        return _account_auth_diagnosis()
    if _has_failure_marker(text, RATE_LIMIT_MARKERS) or failure_type in _rate_limit_types():
        return _rate_limit_diagnosis(failure_type)
    if failure_type == FailureType.CONTENT_REJECTED.value:
        return _content_policy_diagnosis()
    if "context_expired" in text or "上下文过期" in text:
        return _context_expired_diagnosis()
    return _unknown_failure_diagnosis()


def _action_failure_text(action: Action, failure_type: str, failure_reason: str) -> str:
    result = action.result or {}
    parts = [failure_type, failure_reason, result.get("raw_error"), result.get("exception"), result.get("validation_stage")]
    return " ".join(str(part).lower() for part in parts if part)


def _has_failure_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker.lower() in text for marker in markers)


def _target_permission_types() -> set[str]:
    return {FailureType.GROUP_PERMISSION_DENIED.value, FailureType.PEER_INVALID.value, FailureType.CHANNEL_POST_DENIED.value}


def _account_auth_types() -> set[str]:
    return {FailureType.ACCOUNT_UNAVAILABLE.value, FailureType.ACCOUNT_LIMITED.value}


def _rate_limit_types() -> set[str]:
    return {FailureType.FLOOD_WAIT.value, FailureType.SLOWMODE.value}


def _channel_comment_failure_diagnosis(failure_type: str) -> dict[str, str]:
    mapping = {
        "comment_membership_required": _comment_membership_required_diagnosis,
        "comment_account_permission_denied": _comment_account_permission_denied_diagnosis,
        "comment_unavailable_message": _comment_unavailable_message_diagnosis,
        "comment_unavailable_sibling": _comment_unavailable_message_diagnosis,
    }
    builder = mapping.get(failure_type)
    return builder() if builder else {}


def _comment_membership_required_diagnosis() -> dict[str, str]:
    return {
        "category": "comment_membership_required",
        "scope": "account_channel_membership",
        "operator_summary": "等待账号关注 / 加入频道后继续评论",
        "suggested_action": "先处理准入前置，让该账号关注频道并进入关联讨论区；准入完成后再重试当前评论。",
    }


def _comment_account_permission_denied_diagnosis() -> dict[str, str]:
    return {
        "category": "comment_account_permission_denied",
        "scope": "account_channel_comment",
        "operator_summary": "该账号对频道评论区不可发言",
        "suggested_action": "检查该账号在频道讨论区的发言权限，必要时换其他账号继续评论；不要把整条频道消息关闭。",
    }


def _comment_unavailable_message_diagnosis() -> dict[str, str]:
    return {
        "category": "comment_unavailable_message",
        "scope": "channel_message",
        "operator_summary": "该消息无法评论",
        "suggested_action": "确认频道未绑定讨论组、帖子不是频道消息、讨论区入口不可解析或评论已关闭；同帖后续评论应跳过。",
    }


def _target_permission_diagnosis() -> dict[str, str]:
    return {
        "category": "target_permission",
        "scope": "account_target",
        "operator_summary": "账号在线但不能向该目标发送，通常是未加入、被禁言/被踢、目标群私有或准入失效；不是账号掉线。",
        "suggested_action": "到运营目标详情确认目标群准入和账号发言权限，必要时重新拉账号入群、解除禁言，或换可向目标群发言的账号。",
    }


def _account_auth_diagnosis() -> dict[str, str]:
    return {
        "category": "account_auth",
        "scope": "account",
        "operator_summary": "账号会话不可用或账号受限，发送前已被账号状态拦截。",
        "suggested_action": "到 TG 账号管理检查账号状态，按账号详情提示重新登录、刷新 session 或执行健康检查。",
    }


def _rate_limit_diagnosis(failure_type: str) -> dict[str, str]:
    return {
        "category": "rate_limit",
        "scope": "account_target" if failure_type == FailureType.SLOWMODE.value else "account",
        "operator_summary": "Telegram 节流或目标慢速模式触发，当前失败不是配置丢失。",
        "suggested_action": "等待失败详情中的冷却时间后重试，并降低该账号或该目标的发送频率。",
    }


def _content_policy_diagnosis() -> dict[str, str]:
    return {
        "category": "content_policy",
        "scope": "content",
        "operator_summary": "内容在发送前命中规则或风控策略，被系统主动拦截。",
        "suggested_action": "检查规则中心命中的关键词、链接白名单或 AI 候选内容，调整规则或素材后再重试。",
    }


def _comment_unavailable_diagnosis() -> dict[str, str]:
    return {
        "category": "comment_unavailable",
        "scope": "channel_message",
        "operator_summary": "该频道帖子当前无法解析到评论区，通常是帖子未开放讨论、讨论组不可达，或该消息不是可评论频道帖。",
        "suggested_action": "跳过这条频道消息并重新采集频道消息；系统会优先规划已确认可评论的帖子。",
    }


def _context_expired_diagnosis() -> dict[str, str]:
    return {
        "category": "context_expired",
        "scope": "task_context",
        "operator_summary": "这条动作依赖的上下文已经过期，系统跳过旧上下文以避免补发过时内容。",
        "suggested_action": "通常无需重新登录账号；等待新群聊上下文触发，或重置任务重新生成执行计划。",
    }


def _unknown_failure_diagnosis() -> dict[str, str]:
    return {
        "category": "unknown",
        "scope": "action",
        "operator_summary": "当前错误还不能自动归类，需要结合尝试记录和 Trace 查看原始返回。",
        "suggested_action": "打开“尝试”查看原始失败详情；若同一账号持续失败，再检查账号状态和目标群权限。",
    }


def _naive_datetime(value):
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


def _open_actions_state(session: Session, task: Task) -> tuple[bool, bool]:
    earliest = session.scalar(
        select(func.min(Action.scheduled_at)).where(
            Action.task_id == task.id,
            Action.action_type.notin_([TARGET_MEMBERSHIP_ACTION_TYPE, LEGACY_MEMBERSHIP_ACTION_TYPE]),
            Action.status.in_(OPEN_PLAN_ACTION_STATUSES),
        )
    )
    if not earliest:
        return False, False
    task.next_run_at = _absolute_naive_datetime(earliest)
    return True, _scheduled_at_is_future(earliest)


def _scheduled_at_is_future(value: datetime) -> bool:
    if value.tzinfo is not None:
        absolute_now = datetime.now(UTC).replace(tzinfo=None)
        return _absolute_naive_datetime(value) > absolute_now
    return value > _now()


def _absolute_naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


__all__ = [
    "approve_review",
    "check_channel_capacity",
    "create_and_start_channel_comment_task",
    "create_and_start_channel_like_task",
    "create_and_start_channel_view_task",
    "create_and_start_group_ai_chat_task",
    "create_and_start_group_membership_admission_task",
    "create_and_start_group_relay_task",
    "create_and_start_search_join_group_task",
    "create_channel_comment_task",
    "create_channel_like_task",
    "create_channel_view_task",
    "create_group_ai_chat_task",
    "create_group_membership_admission_task",
    "create_group_relay_task",
    "create_search_join_group_task",
    "add_task_source_filter_override",
    "delete_task",
    "drain_task_center",
    "drain_task_dispatcher",
    "drain_task_listener",
    "drain_task_metrics",
    "drain_task_planner",
    "drain_task_recovery",
    "generate_channel_comment_preview",
    "generate_group_ai_chat_preview",
    "get_task_detail",
    "refresh_task_detail_stats",
    "list_ai_cycles_page",
    "list_action_attempts",
    "list_actions_page",
    "list_actions",
    "list_membership_admission_items_page",
    "list_membership_items_page",
    "list_message_groups_page",
    "list_relay_batches_page",
    "list_reviews",
    "list_tasks",
    "mark_membership_admission_manual_handled",
    "membership_admission_failure_rows",
    "pause_task",
    "precheck_task_creation",
    "recommend_accounts",
    "reject_review",
    "ReviewStateError",
    "resume_task",
    "reset_task",
    "retry_failed_membership_admission_items",
    "retry_membership_admission_item",
    "retry_membership_admission_rescue",
    "retry_task",
    "start_task",
    "stop_task",
    "update_channel_comment_config",
    "update_channel_like_config",
    "update_channel_view_config",
    "update_group_ai_chat_config",
    "update_group_relay_config",
    "update_search_join_group_config",
    "update_task_settings",
    "update_task",
]
