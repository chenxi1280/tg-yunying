from __future__ import annotations

import re
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, func, or_, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, GroupAuthStatus, MessageFingerprint, OperationTarget, ReviewQueue, Task, TgAccount, TgGroup, WorkerHeartbeat
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
    ReviewApproveRequest,
    ReviewRejectRequest,
    TaskRetryRequest,
    TaskSettingsUpdate,
    TaskUpdate,
)
from app.services._common import _now, audit

from .account_pool import select_task_accounts
from .ai_generator import generate_channel_comments, generate_group_messages
from .dispatcher import dispatch_action, due_actions
from .executors import build_task_plan, reached_daily_action_limit
from .executors.group_ai_chat import account_profile_summaries
from .fingerprints import content_fingerprint
from .heartbeat import record_worker_heartbeat
from .listener_runtime import drain_listener_runtime, invalidate_listener_collect
from .pacing import next_run_after
from .review import expire_reviews


TYPE_CONFIG_MODELS = {
    "group_ai_chat": GroupAIChatConfig,
    "group_relay": GroupRelayConfig,
    "channel_view": ChannelViewConfig,
    "channel_like": ChannelLikeConfig,
    "channel_comment": ChannelCommentConfig,
}
CHANNEL_DYNAMIC_TASK_TYPES = {"channel_view", "channel_like", "channel_comment"}

COMMON_CREATE_FIELDS = {
    "name",
    "priority",
    "timezone",
    "scheduled_start",
    "scheduled_end",
    "max_duration_hours",
    "account_config",
    "pacing_config",
    "failure_policy",
}

COMMON_SETTINGS_FIELDS = {
    "name",
    "priority",
    "timezone",
    "scheduled_start",
    "scheduled_end",
    "max_duration_hours",
    "account_config",
    "pacing_config",
    "failure_policy",
}

TYPE_SETTINGS_FIELDS = {
    "group_ai_chat": {
        "target_group_id",
        "target_operation_target_id",
        "target_group_name",
        "topic_hint",
        "chat_history_depth",
        "ai_model",
        "system_prompt_override",
        "tone",
        "language",
        "max_message_length",
        "participation_rate",
        "participation_jitter",
        "allow_account_repeat",
        "repeat_cooldown_rounds",
        "account_personas",
        "messages_per_round_mode",
        "messages_per_round",
        "history_fetch_account_id",
        "silent_mode_enabled",
        "silent_start",
        "silent_end",
        "silent_max_accounts",
        "silent_messages_per_round",
        "ramp_up_minutes",
        "ramp_start_ratio",
        "context_expire_after_messages",
    },
    "group_relay": {
        "source_groups",
        "rule_set_id",
        "rule_set_version_id",
        "monitor_account_ids",
        "filters",
        "target_group_id",
        "target_operation_target_id",
        "target_group_ids",
        "target_operation_target_ids",
        "content_mode",
        "rewrite_prompt",
        "preserve_media",
        "add_source_attribution",
        "dedup_window_minutes",
        "dedup_method",
        "require_review",
    },
    "channel_view": {
        "target_views_per_message",
        "view_count_jitter",
        "execution_mode",
    },
    "channel_like": {
        "target_likes_per_message",
        "like_count_jitter",
        "reaction_type",
        "allowed_reactions",
        "max_likes_per_account_per_hour",
    },
    "channel_comment": {
        "target_comments_per_message",
        "comment_count_jitter",
        "comment_mode",
        "reply_to_message_ids",
        "ai_model",
        "comment_style",
        "topic_hint",
        "system_prompt_override",
        "language",
        "max_comment_length",
        "max_comments_per_account_per_hour",
        "require_review",
    },
}


class ReviewStateError(ValueError):
    """Raised when an operator tries to transition a terminal review."""


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
    raw_type_config = payload.model_dump(mode="json", exclude=COMMON_CREATE_FIELDS)
    raw_type_config = _normalize_operation_target_references(session, tenant_id, task_type, raw_type_config)
    type_config = _validated_type_config(task_type, raw_type_config)
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
        pacing_config=payload.pacing_config.model_dump(mode="json"),
        failure_policy=payload.failure_policy.model_dump(mode="json"),
        type_config=type_config,
        stats=_empty_stats(),
    )
    session.add(task)
    session.flush()
    return task


