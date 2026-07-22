from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
import hashlib
import re
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, object_session

from app.config import get_settings
from app.integrations.telegram import OperationResult
from app.models import AccountPool, AccountStatus, Action, ChannelMessage, ExecutionAttempt, FailureType, MessageFingerprint, OperationIssue, OperationPlanTaskLink, OperationTarget, ReviewQueue, RuleSet, RuleSetVersion, SearchJoinPacingDecision, Task, TaskRuntimeSummary, TgAccount, TgGroup, WorkerHeartbeat
from app.models.search_rank_deboost import AccountGroupProxyBinding, SearchRankDeboostClickReservation, SearchRankDeboostExemptGroup
from app.search_keywords import normalized_keyword_hash, repair_legacy_keyword_materials
from app.schemas.task_center import (
    AccountConfig,
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
    SearchJoinGroupSimpleTaskCreate,
    SearchRankDeboostExemptGroupResponse,
    SearchRankDeboostSimpleTaskCreate,
    SearchRankDeboostTaskConfigUpdate,
    SearchRankDeboostTaskCreate,
    TaskPrecheckRequest,
    TaskRetryRequest,
    TaskSettingsUpdate,
    TaskSourceFilterOverrideRequest,
    TaskUpdate,
)
from app.security import encrypt_secret
from app.services._common import _now, audit, gateway, normalize_list_filter
from app.services.account_usage_policy import apply_rank_deboost_account_filters
from app.services.developer_apps import credentials_for_account
from app.timezone import as_beijing

from .account_pool import select_task_accounts
from .account_scope import initialize_all_account_task_scope, process_account_eligibility_events, reconcile_all_account_scopes_if_due
from .ai_act_types import canonical_ai_group_act_type
from .ai_generator import AiGenerationUnavailable, generate_channel_comments, generate_group_messages
from .channel_membership import (
    ACTION_TYPE as TARGET_MEMBERSHIP_ACTION_TYPE,
    LEGACY_ACTION_TYPE as LEGACY_MEMBERSHIP_ACTION_TYPE,
    channel_membership_summary,
    mark_channel_membership_joined,
)
from .dispatcher import _sync_all_account_membership_state, claim_actions, dispatch_action, due_actions, mark_dispatcher_db_error, recover_expired_claims, recover_expired_hard_hourly_actions
from .daily_coverage import recover_terminal_coverage_reservations
from .executors import build_task_plan, channel_comment, prepare_open_actions_for_planning, requires_planning_with_open_actions
from .search_rank_deboost_pacing import DeboostPacingStats, account_click_allowed, deboost_pacing_window, lock_rank_deboost_quota_scope
from .search_rank_deboost_reservations import (
    mark_reserved_reservation_unknown,
    reopen_released_reservation,
    release_reserved_reservation,
    reservation_for_action,
    reserve_click,
)
from .payloads import SearchRankDeboostPayload
from .search_join_membership import (
    MEMBERSHIP_ACTION_TYPE as SEARCH_JOIN_MEMBERSHIP_ACTION_TYPE,
    rebind_membership_action_to_source_account,
)
from .rank_deboost_runtime_authorization import resolve_rank_deboost_runtime_authorization
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
    build_task_list_payload_context,
    TaskListPayloadContext,
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
from .metrics_runtime import drain_task_metrics
from .planner_backlog import planner_global_pending
from .ai_generation_recovery import recover_stale_pre_gateway_generation
from .recovery_claims import (
    RecoveryClaim,
    claim_recovery_actions,
    recovery_claim_owned,
    release_recovery_claim,
)
from .stats import clear_planner_backlog_stats, empty_stats, next_run_after_task, planner_backlog_snapshot, refresh_task_stats, retry_failed_actions
from .utils import as_int as _as_int, as_int_list as _as_int_list
from .runtime_retention import cleanup_runtime_details_if_due, cleanup_runtime_metric_snapshots_if_due
from app.services.tenant_target_profile import tenant_learning_profile_preview
from app.services.source_media import WAITING_MATERIAL_CACHE, expire_waiting_source_media_actions, wake_waiting_actions_for_source_media
from app.services.account_online_projection import task_account_online_summary
from app.services.runtime_summary import clear_task_runtime_artifacts

_empty_stats = empty_stats
_next_run_after_task = next_run_after_task
_retry_failed_actions = retry_failed_actions
PLANNER_GLOBAL_PENDING_SESSION_KEY = "planner_global_pending"
HARD_HOURLY_WAKE_PROGRESS_SESSION_KEY = "task_center.hard_hourly_wake_progress"
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
DEFAULT_RECOVERY_BATCH_LIMIT = 100
UNKNOWN_MEMBERSHIP_REPROBE_PER_DRAIN_LIMIT = 10
UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN = timedelta(minutes=30)
UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES = {"timeout", "connection_error"}
PUBLIC_TELEGRAM_USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
PUBLIC_TELEGRAM_LINK_RE = re.compile(
    r"^(?:https?://)?(?:t\.me|telegram\.me)/([A-Za-z][A-Za-z0-9_]{4,31})/?(?:\?[^#]*)?$",
    re.IGNORECASE,
)


from .config_fields import (
    CHANNEL_DYNAMIC_TASK_TYPES,
    COMMON_CREATE_FIELDS,
    COMMON_SETTINGS_FIELDS,
    GROUP_AI_LEGACY_RUNTIME_FIELDS,
    GROUP_RELAY_LEGACY_CREATE_FIELDS,
    SEARCH_JOIN_PACING_FIELDS,
    TYPE_SETTINGS_FIELDS,
)
from .search_rank_deboost import (
    preselect_exempt_group,
    require_rank_observation_gateway,
    require_real_exempt_group,
    to_exempt_group_response,
    validate_rank_deboost_preconditions,
    validate_rank_deboost_protocol_samples,
)
from .search_click_target_progress import reconcile_search_click_target_progress, search_click_target_progress
from .search_click_controls import (
    DAILY_TARGET_ACTION_SKIP_PROBABILITY,
    NORMAL_SEARCH_CLICK_TASK,
    RANK_SEARCH_CLICK_TASK,
    require_search_click_account_group,
    search_click_account_config,
    search_click_pacing_config,
)
from .search_rank_deboost_targets import (
    TARGET_REFERENCE_OPERATION_TARGET,
    rank_deboost_target_group_refs,
    require_rank_deboost_target_group_refs,
)
from .hard_hourly import (
    current_progress as hard_hourly_current_progress,
    enabled as hard_hourly_enabled,
    next_check_for_progress as hard_hourly_next_check_for_progress,
    planner_progress_snapshot as hard_hourly_planner_progress_snapshot,
    requires_planning as hard_hourly_requires_planning,
    seed_planner_progress_snapshot as seed_hard_hourly_planner_progress_snapshot,
)
from .config_normalization import (
    apply_default_rule_binding,
    apply_default_slang_config,
    apply_group_ai_account_coverage_defaults,
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


def create_simple_search_join_group_task(
    session: Session,
    tenant_id: int,
    payload: SearchJoinGroupSimpleTaskCreate,
    actor: str,
) -> Task:
    return create_search_join_group_task(
        session,
        tenant_id,
        _simple_search_join_group_payload(session, tenant_id, payload),
        actor,
    )


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


def create_and_start_simple_search_join_group_task(
    session: Session,
    tenant_id: int,
    payload: SearchJoinGroupSimpleTaskCreate,
    actor: str,
) -> Task:
    return create_and_start_search_join_group_task(
        session,
        tenant_id,
        _simple_search_join_group_payload(session, tenant_id, payload),
        actor,
    )


def _new_task(session: Session, tenant_id: int, task_type: str, payload) -> Task:
    raw_type_config = payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS, exclude_unset=True)
    raw_type_config = normalize_operation_target_references(session, tenant_id, task_type, raw_type_config)
    raw_type_config = apply_default_slang_config(session, tenant_id, task_type, raw_type_config)
    raw_type_config = apply_default_rule_binding(session, tenant_id, task_type=task_type, config=raw_type_config)
    raw_type_config = apply_group_ai_account_coverage_defaults(task_type, raw_type_config, payload.account_config.model_dump(mode="json"))
    type_config = validated_type_config(task_type, raw_type_config)
    validate_rule_binding(session, tenant_id, type_config)
    pacing_config = pacing_config_payload(payload.pacing_config)
    if task_type == NORMAL_SEARCH_CLICK_TASK and type_config.get("strict_daily_target"):
        pacing_config["skip_probability_per_action"] = DAILY_TARGET_ACTION_SKIP_PROBABILITY
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
        pacing_config=pacing_config,
        failure_policy=payload.failure_policy.model_dump(mode="json"),
        type_config=type_config,
        stats=empty_stats(),
    )
    session.add(task)
    session.flush()
    initialize_all_account_task_scope(session, task)
    return task


def _simple_search_click_target(session: Session, tenant_id: int, target_id: int) -> OperationTarget:
    target = session.scalar(
        select(OperationTarget).where(
            OperationTarget.id == target_id,
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
        )
    )
    if target is None:
        raise ValueError("搜索点击目标群不存在或不属于当前租户")
    raw_username = str(target.username or "")
    username = raw_username[1:] if raw_username.startswith("@") else raw_username
    if not PUBLIC_TELEGRAM_USERNAME_RE.fullmatch(username):
        raise ValueError("搜索点击目标群必须配置合法公开 username")
    return target


def _simple_search_join_group_payload(
    session: Session,
    tenant_id: int,
    payload: SearchJoinGroupSimpleTaskCreate,
) -> SearchJoinGroupTaskCreate:
    _require_future_search_click_deadline(payload.scheduled_end)
    require_search_click_account_group(
        session, tenant_id, NORMAL_SEARCH_CLICK_TASK, payload.account_group_id
    )
    target, canonical_link = _simple_search_click_target_from_input(
        session, tenant_id, payload.target_title, payload.target_link
    )
    task_payload = SearchJoinGroupTaskCreate(
        name=_simple_search_click_name(
            target,
            "搜索目标群点击",
            payload.daily_click_target_count or payload.daily_target_count,
            payload.target_title,
            daily_target=True,
            daily_membership_target_count=(
                payload.daily_target_count if payload.daily_click_target_count is not None else None
            ),
        ),
        target_operation_target_id=target.id,
        target_input=canonical_link,
        target_title=payload.target_title,
        target_link=canonical_link,
        keywords=payload.keywords,
        daily_click_target_count=payload.daily_click_target_count,
        daily_target_count=payload.daily_target_count,
        allow_same_account_repeat_application=payload.allow_same_account_repeat_application,
        strict_daily_target=True,
        search_bots=[{"username": "jisou", "display_name": "极搜"}],
        account_config=search_click_account_config(payload.account_group_id),
        scheduled_end=as_beijing(payload.scheduled_end),
        pacing_config=search_click_pacing_config(payload),
    )
    _validate_search_join_configured_daily_capacity(
        session,
        tenant_id,
        account_config=task_payload.account_config.model_dump(mode="json"),
        pacing_config=task_payload.pacing_config.model_dump(mode="json"),
        daily_click_target_count=task_payload.daily_click_target_count,
        daily_target_count=task_payload.daily_target_count,
        allow_same_account_repeat_application=task_payload.allow_same_account_repeat_application,
        keyword_count=len(task_payload.keyword_hashes),
    )
    return task_payload


def _simple_search_click_target_from_input(
    session: Session,
    tenant_id: int,
    target_title: str,
    target_link: str,
) -> tuple[OperationTarget, str]:
    target_title = target_title.strip()
    if not target_title:
        raise ValueError("搜索点击目标群名称不能为空")
    username, canonical_link = _simple_search_click_public_link(target_link)
    target = session.scalar(
        select(OperationTarget)
        .where(
            OperationTarget.tenant_id == tenant_id,
            OperationTarget.target_type == "group",
            or_(func.lower(OperationTarget.username) == username, func.lower(OperationTarget.tg_peer_id) == username),
        )
        .order_by(OperationTarget.id.asc())
        .limit(1)
    )
    if target is not None:
        return _simple_search_click_target(session, tenant_id, target.id), canonical_link
    target = OperationTarget(
        tenant_id=tenant_id,
        target_type="group",
        tg_peer_id=username,
        title=target_title,
        username=username,
        can_send=False,
        auth_status="未确认",
    )
    session.add(target)
    session.flush()
    return target, canonical_link


def _simple_search_click_public_link(raw_link: str) -> tuple[str, str]:
    match = PUBLIC_TELEGRAM_LINK_RE.fullmatch(raw_link.strip())
    if match is None:
        raise ValueError("搜索点击目标群必须填写合法公开 Telegram 链接")
    username = match.group(1).lower()
    if not PUBLIC_TELEGRAM_USERNAME_RE.fullmatch(username):
        raise ValueError("搜索点击目标群必须配置合法公开 username")
    return username, f"https://t.me/{username}"


def _simple_search_click_name(
    target: OperationTarget,
    task_label: str,
    target_count: int,
    target_title: str = "",
    *,
    daily_target: bool = False,
    daily_membership_target_count: int | None = None,
) -> str:
    target_label = (target_title or target.title or f"@{target.username}").strip()
    count_label = f"每日 {target_count}" if daily_target else str(target_count)
    if daily_target and daily_membership_target_count is not None:
        count_label = f"每日点击 {target_count} 次（加入目标 {daily_membership_target_count} 次）"
        return f"{target_label} {task_label} {count_label}"[:200]
    return f"{target_label} {task_label} {count_label} 次"[:200]


