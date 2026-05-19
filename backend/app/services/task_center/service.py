from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Action, ChannelMessage, ExecutionAttempt, MessageFingerprint, OperationTarget, ReviewQueue, RuntimeMetricSnapshot, RuleSet, RuleSetVersion, Task, TgAccount, TgGroup, WorkerHeartbeat
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
    GroupRelayConfig,
    GroupRelayTaskCreate,
    GroupRelayTaskConfigUpdate,
    RecommendTaskAccountsRequest,
    TaskPrecheckRequest,
    TaskRetryRequest,
    TaskSettingsUpdate,
    TaskUpdate,
)
from app.services._common import _now, audit

from .account_pool import select_task_accounts
from .ai_generator import generate_channel_comments, generate_group_messages
from .channel_membership import channel_membership_summary
from .dispatcher import claim_actions, dispatch_action, due_actions, recover_expired_claims
from .executors import build_task_plan, reached_daily_action_limit
from .details import _ai_account_profiles, _ai_cycles, _ai_generation_records, _channel_subtask_status, _detail_accounts, _membership_accounts, _membership_phase, _message_groups, _relay_batches, _relay_recent_sources, _search_actions, _task_payload
from .fingerprints import content_fingerprint
from .heartbeat import record_worker_heartbeat
from .listener_runtime import drain_listener_runtime, invalidate_listener_collect
from .review import expire_reviews
from .reviews import ReviewStateError, approve_review, list_reviews, reject_review
from .precheck import run_precheck_task_creation
from .stats import empty_stats, next_run_after_task, refresh_task_stats, retry_failed_actions
from .utils import as_int as _as_int, as_int_list as _as_int_list
from .runtime_retention import cleanup_runtime_details
from app.services.source_media import WAITING_MATERIAL_CACHE, expire_waiting_source_media_actions, wake_waiting_actions_for_source_media

_empty_stats = empty_stats
_next_run_after_task = next_run_after_task
_retry_failed_actions = retry_failed_actions