def _normalize_operation_target_references(session: Session, tenant_id: int, task_type: str, config: dict[str, Any]) -> dict[str, Any]:
    next_config = dict(config)
    if task_type == "group_ai_chat":
        target_id = _as_int(next_config.get("target_operation_target_id"))
        if target_id:
            target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=True)
            next_config["target_operation_target_id"] = target.id
            next_config["target_group_id"] = group.id
            next_config["target_group_name"] = next_config.get("target_group_name") or target.title or group.title
    elif task_type == "group_relay":
        normalized_sources: list[dict[str, Any]] = []
        for item in next_config.get("source_groups") or []:
            source = dict(item)
            target_id = _as_int(source.get("operation_target_id"))
            if target_id:
                target, group = _group_for_operation_target(session, tenant_id, target_id, require_can_send=False)
                source["operation_target_id"] = target.id
                source["group_id"] = group.id
                source["group_name"] = source.get("group_name") or target.title or group.title
            normalized_sources.append(source)
        next_config["source_groups"] = normalized_sources

        target_id = _as_int(next_config.get("target_operation_target_id"))
        target_group_ids = _as_int_list(next_config.get("target_group_ids"))
        target_operation_target_ids = _as_int_list(next_config.get("target_operation_target_ids"))
        if target_id and target_id not in target_operation_target_ids:
            target_operation_target_ids.insert(0, target_id)
        resolved_target_group_ids: list[int] = []
        for operation_target_id in target_operation_target_ids:
            target, group = _group_for_operation_target(session, tenant_id, operation_target_id, require_can_send=True)
            resolved_target_group_ids.append(group.id)
        if resolved_target_group_ids:
            next_config["target_operation_target_ids"] = target_operation_target_ids
            next_config["target_operation_target_id"] = target_operation_target_ids[0]
            next_config["target_group_id"] = resolved_target_group_ids[0]
            target_group_ids = [*resolved_target_group_ids, *target_group_ids]
        if target_group_ids:
            next_config["target_group_ids"] = list(dict.fromkeys(target_group_ids))
    return next_config


def _group_for_operation_target(session: Session, tenant_id: int, target_id: int, *, require_can_send: bool) -> tuple[OperationTarget, TgGroup]:
    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != tenant_id or target.target_type != "group":
        raise ValueError("运营目标不存在")
    if target.auth_status != GroupAuthStatus.AUTHORIZED.value:
        raise ValueError("运营目标未授权")
    if require_can_send and not target.can_send:
        raise ValueError("运营目标不可发送")
    group = session.scalar(
        select(TgGroup).where(
            TgGroup.tenant_id == tenant_id,
            TgGroup.tg_peer_id == target.tg_peer_id,
        )
    )
    if not group:
        raise ValueError("运营目标未关联群资产")
    return target, group