def _refresh_simple_search_click_name(session: Session, task: Task, *, task_label: str) -> None:
    config = task.type_config or {}
    target_id = config.get("target_operation_target_id")
    if target_id is None:
        target_group_ids = config.get("target_group_ids")
        if isinstance(target_group_ids, list) and len(target_group_ids) == 1:
            target_id = target_group_ids[0]
    if target_id is None:
        return
    daily_click_target = task.type == NORMAL_SEARCH_CLICK_TASK and config.get("daily_click_target_count") is not None
    daily_target = task.type == NORMAL_SEARCH_CLICK_TASK and config.get("daily_target_count") is not None
    target_count = int(
        config.get("daily_click_target_count") if daily_click_target
        else config.get("daily_target_count") if daily_target
        else config.get("target_count") or 0
    )
    if target_count <= 0:
        return
    target = _simple_search_click_target(session, task.tenant_id, int(target_id))
    task.name = _simple_search_click_name(
        target,
        task_label,
        target_count,
        str(config.get("target_title") or ""),
        daily_target=daily_target or daily_click_target,
        daily_membership_target_count=(
            int(config.get("daily_target_count")) if daily_click_target and config.get("daily_target_count") is not None else None
        ),
    )


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


def _task_payload_with_runtime_summary(
    session: Session,
    task: Task,
    summary: TaskRuntimeSummary | None,
    *,
    list_context: TaskListPayloadContext | None = None,
) -> dict[str, Any]:
    payload = _task_payload(
        session,
        task,
        include_detail_search=True,
        include_live_stats=False,
        list_context=list_context,
    )
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
    list_context = build_task_list_payload_context(session, tasks)
    task_rows = [
        _task_payload_with_runtime_summary(session, task, summaries.get(task.id), list_context=list_context)
        for task in tasks
    ]
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
        "rank_deboost_exempt_group": _rank_deboost_exempt_group_payload(session, task),
        "task_runtime_summary": task_summary,
        "operation_plan_links": operation_plan_links,
        "accounts": [],
        "membership_phase": membership_phase,
        "membership_accounts": [],
        "membership_admission_phase": admission_phase,
        "membership_admission_items": [],
        "account_coverage_items": [],
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


def _rank_deboost_exempt_group_payload(session: Session, task: Task) -> dict[str, Any] | None:
    if task.type != "search_rank_deboost":
        return None
    record = session.scalar(
        select(SearchRankDeboostExemptGroup).where(
            SearchRankDeboostExemptGroup.tenant_id == task.tenant_id,
            SearchRankDeboostExemptGroup.task_id == task.id,
        )
    )
    return to_exempt_group_response(record) if record else None


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
    _require_search_click_dedicated_update(task, raw_data)
    data = payload.model_dump(exclude_unset=True, mode="json")
    for field in ["name", "priority", "timezone", "scheduled_start", "scheduled_end", "max_duration_hours"]:
        if field in raw_data:
            setattr(task, field, raw_data[field])
    for field in ["account_config", "pacing_config", "failure_policy"]:
        if field in data and data[field] is not None:
            setattr(task, field, _pacing_payload_for_task(task, raw_data[field]) if field == "pacing_config" else data[field])
    if task.type == "group_ai_chat" and "account_config" in data:
        task.type_config = apply_group_ai_account_coverage_defaults(task.type, task.type_config or {}, task.account_config or {})
        initialize_all_account_task_scope(session, task)
        _clear_unfinished_plan(session, task)
        _requeue_updated_task(task)
    task.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def _requeue_updated_task(task: Task) -> None:
    if task.status in {"completed", "failed"}:
        return
    now = _now()
    scheduled_start = _naive_datetime(task.scheduled_start)
    task.status = "pending" if scheduled_start and scheduled_start > now else "running"
    task.next_run_at = scheduled_start if task.status == "pending" else now
    task.last_error = ""


def update_task_settings(session: Session, tenant_id: int, task_id: str, payload: TaskSettingsUpdate, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    raw_data = payload.model_dump(exclude_unset=True)
    _require_search_click_dedicated_update(task, raw_data)
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
    if task.type == "group_ai_chat" and {"account_config", "pacing_config"} & set(data) and not type_updates:
        next_config = dict(task.type_config or {})
        for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
            next_config.pop(field, None)
        next_config = apply_group_ai_account_coverage_defaults(task.type, next_config, task.account_config or {})
        task.type_config = validated_type_config(task.type, next_config)
    if type_updates:
        next_config = dict(task.type_config or {})
        next_config.update(type_updates)
        if task.type == "group_ai_chat":
            for field in GROUP_AI_LEGACY_RUNTIME_FIELDS:
                if field not in type_updates:
                    next_config.pop(field, None)
        next_config = normalize_operation_target_references(session, tenant_id, task.type, next_config)
        next_config = apply_group_ai_account_coverage_defaults(task.type, next_config, task.account_config or {})
        task.type_config = validated_type_config(task.type, next_config)
    initialize_all_account_task_scope(session, task)
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
    task = _get_task(session, tenant_id, task_id)
    if task.type != "search_join_group":
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    _require_search_click_operator_fields(payload, task.type)
    update_data, pacing_data, operator_controls = _search_join_update_values(
        session, tenant_id, task, payload
    )
    type_config_updated = bool(update_data)
    daily_target_updated = bool(
        {"daily_click_target_count", "daily_target_count"}.intersection(update_data)
    )
    if type_config_updated:
        task = _apply_type_config_data(
            session,
            tenant_id,
            task_id,
            "search_join_group",
            update_data,
            actor,
            remove_fields=("target_count",) if daily_target_updated else (),
        )
        if {
            "target_operation_target_id",
            "target_title",
            "target_count",
            "daily_click_target_count",
            "daily_target_count",
        }.intersection(update_data):
            _refresh_simple_search_click_name(session, task, task_label="搜索目标群点击")
    pacing_updated = False
    if pacing_data is not None:
        next_pacing = pacing_config_payload(pacing_data)
        pacing_updated = next_pacing != (task.pacing_config or {})
        task.pacing_config = next_pacing
    controls_updated = _apply_search_click_operator_controls(session, task, operator_controls)
    planning_type_config_fields = {
        "allow_same_account_repeat_application",
        "actions_per_round",
        "max_actions_per_hour",
        "hourly_min_successful_joins",
    }
    if planning_type_config_fields.intersection(update_data) or (
        not type_config_updated and (pacing_updated or controls_updated)
    ):
        _clear_unfinished_plan(session, task)
        _requeue_search_join_after_operator_change(session, task, operator_controls)
    if daily_target_updated:
        _requeue_search_join_daily_target(task)
        reconcile_search_click_target_progress(session, task)
    if not type_config_updated and not pacing_updated and not controls_updated:
        return task
    task.updated_at = _now()
    session.commit()
    session.refresh(task)
    return task


def _search_join_update_values(
    session: Session,
    tenant_id: int,
    task: Task,
    payload: SearchJoinGroupTaskConfigUpdate,
) -> tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any]]:
    update_data = payload.model_dump(mode="json", exclude_unset=True)
    pacing_data = update_data.pop("pacing_config", None)
    operator_controls = _search_click_operator_controls(payload, task.type)
    for field in operator_controls:
        update_data.pop(field, None)
    task.type_config = _normalize_legacy_search_join_config(task.type_config or {})
    _validate_search_join_daily_target(session, task, update_data, operator_controls)
    _resolve_search_join_target_input(session, tenant_id, update_data)
    _remove_unchanged_search_join_target_input(task.type_config, update_data)
    if "keywords" in update_data:
        update_data.setdefault("keyword_hashes", [])
        update_data.setdefault("keyword_text_ciphertexts", [])
    return update_data, pacing_data, operator_controls


SEARCH_CLICK_OPERATOR_CONTROL_FIELDS = (
    "account_group_id",
    "max_actions_per_day",
    "scheduled_end",
    "daily_jitter_percent",
    "hourly_jitter_percent",
    "quiet_hours",
)
SEARCH_JOIN_OPERATOR_CONTROL_FIELDS = (
    *SEARCH_CLICK_OPERATOR_CONTROL_FIELDS,
    "per_account_daily_action_limit",
    "enable_strict_daily_target",
)
SEARCH_CLICK_TASK_TYPES = {NORMAL_SEARCH_CLICK_TASK, RANK_SEARCH_CLICK_TASK}
SEARCH_JOIN_OPERATOR_EDIT_FIELDS = {
    "target_title",
    "target_link",
    "keywords",
    "target_count",
    "daily_click_target_count",
    "daily_target_count",
    "allow_same_account_repeat_application",
    "actions_per_round",
    "max_actions_per_hour",
    "hourly_min_successful_joins",
    *SEARCH_JOIN_OPERATOR_CONTROL_FIELDS,
}
SEARCH_RANK_OPERATOR_EDIT_FIELDS = {
    "target_title",
    "target_link",
    "keywords",
    "target_count",
    *SEARCH_CLICK_OPERATOR_CONTROL_FIELDS,
}


def _require_search_click_dedicated_update(task: Task, raw_data: dict[str, Any]) -> None:
    if task.type in SEARCH_CLICK_TASK_TYPES and raw_data:
        raise ValueError("搜索点击任务必须通过专用编辑接口更新")


def _require_search_click_operator_fields(payload: Any, task_type: str) -> None:
    allowed = SEARCH_JOIN_OPERATOR_EDIT_FIELDS if task_type == NORMAL_SEARCH_CLICK_TASK else SEARCH_RANK_OPERATOR_EDIT_FIELDS
    forbidden = sorted(payload.model_fields_set - allowed)
    if forbidden:
        raise ValueError(f"搜索点击任务的系统托管字段不能通过运营编辑接口修改: {', '.join(forbidden)}")


def _search_click_operator_controls(payload: Any, task_type: str) -> dict[str, Any]:
    fields = SEARCH_JOIN_OPERATOR_CONTROL_FIELDS if task_type == NORMAL_SEARCH_CLICK_TASK else SEARCH_CLICK_OPERATOR_CONTROL_FIELDS
    return {
        field: getattr(payload, field)
        for field in fields
        if field in payload.model_fields_set
    }


def _search_click_account_group_changed(task: Task, controls: dict[str, Any]) -> bool:
    if "account_group_id" not in controls:
        return False
    current_group_id = int((task.account_config or {}).get("account_group_id") or 0)
    return current_group_id != int(controls["account_group_id"])


def _validate_search_join_daily_target(
    session: Session,
    task: Task,
    update_data: dict[str, Any],
    controls: dict[str, Any],
) -> None:
    daily_click_target = update_data.get(
        "daily_click_target_count",
        (task.type_config or {}).get("daily_click_target_count"),
    )
    daily_membership_target = update_data.get(
        "daily_target_count",
        (task.type_config or {}).get("daily_target_count"),
    )
    source_target = daily_click_target or daily_membership_target
    if source_target is None:
        return
    max_actions = controls.get("max_actions_per_day", (task.pacing_config or {}).get("max_actions_per_day"))
    if max_actions is None or int(max_actions) < int(source_target):
        raise ValueError("max_actions_per_day 不能小于每日点击目标")
    _validate_search_join_configured_daily_capacity(
        session,
        task.tenant_id,
        account_config=_next_search_join_account_config(session, task, controls),
        pacing_config=_next_search_join_pacing_config(task, controls),
        daily_click_target_count=int(daily_click_target) if daily_click_target is not None else None,
        daily_target_count=int(daily_membership_target) if daily_membership_target is not None else None,
        allow_same_account_repeat_application=bool(
            update_data.get(
                "allow_same_account_repeat_application",
                (task.type_config or {}).get("allow_same_account_repeat_application"),
            )
        ),
        keyword_count=_search_join_keyword_count(task, update_data),
    )


def _next_search_join_account_config(session: Session, task: Task, controls: dict[str, Any]) -> dict[str, Any]:
    account_group_id = controls.get("account_group_id")
    if account_group_id is None:
        return dict(task.account_config or {})
    require_search_click_account_group(session, task.tenant_id, task.type, int(account_group_id))
    return {
        **(task.account_config or {}),
        **search_click_account_config(int(account_group_id)),
    }


def _next_search_join_pacing_config(task: Task, controls: dict[str, Any]) -> dict[str, Any]:
    fields = ("max_actions_per_day", "daily_jitter_percent", "hourly_jitter_percent", "per_account_daily_action_limit")
    return {
        **(task.pacing_config or {}),
        **{field: controls[field] for field in fields if field in controls},
    }


def _search_join_keyword_count(task: Task, update_data: dict[str, Any]) -> int:
    if "keywords" not in update_data:
        return len((task.type_config or {}).get("keyword_hashes") or [])
    return len({normalized_keyword_hash(str(item)) for item in update_data["keywords"] if str(item).strip()})


