from __future__ import annotations

import os
import socket
from datetime import timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.gateways import OutboundSegment
from app.config import get_settings
from app.models import AccountStatus, Action, FailureType, GroupContextMessage, OperationTarget, ReviewQueue, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now, gateway
from app.services.account_capacity import account_capacity_decision
from app.services.content_filters import filter_outbound_content, rewrite_rejected_content
from app.services.developer_apps import credentials_for_account
from app.services.ai_config import get_scheduling_setting

from .account_pool import select_task_accounts
from .payloads import LikeMessagePayload, PostCommentPayload, SendMessagePayload, ViewMessagePayload, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review


def dispatch_action(session: Session, action: Action) -> bool:
    if _legacy_review_enabled() and has_pending_review(session, action.id):
        return False
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用", auto_check="拦截", validation_stage="account")
        return True
    account = _account_after_global_policy(session, action, account)
    if account is None:
        return True
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
        credentials = credentials_for_account(session, account)
        if action.action_type == "send_message":
            return _dispatch_send_message(session, action, account, credentials, payload)
        if action.action_type == "view_message":
            return _dispatch_view(action, account, credentials, session, payload)
        if action.action_type == "like_message":
            return _dispatch_like(action, account, credentials, session, payload)
        if action.action_type == "post_comment":
            return _dispatch_comment(action, account, credentials, session, payload)
        _fail(action, FailureType.UNKNOWN.value, f"未知 action_type: {action.action_type}")
        return True
    except (ValidationError, ValueError) as exc:
        _fail(action, FailureType.UNKNOWN.value, payload_error_message(exc))
        return True
    except Exception as exc:  # noqa: BLE001 - worker must keep draining.
        _fail(action, FailureType.UNKNOWN.value, str(exc))
        return True


def due_actions(session: Session, limit: int = 100, *, exclude_task_ids: set[str] | None = None) -> list[Action]:
    pending_review_exists = None
    if _legacy_review_enabled():
        pending_review_exists = (
            select(ReviewQueue.id)
            .where(ReviewQueue.action_id == Action.id, ReviewQueue.status == "pending")
            .exists()
        )
    filters = [
        Action.status == "pending",
        Action.scheduled_at <= _now(),
        Task.status == "running",
    ]
    if exclude_task_ids:
        filters.append(Action.task_id.not_in(exclude_task_ids))
    if pending_review_exists is not None:
        filters.append(~pending_review_exists)
    return list(
        session.scalars(
            select(Action)
            .join(Task, Task.id == Action.task_id)
            .where(*filters)
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(limit)
        )
    )


def _legacy_review_enabled() -> bool:
    return get_settings().enable_legacy_review_dispatch_gate


def _dispatch_send_message(session: Session, action: Action, account: TgAccount, credentials, payload: SendMessagePayload) -> bool:
    group_id = payload.group_id
    content = payload.message_text
    if group_id:
        group = session.get(TgGroup, group_id)
        if not group:
            _fail(action, FailureType.PEER_INVALID.value, "目标群不存在", auto_check="拦截", validation_stage="target")
            return True
        link = session.scalar(
            select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id, TgGroupAccount.can_send.is_(True))
        )
        if not link:
            _fail_with_policy(
                action,
                FailureType.ACCOUNT_UNAVAILABLE.value,
                "该账号不可向此群发送",
                auto_check="拦截",
                validation_stage="account_target_permission",
            )
            return True
        failure_type, failure_detail = validate_group_send_policy(session, tenant_id=action.tenant_id, group=group, content=content, review_approved=payload.review_approved)
        if failure_type:
            _fail_with_policy(action, failure_type, failure_detail or failure_type, auto_check="拦截", validation_stage="content_policy")
            return True
        filtered = filter_outbound_content(session, tenant_id=action.tenant_id, group=group, content=content)
        if not filtered.ok:
            _fail_with_policy(action, FailureType.CONTENT_REJECTED.value, filtered.reason, auto_check="拦截", validation_stage="content_policy")
            return True
        if _context_expired(session, payload):
            _skip(action, "context_expired", "上下文已过期，跳过本轮剩余发言")
            return True
        account_id = account.id
        group_peer = group.tg_peer_id
        group_pk = group.id
        session_ciphertext = account.session_ciphertext
        _mark_executing(action)
        session.commit()
        result = gateway.send_message(
            account_id,
            group_pk,
            content,
            [OutboundSegment(segment_type="文本", content=content)],
            session_ciphertext,
            group_peer,
            credentials,
        )
        _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
        if result.ok:
            link.last_sent_at = _now()
        return True
    target_peer = payload.chat_id
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    _mark_executing(action)
    session.commit()
    result = gateway.send_message_to_target(account_id, target_peer, content, "channel", None, session_ciphertext, credentials)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
    return True