def _as_int(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _as_int_list(value: Any) -> list[int]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        value = [value]
    if not isinstance(value, list):
        return []
    items: list[int] = []
    for item in value:
        number = _as_int(item)
        if number:
            items.append(number)
    return items


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
    return [_task_payload(session, task) for task in tasks]


def get_task_detail(session: Session, tenant_id: int, task_id: str) -> dict[str, Any]:
    task = _get_task(session, tenant_id, task_id)
    actions = list_actions(session, tenant_id, task_id)
    stats = refresh_task_stats(session, task)
    return {
        "task": _task_payload(session, task, actions=actions),
        "actions": actions,
        "stats": stats,
        "accounts": _detail_accounts(session, actions),
        "message_groups": _message_groups(session, task, actions),
        "ai_cycles": _ai_cycles(actions),
        "ai_generation_records": _ai_generation_records(actions),
        "ai_account_profiles": _ai_account_profiles(session, task, actions),
        "relay_batches": _relay_batches(actions),
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
            setattr(task, field, data[field])
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
            setattr(task, field, data[field])
    if type_updates:
        next_config = dict(task.type_config or {})
        next_config.update(type_updates)
        next_config = _normalize_operation_target_references(session, tenant_id, task.type, next_config)
        task.type_config = _validated_type_config(task.type, next_config)
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
    stats = _empty_stats()
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


def list_reviews(session: Session, tenant_id: int, status: str | None = None, task_id: str | None = None) -> list[ReviewQueue]:
    if expire_reviews(session):
        session.commit()
    stmt = select(ReviewQueue).where(ReviewQueue.tenant_id == tenant_id)
    if status:
        stmt = stmt.where(ReviewQueue.status == status)
    if task_id:
        stmt = stmt.where(ReviewQueue.task_id == task_id)
    return list(session.scalars(stmt.order_by(ReviewQueue.created_at.desc()).limit(500)))


def approve_review(session: Session, tenant_id: int, review_id: str, payload: ReviewApproveRequest, actor: str) -> ReviewQueue:
    review = _get_review(session, tenant_id, review_id)
    if review.status != "pending":
        raise ReviewStateError("只能处理待处理内容")
    action = session.get(Action, review.action_id)
    if not action:
        raise ValueError("action not found")
    if payload.edited_content:
        data = dict(action.payload or {})
        if action.action_type == "post_comment":
            data["comment_text"] = payload.edited_content
        else:
            data["message_text"] = payload.edited_content
        data["review_approved"] = True
        action.payload = data
        review.content_preview = payload.edited_content[:4000]
    else:
        data = dict(action.payload or {})
        data["review_approved"] = True
        action.payload = data
    review.status = "approved"
    review.reviewed_by = actor
    review.reviewed_at = _now()
    action.status = "pending"
    action.scheduled_at = _now()
    audit(session, tenant_id=tenant_id, actor=actor, action="处理通过任务动作", target_type="review_queue", target_id=review.id)
    session.commit()
    session.refresh(review)
    return review


def reject_review(session: Session, tenant_id: int, review_id: str, payload: ReviewRejectRequest, actor: str) -> ReviewQueue:
    review = _get_review(session, tenant_id, review_id)
    if review.status != "pending":
        raise ReviewStateError("只能跳过待处理内容")
    action = session.get(Action, review.action_id)
    review.status = "rejected"
    review.reviewed_by = actor
    review.reviewed_at = _now()
    review.reject_reason = payload.reason
    if action:
        action.status = "skipped"
        action.executed_at = _now()
        action.result = {"success": False, "error_code": "review_rejected", "error_message": payload.reason or "内容处理跳过"}
    audit(session, tenant_id=tenant_id, actor=actor, action="处理跳过任务动作", target_type="review_queue", target_id=review.id)
    session.commit()
    session.refresh(review)
    return review


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
    }


def drain_task_center(session_factory, limit: int = 100) -> int:
    processed = 0
    processed += drain_listener_runtime(session_factory, limit=limit).processed_count
    with session_factory() as session:
        record_worker_heartbeat(session, metadata={"limit": limit})
        processed += _recover_continuous_task_states(session)
        processed += _recover_stale_executing_actions(session)
        processed += expire_reviews(session)
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
    for task_id in task_ids:
        with session_factory() as session:
            task = session.get(Task, task_id)
            if not task or task.status != "running":
                continue
            if _check_stop_conditions(session, task):
                session.commit()
                continue
            processed += _retry_failed_actions(session, task)
            if task.type == "group_ai_chat" and _has_open_actions(session, task):
                refresh_task_stats(session, task)
                session.commit()
                continue
            created = build_task_plan(session, task)
            task.next_run_at = _next_run_after_task(task)
            refresh_task_stats(session, task)
            session.commit()
            processed += created
    with session_factory() as session:
        for action in due_actions(session, limit=max(10, limit)):
            if dispatch_action(session, action):
                processed += 1
                refresh = session.get(Task, action.task_id)
                if refresh:
                    refresh_task_stats(session, refresh)
                session.commit()
    return processed


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
        action.status = "failed"
        action.executed_at = now
        action.lease_owner = ""
        action.lease_expires_at = None
        action.result = {
            "success": False,
            "error_code": "execution_timeout",
            "error_message": "执行项长时间处于执行中，已由投递守护标记为超时",
            "validation_stage": "execution_recovery",
            "auto_check": "超时恢复",
            "recovery_reason": recovery_reason,
            "recovered_at": now.isoformat(),
            "previous_lease_owner": previous_lease_owner,
            "previous_lease_expires_at": previous_lease_expires_at.isoformat() if previous_lease_expires_at else "",
        }
        if previous_result:
            action.result["previous_result"] = previous_result
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
        stats["last_recovery_stage"] = "execution_recovery"
        task.stats = stats
        recovered += 1
    return recovered


def _next_run_after_task(task: Task):
    config = task.type_config or {}
    if task.type in CHANNEL_DYNAMIC_TASK_TYPES and (config.get("message_scope") or "latest_n") == "dynamic_new":
        interval = int(config.get("listener_interval_seconds") or 30)
        return _now() + timedelta(seconds=max(1, interval))
    return next_run_after(task.pacing_config or {})


def refresh_task_stats(session: Session, task: Task) -> dict[str, Any]:
    session.flush()
    rows = session.execute(select(Action.status, func.count(Action.id)).where(Action.task_id == task.id).group_by(Action.status)).all()
    counts = {str(status): int(count) for status, count in rows}
    accounts_used = session.scalar(select(func.count(func.distinct(Action.account_id))).where(Action.task_id == task.id, Action.account_id.is_not(None))) or 0
    last_action_at = session.scalar(select(func.max(Action.executed_at)).where(Action.task_id == task.id))
    stats = dict(task.stats or _empty_stats())
    stats.update(
        {
            "total_actions": sum(counts.values()),
            "success_count": counts.get("success", 0),
            "failure_count": counts.get("failed", 0),
            "pending_count": counts.get("pending", 0),
            "executing_count": counts.get("executing", 0),
            "skipped_count": counts.get("skipped", 0),
            "accounts_used": int(accounts_used or 0),
            "last_action_at": last_action_at.isoformat() if last_action_at else stats.get("last_action_at"),
        }
    )
    task.stats = stats
    return stats


def _retry_failed_actions(session: Session, task: Task) -> int:
    policy = task.failure_policy or {}
    max_retries = int(policy.get("max_retries") or 0)
    if max_retries <= 0:
        return 0
    retry_delay = int(policy["retry_delay_seconds"]) if policy.get("retry_delay_seconds") is not None else 60
    backoff = policy.get("retry_backoff") or "none"
    count = 0
    for action in session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "failed", Action.retry_count < max_retries)):
        previous_result = dict(action.result or {})
        action.retry_count += 1
        delay = retry_delay
        if backoff == "linear":
            delay *= action.retry_count
        elif backoff == "exponential":
            delay *= 2 ** max(0, action.retry_count - 1)
        action.status = "pending"
        action.scheduled_at = _now() + timedelta(seconds=delay)
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
    stats = dict(task.stats or _empty_stats())
    stats["started_at"] = stats.get("started_at") or now.isoformat()
    task.stats = stats
    task.last_error = ""