def _validate_search_join_configured_daily_capacity(
    session: Session,
    tenant_id: int,
    *,
    account_config: dict[str, Any],
    pacing_config: dict[str, Any],
    daily_click_target_count: int | None,
    daily_target_count: int | None,
    allow_same_account_repeat_application: bool,
    keyword_count: int,
) -> None:
    source_target = daily_click_target_count or daily_target_count
    if source_target is None:
        return
    target = int(source_target)
    daily_budget = int(pacing_config.get("max_actions_per_day") or 0) or target
    candidates = select_task_accounts(
        session,
        tenant_id,
        account_config,
        enforce_capacity=False,
        scan_all_candidates=True,
    )
    per_account_limit = (
        daily_budget
        if allow_same_account_repeat_application
        else _effective_search_join_account_daily_limit(pacing_config, keyword_count, daily_budget)
    )
    capacity = min(daily_budget, len(candidates) * per_account_limit)
    if target > capacity:
        target_field = "daily_click_target_count" if daily_click_target_count is not None else "daily_target_count"
        raise ValueError(
            "daily_target_capacity_insufficient: "
            f"{target_field}={target}, candidate_accounts={len(candidates)}, "
            f"effective_per_account_daily_limit={per_account_limit}, configured_daily_capacity={capacity}"
        )


def _effective_search_join_account_daily_limit(
    pacing_config: dict[str, Any],
    keyword_count: int,
    daily_budget: int,
) -> int:
    account_limit = int(pacing_config.get("per_account_daily_action_limit") or 0)
    keyword_limit = int(pacing_config.get("per_keyword_account_daily_limit") or 0)
    limits = [limit for limit in (account_limit, keyword_limit * max(1, keyword_count)) if limit > 0]
    return min(limits) if limits else daily_budget


def _requeue_search_join_daily_target(task: Task) -> None:
    if task.status == "completed":
        task.status = "running"
    _requeue_updated_task(task)


def _requeue_search_join_after_operator_change(session: Session, task: Task, controls: dict[str, Any]) -> None:
    if _deadline_extension_reopens_daily_target(session, task, controls):
        _requeue_search_join_daily_target(task)
        return
    _requeue_updated_task(task)


def _deadline_extension_reopens_daily_target(
    session: Session,
    task: Task,
    controls: dict[str, Any],
) -> bool:
    if task.status != "completed" or "scheduled_end" not in controls:
        return False
    progress = search_click_target_progress(session, task)
    return progress.is_daily_target


def _apply_search_click_operator_controls(
    session: Session,
    task: Task,
    controls: dict[str, Any],
) -> bool:
    if not controls:
        return False
    changed = False
    if "account_group_id" in controls:
        account_group_id = int(controls["account_group_id"])
        require_search_click_account_group(session, task.tenant_id, task.type, account_group_id)
        next_account_config = AccountConfig.model_validate({
            **(task.account_config or {}),
            **search_click_account_config(account_group_id),
        }).model_dump(mode="json")
        if next_account_config != (task.account_config or {}):
            task.account_config = next_account_config
            changed = True
    next_pacing = dict(task.pacing_config or {})
    for field in ("max_actions_per_day", "daily_jitter_percent", "hourly_jitter_percent", "per_account_daily_action_limit"):
        if field in controls:
            next_pacing[field] = controls[field]
    if "quiet_hours" in controls:
        quiet_hours = controls["quiet_hours"]
        if quiet_hours is None:
            next_pacing.pop("quiet_hours", None)
        else:
            next_pacing["quiet_hours"] = quiet_hours.model_dump(mode="json")
    if controls.get("enable_strict_daily_target"):
        if (task.type_config or {}).get("daily_target_count") is None:
            raise ValueError("严格每日目标仅适用于 daily_target_count 任务")
        next_type_config = {**(task.type_config or {}), "strict_daily_target": True}
        if next_type_config != (task.type_config or {}):
            task.type_config = next_type_config
            changed = True
        next_pacing["skip_probability_per_action"] = DAILY_TARGET_ACTION_SKIP_PROBABILITY
    if next_pacing != (task.pacing_config or {}):
        task.pacing_config = next_pacing
        changed = True
    if "scheduled_end" in controls:
        _require_future_search_click_deadline(controls["scheduled_end"])
        scheduled_end = as_beijing(controls["scheduled_end"])
        if scheduled_end != task.scheduled_end:
            task.scheduled_end = scheduled_end
            changed = True
    return changed


def _require_future_search_click_deadline(scheduled_end: datetime | None) -> None:
    deadline = as_beijing(scheduled_end)
    if deadline is None or deadline <= _now():
        raise ValueError("完成截止时间必须晚于当前时间")


def _resolve_search_join_target_input(session: Session, tenant_id: int, update_data: dict[str, Any]) -> None:
    if "target_link" not in update_data:
        return
    target, canonical_link = _simple_search_click_target_from_input(
        session, tenant_id, str(update_data["target_title"]), str(update_data["target_link"])
    )
    update_data["target_operation_target_id"] = target.id
    update_data["target_input"] = canonical_link
    update_data["target_link"] = canonical_link


def _remove_unchanged_search_join_target_input(config: dict[str, Any], update_data: dict[str, Any]) -> None:
    if "target_link" not in update_data:
        return
    unchanged = (
        int(config.get("target_operation_target_id") or 0) == int(update_data["target_operation_target_id"])
        and str(config.get("target_title") or "").strip() == str(update_data["target_title"]).strip()
        and str(config.get("target_link") or config.get("target_input") or "").strip() == str(update_data["target_link"])
    )
    if unchanged:
        for field in ("target_operation_target_id", "target_input", "target_title", "target_link"):
            update_data.pop(field, None)