def _dispatch_view(action: Action, account: TgAccount, credentials, session: Session, payload: ViewMessagePayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    _mark_executing(action)
    session.commit()
    result = gateway.view_channel_message(account_id, channel_peer, message_id, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail)
    return True


def _dispatch_like(action: Action, account: TgAccount, credentials, session: Session, payload: LikeMessagePayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    reaction = payload.reaction_emoji
    _mark_executing(action)
    session.commit()
    result = gateway.send_channel_reaction(account_id, channel_peer, message_id, reaction, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail)
    return True


def _dispatch_comment(action: Action, account: TgAccount, credentials, session: Session, payload: PostCommentPayload) -> bool:
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    content = payload.comment_text
    _mark_executing(action)
    session.commit()
    result = gateway.reply_channel_message(account_id, channel_peer, message_id, content, session_ciphertext, credentials, reply_to_message_id=payload.reply_to_message_id)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "")
    return True


def _apply_operation_result(action: Action, account: TgAccount, ok: bool, failure_type: str = "", detail: str = "") -> None:
    _apply_send_result(action, account, ok, "", failure_type, detail)


def _apply_send_result(action: Action, account: TgAccount, ok: bool, remote_id: str = "", failure_type: str = "", detail: str = "") -> None:
    if ok:
        action.status = "success"
        action.result = {**(action.result or {}), "success": True, "telegram_msg_id": remote_id, "auto_check": "通过", "validation_stage": "sent"}
        _clear_action_lease(action)
        account.last_active_at = _now()
    else:
        _fail(action, failure_type or FailureType.UNKNOWN.value, detail or "执行失败", auto_check="失败", validation_stage="telegram_api")
        if failure_type == FailureType.ACCOUNT_LIMITED.value:
            account.status = AccountStatus.LIMITED.value
            account.health_score = min(account.health_score, 55)
        _apply_default_failure_policy(action, failure_type or FailureType.UNKNOWN.value)
    action.executed_at = None if action.status == "pending" else _now()


def _fail(action: Action, failure_type: str, detail: str, *, auto_check: str = "失败", validation_stage: str = "") -> None:
    action.status = "failed"
    _clear_action_lease(action)
    action.result = {
        "success": False,
        "error_code": failure_type,
        "error_message": detail,
        "auto_check": auto_check,
        "validation_stage": validation_stage,
    }
    action.executed_at = _now()


def _fail_with_policy(action: Action, failure_type: str, detail: str, *, auto_check: str = "失败", validation_stage: str = "") -> None:
    _fail(action, failure_type, detail, auto_check=auto_check, validation_stage=validation_stage)
    _apply_default_failure_policy(action, failure_type)


def _skip(action: Action, code: str, detail: str) -> None:
    action.status = "skipped"
    _clear_action_lease(action)
    action.result = {"success": False, "error_code": code, "error_message": detail, "auto_check": "跳过", "validation_stage": "context"}
    action.executed_at = _now()


def _defer(action: Action, scheduled_at, code: str, detail: str) -> None:
    action.status = "pending"
    action.scheduled_at = scheduled_at
    _clear_action_lease(action)
    action.result = {"success": False, "error_code": code, "error_message": detail, "auto_check": "延后", "validation_stage": "account_policy"}


def _account_after_global_policy(session: Session, action: Action, account: TgAccount) -> TgAccount | None:
    decision = account_capacity_decision(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        scheduled_at=action.scheduled_at,
        exclude_action_id=action.id,
    )
    if decision.available:
        return account
    replacement = _replacement_account_for_action(session, action, account)
    if replacement:
        action.result = {
            **(action.result or {}),
            "auto_check": "转派",
            "validation_stage": "account_policy",
            "account_policy_action": "reassigned",
            "account_policy_reason": decision.reason,
            "original_account_id": account.id,
            "reassigned_account_id": replacement.id,
        }
        action.account_id = replacement.id
        return replacement
    _defer(
        action,
        decision.defer_until or (_now() + timedelta(seconds=60)),
        "global_account_policy",
        decision.reason or "账号全局限额或冷却中，已延后执行",
    )
    return None


def _replacement_account_for_action(session: Session, action: Action, account: TgAccount) -> TgAccount | None:
    task = session.get(Task, action.task_id)
    if not task:
        return None
    payload = action.payload if isinstance(action.payload, dict) else {}
    group_id = int(payload.get("group_id") or 0) or None
    candidates = select_task_accounts(
        session,
        action.tenant_id,
        task.account_config or {},
        target_group_id=group_id,
        scheduled_at=action.scheduled_at,
        limit=10,
    )
    return next((candidate for candidate in candidates if candidate.id != account.id), None)


def _mark_executing(action: Action, *, lease_seconds: int = 1800) -> None:
    action.status = "executing"
    action.lease_owner = _lease_owner()
    action.lease_expires_at = _now() + timedelta(seconds=max(60, int(lease_seconds or 1800)))


def _clear_action_lease(action: Action) -> None:
    action.lease_owner = ""
    action.lease_expires_at = None


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _apply_default_failure_policy(action: Action, failure_type: str) -> None:
    task = action and getattr(action, "task", None)
    # Action is usually loaded without relationship state in this project, so look up via the attached session.
    from sqlalchemy.orm import object_session

    session = object_session(action)
    task = session.get(Task, action.task_id) if session and action.task_id else task
    if not session or not task:
        return
    setting = get_scheduling_setting(session, action.tenant_id)
    stats = dict(task.stats or {})
    if failure_type in {FailureType.ACCOUNT_LIMITED.value, FailureType.ACCOUNT_UNAVAILABLE.value}:
        policy = setting.default_on_account_banned
        stats["last_failure_policy"] = policy
        if policy == "pause_task":
            _pause_task(task, action, failure_type)
        elif policy == "stop_task":
            _stop_task(session, task, action, failure_type)
    elif failure_type in {FailureType.FLOOD_WAIT.value, FailureType.SLOWMODE.value}:
        policy = setting.default_on_api_rate_limit
        stats["last_failure_policy"] = policy
        if policy == "pause":
            _pause_task(task, action, failure_type)
        elif policy == "wait_and_retry":
            retry_after = _retry_after_seconds(action.result.get("error_message") or "")
            if retry_after > 0:
                action.status = "pending"
                action.scheduled_at = _now() + timedelta(seconds=retry_after)
                _clear_action_lease(action)
                action.result = {
                    **(action.result or {}),
                    "auto_check": "延后",
                    "validation_stage": "failure_policy",
                    "retry_after_seconds": retry_after,
                }
    elif failure_type == FailureType.CONTENT_REJECTED.value:
        policy = setting.default_on_content_rejected
        stats["last_failure_policy"] = policy
        if policy == "pause":
            _pause_task(task, action, failure_type)
        elif policy == "rewrite_and_retry":
            rewritten = _rewrite_rejected_action_content(session, action)
            if rewritten:
                action.status = "pending"
                action.scheduled_at = _now() + timedelta(seconds=max(1, int(setting.default_retry_delay_seconds or 1)))
                action.executed_at = None
                action.retry_count = int(action.retry_count or 0) + 1
                _clear_action_lease(action)
                action.result = {
                    **(action.result or {}),
                    "validation_stage": "failure_policy",
                    "failure_policy_action": "rewrite_and_retry",
                    "auto_check": "延后",
                    "rewritten_content_preview": rewritten[:120],
                }
            else:
                task.last_error = action.result.get("error_message") or failure_type
                action.result = {
                    **(action.result or {}),
                    "validation_stage": "failure_policy",
                    "failure_policy_action": "rewrite_and_retry_required",
                    "auto_check": "待改写",
                }
    task.stats = stats


def _rewrite_rejected_action_content(session: Session, action: Action) -> str:
    payload = dict(action.payload or {})
    group_id = payload.get("group_id")
    content = str(payload.get("message_text") or "")
    group = session.get(TgGroup, group_id) if group_id else None
    if not group or not content:
        return ""
    rewritten = rewrite_rejected_content(session, tenant_id=action.tenant_id, group=group, content=content)
    if not rewritten.ok or not rewritten.content:
        return ""
    payload["message_text"] = rewritten.content
    action.payload = payload
    return rewritten.content


def _pause_task(task: Task, action: Action, failure_type: str) -> None:
    task.status = "paused"
    task.next_run_at = None
    task.last_error = action.result.get("error_message") or failure_type


def _stop_task(session: Session, task: Task, action: Action, failure_type: str) -> None:
    task.status = "stopped"
    task.next_run_at = None
    task.last_error = action.result.get("error_message") or failure_type
    for pending in session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending", Action.id != action.id)):
        pending.status = "skipped"
        pending.executed_at = _now()
        _clear_action_lease(pending)
        pending.result = {
            "success": False,
            "error_code": "task_stopped_by_failure_policy",
            "error_message": "任务已按默认失败策略停止，待执行项已跳过",
            "auto_check": "跳过",
            "validation_stage": "failure_policy",
        }


def _retry_after_seconds(detail: str) -> int:
    import re

    match = re.search(r"(\d+)", detail or "")
    if not match:
        return 0
    return max(1, min(24 * 60 * 60, int(match.group(1))))


def _context_expired(session: Session, payload: SendMessagePayload) -> bool:
    if not payload.cycle_id or not payload.group_id or not payload.context_snapshot_message_id or payload.context_expire_after_messages <= 0:
        return False
    newer_count = session.scalar(
        select(func.count(GroupContextMessage.id)).where(
            GroupContextMessage.group_id == payload.group_id,
            GroupContextMessage.id > payload.context_snapshot_message_id,
        )
    ) or 0
    return int(newer_count) >= payload.context_expire_after_messages


__all__ = ["dispatch_action", "due_actions"]