def _validated_type_config(task_type: str, data: dict[str, Any]) -> dict[str, Any]:
    model = TYPE_CONFIG_MODELS.get(task_type)
    if not model:
        raise ValueError(f"unknown task type: {task_type}")
    normalized = model(**(data or {})).model_dump(mode="json")
    if task_type in {"group_relay", "channel_comment"}:
        normalized["require_review"] = False
    return normalized


def _update_type_config(session: Session, tenant_id: int, task_id: str, expected_type: str, payload, actor: str) -> Task:
    task = _get_task(session, tenant_id, task_id)
    if task.type != expected_type:
        raise ValueError(f"任务类型不匹配，当前任务是 {task.type}")
    task.type_config = _normalize_operation_target_references(session, tenant_id, expected_type, payload.model_dump(mode="json"))
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


def _task_payload(session: Session, task: Task, actions: list[Action] | None = None) -> dict[str, Any]:
    target_summary = _target_summary(session, task)
    search_parts = [
        task.id,
        task.name,
        task.type,
        task.status,
        task.last_error,
        target_summary,
        _task_config_search_text(session, task),
    ]
    if actions:
        for action in actions:
            payload = action.payload or {}
            search_parts.extend(
                [
                    action.action_type,
                    action.status,
                    str(payload.get("target_display") or ""),
                    str(payload.get("message_content") or ""),
                    str(payload.get("message_text") or ""),
                    str(payload.get("comment_text") or ""),
                ]
            )
    return {
        "id": task.id,
        "tenant_id": task.tenant_id,
        "name": task.name,
        "type": task.type,
        "status": task.status,
        "priority": task.priority,
        "timezone": task.timezone,
        "scheduled_start": task.scheduled_start,
        "scheduled_end": task.scheduled_end,
        "max_duration_hours": task.max_duration_hours,
        "next_run_at": task.next_run_at,
        "last_error": task.last_error,
        "account_config": task.account_config or {},
        "pacing_config": task.pacing_config or {},
        "failure_policy": task.failure_policy or {},
        "type_config": task.type_config or {},
        "stats": task.stats or {},
        "target_summary": target_summary,
        "search_text": " ".join(str(item) for item in search_parts if item),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


def _target_summary(session: Session, task: Task) -> str:
    config = task.type_config or {}
    if task.type.startswith("channel_"):
        channel = _channel_for_config(session, task)
        if channel:
            return f"{channel.title} @{channel.username}" if channel.username else channel.title
        return str(config.get("target_channel_name") or "")
    if task.type == "group_ai_chat":
        return str(
            config.get("target_group_name")
            or _operation_target_title(session, task.tenant_id, config.get("target_operation_target_id"))
            or config.get("target_group_id")
            or ""
        )
    if task.type == "group_relay":
        sources = [
            str(item.get("group_name") or _operation_target_title(session, task.tenant_id, item.get("operation_target_id")) or item.get("group_id") or "")
            for item in config.get("source_groups") or []
            if isinstance(item, dict)
        ]
        targets = [
            _operation_target_title(session, task.tenant_id, item) or f"运营目标#{item}"
            for item in config.get("target_operation_target_ids") or []
        ]
        targets.extend(str(item) for item in config.get("target_group_ids") or [])
        if config.get("target_operation_target_id"):
            label = _operation_target_title(session, task.tenant_id, config.get("target_operation_target_id")) or f"运营目标#{config.get('target_operation_target_id')}"
            if label not in targets:
                targets.insert(0, label)
        if config.get("target_group_id") and str(config.get("target_group_id")) not in targets:
            targets.insert(0, str(config.get("target_group_id")))
        return " ".join([*sources, *targets])
    return ""


def _operation_target_title(session: Session, tenant_id: int, target_id: Any) -> str:
    try:
        parsed_id = int(target_id or 0)
    except (TypeError, ValueError):
        return ""
    if not parsed_id:
        return ""
    target = session.get(OperationTarget, parsed_id)
    if not target or target.tenant_id != tenant_id:
        return ""
    return target.title


def _task_config_search_text(session: Session, task: Task) -> str:
    config = task.type_config or {}
    parts: list[str] = []
    if task.type.startswith("channel_"):
        channel = _channel_for_config(session, task)
        if channel:
            parts.extend([channel.title, channel.username, channel.tg_peer_id])
        message_ids = [int(item) for item in config.get("message_ids") or [] if str(item).isdigit()]
        stmt = select(ChannelMessage).where(ChannelMessage.tenant_id == task.tenant_id)
        if channel:
            stmt = stmt.where(ChannelMessage.channel_target_id == channel.id)
        if message_ids:
            stmt = stmt.where((ChannelMessage.id.in_(message_ids)) | (ChannelMessage.message_id.in_(message_ids)))
        else:
            stmt = stmt.limit(20)
        for message in session.scalars(stmt):
            parts.extend([str(message.message_id), message.content_preview, message.message_url])
    return " ".join(part for part in parts if part)


def _channel_for_config(session: Session, task: Task) -> OperationTarget | None:
    config = task.type_config or {}
    target_id = int(config.get("target_channel_id") or 0)
    if not target_id:
        return None
    channel = session.get(OperationTarget, target_id)
    if not channel or channel.tenant_id != task.tenant_id or channel.target_type != "channel":
        return None
    return channel


def _detail_accounts(session: Session, actions: list[Action]) -> list[dict[str, Any]]:
    ids = sorted({int(action.account_id) for action in actions if action.account_id})
    if not ids:
        return []
    accounts = list(session.scalars(select(TgAccount).where(TgAccount.id.in_(ids))))
    by_id = {account.id: account for account in accounts}
    return [
        {
            "id": account_id,
            "display_name": by_id[account_id].display_name if account_id in by_id else f"账号 #{account_id}",
            "username": by_id[account_id].username if account_id in by_id else None,
            "status": by_id[account_id].status if account_id in by_id else "未知",
        }
        for account_id in ids
    ]


def _message_groups(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    groups: dict[tuple[int | None, int | None, str], dict[str, Any]] = {}
    message_ids = {
        int(action.payload["channel_message_id"])
        for action in actions
        if isinstance(action.payload, dict) and isinstance(action.payload.get("channel_message_id"), int)
    }
    messages = list(session.scalars(select(ChannelMessage).where(ChannelMessage.id.in_(message_ids)))) if message_ids else []
    messages_by_id = {message.id: message for message in messages}
    channel_ids = {
        int(message.channel_target_id)
        for message in messages
        if message.channel_target_id
    } | {
        int(action.payload["channel_target_id"])
        for action in actions
        if isinstance(action.payload, dict) and isinstance(action.payload.get("channel_target_id"), int)
    }
    channels = list(session.scalars(select(OperationTarget).where(OperationTarget.id.in_(channel_ids)))) if channel_ids else []
    channels_by_id = {channel.id: channel for channel in channels}

    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict) or "message_id" not in payload or "channel_id" not in payload:
            continue
        message = messages_by_id.get(payload.get("channel_message_id"))
        channel_target_id = int(payload.get("channel_target_id") or (message.channel_target_id if message else 0) or 0) or None
        channel = channels_by_id.get(channel_target_id) if channel_target_id else None
        action_type = action.action_type
        key = (channel_target_id, int(payload.get("message_id") or 0) or None, action_type)
        target_count = _channel_subtask_target_count(task, action_type)
        item = groups.setdefault(
            key,
            {
                "channel_target_id": channel_target_id,
                "channel_title": channel.title if channel else str(payload.get("target_display") or ""),
                "channel_username": channel.username if channel else "",
                "message_id": key[1],
                "action_type": action_type,
                "action_label": _action_label(action_type),
                "message_url": message.message_url if message else "",
                "content_preview": message.content_preview if message else str(payload.get("message_content") or ""),
                "target_count": target_count,
                "completed_count": 0,
                "failed_count": 0,
                "running_count": 0,
                "skipped_count": 0,
                "duplicate_count": 0,
                "capacity_shortfall": 0,
                "subtask_status": "运行中",
                "stats": {"target": target_count, "total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0, "duplicate": 0},
                "actions": [],
            },
        )
        item["actions"].append(action)
        stats = item["stats"]
        stats["total"] += 1
        if action.status in stats:
            stats[action.status] += 1
        if _is_duplicate_action(action):
            stats["duplicate"] += 1
        if action.result and action.result.get("error_message"):
            stats["last_error"] = action.result["error_message"]
    for item in groups.values():
        stats = item["stats"]
        item["completed_count"] = int(stats.get("success") or 0)
        item["failed_count"] = int(stats.get("failed") or 0)
        item["running_count"] = int(stats.get("pending") or 0) + int(stats.get("executing") or 0)
        item["skipped_count"] = int(stats.get("skipped") or 0)
        item["duplicate_count"] = int(stats.get("duplicate") or 0)
        item["capacity_shortfall"] = max(int(item.get("target_count") or 0) - int(stats.get("total") or 0), 0)
        item["subtask_status"] = _channel_subtask_status(item)
    return sorted(groups.values(), key=lambda item: (item.get("channel_title") or "", -(item.get("message_id") or 0)))


def _channel_subtask_target_count(task: Task, action_type: str) -> int:
    config = task.type_config or {}
    if action_type == "view_message":
        return int(config.get("target_views_per_message") or 0)
    if action_type == "like_message":
        return int(config.get("target_likes_per_message") or 0)
    if action_type == "post_comment":
        return int(config.get("target_comments_per_message") or 0)
    return 0


def _action_label(action_type: str) -> str:
    return {
        "view_message": "浏览",
        "like_message": "点赞",
        "post_comment": "评论/回复",
        "send_message": "发送",
    }.get(action_type, action_type)


def _is_duplicate_action(action: Action) -> bool:
    result = action.result or {}
    text = " ".join(str(result.get(key) or "") for key in ("error_code", "failure_type", "error_message", "detail")).lower()
    return "duplicate" in text or "重复" in text


def _channel_subtask_status(item: dict[str, Any]) -> str:
    if item.get("capacity_shortfall"):
        return "容量不足"
    if item.get("running_count"):
        return "运行中"
    if item.get("target_count") and item.get("completed_count", 0) >= item["target_count"]:
        return "已达标"
    if item.get("failed_count"):
        return "有失败"
    if item.get("skipped_count"):
        return "已跳过"
    return "待规划"


def _ai_cycles(actions: list[Action]) -> list[dict[str, Any]]:
    cycles: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        cycle_id = str(payload.get("cycle_id") or "")
        if not cycle_id:
            continue
        item = cycles.setdefault(
            cycle_id,
            {
                "cycle_id": cycle_id,
                "context_message_ids": payload.get("context_message_ids") if isinstance(payload.get("context_message_ids"), list) else [],
                "stats": {"total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0},
                "turns": [],
            },
        )
        item["turns"].append(
            {
                "action_id": action.id,
                "turn_index": int(payload.get("turn_index") or len(item["turns"]) + 1),
                "account_id": action.account_id,
                "account_role": str(payload.get("account_role") or ""),
                "account_memory": str(payload.get("account_memory") or ""),
                "account_profile": str(payload.get("account_profile") or ""),
                "topic_thread": str(payload.get("topic_thread") or ""),
                "topic_plan": str(payload.get("topic_plan") or ""),
                "intent": str(payload.get("intent") or ""),
                "content": str(payload.get("message_text") or ""),
                "status": action.status,
                "scheduled_at": action.scheduled_at,
                "executed_at": action.executed_at,
                "result": action.result or {},
            }
        )
        _group_stats_inc(item["stats"], action.status)
    for item in cycles.values():
        item["turns"].sort(key=lambda row: (row["turn_index"], row["scheduled_at"]))
    return sorted(cycles.values(), key=lambda item: item["cycle_id"])


def _ai_generation_records(actions: list[Action]) -> list[dict[str, Any]]:
    records: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        generation_id = str(payload.get("ai_generation_id") or "")
        if not generation_id:
            continue
        record = records.setdefault(
            generation_id,
            {
                "generation_id": generation_id,
                "cycle_id": str(payload.get("cycle_id") or generation_id),
                "status": str(payload.get("ai_generation_status") or ""),
                "generated_count": int(payload.get("ai_generation_count") or 0),
                "token_count": int(payload.get("ai_generation_tokens") or 0),
                "context_message_count": int(payload.get("ai_generation_context_count") or 0),
                "account_memory_count": int(payload.get("ai_generation_memory_count") or 0),
                "scheduled_at": action.scheduled_at,
                "created_at": action.created_at,
            },
        )
        record["generated_count"] = max(record["generated_count"], int(payload.get("ai_generation_count") or 0))
        record["token_count"] = max(record["token_count"], int(payload.get("ai_generation_tokens") or 0))
        record["context_message_count"] = max(record["context_message_count"], int(payload.get("ai_generation_context_count") or 0))
        record["account_memory_count"] = max(record["account_memory_count"], int(payload.get("ai_generation_memory_count") or 0))
        if action.created_at < record["created_at"]:
            record["created_at"] = action.created_at
        if action.scheduled_at < record["scheduled_at"]:
            record["scheduled_at"] = action.scheduled_at
    return sorted(records.values(), key=lambda item: item["created_at"], reverse=True)


def _ai_account_profiles(session: Session, task: Task, actions: list[Action]) -> list[dict[str, Any]]:
    if task.type != "group_ai_chat":
        return []
    account_ids = {int(action.account_id) for action in actions if action.account_id}
    account_config = task.account_config if isinstance(task.account_config, dict) else {}
    for account_id in account_config.get("account_ids") or []:
        try:
            account_ids.add(int(account_id))
        except (TypeError, ValueError):
            continue
    if not account_ids:
        return []
    summaries = account_profile_summaries(session, task, sorted(account_ids), recent_limit=5)
    if not summaries:
        return []
    current_counts = dict(
        session.execute(
            select(Action.account_id, func.count(Action.id))
            .where(
                Action.task_id == task.id,
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status == "success",
                Action.account_id.in_(account_ids),
            )
            .group_by(Action.account_id)
        ).all()
    )
    accounts = {account.id: account for account in session.scalars(select(TgAccount).where(TgAccount.id.in_(account_ids)))}
    rows: list[dict[str, Any]] = []
    for account_id in sorted(account_ids):
        summary = summaries.get(str(account_id))
        if not summary:
            continue
        current_count = int(current_counts.get(account_id) or 0)
        total = _profile_total_success(summary)
        account = accounts.get(account_id)
        rows.append(
            {
                "account_id": account_id,
                "display_name": account.display_name if account else f"账号 #{account_id}",
                "username": account.username if account else None,
                "status": account.status if account else "未知",
                "total_success_count": total,
                "current_task_success_count": current_count,
                "cross_task_success_count": max(0, total - current_count),
                "profile_summary": summary,
            }
        )
    return sorted(rows, key=lambda item: (-item["total_success_count"], item["account_id"]))


def _profile_total_success(summary: str) -> int:
    match = re.search(r"历史成功发言\s+(\d+)\s+次", summary or "")
    return int(match.group(1)) if match else 0


def _relay_batches(actions: list[Action]) -> list[dict[str, Any]]:
    batches: dict[str, dict[str, Any]] = {}
    for action in actions:
        payload = action.payload or {}
        if not isinstance(payload, dict):
            continue
        batch_id = str(payload.get("relay_batch_id") or "")
        if not batch_id:
            continue
        item = batches.setdefault(
            batch_id,
            {
                "relay_batch_id": batch_id,
                "stats": {"total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0},
                "source_event_count": 0,
                "material_count": 0,
                "rule_version_count": 0,
                "items": [],
            },
        )
        relay_event_id = str(payload.get("relay_event_id") or "")
        source_group_id = payload.get("source_group_id") if isinstance(payload.get("source_group_id"), int) else None
        source_operation_target_id = payload.get("source_operation_target_id") if isinstance(payload.get("source_operation_target_id"), int) else None
        original_text = str(payload.get("original_text") or "")
        transformed_text = str(payload.get("message_text") or "")
        material_text = original_text or transformed_text
        source_event_key = f"{source_operation_target_id or source_group_id or '-'}:{relay_event_id or '-'}"
        item["items"].append(
            {
                "action_id": action.id,
                "relay_event_id": relay_event_id,
                "source_event_key": source_event_key,
                "source_group_id": source_group_id,
                "source_operation_target_id": source_operation_target_id,
                "operation_target_id": payload.get("operation_target_id") if isinstance(payload.get("operation_target_id"), int) else None,
                "source_info": str(payload.get("source_info") or ""),
                "original_text": original_text,
                "transformed_text": transformed_text,
                "material_fingerprint": content_fingerprint(material_text) if material_text else "",
                "rule_set_id": payload.get("rule_set_id") if isinstance(payload.get("rule_set_id"), int) else None,
                "rule_set_version_id": payload.get("rule_set_version_id") if isinstance(payload.get("rule_set_version_id"), int) else None,
                "rule_trace": payload.get("rule_trace") if isinstance(payload.get("rule_trace"), dict) else {},
                "account_id": action.account_id,
                "status": action.status,
                "retry_count": int(action.retry_count or 0),
                "scheduled_at": action.scheduled_at,
                "executed_at": action.executed_at,
                "result": action.result or {},
            }
        )
        _group_stats_inc(item["stats"], action.status)
    for item in batches.values():
        item["items"].sort(key=lambda row: row["scheduled_at"])
        item["source_event_count"] = len({row["source_event_key"] for row in item["items"] if row.get("source_event_key") and not str(row.get("source_event_key")).endswith(":-")})
        item["material_count"] = len({row["material_fingerprint"] for row in item["items"] if row.get("material_fingerprint")})
        item["rule_version_count"] = len({row["rule_set_version_id"] for row in item["items"] if row.get("rule_set_version_id")})
    return sorted(batches.values(), key=lambda item: item["relay_batch_id"])


def _group_stats_inc(stats: dict[str, int], status: str) -> None:
    stats["total"] = int(stats.get("total") or 0) + 1
    if status in stats:
        stats[status] = int(stats.get(status) or 0) + 1


def _get_review(session: Session, tenant_id: int, review_id: str) -> ReviewQueue:
    review = session.get(ReviewQueue, review_id)
    if not review or review.tenant_id != tenant_id:
        raise ValueError("review not found")
    return review


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
    task.next_run_at = earliest
    return True


def _empty_stats() -> dict[str, Any]:
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
    "generate_channel_comment_preview",
    "generate_group_ai_chat_preview",
    "get_task_detail",
    "list_actions",
    "list_reviews",
    "list_tasks",
    "pause_task",
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