def _normalize_legacy_search_join_config(config: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(config)
    hashes = list(normalized.get("keyword_hashes") or [])
    ciphertexts = list(normalized.get("keyword_text_ciphertexts") or [])
    if hashes or ciphertexts:
        pairs = repair_legacy_keyword_materials(hashes, ciphertexts)
        normalized["keyword_hashes"] = [item[0] for item in pairs]
        normalized["keyword_text_ciphertexts"] = [item[1] for item in pairs]
    if "post_join_safe_navigation_min" in normalized or "post_join_safe_navigation_max" in normalized:
        normalized["post_join_safe_navigation_min"] = 0
        normalized["post_join_safe_navigation_max"] = 0
    return normalized


def create_search_rank_deboost_task(
    session: Session,
    tenant_id: int,
    payload: SearchRankDeboostTaskCreate,
    operator: str,
    *,
    commit: bool = True,
    defer_readiness: bool = False,
) -> Task:
    """创建搜索排名观察任务。

    - 预检：分组级代理绑定、节点健康、节点容量、协议样本、灰度账号数（validate_rank_deboost_preconditions）
    - 创建 task 记录（task_type='search_rank_deboost'）
    - 创建 account_group_proxy_bindings 记录
    - 预选随机豁免群

    简单三字段创建传入 ``defer_readiness=True``，只创建草稿；真实执行准备在
    ``start_task`` 中完成。
    """
    bot_username = _first_rank_deboost_bot(payload.search_bots)
    legacy_binding_requested = payload.account_pool_id is not None and payload.proxy_airport_node_id is not None
    if legacy_binding_requested:
        validate_rank_deboost_preconditions(
            session,
            tenant_id=tenant_id,
            account_pool_id=int(payload.account_pool_id or 0),
            proxy_airport_node_id=int(payload.proxy_airport_node_id or 0),
            target_group_ids=list(payload.target_group_ids),
            bot_username=bot_username,
        )
        account_pool_id = int(payload.account_pool_id or 0)
        proxy_airport_node_id = int(payload.proxy_airport_node_id or 0)
    elif defer_readiness:
        account_pool_id = 0
        proxy_airport_node_id = 0
    else:
        validate_rank_deboost_protocol_samples(session, tenant_id, bot_username)
        bindings = _rank_deboost_ready_bindings(
            session,
            tenant_id,
            payload.account_config.model_dump(mode="json"),
        )
        account_pool_id = int(bindings[0].account_pool_id)
        proxy_airport_node_id = int(bindings[0].proxy_airport_node_id)

    type_config = _build_rank_deboost_type_config(payload, account_pool_id, proxy_airport_node_id)

    task = Task(
        tenant_id=tenant_id,
        name=payload.name,
        type="search_rank_deboost",
        status="draft",
        priority=3,
        timezone=payload.timezone,
        scheduled_end=as_beijing(payload.scheduled_end),
        account_config=payload.account_config.model_dump(mode="json"),
        pacing_config=pacing_config_payload(payload.pacing_config),
        failure_policy={},
        type_config=type_config,
        stats=empty_stats(),
    )
    session.add(task)
    session.flush()

    if legacy_binding_requested:
        from app.services.proxy_group_binding_service import create_group_proxy_binding

        create_group_proxy_binding(
            session,
            tenant_id=tenant_id,
            account_pool_id=account_pool_id,
            proxy_airport_node_id=proxy_airport_node_id,
            operator=operator,
        )

    preselect_exempt_group(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        operator=operator,
        my_target_ids=list(payload.target_group_ids),
        search_results=None,
    )
    _record_rank_deboost_readiness_pending(task, "draft_created")

    audit(
        session,
        tenant_id=tenant_id,
        actor=operator,
        action="创建任务中心任务",
        target_type="task",
        target_id=task.id,
        detail="search_rank_deboost",
    )
    if commit:
        session.commit()
        session.refresh(task)
    else:
        session.flush()
    return task


def create_simple_search_rank_deboost_task(
    session: Session,
    tenant_id: int,
    payload: SearchRankDeboostSimpleTaskCreate,
    operator: str,
) -> Task:
    _require_future_search_click_deadline(payload.scheduled_end)
    require_search_click_account_group(
        session, tenant_id, RANK_SEARCH_CLICK_TASK, payload.account_group_id
    )
    target, canonical_link = _simple_search_click_target_from_input(
        session, tenant_id, payload.target_title, payload.target_link
    )
    return create_search_rank_deboost_task(
        session,
        tenant_id,
        SearchRankDeboostTaskCreate(
            name=_simple_search_click_name(target, "搜索排名观察", payload.target_count, payload.target_title),
            search_bots=["jisou"],
            keywords=[{"text": keyword} for keyword in payload.keywords],
            target_group_ids=[target.id],
            account_config=search_click_account_config(payload.account_group_id),
            scheduled_end=as_beijing(payload.scheduled_end),
            pacing_config=search_click_pacing_config(payload),
            config={
                "target_count": payload.target_count,
                "target_operation_target_id": target.id,
                "target_reference_type": TARGET_REFERENCE_OPERATION_TARGET,
                "target_title": payload.target_title,
                "target_link": canonical_link,
            },
        ),
        operator,
        defer_readiness=True,
    )


def create_and_start_search_rank_deboost_task(
    session: Session,
    tenant_id: int,
    payload: SearchRankDeboostTaskCreate,
    operator: str,
) -> Task:
    """创建并启动搜索排名观察任务；未满足真实执行闸门时回滚全部创建痕迹。"""
    try:
        task = create_search_rank_deboost_task(session, tenant_id, payload, operator, commit=False)
        return start_task(session, tenant_id, task.id, operator, persist_rank_readiness_failure=False)
    except Exception:
        session.rollback()
        raise


def create_and_start_simple_search_rank_deboost_task(
    session: Session,
    tenant_id: int,
    payload: SearchRankDeboostSimpleTaskCreate,
    operator: str,
) -> Task:
    raise ValueError("搜索排名观察任务只能先创建草稿，再由服务端准备并启动")


def update_search_rank_deboost_config(
    session: Session,
    tenant_id: int,
    task_id: str,
    payload: SearchRankDeboostTaskConfigUpdate,
    operator: str,
) -> Task:
    """更新搜索排名观察任务的业务目标与运营执行范围。"""
    task = _get_task(session, tenant_id, task_id)
    if task.type != "search_rank_deboost":
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    _require_search_click_operator_fields(payload, task.type)
    update_data = payload.model_dump(mode="json", exclude_unset=True)
    operator_controls = _search_click_operator_controls(payload, task.type)
    for field in operator_controls:
        update_data.pop(field, None)
    next_config, target_changed, target_display_changed, keywords_changed, target_count_changed = _next_rank_deboost_config(
        session,
        tenant_id,
        config=task.type_config or {},
        update_data=update_data,
    )
    type_config_updated = target_changed or target_display_changed or keywords_changed or target_count_changed
    account_group_changed = _search_click_account_group_changed(task, operator_controls)
    controls_updated = _apply_search_click_operator_controls(session, task, operator_controls)
    if not type_config_updated and not controls_updated:
        return task
    type_plan_rebuilt = False
    if type_config_updated:
        task.type_config = next_config
        type_plan_rebuilt = _apply_rank_deboost_config_change(
            session,
            tenant_id,
            task=task,
            config=next_config,
            target_changed=target_changed,
            target_display_changed=target_display_changed,
            keywords_changed=keywords_changed,
            target_count_changed=target_count_changed,
            operator=operator,
        )
    if controls_updated and not type_plan_rebuilt:
        _reset_rank_deboost_plan_after_operator_change(session, task)
    if controls_updated:
        _mark_rank_deboost_account_group_binding_recheck(task, account_group_changed)
    task.updated_at = _now()
    task.last_error = ""
    audit(
        session,
        tenant_id=tenant_id,
        actor=operator,
        action="更新任务类型配置",
        target_type="task",
        target_id=task.id,
        detail="search_rank_deboost",
    )
    session.commit()
    session.refresh(task)
    return task


def _next_rank_deboost_config(
    session: Session,
    tenant_id: int,
    *,
    config: dict[str, Any],
    update_data: dict[str, Any],
) -> tuple[dict[str, Any], bool, bool, bool, bool]:
    next_config = dict(config)
    target_changed = False
    target_display_changed = False
    keywords_changed = False
    target_count_changed = False
    if "target_link" in update_data:
        target, canonical_link = _simple_search_click_target_from_input(
            session, tenant_id, str(update_data["target_title"]), str(update_data["target_link"])
        )
        target_changed = _rank_deboost_target_changed(next_config, target.id)
        target_display_changed = (
            str(next_config.get("target_title") or "").strip() != str(update_data["target_title"]).strip()
            or str(next_config.get("target_link") or "").strip() != canonical_link
        )
        next_config["target_title"] = str(update_data["target_title"]).strip()
        next_config["target_link"] = canonical_link
        next_config["target_group_ids"] = [target.id]
        next_config["target_operation_target_id"] = target.id
        next_config["target_reference_type"] = TARGET_REFERENCE_OPERATION_TARGET
    elif "target_operation_target_id" in update_data:
        target = _simple_search_click_target(session, tenant_id, update_data["target_operation_target_id"])
        target_changed = _rank_deboost_target_changed(next_config, target.id)
        if target_changed:
            next_config["target_group_ids"] = [target.id]
            next_config["target_operation_target_id"] = target.id
            next_config["target_reference_type"] = TARGET_REFERENCE_OPERATION_TARGET
    if "keywords" in update_data:
        keywords = list(update_data["keywords"])
        keywords_changed = keywords != _rank_deboost_keywords(next_config)
        if keywords_changed:
            next_config["keywords"] = [{"text": keyword} for keyword in keywords]
    if "target_count" in update_data:
        target_count = int(update_data["target_count"])
        target_count_changed = target_count != int(next_config.get("target_count") or 0)
        if target_count_changed:
            next_config["target_count"] = target_count
    return next_config, target_changed, target_display_changed, keywords_changed, target_count_changed


def _apply_rank_deboost_config_change(
    session: Session,
    tenant_id: int,
    *,
    task: Task,
    config: dict[str, Any],
    target_changed: bool,
    target_display_changed: bool,
    keywords_changed: bool,
    target_count_changed: bool,
    operator: str,
) -> bool:
    if target_changed or target_display_changed or target_count_changed:
        _refresh_simple_search_click_name(session, task, task_label="搜索排名观察")
    if not (target_changed or keywords_changed or target_count_changed):
        return False
    _clear_unfinished_plan(session, task)
    if target_changed or keywords_changed:
        preselect_exempt_group(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            operator=operator,
            my_target_ids=_rank_deboost_target_tokens(session, task, config),
            search_results=None,
        )
        _record_rank_deboost_readiness_pending(task, "configuration_changed")
        task.status = "draft"
        task.next_run_at = None
        return True
    _mark_rank_deboost_pending_if_incomplete(session, task)
    return True


def _reset_rank_deboost_plan_after_operator_change(session: Session, task: Task) -> None:
    _clear_unfinished_plan(session, task)
    _mark_rank_deboost_pending_if_incomplete(session, task)


def _mark_rank_deboost_pending_if_incomplete(session: Session, task: Task) -> None:
    if reconcile_search_click_target_progress(session, task).completed:
        return
    task.status = "draft"
    task.next_run_at = None


def _mark_rank_deboost_account_group_binding_recheck(task: Task, account_group_changed: bool) -> None:
    if not account_group_changed or not _rank_deboost_readiness_is_ready(task):
        return
    _record_rank_deboost_readiness_pending(
        task,
        "account_group_changed",
        required_check="account_group_binding",
    )


def _rank_deboost_target_changed(config: dict[str, Any], target_id: int) -> bool:
    configured_target_id = config.get("target_operation_target_id")
    if configured_target_id is None:
        target_group_ids = config.get("target_group_ids") or []
        configured_target_id = target_group_ids[0] if len(target_group_ids) == 1 else 0
    return (
        int(configured_target_id or 0) != target_id
        or _rank_deboost_target_reference_type(config) != TARGET_REFERENCE_OPERATION_TARGET
    )


def _rank_deboost_keywords(config: dict[str, Any]) -> list[str]:
    return [
        str(item.get("text") if isinstance(item, dict) else item).strip()
        for item in config.get("keywords") or []
        if str(item.get("text") if isinstance(item, dict) else item).strip()
    ]


def reroll_search_rank_deboost_exempt_group(
    session: Session,
    tenant_id: int,
    task_id: str,
    operator: str,
) -> SearchRankDeboostExemptGroupResponse:
    """重选随机豁免群。

    - 触发一次真实候选群搜索
    - 从结果中随机选取 1 个非我方目标群作为新豁免群
    - 覆盖当前 search_rank_deboost_exempt_groups 记录（旧值写入 previous_*）
    - 写审计
    - 返回新豁免群响应
    """
    task = _get_task(session, tenant_id, task_id)
    if task.type != "search_rank_deboost":
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")

    type_config = task.type_config or {}
    my_target_ids = _rank_deboost_target_tokens(session, task, dict(type_config))
    search_results = _rank_deboost_exempt_search_results(session, task, dict(type_config))

    record = preselect_exempt_group(
        session,
        tenant_id=tenant_id,
        task_id=task.id,
        operator=operator,
        my_target_ids=my_target_ids,
        search_results=search_results,
    )

    audit(
        session,
        tenant_id=tenant_id,
        actor=operator,
        action="重选搜索排名观察随机豁免群",
        target_type="task",
        target_id=task.id,
        detail=(
            f"new_username={record.exempt_group_username or '-'}; "
            f"previous_username={record.previous_exempt_group_username or '-'}"
        ),
    )
    session.commit()
    session.refresh(record)
    return SearchRankDeboostExemptGroupResponse(**to_exempt_group_response(record))


def _rank_deboost_exempt_search_results(session: Session, task: Task, type_config: dict[str, Any]) -> list[dict]:
    account = _rank_deboost_exempt_account(session, task)
    payload, keyword_text = _rank_deboost_exempt_payload(session, task, account, type_config)
    authorization = resolve_rank_deboost_runtime_authorization(session, account, payload)
    searcher = getattr(gateway, "search_rank_deboost_candidates", None)
    if not callable(searcher):
        raise ValueError("搜索排名观察真实搜索候选源未接入，不能重选随机豁免群")
    try:
        result = searcher(
            account.id,
            payload.model_dump(mode="json"),
            session_ciphertext=authorization.session_ciphertext,
            credentials=authorization.credentials,
            keyword_text=keyword_text,
        )
    except Exception as exc:
        raise ValueError(f"搜索排名观察真实候选搜索失败：{type(exc).__name__}") from exc
    if not isinstance(result, dict) or result.get("execution_status") != "candidates_found" or not result.get("success"):
        raise ValueError("搜索排名观察真实搜索候选源返回格式无效")
    results = result.get("search_results")
    if not isinstance(results, list):
        raise ValueError("搜索排名观察真实搜索候选源返回格式无效")
    if not results:
        raise ValueError("搜索排名观察真实搜索候选源没有返回可用候选群")
    return results


def _rank_deboost_exempt_account(session: Session, task: Task) -> TgAccount:
    pool_ids = _rank_deboost_selected_pool_ids(session, task.tenant_id, dict(task.account_config or {}))
    statement = apply_rank_deboost_account_filters(select(TgAccount).where(
        TgAccount.tenant_id == task.tenant_id,
        TgAccount.pool_id.in_(pool_ids),
        TgAccount.status == AccountStatus.ACTIVE.value,
        TgAccount.deleted_at.is_(None),
    ))
    account = session.scalar(statement.order_by(TgAccount.id.asc()).limit(1))
    if account is None:
        raise ValueError("搜索排名观察真实候选搜索缺少可执行黑账号")
    return account


def _rank_deboost_exempt_payload(
    session: Session,
    task: Task,
    account: TgAccount,
    type_config: dict[str, Any],
) -> tuple[SearchRankDeboostPayload, str]:
    keyword_text = _rank_deboost_first_keyword(type_config)
    binding = _rank_deboost_active_binding(session, task.tenant_id, int(account.pool_id or 0))
    target_refs = require_rank_deboost_target_group_refs(
        session,
        task.tenant_id,
        list(type_config.get("target_group_ids") or []),
        reference_type=_rank_deboost_target_reference_type(type_config),
    )
    return SearchRankDeboostPayload(
        bot_username=_first_rank_deboost_bot(list(type_config.get("search_bots") or [])) or "jisou",
        keyword_hash=hashlib.sha256(keyword_text.lower().encode("utf-8")).hexdigest(),
        keyword_text_ciphertext=encrypt_secret(keyword_text),
        target_group_ids=list(type_config.get("target_group_ids") or []),
        target_group_refs=target_refs,
        account_pool_id=int(account.pool_id or 0),
        proxy_airport_node_id=int(binding.proxy_airport_node_id),
        runtime_environment={
            "group_proxy_binding_id": str(binding.id),
            "runtime_proxy_id": str(binding.runtime_proxy_id or ""),
            "binding_generation": str(binding.binding_generation),
            "account_pool_id": str(account.pool_id or ""),
            "observed_exit_ip": binding.observed_exit_ip or "",
        },
    ), keyword_text


def _rank_deboost_first_keyword(type_config: dict[str, Any]) -> str:
    for item in type_config.get("keywords") or []:
        text = str(item.get("text") if isinstance(item, dict) else item).strip()
        if text:
            return text
    raise ValueError("搜索排名观察真实候选搜索缺少关键词")


def _rank_deboost_active_binding(session: Session, tenant_id: int, account_pool_id: int) -> AccountGroupProxyBinding:
    binding = session.scalar(select(AccountGroupProxyBinding).where(
        AccountGroupProxyBinding.tenant_id == tenant_id,
        AccountGroupProxyBinding.account_pool_id == account_pool_id,
        AccountGroupProxyBinding.status == "active",
        AccountGroupProxyBinding.unbound_at.is_(None),
    ).limit(1))
    if binding is None:
        raise ValueError("搜索排名观察真实候选搜索缺少 active 分组代理绑定")
    return binding


def _rank_deboost_target_tokens(session: Session, task: Task, type_config: dict[str, Any]) -> list[int | str]:
    target_ids = list(type_config.get("target_group_ids") or [])
    refs = rank_deboost_target_group_refs(
        session,
        task.tenant_id,
        target_ids,
        reference_type=_rank_deboost_target_reference_type(type_config),
    )
    tokens: list[int | str] = [*target_ids]
    for ref in refs:
        tokens.extend([str(ref.get("username") or ""), str(ref.get("peer_id") or "")])
    return tokens


def _rank_deboost_target_reference_type(type_config: dict[str, Any]) -> str | None:
    reference_type = str(type_config.get("target_reference_type") or "").strip()
    return reference_type or None


def _first_rank_deboost_bot(search_bots: list[str]) -> str:
    if not search_bots:
        return ""
    return str(search_bots[0]).strip().lstrip("@")


def _rank_deboost_ready_bindings(
    session: Session,
    tenant_id: int,
    account_config: dict[str, Any],
) -> list[AccountGroupProxyBinding]:
    pool_ids = _rank_deboost_selected_pool_ids(session, tenant_id, account_config)
    bindings = list(session.scalars(select(AccountGroupProxyBinding).where(
        AccountGroupProxyBinding.tenant_id == tenant_id,
        AccountGroupProxyBinding.account_pool_id.in_(pool_ids),
        AccountGroupProxyBinding.status == "active",
        AccountGroupProxyBinding.runtime_proxy_id.is_not(None),
        AccountGroupProxyBinding.unbound_at.is_(None),
    ).order_by(AccountGroupProxyBinding.account_pool_id.asc())))
    bound_pool_ids = {int(binding.account_pool_id) for binding in bindings}
    missing_pool_ids = [pool_id for pool_id in pool_ids if pool_id not in bound_pool_ids]
    if missing_pool_ids:
        raise ValueError(f"搜索排名观察专用分组缺少 active runtime 代理绑定：{missing_pool_ids}")
    return bindings


def _rank_deboost_selected_pool_ids(session: Session, tenant_id: int, account_config: dict[str, Any]) -> list[int]:
    mode = str(account_config.get("selection_mode") or "all")
    stmt = select(AccountPool.id).where(
        AccountPool.tenant_id == tenant_id,
        AccountPool.pool_purpose == "rank_deboost",
        AccountPool.is_enabled.is_(True),
    )
    if mode == "group":
        pool_id = int(account_config.get("account_group_id") or 0)
        if pool_id <= 0:
            raise ValueError("搜索排名观察任务缺少黑账号组")
        stmt = stmt.where(AccountPool.id == pool_id)
    elif mode == "manual":
        account_ids = [int(item) for item in account_config.get("account_ids") or [] if int(item) > 0]
        if not account_ids:
            raise ValueError("搜索排名观察任务缺少手动黑账号")
        stmt = stmt.where(AccountPool.id.in_(
            select(TgAccount.pool_id).where(
                TgAccount.tenant_id == tenant_id,
                TgAccount.id.in_(account_ids),
                TgAccount.account_identity == "rank_deboost",
                TgAccount.pool_id.is_not(None),
            )
        ))
    pool_ids = [int(pool_id) for pool_id in session.scalars(stmt.order_by(AccountPool.id.asc()))]
    if not pool_ids:
        raise ValueError("搜索排名观察任务没有可用黑账号组")
    return pool_ids


def _build_rank_deboost_type_config(
    payload: SearchRankDeboostTaskCreate,
    account_pool_id: int,
    proxy_airport_node_id: int,
) -> dict[str, Any]:
    config: dict[str, Any] = {
        "search_bots": list(payload.search_bots),
        "keywords": list(payload.keywords),
        "target_group_ids": list(payload.target_group_ids),
        "account_pool_id": account_pool_id,
        "proxy_airport_node_id": proxy_airport_node_id,
        "notes": payload.notes,
    }
    if isinstance(payload.config, dict):
        config.update(payload.config)
    return config


def start_task(
    session: Session,
    tenant_id: int,
    task_id: str,
    actor: str,
    *,
    persist_rank_readiness_failure: bool = True,
) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type == "channel_comment":
        channel_comment.reconcile_lifetime_cap(session, task)
        if task.status == "completed":
            audit(
                session,
                tenant_id=tenant_id,
                actor=actor,
                action="启动任务中心任务",
                target_type="task",
                target_id=task.id,
                detail="评论任务已达到生命周期总上限",
            )
            session.commit()
            session.refresh(task)
            return task
    if task.type in {"search_join_group", "search_rank_deboost"}:
        if _check_stop_conditions(session, task) or _search_click_task_completed_on_start(session, task):
            session.commit()
            session.refresh(task)
            return task
    if task.type == "search_rank_deboost":
        if task.status in {"running", "pending"}:
            return task
        try:
            _prepare_rank_deboost_start(session, tenant_id, task, actor)
        except ValueError as exc:
            _record_rank_deboost_readiness_blocker(task, exc)
            if persist_rank_readiness_failure:
                session.commit()
                session.refresh(task)
            raise
    else:
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
    retry_slots: int | None = None
    if task.type in {"search_join_group", "search_rank_deboost"}:
        if task.type == "search_rank_deboost":
            lock_rank_deboost_quota_scope(session, task)
        session.execute(select(Task.id).where(Task.id == task.id).with_for_update()).scalar_one()
        session.refresh(task)
        target_progress = reconcile_search_click_target_progress(session, task)
        if target_progress.completed:
            session.commit()
            session.refresh(task)
            return task
        retry_slots = target_progress.remaining_slot_count
    stmt = select(Action).where(Action.task_id == task.id)
    if payload.failed_only or retry_slots is not None:
        stmt = stmt.where(Action.status.in_(["failed", "unknown_after_send", "skipped"]))
    now = _now()
    retried_action_count = 0
    for action in session.scalars(stmt):
        if retry_slots is not None and retry_slots <= 0:
            break
        if payload.failed_only and not _action_should_retry(session, task, action):
            continue
        if not _prepare_action_retry(session, task, action, now):
            continue
        action.status = "pending"
        action.retry_count = 0
        action.scheduled_at = now
        action.executed_at = None
        action.result = {}
        retried_action_count += 1
        if retry_slots is not None:
            retry_slots -= 1
    if retry_slots is not None and not retried_action_count:
        session.commit()
        session.refresh(task)
        return task
    task.status = "running"
    task.next_run_at = now
    task.last_error = ""
    audit(session, tenant_id=tenant_id, actor=actor, action="重试任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def _action_should_retry(session: Session, task: Task, action: Action) -> bool:
    if task.type == "search_rank_deboost" and action.status == "unknown_after_send":
        _set_rank_deboost_retry_blocker(action, "rank_deboost_gateway_outcome_unknown")
        return False
    if action.status in {"failed", "unknown_after_send"}:
        return True
    if task.type == "search_rank_deboost" and action.status == "skipped":
        reservation = reservation_for_action(session, action.id)
        return reservation is None or reservation.status == "released"
    if task.type != "target_admission_retry":
        return False
    result = action.result or {}
    return (
        action.action_type in MEMBERSHIP_ACTION_TYPES
        and result.get("error_code") == "membership_permission_denied"
        and result.get("membership_status") == "permission_denied"
    )


def _prepare_action_retry(session: Session, task: Task, action: Action, now: datetime) -> bool:
    if action.action_type == SEARCH_JOIN_MEMBERSHIP_ACTION_TYPE:
        rebind_membership_action_to_source_account(session, action)
        return True
    if task.type != "search_rank_deboost":
        return True
    if action.status == "unknown_after_send":
        _set_rank_deboost_retry_blocker(action, "rank_deboost_gateway_outcome_unknown")
        return False
    reservation = reservation_for_action(session, action.id)
    if reservation is not None and reservation.status == "reserved":
        return True
    if reservation is not None and reservation.status != "released":
        _set_rank_deboost_retry_blocker(action, f"rank_deboost_reservation_{reservation.status}")
        return False
    return _reopen_rank_deboost_retry_reservation(
        session,
        task=task,
        action=action,
        reservation=reservation,
        now=now,
    )


def _reopen_rank_deboost_retry_reservation(
    session: Session,
    *,
    task: Task,
    action: Action,
    reservation,
    now: datetime,
) -> bool:
    try:
        payload = SearchRankDeboostPayload.model_validate(action.payload or {})
    except ValueError:
        _set_rank_deboost_retry_blocker(action, "rank_deboost_retry_payload_invalid")
        return False
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if account is None:
        _set_rank_deboost_retry_blocker(action, "rank_deboost_retry_account_missing")
        return False
    window = deboost_pacing_window(task, now)
    allowed = account_click_allowed(
        session,
        task,
        account.id,
        payload.keyword_hash,
        payload.account_pool_id,
        window,
        DeboostPacingStats(),
    )
    if not allowed:
        _set_rank_deboost_retry_blocker(action, "rank_deboost_retry_quota_exhausted")
        return False
    if reservation is None:
        reserve_click(
            session,
            task=task,
            action=action,
            account=account,
            account_pool_id=payload.account_pool_id,
            keyword_hash=payload.keyword_hash,
            now_value=now,
        )
    else:
        reopen_released_reservation(session, action.id, now_value=now)
    return True


def _set_rank_deboost_retry_blocker(action: Action, reason: str) -> None:
    action.result = {**(action.result or {}), "retry_skipped_reason": reason}


def reset_task(session: Session, tenant_id: int, task_id: str, actor: str, reason: str = "") -> Task:
    task = _get_task(session, tenant_id, task_id)
    now = _now()
    stats = empty_stats()
    if task.type != "search_rank_deboost":
        stats["started_at"] = now.isoformat()
    if task.type == "group_ai_chat":
        stats["force_bootstrap_once"] = True
    task.stats = stats
    _clear_unfinished_plan(session, task)
    _clear_group_ai_context_fingerprints(session, task)
    _invalidate_task_listener_cache(task)
    if task.type == "search_rank_deboost":
        preselect_exempt_group(
            session,
            tenant_id=tenant_id,
            task_id=task.id,
            operator=actor,
            my_target_ids=_rank_deboost_target_tokens(session, task, dict(task.type_config or {})),
            search_results=None,
        )
        _record_rank_deboost_readiness_pending(task, "reset_requires_start")
        task.status = "draft"
        task.next_run_at = None
    else:
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


def _assert_rank_deboost_allows_start(session: Session, tenant_id: int, task: Task, actor: str) -> None:
    config = dict(task.type_config or {})
    bot_username = _first_rank_deboost_bot(list(config.get("search_bots") or ["jisou"]))
    target_group_ids = list(config.get("target_group_ids") or [])
    require_rank_deboost_target_group_refs(
        session,
        tenant_id,
        target_group_ids,
        reference_type=_rank_deboost_target_reference_type(config),
    )
    validate_rank_deboost_protocol_samples(session, tenant_id, bot_username)
    bindings = _rank_deboost_ready_bindings(session, tenant_id, dict(task.account_config or {}))
    for binding in bindings:
        validate_rank_deboost_preconditions(
            session,
            tenant_id=tenant_id,
            account_pool_id=int(binding.account_pool_id),
            proxy_airport_node_id=int(binding.proxy_airport_node_id),
            target_group_ids=target_group_ids,
            bot_username=bot_username,
        )
    require_rank_observation_gateway()
    _prepare_pending_rank_deboost_exempt_group(session, task, actor)


def _prepare_rank_deboost_start(session: Session, tenant_id: int, task: Task, actor: str) -> None:
    if _rank_deboost_readiness_is_ready(task):
        return
    if _rank_deboost_requires_account_group_binding(task):
        _assert_rank_deboost_account_group_binding(session, tenant_id, task)
        _record_rank_deboost_readiness_ready(task, evidence_summary="rank_account_group_binding")
        return
    _assert_rank_deboost_allows_start(session, tenant_id, task, actor)
    _record_rank_deboost_readiness_ready(task)


def _assert_rank_deboost_account_group_binding(session: Session, tenant_id: int, task: Task) -> None:
    _rank_deboost_ready_bindings(session, tenant_id, dict(task.account_config or {}))


def _record_rank_deboost_readiness_blocker(task: Task, error: ValueError) -> None:
    stats = dict(task.stats or {})
    existing = dict(stats.get("rank_deboost_readiness") or {})
    readiness = {
        "status": "blocked",
        "blocker": str(error),
        "checked_at": _now().isoformat(),
        "evidence_summary": "rank_start_preparation",
    }
    if existing.get("required_check"):
        readiness["required_check"] = existing["required_check"]
    stats["rank_deboost_readiness"] = readiness
    task.stats = stats
    task.last_error = str(error)
    task.next_run_at = None


def _record_rank_deboost_readiness_pending(
    task: Task,
    evidence_summary: str,
    *,
    required_check: str | None = None,
) -> None:
    stats = dict(task.stats or {})
    readiness = {
        "status": "pending",
        "checked_at": _now().isoformat(),
        "evidence_summary": evidence_summary,
    }
    if required_check:
        readiness["required_check"] = required_check
    stats["rank_deboost_readiness"] = readiness
    task.stats = stats


def _search_click_task_completed_on_start(session: Session, task: Task) -> bool:
    session.execute(select(Task.id).where(Task.id == task.id).with_for_update()).scalar_one()
    session.refresh(task)
    return reconcile_search_click_target_progress(session, task).completed


def _rank_deboost_readiness_is_ready(task: Task) -> bool:
    readiness = (task.stats or {}).get("rank_deboost_readiness") or {}
    return readiness.get("status") == "ready"


def _rank_deboost_requires_account_group_binding(task: Task) -> bool:
    readiness = (task.stats or {}).get("rank_deboost_readiness") or {}
    return readiness.get("required_check") == "account_group_binding"


def _record_rank_deboost_readiness_ready(task: Task, *, evidence_summary: str = "rank_start_preparation") -> None:
    stats = dict(task.stats or {})
    stats["rank_deboost_readiness"] = {
        "status": "ready",
        "checked_at": _now().isoformat(),
        "evidence_summary": evidence_summary,
    }
    task.stats = stats


def _prepare_pending_rank_deboost_exempt_group(session: Session, task: Task, actor: str) -> None:
    try:
        require_real_exempt_group(session, tenant_id=task.tenant_id, task_id=task.id)
        return
    except ValueError:
        pass
    type_config = dict(task.type_config or {})
    search_results = _rank_deboost_exempt_search_results(session, task, type_config)
    preselect_exempt_group(
        session,
        tenant_id=task.tenant_id,
        task_id=task.id,
        operator=actor,
        my_target_ids=_rank_deboost_target_tokens(session, task, type_config),
        search_results=search_results,
    )
    require_real_exempt_group(session, tenant_id=task.tenant_id, task_id=task.id)


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
        processed += recover_terminal_coverage_reservations(session, limit=limit)
        session.commit()
        processed += _recover_continuous_task_states(session)
        session.commit()
        processed += _recover_stale_executing_actions(session, limit=limit)
        processed += expire_reviews(session)
        settings = get_settings()
        if settings.enable_runtime_retention_cleanup:
            processed += cleanup_runtime_details_if_due(
                session,
                retention_days=settings.runtime_detail_retention_days,
                batch_size=limit,
                interval_seconds=settings.runtime_detail_cleanup_interval_seconds,
            )
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
    hard_hourly_progress_by_task: dict[str, dict[str, Any]] = {}
    with session_factory() as session:
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
        processed += process_account_eligibility_events(session, limit=limit)
        processed += reconcile_all_account_scopes_if_due(session)
        _activate_pending_tasks(session)
        now = _now()
        hard_hourly_task_ids = _wake_hard_hourly_tasks(session, limit=limit, now=now)
        hard_hourly_progress_by_task = session.info.pop(HARD_HOURLY_WAKE_PROGRESS_SESSION_KEY, {})
        task_ids = _normal_planner_task_ids(session, limit=limit, now=now)
        task_ids = _merge_planner_task_ids(hard_hourly_task_ids, task_ids, limit)
        global_pending = planner_global_pending(session) if task_ids else 0
        session.commit()
    future_open_action_task_ids: set[str] = set()
    for task_id in task_ids:
        task_processed, future_open, global_pending = _plan_due_task(
            session_factory,
            task_id,
            process_type,
            limit=limit,
            global_pending=global_pending,
            round_hard_progress=hard_hourly_progress_by_task.get(task_id),
        )
        processed += task_processed
        if future_open:
            future_open_action_task_ids.add(task_id)
    return processed, future_open_action_task_ids


def _plan_due_task(
    session_factory,
    task_id: str,
    process_type: str | None,
    *,
    limit: int,
    global_pending: int | None = None,
    round_hard_progress: dict[str, Any] | None = None,
) -> tuple[int, bool, int]:
    round_goal = _coverage_round_goal(session_factory, task_id)
    if round_hard_progress is None:
        round_hard_progress = _hard_hourly_round_progress(session_factory, task_id)
    round_goal = _hard_hourly_round_goal(round_goal, round_hard_progress)
    processed = 0
    planned = 0
    future_open = False
    while planned < round_goal:
        plan_limit = round_goal - planned
        batch_processed, batch_planned, future_open, global_pending = _plan_due_task_batch(
            session_factory,
            task_id,
            process_type,
            limit=limit,
            plan_limit=plan_limit,
            global_pending=global_pending,
            round_hard_progress=round_hard_progress,
        )
        processed += batch_processed
        planned += batch_planned
        if batch_planned <= 0 or round_goal == 1:
            break
    return processed, future_open, global_pending


def _hard_hourly_round_progress(session_factory, task_id: str) -> dict[str, Any] | None:
    with session_factory() as session:
        task = session.get(Task, task_id)
        if not task or not hard_hourly_enabled(task):
            return None
        return hard_hourly_planner_progress_snapshot(session, task, _now())


def _hard_hourly_round_goal(round_goal: int, progress: dict[str, Any] | None) -> int:
    deficit = int((progress or {}).get("deficit") or 0)
    return min(round_goal, deficit) if deficit > 0 else round_goal


def _plan_due_task_batch(
    session_factory,
    task_id: str,
    process_type: str | None,
    *,
    limit: int,
    plan_limit: int,
    global_pending: int | None = None,
    round_hard_progress: dict[str, Any] | None = None,
) -> tuple[int, int, bool, int]:
    with session_factory() as session:
        current_global_pending = global_pending if global_pending is not None else planner_global_pending(session)
        session.info["daily_coverage_plan_limit"] = max(1, plan_limit)
        _refresh_planner_heartbeat(session, process_type, limit, task_id=task_id)
        task = session.get(Task, task_id)
        if not task or task.status != "running":
            return 0, 0, False, current_global_pending
        if _check_stop_conditions(session, task):
            session.commit()
            return 0, 0, False, current_global_pending
        retried = retry_failed_actions(session, task, limit=max(1, limit))
        processed = retried
        current_global_pending += max(0, int(retried))
        hard_progress = _hard_hourly_batch_progress(session, task, round_hard_progress)
        has_open_actions, open_actions_are_future = _open_actions_state(session, task)
        if has_open_actions:
            processed += prepare_open_actions_for_planning(session, task)
            has_open_actions, open_actions_are_future = _open_actions_state(session, task)
        open_actions_allow_planning = has_open_actions and requires_planning_with_open_actions(session, task)
        if _skip_open_ai_plan(session, task, has_open_actions, allow_planning=open_actions_allow_planning):
            session.commit()
            return processed, 0, open_actions_are_future, current_global_pending
        session.info[PLANNER_GLOBAL_PENDING_SESSION_KEY] = current_global_pending
        if _planning_backlog_blocked(session, task):
            session.commit()
            return processed, 0, False, current_global_pending
        planned = build_task_plan(session, task)
        processed += planned
        current_global_pending += max(0, int(planned))
        if task.status == "running":
            _ensure_hard_hourly_checkpoint(task, hard_progress, _now())
            task.next_run_at = next_run_after_task(task)
        session.commit()
        return processed, planned, False, current_global_pending


def _hard_hourly_batch_progress(session: Session, task: Task, progress: dict[str, Any] | None) -> dict[str, Any]:
    if not hard_hourly_enabled(task):
        return {}
    if progress is None:
        return hard_hourly_planner_progress_snapshot(session, task, _now())
    return seed_hard_hourly_planner_progress_snapshot(session, task, progress)


def _coverage_round_goal(session_factory, task_id: str) -> int:
    with session_factory() as session:
        task = session.get(Task, task_id)
        config = task.type_config if task and isinstance(task.type_config, dict) else {}
        if not task or task.type != "group_ai_chat":
            return 1
        if config.get("account_coverage_mode") != "all_accounts_daily":
            return 1
        if config.get("messages_per_round_mode") != "manual":
            return 1
        return max(1, int(config.get("messages_per_round") or 1))


def _skip_open_ai_plan(session: Session, task: Task, has_open_actions: bool, *, allow_planning: bool) -> bool:
    return (
        task.type == "group_ai_chat"
        and has_open_actions
        and not hard_hourly_requires_planning(session, task, _now())
        and not allow_planning
    )


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
        effective_concurrency = _dispatcher_concurrency()
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
            session.commit()
        claim_limit = min(max(1, int(limit or 1)), effective_concurrency)
        claimed = claim_actions(session, limit=claim_limit, exclude_task_ids=exclude_task_ids)
        action_ids = [action.id for action in claimed]
        serialize_generation = _has_shared_ai_generation_batch(claimed)
    if not action_ids:
        return 0
    concurrency = 1 if dialect_name == "sqlite" else effective_concurrency
    if serialize_generation:
        concurrency = 1
    if concurrency <= 1 or len(action_ids) == 1:
        return sum(_dispatch_claimed_action(session_factory, action_id) for action_id in action_ids)
    processed = 0
    with ThreadPoolExecutor(max_workers=min(concurrency, len(action_ids)), thread_name_prefix="task-dispatcher") as executor:
        futures = [executor.submit(_dispatch_claimed_action, session_factory, action_id) for action_id in action_ids]
        for future in as_completed(futures):
            processed += int(future.result() or 0)
    return processed


def _has_shared_ai_generation_batch(actions: list[Action]) -> bool:
    generation_keys: set[tuple] = set()
    for action in actions:
        payload = action.payload if isinstance(action.payload, dict) else {}
        if action.action_type != "send_message" or str(payload.get("message_text") or "").strip():
            continue
        if payload.get("reply_to_message_id"):
            continue
        if payload.get("ai_generation_status") not in {"pending", "ai_result_persist_unknown"}:
            continue
        key = (
            action.tenant_id,
            action.task_id,
            payload.get("ai_generation_id"),
            payload.get("ai_generation_claim_owner"),
            payload.get("ai_generation_claim_token"),
        )
        if not all(key):
            continue
        if key in generation_keys:
            return True
        generation_keys.add(key)
    return False


def _dispatcher_concurrency() -> int:
    settings = get_settings()
    configured = max(1, int(settings.dispatcher_concurrency or 1))
    db_budget = max(1, int(settings.db_pool_size or 1) + int(settings.db_max_overflow or 0) - 2)
    return max(1, min(configured, db_budget))


def _dispatch_claimed_action(session_factory, action_id: str) -> int:
    try:
        return _dispatch_claimed_action_once(session_factory, action_id)
    except SQLAlchemyError as exc:
        return _record_dispatch_db_error(session_factory, action_id, exc)


def _dispatch_claimed_action_once(session_factory, action_id: str) -> int:
    with session_factory() as session:
        action = session.get(Action, action_id)
        if not action or action.status != "executing":
            return 0
        if not dispatch_action(session, action):
            session.commit()
            return 0
        session.commit()
        return 1


def _record_dispatch_db_error(session_factory, action_id: str, exc: SQLAlchemyError) -> int:
    with session_factory() as session:
        if not mark_dispatcher_db_error(session, action_id, str(exc)):
            return 0
        session.commit()
    return 0


def _planning_backlog_blocked(session: Session, task: Task) -> bool:
    now_value = _now()
    if hard_hourly_requires_planning(session, task, now_value):
        task.stats = clear_planner_backlog_stats(dict(task.stats or {}))
        return False
    snapshot = planner_backlog_snapshot(
        session,
        task,
        global_pending=session.info.get(PLANNER_GLOBAL_PENDING_SESSION_KEY),
    )
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
        has_actions = session.scalar(select(Action.id).where(Action.task_id == task.id).limit(1))
        if has_actions:
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
        stats = task.stats if isinstance(task.stats, dict) else {}
        if task.type == "channel_comment" and stats.get("completion_reason") == "lifetime_cap_reached":
            continue
        task.status = "running"
        task.next_run_at = now
        task.last_error = ""
        task.updated_at = now
        recovered += 1
    return recovered


def _recover_stale_executing_actions(session: Session, *, timeout_minutes: int = 30, limit: int = DEFAULT_RECOVERY_BATCH_LIMIT) -> int:
    session.commit()
    now = _now()
    claims, stale_worker_ids = _claim_stale_executing_action_ids(
        session,
        now=now,
        timeout_minutes=timeout_minutes,
        limit=limit,
    )
    recovered = sum(
        _recover_claimed_stale_action(session, claim, stale_worker_ids=stale_worker_ids, now=now)
        for claim in claims
    )
    recovered += _recover_existing_unknown_membership_actions(session, now, limit=_membership_reprobe_limit(limit))
    return recovered


def _claim_stale_executing_action_ids(
    session: Session,
    *,
    now: datetime,
    timeout_minutes: int,
    limit: int,
) -> tuple[list[RecoveryClaim], set[str]]:
    heartbeat_cutoff = now - timedelta(minutes=2)
    stale_worker_ids = _stale_worker_lease_owners(session, heartbeat_cutoff)
    claims = claim_recovery_actions(
        session,
        conditions=_stale_executing_conditions(now, timeout_minutes, stale_worker_ids),
        order_by=(Action.scheduled_at.asc(), Action.id.asc()),
        now=now,
        limit=min(20, _recovery_batch_limit(limit)),
    )
    return claims, stale_worker_ids


def _recover_claimed_stale_action(
    session: Session,
    claim: RecoveryClaim,
    *,
    stale_worker_ids: set[str],
    now: datetime,
) -> int:
    action = session.get(Action, claim.action_id)
    task = session.get(Task, action.task_id) if action else None
    if not recovery_claim_owned(action, claim) or task is None:
        session.rollback()
        return 0
    latest_attempt = _latest_execution_attempt(session, action)
    gateway_started = _attempt_gateway_started(latest_attempt)
    recovered = _recover_claimed_gateway_action(
        session,
        claim,
        action=action,
        task=task,
        latest_attempt=latest_attempt,
        gateway_started=gateway_started,
        now=now,
    )
    if not recovery_claim_owned(action, claim):
        session.rollback()
        return 0
    if recovered is None and not gateway_started and recover_stale_pre_gateway_generation(action):
        recovered = 1
    if recovered is None:
        _mark_stale_executing_action(action=action, task=task, latest_attempt=latest_attempt, stale_worker_ids=stale_worker_ids, now=now)
        recovered = 1
    release_recovery_claim(action, claim)
    session.commit()
    return recovered


def _recover_claimed_gateway_action(
    session: Session,
    claim: RecoveryClaim,
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    gateway_started: bool,
    now: datetime,
) -> int | None:
    if not gateway_started:
        return None
    if _recover_unknown_membership_action(
        session, action=action, task=task, latest_attempt=latest_attempt, now=now, recovery_claim=claim,
    ):
        return 1
    if not recovery_claim_owned(action, claim):
        return 0
    if _membership_reprobe_deferred(action) or _membership_reprobe_failed(action):
        _release_unknown_membership_reprobe_result(action=action, task=task, latest_attempt=latest_attempt, now=now)
        return 0
    return None


def _stale_executing_conditions(now: datetime, timeout_minutes: int, stale_worker_ids: set[str]):
    cutoff = now - timedelta(minutes=max(1, int(timeout_minutes or 30)))
    conditions = [
        and_(Action.lease_expires_at.is_not(None), Action.lease_expires_at <= now),
        and_(Action.lease_expires_at.is_(None), Action.scheduled_at <= cutoff),
    ]
    if stale_worker_ids:
        conditions.append(Action.lease_owner.in_(stale_worker_ids))
    return (Action.status == "executing", or_(*conditions))


def _stale_executing_action_ids(
    session: Session,
    *,
    now: datetime,
    timeout_minutes: int,
    limit: int,
) -> list[str]:
    cutoff = now - timedelta(minutes=max(1, int(timeout_minutes or 30)))
    heartbeat_cutoff = now - timedelta(minutes=2)
    stale_worker_ids = _stale_worker_lease_owners(session, heartbeat_cutoff)
    recovery_conditions = [
        and_(Action.lease_expires_at.is_not(None), Action.lease_expires_at <= now),
        and_(Action.lease_expires_at.is_(None), Action.scheduled_at <= cutoff),
    ]
    if stale_worker_ids:
        recovery_conditions.append(Action.lease_owner.in_(stale_worker_ids))
    return list(session.scalars(
        select(Action.id)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "executing",
            or_(*recovery_conditions),
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
        .order_by(Action.scheduled_at.asc(), Action.id.asc())
        .limit(min(20, _recovery_batch_limit(limit)))
    ))


def _stale_worker_lease_owners(session: Session, heartbeat_cutoff: datetime) -> set[str]:
    lease_owners = _executing_lease_owners(session)
    if not lease_owners:
        return set()
    rows = list(session.execute(
        select(WorkerHeartbeat.worker_id, WorkerHeartbeat.hostname, WorkerHeartbeat.pid).where(
            WorkerHeartbeat.last_seen_at < heartbeat_cutoff,
            WorkerHeartbeat.worker_id.in_(lease_owners),
        )
    ))
    legacy_pairs = _legacy_lease_owner_pairs(lease_owners)
    if legacy_pairs:
        rows.extend(session.execute(
            select(WorkerHeartbeat.worker_id, WorkerHeartbeat.hostname, WorkerHeartbeat.pid).where(
                WorkerHeartbeat.last_seen_at < heartbeat_cutoff,
                tuple_(WorkerHeartbeat.hostname, WorkerHeartbeat.pid).in_(legacy_pairs),
            )
        ))
    return _matching_stale_lease_owners(rows, lease_owners)


def _executing_lease_owners(session: Session) -> set[str]:
    statement = select(Action.lease_owner).where(
        Action.status == "executing",
        Action.lease_owner.is_not(None),
        Action.lease_owner != "",
    ).distinct()
    return {str(owner) for owner in session.scalars(statement) if owner}


def _legacy_lease_owner_pairs(lease_owners: set[str]) -> list[tuple[str, int]]:
    pairs: set[tuple[str, int]] = set()
    for owner in lease_owners:
        hostname, separator, pid_value = owner.rpartition(":")
        if not separator or not hostname:
            continue
        try:
            pairs.add((hostname, int(pid_value)))
        except ValueError:
            continue
    return list(pairs)


def _matching_stale_lease_owners(rows, lease_owners: set[str]) -> set[str]:
    owners: set[str] = set()
    for worker_id, hostname, pid in rows:
        if worker_id and str(worker_id) in lease_owners:
            owners.add(str(worker_id))
        if hostname and pid is not None:
            legacy_owner = f"{hostname}:{pid}"
            if legacy_owner in lease_owners:
                owners.add(legacy_owner)
    return owners


def _mark_stale_executing_action(
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    stale_worker_ids: set[str],
    now: datetime,
) -> None:
    previous_result = dict(action.result or {})
    previous_lease_owner = action.lease_owner or ""
    previous_lease_expires_at = action.lease_expires_at
    gateway_started = _attempt_gateway_started(latest_attempt)
    recovery_reason = "stale_worker" if previous_lease_owner in stale_worker_ids else "lease_expired" if previous_lease_expires_at else "execution_timeout"
    _reconcile_rank_deboost_reservation_after_stale_execution(action, gateway_started)
    action.status = "unknown_after_send" if gateway_started else "failed"
    action.executed_at = now
    action.lease_owner = ""
    action.lease_expires_at = None
    action.result = _stale_executing_result(
        gateway_started=gateway_started,
        recovery_reason=recovery_reason,
        previous_lease_owner=previous_lease_owner,
        previous_lease_expires_at=previous_lease_expires_at,
        now=now,
    )
    if previous_result:
        action.result["previous_result"] = previous_result
    if latest_attempt:
        latest_attempt.status = "result_unknown" if gateway_started else "call_not_started"
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result or {})
    _record_stale_recovery_stats(
        action=action,
        task=task,
        previous_lease_owner=previous_lease_owner,
        recovery_reason=recovery_reason,
        gateway_started=gateway_started,
        now=now,
    )


def _reconcile_rank_deboost_reservation_after_stale_execution(action: Action, gateway_started: bool) -> None:
    if action.action_type != "search_rank_deboost":
        return
    session = object_session(action)
    if session is None:
        return
    if gateway_started:
        mark_reserved_reservation_unknown(session, action.id)
        return
    release_reserved_reservation(session, action.id)


def _stale_executing_result(
    *,
    gateway_started: bool,
    recovery_reason: str,
    previous_lease_owner: str,
    previous_lease_expires_at: datetime | None,
    now: datetime,
) -> dict[str, Any]:
    return {
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


def _record_stale_recovery_stats(
    *,
    action: Action,
    task: Task,
    previous_lease_owner: str,
    recovery_reason: str,
    gateway_started: bool,
    now: datetime,
) -> None:
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


def _latest_execution_attempt(session: Session, action: Action) -> ExecutionAttempt | None:
    return session.scalar(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id).order_by(ExecutionAttempt.attempt_no.desc()).limit(1))


def _attempt_gateway_started(latest_attempt: ExecutionAttempt | None) -> bool:
    return bool(latest_attempt and latest_attempt.gateway_call_started_at and latest_attempt.status not in {"success", "failed", "call_not_started"})


def _recovery_batch_limit(limit: int) -> int:
    return max(1, int(limit or DEFAULT_RECOVERY_BATCH_LIMIT))


def _membership_reprobe_limit(limit: int) -> int:
    configured = _recovery_batch_limit(limit)
    return max(1, min(configured, UNKNOWN_MEMBERSHIP_REPROBE_PER_DRAIN_LIMIT))


def _recover_existing_unknown_membership_actions(session: Session, now: datetime, *, limit: int) -> int:
    claims = claim_recovery_actions(
        session,
        conditions=(
            Action.status == "unknown_after_send",
            Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
            _unknown_membership_reprobe_due_clause(now),
        ),
        order_by=(Action.executed_at.asc().nullsfirst(), Action.scheduled_at.asc(), Action.id.asc()),
        now=now,
        limit=_recovery_batch_limit(limit),
    )
    reprobed_identities: set[tuple[int, int, str]] = set()
    return sum(
        _recover_claimed_unknown_action(session, claim, now=now, reprobed_identities=reprobed_identities)
        for claim in claims
    )


def _recover_claimed_unknown_action(
    session: Session,
    claim: RecoveryClaim,
    *,
    now: datetime,
    reprobed_identities: set[tuple[int, int, str]],
) -> int:
    action = session.get(Action, claim.action_id)
    task = session.get(Task, action.task_id) if action else None
    if not recovery_claim_owned(action, claim) or task is None:
        session.rollback()
        return 0
    identity = _unknown_membership_reprobe_identity(action)
    if _skip_unknown_membership_reprobe(action, now) or identity in reprobed_identities:
        release_recovery_claim(action, claim)
        session.commit()
        return 0
    reprobed_identities.add(identity)
    latest_attempt = _latest_execution_attempt(session, action)
    recovered = _recover_unknown_membership_action(
        session, action=action, task=task, latest_attempt=latest_attempt, now=now, recovery_claim=claim,
    )
    if not recovery_claim_owned(action, claim):
        session.rollback()
        return 0
    if not recovered:
        _finalize_failed_unknown_reprobe(session, action, now)
    release_recovery_claim(action, claim)
    session.commit()
    return int(recovered)


def _finalize_failed_unknown_reprobe(session: Session, action: Action, now: datetime) -> None:
    if _membership_reprobe_deferred(action) or _membership_reprobe_failed(action):
        _propagate_unknown_membership_reprobe_result(session, source_action=action, now=now)
        return
    action.result = {
        **dict(action.result or {}),
        "unknown_membership_reprobe_status": "failed",
        "unknown_membership_reprobe_at": now.isoformat(),
    }


def _propagate_unknown_membership_reprobe_result(session: Session, *, source_action: Action, now: datetime) -> int:
    update_fields = _unknown_membership_reprobe_result_fields(source_action)
    if not update_fields:
        return 0
    source_identity = _unknown_membership_reprobe_identity(source_action)
    if not source_identity[0] or not source_identity[1] or not source_identity[2]:
        return 0
    rows = session.scalars(
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(
            Action.status == "unknown_after_send",
            Action.action_type.in_(MEMBERSHIP_ACTION_TYPES),
            Action.account_id == source_identity[0],
            Task.status == "running",
            Task.deleted_at.is_(None),
        )
    ).all()
    updated = 0
    for action in rows:
        if action.id == source_action.id:
            continue
        if _unknown_membership_reprobe_identity(action) != source_identity:
            continue
        if _skip_unknown_membership_reprobe(action, now):
            continue
        action.result = {**dict(action.result or {}), **update_fields, "reprobe_deduped_from_action_id": source_action.id}
        updated += 1
    return updated


def _unknown_membership_reprobe_result_fields(action: Action) -> dict[str, Any]:
    result = dict(action.result or {})
    status = result.get("unknown_membership_reprobe_status")
    if status not in {"failed", *UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES}:
        return {}
    field_names = (
        "success",
        "error_code",
        "error_message",
        "unknown_membership_reprobe_status",
        "unknown_membership_reprobe_at",
        "unknown_membership_reprobe_next_at",
        "unknown_membership_reprobe_error",
    )
    return {name: result[name] for name in field_names if name in result}


def _unknown_membership_reprobe_due_clause(now: datetime):
    status = Action.result["unknown_membership_reprobe_status"].as_string()
    next_at = Action.result["unknown_membership_reprobe_next_at"].as_string()
    return or_(
        status.is_(None),
        status.notin_(("failed", *UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES)),
        and_(
            status.in_(UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES),
            or_(next_at.is_(None), next_at <= now.isoformat()),
        ),
    )


def _unknown_membership_reprobe_identity(action: Action) -> tuple[int, int, str]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return (
        int(action.account_id or 0),
        _as_int(payload.get("channel_target_id")),
        str(payload.get("channel_id") or ""),
    )


def _skip_unknown_membership_reprobe(action: Action, now: datetime) -> bool:
    result = dict(action.result or {})
    status = result.get("unknown_membership_reprobe_status")
    if status == "failed":
        return True
    if status not in UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES:
        return False
    return _iso_datetime_after(result.get("unknown_membership_reprobe_next_at"), now)


def _membership_reprobe_deferred(action: Action) -> bool:
    result = dict(action.result or {})
    return result.get("unknown_membership_reprobe_status") in UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN_STATUSES


def _membership_reprobe_failed(action: Action) -> bool:
    result = dict(action.result or {})
    return result.get("unknown_membership_reprobe_status") == "failed"


def _iso_datetime_after(value: Any, now: datetime) -> bool:
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return False
    return as_beijing(parsed) > as_beijing(now)


def _recover_unknown_membership_action(
    session: Session,
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    recovery_claim: RecoveryClaim | None = None,
) -> bool:
    if action.action_type not in MEMBERSHIP_ACTION_TYPES or not action.account_id:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id = _as_int(payload.get("channel_target_id"))
    channel_id = str(payload.get("channel_id") or "")
    if not channel_target_id or not channel_id:
        return False
    probe_args = _unknown_membership_probe_args(session, action, payload)
    if probe_args is None:
        return False
    session.commit()
    try:
        result = gateway.probe_target_capabilities(*probe_args)
    except TimeoutError as exc:
        if recovery_claim and not recovery_claim_owned(action, recovery_claim):
            return False
        _mark_unknown_membership_reprobe_timeout(action=action, task=task, latest_attempt=latest_attempt, now=now, exc=exc)
        return False
    except ConnectionError as exc:
        if recovery_claim and not recovery_claim_owned(action, recovery_claim):
            return False
        _mark_unknown_membership_reprobe_connection_error(action=action, task=task, latest_attempt=latest_attempt, now=now, exc=exc)
        return False
    if recovery_claim and not recovery_claim_owned(action, recovery_claim):
        return False
    if not result.ok:
        _mark_unknown_membership_reprobe_failed(action=action, task=task, latest_attempt=latest_attempt, now=now, result=result)
        return False
    _complete_unknown_membership_recovery(
        session, action, task=task, latest_attempt=latest_attempt,
        now=now, result=result, channel_target_id=channel_target_id,
    )
    return True


def _unknown_membership_probe_args(session: Session, action: Action, payload: dict) -> tuple | None:
    account = session.get(TgAccount, action.account_id)
    if account is None or account.deleted_at is not None:
        return None
    return (
        account.id,
        str(payload.get("channel_id") or ""),
        str(payload.get("target_type") or "channel"),
        account.session_ciphertext,
        credentials_for_account(session, account),
    )


def _complete_unknown_membership_recovery(
    session: Session,
    action: Action,
    *,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    result,
    channel_target_id: int,
) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    label = "可发言" if payload.get("require_send") else "已关注"
    mark_channel_membership_joined(session, action.tenant_id, channel_target_id, action.account_id, permission_label=label)
    _mark_membership_action_recovered(action, task, latest_attempt, now, result.detail or "补偿复检已满足目标准入")
    _sync_all_account_membership_state(session, action)


def _mark_unknown_membership_reprobe_timeout(
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    exc: TimeoutError,
) -> None:
    error_message = "Telegram 补偿复检超时，已进入冷却等待下一轮显式复检"
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "telegram_probe_timeout",
        "error_message": error_message,
        "unknown_membership_reprobe_status": "timeout",
        "unknown_membership_reprobe_at": now.isoformat(),
        "unknown_membership_reprobe_next_at": (now + UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN).isoformat(),
        "unknown_membership_reprobe_error": str(exc),
    }
    task.last_error = error_message
    if latest_attempt:
        latest_attempt.status = "result_unknown"
        latest_attempt.failure_type = "telegram_probe_timeout"
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result)