from .config_fields import (
    CHANNEL_DYNAMIC_TASK_TYPES,
    COMMON_CREATE_FIELDS,
    COMMON_SETTINGS_FIELDS,
    GROUP_AI_LEGACY_RUNTIME_FIELDS,
    GROUP_RELAY_LEGACY_CREATE_FIELDS,
    TYPE_SETTINGS_FIELDS,
)
from .config_normalization import (
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


def create_channel_view_task(session: Session, tenant_id: int, payload: ChannelViewTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_view", payload, actor)


def create_channel_like_task(session: Session, tenant_id: int, payload: ChannelLikeTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_like", payload, actor)


def create_channel_comment_task(session: Session, tenant_id: int, payload: ChannelCommentTaskCreate, actor: str) -> Task:
    return _create_task(session, tenant_id, "channel_comment", payload, actor)


def create_and_start_group_ai_chat_task(session: Session, tenant_id: int, payload: GroupAIChatTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "group_ai_chat", payload, actor)


def create_and_start_group_relay_task(session: Session, tenant_id: int, payload: GroupRelayTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "group_relay", payload, actor)


def create_and_start_channel_view_task(session: Session, tenant_id: int, payload: ChannelViewTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_view", payload, actor)


def create_and_start_channel_like_task(session: Session, tenant_id: int, payload: ChannelLikeTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_like", payload, actor)


def create_and_start_channel_comment_task(session: Session, tenant_id: int, payload: ChannelCommentTaskCreate, actor: str) -> Task:
    return _create_and_start_task(session, tenant_id, "channel_comment", payload, actor)


def _new_task(session: Session, tenant_id: int, task_type: str, payload) -> Task:
    raw_type_config = payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS, exclude_unset=True)
    raw_type_config = normalize_operation_target_references(session, tenant_id, task_type, raw_type_config)
    raw_type_config = apply_default_slang_config(session, tenant_id, task_type, raw_type_config)
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
    task = _new_task(session, tenant_id, task_type, payload)
    audit(session, tenant_id=tenant_id, actor=actor, action="创建任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    _mark_task_started(task)
    audit(session, tenant_id=tenant_id, actor=actor, action="启动任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def list_tasks(session: Session, tenant_id: int, task_type: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
    stmt = select(Task).where(Task.tenant_id == tenant_id, Task.deleted_at.is_(None))
    if task_type:
        stmt = stmt.where(Task.type == task_type)
    if status:
        stmt = stmt.where(Task.status == status)
    tasks = list(session.scalars(stmt.order_by(Task.priority.asc(), Task.created_at.desc())))
    return [_task_payload(session, task, actions=_search_actions(session, task), include_detail_search=False) for task in tasks]


def get_task_detail(session: Session, tenant_id: int, task_id: str) -> dict[str, Any]:
    task = _get_task(session, tenant_id, task_id)
    actions = list_actions(session, tenant_id, task_id)
    business_actions = [action for action in actions if action.action_type != "ensure_channel_membership"]
    stats = refresh_task_stats(session, task)
    return {
        "task": _task_payload(session, task, actions=business_actions),
        "actions": business_actions,
        "stats": stats,
        "accounts": _detail_accounts(session, business_actions),
        "membership_phase": _membership_phase(task),
        "membership_accounts": _membership_accounts(session, actions),
        "message_groups": _message_groups(session, task, business_actions),
        "ai_cycles": _ai_cycles(business_actions),
        "ai_generation_records": _ai_generation_records(business_actions),
        "ai_account_profiles": _ai_account_profiles(session, task, business_actions),
        "relay_batches": _relay_batches(business_actions),
        "recent_relay_sources": _relay_recent_sources(session, task),
    }


def update_task(session: Session, tenant_id: int, task_id: str, payload: TaskUpdate, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    raw_data = payload.model_dump(exclude_unset=True)
    data = payload.model_dump(exclude_unset=True, mode="json")
    for field in ["name", "priority", "timezone", "scheduled_start", "scheduled_end", "max_duration_hours"]:
        if field in raw_data:
            setattr(task, field, raw_data[field])
    for field in ["account_config", "pacing_config", "failure_policy"]:
        if field in data and data[field] is not None:
            setattr(task, field, pacing_config_payload(raw_data[field]) if field == "pacing_config" else data[field])
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
            setattr(task, field, pacing_config_payload(raw_data[field]) if field == "pacing_config" else data[field])
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
        now = _utc_now_naive()
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


def start_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
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


def stop_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    task.status = "stopped"
    task.next_run_at = None
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")):
        action.status = "skipped"
        action.result = {"success": False, "error_code": "task_stopped", "error_message": "任务已停止"}
        action.executed_at = _now()
    refresh_task_stats(session, task)
    audit(session, tenant_id=tenant_id, actor=actor, action="停止任务中心任务", target_type="task", target_id=task.id)
    session.commit()
    session.refresh(task)
    return task


def delete_task(session: Session, tenant_id: int, task_id: str, actor: str) -> None:
    task = _get_task(session, tenant_id, task_id)
    now = _now()
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status.in_(["pending", "executing"]))):
        action.status = "skipped"
        action.result = {"success": False, "error_code": "task_deleted", "error_message": "任务已删除"}
        action.executed_at = now
    task.status = "deleted"
    task.next_run_at = None
    task.deleted_at = now
    task.deleted_by = actor
    task.delete_reason = "用户删除"
    task.updated_at = now
    refresh_task_stats(session, task)
    audit(session, tenant_id=tenant_id, actor=actor, action="删除任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    session.commit()


def retry_task(session: Session, tenant_id: int, task_id: str, payload: TaskRetryRequest, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    stmt = select(Action).where(Action.task_id == task.id)
    if payload.failed_only:
        stmt = stmt.where(Action.status == "failed")
    for action in session.scalars(stmt):
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


def reset_task(session: Session, tenant_id: int, task_id: str, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    now = _now()
    stats = empty_stats()
    stats["started_at"] = now.isoformat()
    if task.type == "group_ai_chat":
        stats["force_bootstrap_once"] = True
    task.stats = stats
    _clear_unfinished_plan(session, task)
    _invalidate_task_listener_cache(task)
    task.status = "pending" if task.scheduled_start and task.scheduled_start > now else "running"
    task.next_run_at = task.scheduled_start if task.status == "pending" else now
    task.last_error = ""
    task.updated_at = now
    refresh_task_stats(session, task)
    audit(session, tenant_id=tenant_id, actor=actor, action="重置任务中心任务", target_type="task", target_id=task.id, detail=task.type)
    session.commit()
    session.refresh(task)
    return task


def list_actions(session: Session, tenant_id: int, task_id: str | None = None, status: str | None = None) -> list[Action]:
    stmt = select(Action).where(Action.tenant_id == tenant_id)
    if task_id:
        stmt = stmt.where(Action.task_id == task_id)
    if status:
        stmt = stmt.where(Action.status == status)
    return list(session.scalars(stmt.order_by(Action.scheduled_at.desc(), Action.created_at.desc()).limit(500)))


def generate_group_ai_chat_preview(session: Session, tenant_id: int, payload: GroupAIChatTaskPreviewRequest) -> dict[str, list[str]]:
    config = GroupAIChatConfig(**payload.model_dump(mode="json", exclude={"count"})).model_dump(mode="json")
    contents, _ = generate_group_messages(session, tenant_id, config, count=payload.count, target_label="群组", history="")
    return {"previews": contents[: payload.count]}


def generate_channel_comment_preview(session: Session, tenant_id: int, payload: ChannelCommentTaskPreviewRequest) -> dict[str, list[str]]:
    config = ChannelCommentConfig(**payload.model_dump(mode="json", exclude={"count", "message_content"})).model_dump(mode="json")
    contents, _ = generate_channel_comments(session, tenant_id, config, count=payload.count, message_content=payload.message_content or "频道消息内容示例", target_label="频道")
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
        processed += _recover_continuous_task_states(session)
        processed += _recover_stale_executing_actions(session)
        processed += expire_reviews(session)
        settings = get_settings()
        if settings.enable_runtime_retention_cleanup:
            processed += cleanup_runtime_details(session, retention_days=settings.runtime_detail_retention_days)
        processed += expire_waiting_source_media_actions(session, limit=max(10, limit))
        tenant_ids = list(session.scalars(select(Action.tenant_id).where(Action.status == WAITING_MATERIAL_CACHE).distinct()))
        touched_tenant_ids.update(int(tenant_id) for tenant_id in tenant_ids)
        for tenant_id in tenant_ids:
            processed += wake_waiting_actions_for_source_media(session, tenant_id=tenant_id, limit=max(10, limit))
        session.commit()
    return processed, touched_tenant_ids


def drain_task_planner(session_factory, limit: int = 100) -> int:
    processed, _ = _drain_task_planner(session_factory, limit=limit, process_type="planner")
    return processed


def _drain_task_planner(session_factory, *, limit: int, process_type: str | None) -> tuple[int, set[str]]:
    processed = 0
    with session_factory() as session:
        if process_type:
            record_worker_heartbeat(session, process_type=process_type, metadata={"limit": limit})
        _activate_pending_tasks(session)
        task_ids = list(
            session.scalars(
                select(Task.id)
                .where(Task.status == "running", (Task.next_run_at.is_(None)) | (Task.next_run_at <= _now()))
                .order_by(Task.priority.asc(), Task.next_run_at.asc().nullsfirst(), Task.created_at.asc())
                .limit(max(1, limit))
            )
        )
        session.commit()
    future_open_action_task_ids: set[str] = set()
    for task_id in task_ids:
        with session_factory() as session:
            task = session.get(Task, task_id)
            if not task or task.status != "running":
                continue
            if _check_stop_conditions(session, task):
                session.commit()
                continue
            processed += retry_failed_actions(session, task)
            if task.type == "group_ai_chat" and _has_open_actions(session, task):
                if task.next_run_at and _absolute_naive_datetime(task.next_run_at) > _utc_now_naive():
                    future_open_action_task_ids.add(task.id)
                refresh_task_stats(session, task)
                session.commit()
                continue
            if _planning_backlog_blocked(session, task):
                refresh_task_stats(session, task)
                session.commit()
                continue
            created = build_task_plan(session, task)
            task.next_run_at = next_run_after_task(task)
            refresh_task_stats(session, task)
            session.commit()
            processed += created
    return processed, future_open_action_task_ids


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
        statuses = dict(session.execute(select(Action.status, func.count(Action.id)).group_by(Action.status)).all())
        oldest_pending = session.scalar(select(func.min(Action.scheduled_at)).where(Action.status == "pending"))
        oldest_pending_age = int((now_value - _naive_datetime(oldest_pending)).total_seconds()) if oldest_pending else 0
        minute_cutoff = now_value - timedelta(minutes=1)
        recent_statuses = dict(
            session.execute(
                select(Action.status, func.count(Action.id))
                .where(Action.executed_at >= minute_cutoff)
                .group_by(Action.status)
            ).all()
        )
        created_last_minute = session.scalar(select(func.count(Action.id)).where(Action.created_at >= minute_cutoff)) or 0
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
        session.commit()
    return len(rows)


def _planning_backlog_blocked(session: Session, task: Task) -> bool:
    settings = get_settings()
    pending_statuses = {"pending", "claiming", "executing"}
    global_pending = session.scalar(select(func.count(Action.id)).where(Action.status.in_(pending_statuses))) or 0
    task_pending = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id, Action.status.in_(pending_statuses))) or 0
    oldest_pending = session.scalar(select(func.min(Action.scheduled_at)).where(Action.status.in_(pending_statuses)))
    now_value = _now()
    oldest_age = int((now_value - _naive_datetime(oldest_pending)).total_seconds()) if oldest_pending else 0
    blocked = (
        int(global_pending or 0) >= int(settings.max_pending_global or 0)
        or int(task_pending or 0) >= int(settings.max_pending_per_task or 0)
        or oldest_age >= int(settings.oldest_pending_age_seconds or 0)
    )
    if not blocked:
        return False
    stats = dict(task.stats or {})
    stats["planner_backlog_blocked"] = True
    stats["planner_backlog_blocked_at"] = now_value.isoformat()
    stats["planner_backlog_global_pending"] = int(global_pending or 0)
    stats["planner_backlog_task_pending"] = int(task_pending or 0)
    stats["planner_backlog_oldest_age_seconds"] = int(oldest_age)
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
    return recovered


def _activate_pending_tasks(session: Session) -> None:
    for task in session.scalars(select(Task).where(Task.status == "pending", (Task.scheduled_start.is_(None)) | (Task.scheduled_start <= _now()))):
        task.status = "running"
        task.next_run_at = _now()


def _check_stop_conditions(session: Session, task: Task) -> bool:
    now = _now()
    scheduled_end = _naive_datetime(task.scheduled_end)
    if scheduled_end and scheduled_end <= now:
        task.status = "completed"
        task.next_run_at = None
        refresh_task_stats(session, task)
        return True
    if reached_daily_action_limit(session, task):
        task.next_run_at = now + timedelta(hours=1)
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
    task.stats = stats
    task.last_error = ""


def _update_type_config(session: Session, tenant_id: int, task_id: str, expected_type: str, payload, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type != expected_type:
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    next_config = normalize_operation_target_references(session, tenant_id, expected_type, payload.model_dump(mode="json"))
    next_config = validated_type_config(expected_type, next_config)
    validate_rule_binding(session, tenant_id, next_config)
    task.type_config = next_config
    task.updated_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="更新任务类型配置", target_type="task", target_id=task.id, detail=expected_type)
    session.commit()
    session.refresh(task)
    return task


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
        session.execute(delete(Action).where(Action.task_id == task.id, Action.status == "pending"))
    session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.status == "pending"))


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


def _naive_datetime(value):
    if value and getattr(value, "tzinfo", None):
        return value.replace(tzinfo=None)
    return value


def _has_open_actions(session: Session, task: Task) -> bool:
    earliest = session.scalar(
        select(func.min(Action.scheduled_at)).where(
            Action.task_id == task.id,
            Action.status.in_(["pending", "executing"]),
        )
    )
    if not earliest:
        return False
    task.next_run_at = _absolute_naive_datetime(earliest)
    return True


def _absolute_naive_datetime(value: datetime) -> datetime:
    if value.tzinfo is not None:
        return value.astimezone(UTC).replace(tzinfo=None)
    return value


def _utc_now_naive() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


__all__ = [
    "approve_review",
    "check_channel_capacity",
    "create_and_start_channel_comment_task",
    "create_and_start_channel_like_task",
    "create_and_start_channel_view_task",
    "create_and_start_group_ai_chat_task",
    "create_and_start_group_relay_task",
    "create_channel_comment_task",
    "create_channel_like_task",
    "create_channel_view_task",
    "create_group_ai_chat_task",
    "create_group_relay_task",
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
    "list_actions",
    "list_reviews",
    "list_tasks",
    "pause_task",
    "precheck_task_creation",
    "recommend_accounts",
    "reject_review",
    "ReviewStateError",
    "resume_task",
    "reset_task",
    "retry_task",
    "start_task",
    "stop_task",
    "update_channel_comment_config",
    "update_channel_like_config",
    "update_channel_view_config",
    "update_group_ai_chat_config",
    "update_group_relay_config",
    "update_task_settings",
    "update_task",
]
