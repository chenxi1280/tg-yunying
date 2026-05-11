from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import Action, ChannelMessage, MessageFingerprint, OperationTarget, ReviewQueue, Task, TgAccount
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
from .pacing import next_run_after
from .review import expire_reviews


TYPE_CONFIG_MODELS = {
    "group_ai_chat": GroupAIChatConfig,
    "group_relay": GroupRelayConfig,
    "channel_view": ChannelViewConfig,
    "channel_like": ChannelLikeConfig,
    "channel_comment": ChannelCommentConfig,
}

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
        "messages_per_round",
        "history_fetch_account_id",
    },
    "group_relay": {
        "monitor_account_ids",
        "filters",
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
    reviews = list_reviews(session, tenant_id, task_id=task_id)
    stats = refresh_task_stats(session, task)
    return {
        "task": _task_payload(session, task, actions=actions),
        "actions": actions,
        "reviews": reviews,
        "stats": stats,
        "accounts": _detail_accounts(session, actions),
        "message_groups": _message_groups(session, actions),
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
    task.status = "completed"
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
    task.status = "completed"
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
    session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id))
    session.execute(delete(Action).where(Action.task_id == task.id))
    _clear_task_fingerprints(session, task)
    stats = _empty_stats()
    stats["started_at"] = now.isoformat()
    task.stats = stats
    task.status = "pending" if task.scheduled_start and task.scheduled_start > now else "running"
    task.next_run_at = task.scheduled_start if task.status == "pending" else now
    task.last_error = ""
    task.updated_at = now
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
        raise ReviewStateError("只能通过待审核内容")
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
    audit(session, tenant_id=tenant_id, actor=actor, action="审核通过任务动作", target_type="review_queue", target_id=review.id)
    session.commit()
    session.refresh(review)
    return review


def reject_review(session: Session, tenant_id: int, review_id: str, payload: ReviewRejectRequest, actor: str) -> ReviewQueue:
    review = _get_review(session, tenant_id, review_id)
    if review.status != "pending":
        raise ReviewStateError("只能拒绝待审核内容")
    action = session.get(Action, review.action_id)
    review.status = "rejected"
    review.reviewed_by = actor
    review.reviewed_at = _now()
    review.reject_reason = payload.reason
    if action:
        action.status = "skipped"
        action.executed_at = _now()
        action.result = {"success": False, "error_code": "review_rejected", "error_message": payload.reason or "审核拒绝"}
    audit(session, tenant_id=tenant_id, actor=actor, action="审核拒绝任务动作", target_type="review_queue", target_id=review.id)
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
    action_label = "浏览" if payload.task_type == "channel_view" else "点赞"
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
    with session_factory() as session:
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
            task.next_run_at = next_run_after(task.pacing_config or {})
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
        action.retry_count += 1
        delay = retry_delay
        if backoff == "linear":
            delay *= action.retry_count
        elif backoff == "exponential":
            delay *= 2 ** max(0, action.retry_count - 1)
        action.status = "pending"
        action.scheduled_at = _now() + timedelta(seconds=delay)
        action.executed_at = None
        action.result = {}
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
    task.type_config = payload.model_dump(mode="json")
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
        return str(config.get("target_group_name") or config.get("target_group_id") or "")
    if task.type == "group_relay":
        sources = [str(item.get("group_name") or item.get("group_id") or "") for item in config.get("source_groups") or []]
        target = str(config.get("target_group_id") or "")
        return " ".join([*sources, target])
    return ""


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


def _message_groups(session: Session, actions: list[Action]) -> list[dict[str, Any]]:
    groups: dict[tuple[int | None, int | None], dict[str, Any]] = {}
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
        key = (channel_target_id, int(payload.get("message_id") or 0) or None)
        item = groups.setdefault(
            key,
            {
                "channel_target_id": channel_target_id,
                "channel_title": channel.title if channel else str(payload.get("target_display") or ""),
                "channel_username": channel.username if channel else "",
                "message_id": key[1],
                "message_url": message.message_url if message else "",
                "content_preview": message.content_preview if message else str(payload.get("message_content") or ""),
                "stats": {"total": 0, "pending": 0, "executing": 0, "success": 0, "failed": 0, "skipped": 0},
                "actions": [],
            },
        )
        item["actions"].append(action)
        stats = item["stats"]
        stats["total"] += 1
        if action.status in stats:
            stats[action.status] += 1
        if action.result and action.result.get("error_message"):
            stats["last_error"] = action.result["error_message"]
    return sorted(groups.values(), key=lambda item: (item.get("channel_title") or "", -(item.get("message_id") or 0)))


def _get_review(session: Session, tenant_id: int, review_id: str) -> ReviewQueue:
    review = session.get(ReviewQueue, review_id)
    if not review or review.tenant_id != tenant_id:
        raise ValueError("review not found")
    return review


def _clear_task_fingerprints(session: Session, task: Task) -> None:
    session.execute(
        delete(MessageFingerprint).where(
            MessageFingerprint.tenant_id == task.tenant_id,
            MessageFingerprint.source_group_id.like(f"{task.id}:%"),
        )
    )


def _clear_unfinished_plan(session: Session, task: Task) -> None:
    pending_action_ids = list(
        session.scalars(
            select(Action.id).where(
                Action.task_id == task.id,
                Action.status == "pending",
            )
        )
    )
    if pending_action_ids:
        session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.action_id.in_(pending_action_ids)))
        session.execute(delete(Action).where(Action.task_id == task.id, Action.status == "pending"))
    session.execute(delete(ReviewQueue).where(ReviewQueue.task_id == task.id, ReviewQueue.status == "pending"))
    if task.type == "group_ai_chat":
        _clear_task_fingerprints(session, task)


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