def _mark_unknown_membership_reprobe_failed(
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    result: OperationResult,
) -> None:
    error_code = result.failure_type or FailureType.UNKNOWN.value
    error_message = result.detail or result.status or "Telegram 补偿复检未满足目标准入"
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": error_code,
        "error_message": error_message,
        "unknown_membership_reprobe_status": "failed",
        "unknown_membership_reprobe_at": now.isoformat(),
        "unknown_membership_reprobe_error": error_message,
    }
    task.last_error = error_message
    if latest_attempt:
        latest_attempt.status = "result_unknown"
        latest_attempt.failure_type = error_code
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result)


def _mark_unknown_membership_reprobe_connection_error(
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
    exc: ConnectionError,
) -> None:
    error_message = "Telegram 补偿复检连接失败，已进入冷却等待下一轮显式复检"
    action.result = {
        **dict(action.result or {}),
        "success": False,
        "error_code": "telegram_probe_connection_error",
        "error_message": error_message,
        "unknown_membership_reprobe_status": "connection_error",
        "unknown_membership_reprobe_at": now.isoformat(),
        "unknown_membership_reprobe_next_at": (now + UNKNOWN_MEMBERSHIP_REPROBE_COOLDOWN).isoformat(),
        "unknown_membership_reprobe_error": str(exc),
    }
    task.last_error = error_message
    if latest_attempt:
        latest_attempt.status = "result_unknown"
        latest_attempt.failure_type = "telegram_probe_connection_error"
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result)


def _release_unknown_membership_reprobe_result(
    *,
    action: Action,
    task: Task,
    latest_attempt: ExecutionAttempt | None,
    now: datetime,
) -> None:
    action.status = "unknown_after_send"
    action.executed_at = now
    action.lease_owner = ""
    action.lease_expires_at = None
    if latest_attempt:
        latest_attempt.status = "result_unknown"
        latest_attempt.after_call_at = now
        latest_attempt.result_snapshot = dict(action.result or {})
    task.last_error = str((action.result or {}).get("error_message") or task.last_error or "")


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


def _normal_planner_task_ids(session: Session, *, limit: int, now: datetime) -> list[str]:
    target_count = max(1, limit)
    task_ids: list[str] = []
    query = (
        select(Task)
        .where(Task.status == "running", (Task.next_run_at.is_(None)) | (Task.next_run_at <= now))
        .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
    )
    for task in session.scalars(query).yield_per(target_count):
        if _hard_hourly_recheck_is_pending(task, now):
            continue
        task_ids.append(task.id)
        if len(task_ids) >= target_count:
            break
    return task_ids


def _wake_hard_hourly_tasks(session: Session, *, limit: int, now: datetime | None = None) -> list[str]:
    now = now or _now()
    target_count = max(HARD_HOURLY_WAKE_MIN_SCAN, max(1, limit))
    candidates = sorted(
        (
            candidate
            for task in session.scalars(_hard_hourly_wake_query(now))
            if (candidate := _hard_hourly_due_candidate(session, task, now)) is not None
        ),
        key=lambda candidate: candidate[0],
    )
    selected_candidates = candidates[:target_count]
    session.info[HARD_HOURLY_WAKE_PROGRESS_SESSION_KEY] = {
        task.id: dict(progress) for _sort_key, task, progress in selected_candidates
    }
    selected = [task for _sort_key, task, _progress in selected_candidates]
    for task in selected:
        next_run_at = _naive_datetime(task.next_run_at)
        if next_run_at is None or next_run_at > now:
            task.next_run_at = now
    return [task.id for task in selected]


def _hard_hourly_wake_query(now: datetime):
    return (
        select(Task)
        .where(
            Task.status == "running",
            Task.type == "group_ai_chat",
            Task.deleted_at.is_(None),
            or_(
                Task.type_config["hard_hourly_target_enabled"].as_boolean().is_(True),
                Task.type_config["hard_hourly_target_enabled"].as_string() == "true",
            ),
            or_(Task.hard_hourly_next_check_at.is_(None), Task.hard_hourly_next_check_at <= now),
        )
        .order_by(Task.hard_hourly_next_check_at.asc().nullsfirst(), Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
    )


def _hard_hourly_due_for_planner(session: Session, task: Task, now: datetime) -> bool:
    return _hard_hourly_due_candidate(session, task, now) is not None


def _hard_hourly_due_candidate(session: Session, task: Task, now: datetime):
    if not hard_hourly_enabled(task):
        return None
    next_check_at = _hard_hourly_next_check_at(task)
    if next_check_at is not None and next_check_at > now:
        return None
    progress = hard_hourly_current_progress(session, task, now)
    _record_hard_hourly_checkpoint(task, progress, now)
    if int(progress.get("deficit") or 0) <= 0:
        return None
    return (_hard_hourly_due_sort_key(task, progress, next_check_at), task, progress)


def _record_hard_hourly_checkpoint(task: Task, progress: dict[str, Any], now: datetime) -> None:
    stats = dict(task.stats or {})
    next_check_at = hard_hourly_next_check_for_progress(task, progress, now)
    stats["hard_hourly_next_check_at"] = next_check_at.isoformat()
    task.hard_hourly_next_check_at = next_check_at
    task.stats = stats


def _ensure_hard_hourly_checkpoint(task: Task, progress: dict[str, Any], now: datetime) -> None:
    if not hard_hourly_enabled(task) or not progress or _hard_hourly_next_check_at(task) is not None:
        return
    _record_hard_hourly_checkpoint(task, progress, now)


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
    checkpoint = as_beijing(task.hard_hourly_next_check_at)
    if checkpoint is not None:
        return checkpoint
    checkpoint = _task_stats_datetime(task, "hard_hourly_next_check_at")
    if checkpoint is not None:
        task.hard_hourly_next_check_at = checkpoint
    return checkpoint


def _hard_hourly_recheck_is_pending(task: Task, now: datetime) -> bool:
    hard_next_check = _hard_hourly_next_check_at(task)
    if not hard_hourly_enabled(task) or hard_next_check is None or hard_next_check <= now:
        return False
    coverage_next_check = _task_stats_datetime(task, "daily_coverage_next_check_at")
    return coverage_next_check is None or coverage_next_check > now


def _task_stats_datetime(task: Task, key: str) -> datetime | None:
    stats = task.stats if isinstance(task.stats, dict) else {}
    value = stats.get(key)
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
        if task.type in SEARCH_CLICK_TASK_TYPES:
            _clear_unfinished_plan(session, task)
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


def _apply_type_config_data(
    session: Session,
    tenant_id: int,
    task_id: str,
    expected_type: str,
    update_data: dict[str, Any],
    actor: str,
    *,
    remove_fields: tuple[str, ...] = (),
) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type != expected_type:
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    next_config = {**(task.type_config or {}), **update_data}
    for field in remove_fields:
        next_config.pop(field, None)
    next_config = normalize_operation_target_references(session, tenant_id, expected_type, next_config)
    next_config = apply_default_rule_binding(session, tenant_id, task_type=expected_type, config=next_config)
    next_config = apply_group_ai_account_coverage_defaults(expected_type, next_config, task.account_config or {})
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
    _clear_hard_hourly_checkpoint(task)
    pending_actions = list(
        session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending"))
    )
    _clear_superseded_search_join_pacing_decisions(session, pending_actions)
    pending_action_ids = [action.id for action in pending_actions]
    if pending_action_ids:
        _clear_pending_relay_fingerprints(session, task, pending_actions)
        session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.action_id.in_(pending_action_ids)))
        attempted_action_ids = set(session.scalars(select(ExecutionAttempt.action_id).where(ExecutionAttempt.action_id.in_(pending_action_ids))))
        _skip_attempted_pending_actions(session, pending_actions, attempted_action_ids)
        deletable_action_ids = [action_id for action_id in pending_action_ids if action_id not in attempted_action_ids]
        if deletable_action_ids:
            session.execute(delete(SearchRankDeboostClickReservation).where(SearchRankDeboostClickReservation.action_id.in_(deletable_action_ids)))
            session.execute(delete(Action).where(Action.id.in_(deletable_action_ids)))
    _supersede_active_plan_actions(session, task)
    _clear_orphaned_search_join_pacing_decisions(session, task)
    session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.status == "pending"))


def _clear_hard_hourly_checkpoint(task: Task) -> None:
    if task.type != "group_ai_chat":
        return
    stats = dict(task.stats or {})
    stats.pop("hard_hourly_next_check_at", None)
    task.hard_hourly_next_check_at = None
    task.stats = stats


def _skip_attempted_pending_actions(session: Session, pending_actions: list[Action], attempted_action_ids: set[str]) -> None:
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
        release_reserved_reservation(session, action.id)


def _clear_superseded_search_join_pacing_decisions(session: Session, actions: list[Action]) -> None:
    for action in actions:
        _clear_search_join_pacing_decision(session, action)


def _clear_search_join_pacing_decision(session: Session, action: Action) -> None:
    if action.action_type != "search_join":
        return
    keyword_hash = str((action.payload or {}).get("keyword_hash") or "")
    if not keyword_hash:
        return
    session.execute(
        delete(SearchJoinPacingDecision).where(
            SearchJoinPacingDecision.task_id == action.task_id,
            SearchJoinPacingDecision.decision_scope == "action",
            SearchJoinPacingDecision.account_id == action.account_id,
            SearchJoinPacingDecision.keyword_hash == keyword_hash,
            SearchJoinPacingDecision.scheduled_at == action.scheduled_at,
        )
    )


def _clear_orphaned_search_join_pacing_decisions(session: Session, task: Task) -> None:
    if task.type != "search_join_group":
        return
    retained = {
        _search_join_pacing_decision_key(action)
        for action in session.scalars(select(Action).where(Action.task_id == task.id))
        if _retains_search_join_pacing_decision(action)
    }
    for decision in session.scalars(
        select(SearchJoinPacingDecision).where(
            SearchJoinPacingDecision.task_id == task.id,
            SearchJoinPacingDecision.decision_scope == "action",
        )
    ):
        if (decision.account_id, decision.keyword_hash, decision.scheduled_at) not in retained:
            session.delete(decision)


def _retains_search_join_pacing_decision(action: Action) -> bool:
    if action.action_type != "search_join":
        return False
    if action.status in {"success", "unknown_after_send"}:
        return True
    return str((action.result or {}).get("gateway_call_state") or "") == "started"


def _search_join_pacing_decision_key(action: Action) -> tuple[int | None, str, datetime | None]:
    return action.account_id, str((action.payload or {}).get("keyword_hash") or ""), action.scheduled_at


def _supersede_active_plan_actions(session: Session, task: Task) -> None:
    now = _now()
    if task.type in SEARCH_CLICK_TASK_TYPES:
        statuses = ["claiming", "retryable_failed", "executing"]
    else:
        statuses = sorted(OPEN_PLAN_ACTION_STATUSES - {"pending"})
    actions = session.scalars(select(Action).where(Action.task_id == task.id, Action.status.in_(statuses)))
    for action in actions:
        if (
            task.type in SEARCH_CLICK_TASK_TYPES
            and action.status == "executing"
            and not _search_click_action_is_pre_gateway(session, action)
        ):
            continue
        _mark_action_plan_superseded(session, action, now)


def _search_click_action_is_pre_gateway(session: Session, action: Action) -> bool:
    if action.action_type == "search_rank_deboost":
        attempts = list(session.scalars(select(ExecutionAttempt).where(ExecutionAttempt.action_id == action.id)))
        return not any(attempt.gateway_call_started_at is not None for attempt in attempts)
    if action.action_type == "search_join":
        return str((action.result or {}).get("gateway_call_state") or "") != "started"
    return False


def _mark_action_plan_superseded(session: Session, action: Action, now: datetime) -> None:
    _clear_search_join_pacing_decision(session, action)
    action.status = "skipped"
    action.executed_at = now
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.result = {"success": False, "error_code": "plan_superseded", "error_message": "任务配置已更新，旧执行计划已废弃"}
    release_reserved_reservation(session, action.id)


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
    "create_and_start_simple_search_join_group_task",
    "create_and_start_search_rank_deboost_task",
    "create_and_start_simple_search_rank_deboost_task",
    "create_channel_comment_task",
    "create_channel_like_task",
    "create_channel_view_task",
    "create_group_ai_chat_task",
    "create_group_membership_admission_task",
    "create_group_relay_task",
    "create_search_join_group_task",
    "create_simple_search_join_group_task",
    "create_search_rank_deboost_task",
    "create_simple_search_rank_deboost_task",
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
    "reroll_search_rank_deboost_exempt_group",
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
    "update_search_rank_deboost_config",
    "update_task_settings",
    "update_task",
]
