from __future__ import annotations

import os
import re
import socket
from dataclasses import dataclass
from uuid import uuid4
from datetime import datetime, timedelta

from sqlalchemy import case, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.integrations.telegram import OperationResult, OutboundSegment
from app.config import get_settings
from app.models import AccountStatus, Action, ChannelMessage, ExecutionAttempt, FailureType, GroupAuthStatus, GroupContextMessage, OperationTarget, ReviewQueue, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.services._common import _now, audit, gateway
from app.services.account_authorizations import attempt_primary_proxy_recovery, attempt_standby_authorization_recovery
from app.services.account_capacity import account_capacity_decision
from app.services.content_filters import filter_outbound_content, rewrite_rejected_content
from app.services.developer_apps import credentials_for_account
from app.services.ai_config import get_scheduling_setting
from app.services.membership_challenges import auto_resolve_image_verification, auto_resolve_text_verification, read_challenge_context_with_fallback, record_challenge_attempt
from app.services.required_channel_prompts import (
    REQUIRED_CHANNEL_BLOCKED_LABEL,
    REQUIRED_CHANNEL_PERMISSION_LABEL,
    REQUIRED_CHANNEL_PROMPT_PREVIEW_LENGTH,
    required_channel_prompt_applies_to_send,
    required_channel_references,
)
from app.services.verification import create_verification_task
from app.timezone import BEIJING_TZ

from .account_pool import account_matches_current_shard, current_account_shard, select_task_accounts
from .ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable, generate_group_messages
from .channel_membership import account_satisfies_authorized_target, linked_channel_group, mark_channel_membership_joined
from .executors.common import quantity_jitter_bounds
from .executors.channel_comment import _resolved_total_comment_limit, _total_comment_action_count
from .group_rescue import GROUP_RESCUE_FAILURE_THRESHOLD, permission_failure_count_for_send_action, refresh_group_rescue_action, trigger_group_rescue
from .payloads import DeprecatedGroupRescuePayload, DeleteMessagePayload, EnsureChannelMembershipPayload, InviteGroupAccountPayload, LikeMessagePayload, PostCommentPayload, SendMessagePayload, ViewMessagePayload, create_membership_action, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review
from . import runtime_resources as _runtime_resources

_ACTION_RESERVATIONS = _runtime_resources._ACTION_RESERVATIONS
_IN_FLIGHT_ACCOUNTS = _runtime_resources._IN_FLIGHT_ACCOUNTS
_redis_client = _runtime_resources._redis_client
MEMBERSHIP_ACTION_TYPES = ("ensure_channel_membership", "ensure_target_membership")
_COMMENT_THREAD_UNAVAILABLE_FAILURES = {FailureType.COMMENT_UNAVAILABLE.value}
_COMMENT_THREAD_SKIP_CODES = {
    FailureType.COMMENT_UNAVAILABLE.value: "comment_unavailable_sibling",
}
_REACTION_UNAVAILABLE_SKIP_CODE = "reaction_unavailable_sibling"
_COMMENT_MEMBERSHIP_RETRY_DELAY = timedelta(minutes=5)
_COMMENT_MEMBERSHIP_REQUIRED_MARKERS = (
    "not participant",
    "not a participant",
    "usernotparticipant",
    "未关注",
    "未加入",
    "不在目标",
    "无法进入关联讨论区",
)
_GROUP_SEND_LINKED_CHANNEL_REQUIRED_MARKERS = (
    "未关注",
    "关注",
    "follow",
    "subscribe",
    "未加入目标频道",
    "无法进入关联讨论区",
    "缓存频道不可访问",
)
_GROUP_SEND_BUTTON_VERIFICATION_MARKERS = ("按钮", "button", "click", "点击")
_GROUP_SEND_REPLY_VERIFICATION_MARKERS = ("/start", "发送验证回复", "send reply")
_GROUP_SEND_TEXT_VERIFICATION_MARKERS = (
    "验证码",
    "验证问题",
    "验证回复",
    "请输入",
    "输入",
    "算数",
    "算术",
    "加减",
    "计算",
    "结果",
    "等于",
    "captcha",
    "code",
)
_PEER_REF_INVALID_MARKERS = (
    "no user has",
    "could not find the input entity",
    "cannot find any entity",
    "cannot cast inputpeeruser to any kind of inputchannel",
    "目标实体无法解析",
    "目标群无效",
    "目标无效",
    "频道不可访问",
    "缺少频道地址",
)
_GROUP_SEND_IMAGE_VERIFICATION_MARKERS = (
    "图片验证码",
    "图形验证码",
    "验证码图片",
    "captcha image",
    "image captcha",
    "机器人验证码",
    "bot 的验证码",
)
_GROUP_SEND_RETRYABLE_VERIFICATION_MARKERS = (
    "未解析到群关联频道",
    "未获群发言权限",
    "没有群发言权限",
    "群无权限或账号不可发言",
    "不可发言",
)
RECENT_REQUIRED_CHANNEL_PROMPT_LIMIT = 25
RECENT_REQUIRED_CHANNEL_PROMPT_LOOKBACK_HOURS = 6
REQUIRED_CHANNEL_ADMISSION_RETRY_SECONDS = 300
VERIFICATION_READER_CANDIDATE_LIMIT = 5
HARD_HOURLY_OVERDUE_SEND_PRIORITY_SECONDS = 300
AI_DISPATCH_GENERATION_BATCH_SIZE = 10
_ACCOUNT_SESSION_FAILURE_MARKERS = (
    "session",
    "auth key",
    "auth_key",
    "unauthorized",
    "重新登录",
    "账号没有可用 session",
    "session 已失效",
)
_ACCOUNT_PROXY_FAILURE_MARKERS = (
    "proxy",
    "代理",
    "connect",
    "connection",
    "timeout",
    "timed out",
    "unreachable",
    "network",
)
_ACCOUNT_FROZEN_FAILURE_MARKERS = (
    "frozen account",
    "frozen accounts",
    "not available for frozen accounts",
)
FROZEN_ACCOUNT_HEALTH_SCORE = 20


@dataclass(frozen=True)
class MembershipDispatchContext:
    session: Session
    action: Action
    account: TgAccount
    credentials: object
    payload: EnsureChannelMembershipPayload
    attempt: ExecutionAttempt | None


@dataclass(frozen=True)
class PreSendRequiredChannelContext:
    session: Session
    action: Action
    account: TgAccount
    credentials: object
    group: TgGroup
    link: TgGroupAccount
    payload: SendMessagePayload


@dataclass(frozen=True)
class InviteGroupAccountContext:
    session: Session
    account: TgAccount
    credentials: object
    payload: InviteGroupAccountPayload


def _reserve_runtime_resources(action: Action) -> bool:
    _runtime_resources.get_settings = get_settings
    _runtime_resources._redis_client = _redis_client
    return _runtime_resources._reserve_runtime_resources(action)


def _release_runtime_resources(action: Action) -> None:
    _runtime_resources.get_settings = get_settings
    _runtime_resources._redis_client = _redis_client
    _runtime_resources._release_runtime_resources(action)


def dispatch_action(session: Session, action: Action) -> bool:
    if _legacy_review_enabled() and has_pending_review(session, action.id):
        return False
    if _skip_expired_hard_hourly_action(session, action):
        return True
    if action.action_type == "invite_group_bot" and not _migrate_deprecated_group_rescue_action(session, action):
        return True
    if action.action_type == "invite_group_account" and not _refresh_stale_invite_group_account_action(session, action):
        return True
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用", auto_check="拦截", validation_stage="account")
        return True
    if _is_reserved_rescue_admin_action(session, action, account):
        _skip(action, "rescue_admin_reserved", "救援管理员账号只允许执行群聊救援动作，不参与普通任务发送、点赞、评论或准入")
        return True
    can_reassign = action.status != "executing" and not _is_membership_action(action) and action.action_type not in {"invite_group_bot", "invite_group_account"}
    account = _account_after_global_policy(session, action, account, allow_reassign=can_reassign)
    if account is None:
        return True
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
        if action.action_type in {"view_message", "like_message", "post_comment"} and not _ensure_channel_action_membership(session, action, account, payload.channel_target_id):
            return True
        credentials = credentials_for_account(session, account)
        if action.action_type in {"ensure_channel_membership", "ensure_target_membership"}:
            return _dispatch_channel_membership(session, action, account, credentials, payload)
        if action.action_type == "send_message":
            return _dispatch_send_message(session, action, account, credentials, payload)
        if action.action_type == "delete_message":
            return _dispatch_delete_message(session, action, account, credentials, payload)
        if action.action_type == "invite_group_account":
            return _dispatch_invite_group_account(session, action, account, credentials, payload)
        if action.action_type == "view_message":
            return _dispatch_view(action, account, credentials, session, payload)
        if action.action_type == "like_message":
            return _dispatch_like(action, account, credentials, session, payload)
        if action.action_type == "post_comment":
            return _dispatch_comment(action, account, credentials, session, payload)
        _fail(action, FailureType.UNKNOWN.value, f"未知 action_type: {action.action_type}")
        return True
    except (ValidationError, ValueError) as exc:
        _update_reply_payload_error_stats(action)
        _fail(action, FailureType.UNKNOWN.value, payload_error_message(exc))
        return True
    except Exception as exc:  # noqa: BLE001 - worker must keep draining.
        if _gateway_call_started(session, action):
            _mark_unknown_after_send(session, action, str(exc))
        else:
            _fail(action, FailureType.UNKNOWN.value, str(exc))
        return True


def _is_reserved_rescue_admin_action(session: Session, action: Action, account: TgAccount) -> bool:
    if action.action_type in {"invite_group_bot", "invite_group_account"}:
        return False
    tenant = session.get(Tenant, action.tenant_id)
    return bool(tenant and tenant.group_rescue_admin_account_id and int(tenant.group_rescue_admin_account_id) == int(account.id))


def _migrate_deprecated_group_rescue_action(session: Session, action: Action) -> bool:
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
    except (ValidationError, ValueError) as exc:
        _fail(action, FailureType.UNKNOWN.value, payload_error_message(exc), validation_stage="rescue")
        return False
    if not isinstance(payload, DeprecatedGroupRescuePayload):
        _fail(action, FailureType.UNKNOWN.value, "旧群聊救援动作格式异常", validation_stage="rescue")
        return False
    task = session.get(Task, action.task_id) if action.task_id else None
    group = _deprecated_group_rescue_group(session, action, payload)
    if not task or not group:
        _fail(action, FailureType.PEER_INVALID.value, "旧群聊救援动作缺少目标群，无法迁移为账号邀请救援", validation_stage="rescue")
        return False
    if not payload.trigger_account_id:
        _fail(action, FailureType.ACCOUNT_UNAVAILABLE.value, "旧群聊救援动作缺少触发账号，无法迁移为账号邀请救援", validation_stage="rescue")
        return False
    result = refresh_group_rescue_action(
        session=session,
        task=task,
        group=group,
        action=action,
        trigger_account_id=payload.trigger_account_id,
        trigger_reason=payload.trigger_reason or "旧群聊救援动作迁移",
        operation_target_id=payload.operation_target_id,
    )
    if not result.action:
        _fail(action, FailureType.UNKNOWN.value, result.detail or "旧群聊救援动作迁移失败", validation_stage="rescue")
        return False
    return True


def _refresh_stale_invite_group_account_action(session: Session, action: Action) -> bool:
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
    except (ValidationError, ValueError) as exc:
        _fail(action, FailureType.UNKNOWN.value, payload_error_message(exc), validation_stage="rescue")
        return False
    tenant = session.get(Tenant, action.tenant_id)
    if not tenant or not tenant.group_rescue_admin_account_id:
        _fail(action, FailureType.UNKNOWN.value, "救援配置缺失：未选择救援管理员账号", validation_stage="rescue")
        return False
    if int(action.account_id or 0) == int(tenant.group_rescue_admin_account_id):
        return True
    task = session.get(Task, action.task_id) if action.task_id else None
    group = session.get(TgGroup, payload.group_id) if payload.group_id else None
    if not task or not group:
        _fail(action, FailureType.PEER_INVALID.value, "群聊救援动作缺少目标群，无法刷新执行账号", validation_stage="rescue")
        return False
    result = refresh_group_rescue_action(
        session=session,
        task=task,
        group=group,
        action=action,
        trigger_account_id=payload.trigger_account_id or payload.target_account_id,
        trigger_reason=payload.trigger_reason or "刷新群聊救援执行账号",
        operation_target_id=payload.operation_target_id,
    )
    if not result.action:
        _fail(action, FailureType.UNKNOWN.value, result.detail or "群聊救援执行账号刷新失败", validation_stage="rescue")
        return False
    return True


def _deprecated_group_rescue_group(session: Session, action: Action, payload: DeprecatedGroupRescuePayload) -> TgGroup | None:
    if payload.group_id:
        group = session.get(TgGroup, payload.group_id)
        if group and group.tenant_id == action.tenant_id:
            return group
    peer_id = payload.group_peer_id.strip()
    if payload.operation_target_id:
        target = session.get(OperationTarget, payload.operation_target_id)
        if target and target.tenant_id == action.tenant_id:
            peer_id = peer_id or target.tg_peer_id
    if not peer_id:
        return None
    return session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.tg_peer_id == peer_id))


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
            .order_by(_hard_hourly_claim_rank(), Task.priority.asc(), Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(limit)
        )
    )


def claim_actions(session: Session, limit: int = 100, *, exclude_task_ids: set[str] | None = None, worker_id: str | None = None) -> list[Action]:
    """Two-stage claim for dispatcher workers.

    The first transaction marks rows as ``claiming`` and releases DB locks before
    any runtime resource checks. The second transaction confirms rows as
    ``executing`` only after account in-flight and rate-limit reservations pass.
    """

    settings = get_settings()
    configured_limit = _setting(settings, "action_claim_limit", 100)
    claim_limit = max(1, min(int(limit or configured_limit or 100), int(configured_limit or limit or 100)))
    owner = worker_id or _lease_owner()
    token = str(uuid4())
    now_value = _now()
    pending_review_exists = None
    if _legacy_review_enabled():
        pending_review_exists = (
            select(ReviewQueue.id)
            .where(ReviewQueue.action_id == Action.id, ReviewQueue.status == "pending")
            .exists()
        )
    filters = [
        Action.status == "pending",
        Action.scheduled_at <= now_value,
        Task.status == "running",
        Task.deleted_at.is_(None),
    ]
    shard_total, shard_index = current_account_shard()
    if shard_total > 1:
        filters.append(or_(Action.account_id.is_(None), (Action.account_id % shard_total) == shard_index))
    if exclude_task_ids:
        filters.append(Action.task_id.not_in(exclude_task_ids))
    if pending_review_exists is not None:
        filters.append(~pending_review_exists)
    stmt = (
        select(Action)
        .join(Task, Task.id == Action.task_id)
        .where(*filters)
        .order_by(_hard_hourly_claim_rank(), Task.priority.asc(), Action.scheduled_at.asc(), Action.created_at.asc())
        .limit(claim_limit)
    )
    if session.bind and session.bind.dialect.name != "sqlite":
        stmt = stmt.with_for_update(skip_locked=True)
    candidates = list(session.scalars(stmt))
    claim_until = now_value + timedelta(seconds=max(5, int(_setting(settings, "action_claim_seconds", 60) or 60)))
    for action in candidates:
        action.status = "claiming"
        action.claim_owner = owner
        action.claim_token = token
        action.claim_expires_at = claim_until
        action.result = {**(action.result or {}), "claim_owner": owner, "claim_token": token}
    session.commit()

    confirmed_ids: list[str] = []
    for candidate in candidates:
        action = session.get(Action, candidate.id)
        if not action or action.status != "claiming" or action.claim_owner != owner or action.claim_token != token:
            continue
        if _skip_expired_hard_hourly_action(session, action):
            session.commit()
            continue
        if not _apply_claim_account_policy(session, action):
            session.commit()
            continue
        if not _reserve_runtime_resources(action):
            result = action.result or {}
            delay_seconds = int(result.get("rate_limit_wait_seconds") or result.get("runtime_resource_wait_seconds") or 0)
            _release_claim(action, delay_seconds=delay_seconds, reason=str(result.get("runtime_resource_reason") or "runtime_resource_unavailable"))
            session.commit()
            continue
        action_id = action.id
        try:
            if _confirm_claim(session, action.id, owner, token):
                session.commit()
                confirmed_ids.append(action_id)
            else:
                _release_runtime_resources(action)
                session.rollback()
        except IntegrityError:
            session.rollback()
            _release_runtime_resources(action)
            action = session.get(Action, action_id)
            if action and action.status == "claiming" and action.claim_owner == owner and action.claim_token == token:
                _release_claim(action, delay_seconds=1, reason="account_inflight_conflict")
                session.commit()
    return [action for action in (session.get(Action, action_id) for action_id in confirmed_ids) if action]


def _hard_hourly_claim_rank():
    hard_hourly_membership = (
        Task.type_config["hard_hourly_target_enabled"].as_boolean().is_(True)
        & Action.action_type.in_(MEMBERSHIP_ACTION_TYPES)
    )
    hard_hourly_send = Action.payload["hard_hourly_target"].as_boolean().is_(True)
    overdue_hard_hourly_send = hard_hourly_send & (Action.scheduled_at <= _now() - timedelta(seconds=HARD_HOURLY_OVERDUE_SEND_PRIORITY_SECONDS))
    return case(
        (overdue_hard_hourly_send, 0),
        (hard_hourly_membership, 0),
        (hard_hourly_send, 1),
        else_=2,
    )


def recover_expired_claims(session: Session) -> int:
    now_value = _now()
    rows = list(session.scalars(select(Action).where(Action.status == "claiming", Action.claim_expires_at <= now_value).order_by(Action.scheduled_at.asc())))
    for action in rows:
        _release_claim(action, delay_seconds=0, reason="claim_expired")
    return len(rows)


def recover_expired_hard_hourly_actions(session: Session, limit: int = 100) -> int:
    rows = list(
        session.scalars(
            select(Action)
            .where(
                Action.task_type == "group_ai_chat",
                Action.action_type == "send_message",
                Action.status.in_(["pending", "claiming"]),
                Action.payload["hard_hourly_target"].as_boolean().is_(True),
            )
            .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
            .limit(max(1, int(limit or 100)))
        )
    )
    recovered = 0
    for action in rows:
        if not _hard_hourly_bucket_expired(action):
            continue
        _skip(action, "hard_hourly_bucket_expired", "硬目标小时窗口已结束，过期补量已跳过")
        recovered += 1
    return recovered


def _confirm_claim(session: Session, action_id: str, owner: str, token: str) -> bool:
    action = session.get(Action, action_id)
    if not action or action.status != "claiming" or action.claim_owner != owner or action.claim_token != token:
        return False
    _mark_executing(action, lease_seconds=_setting(get_settings(), "action_lease_seconds", 1800))
    action.result = {**(action.result or {}), "claim_confirmed_at": _now().isoformat()}
    session.flush()
    return True


def _release_claim(action: Action, *, delay_seconds: int, reason: str) -> None:
    action.status = "pending"
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None
    action.scheduled_at = _now() + timedelta(seconds=max(0, int(delay_seconds or 0)))
    action.result = {**(action.result or {}), "claim_released_reason": reason, "claim_released_at": _now().isoformat()}


def _apply_claim_account_policy(session: Session, action: Action) -> bool:
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用", auto_check="拦截", validation_stage="account")
        return False
    if not account_matches_current_shard(account.id):
        _release_claim(action, delay_seconds=30, reason="account_shard_mismatch")
        action.result = {
            **(action.result or {}),
            "runtime_resource_reason": "account_shard_mismatch",
            "shard_total": current_account_shard()[0],
            "shard_index": current_account_shard()[1],
        }
        return False
    replacement = _replacement_for_lost_group_send_permission(session, action, account)
    if replacement:
        action.result = {
            **(action.result or {}),
            "auto_check": "转派",
            "validation_stage": "account_target_permission",
            "account_policy_action": "reassigned",
            "account_policy_reason": "account_target_permission_unavailable",
            "original_account_id": account.id,
            "reassigned_account_id": replacement.id,
        }
        action.account_id = replacement.id
        return True
    if _is_hard_hourly_membership_action(session, action):
        return True
    if _is_hard_hourly_send_action(action):
        _record_hard_hourly_capacity_override(action)
        return True
    decision = account_capacity_decision(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        scheduled_at=_capacity_check_at(action),
        exclude_action_ids=_capacity_excluded_action_ids(session, action, account.id),
    )
    if decision.available:
        return True
    replacement = _replacement_account_for_action(session, action, account) if not _is_membership_action(action) else None
    if replacement:
        action.result = {
            **(action.result or {}),
            "auto_check": "转派",
            "validation_stage": "claim_account_policy",
            "account_policy_action": "reassigned",
            "account_policy_reason": decision.reason,
            "original_account_id": account.id,
            "reassigned_account_id": replacement.id,
        }
        action.account_id = replacement.id
        return True
    _defer(
        action,
        decision.defer_until or (_now() + timedelta(seconds=60)),
        "global_account_policy",
        decision.reason or "账号全局限额或冷却中，已延后执行",
    )
    return False


def _is_membership_action(action: Action) -> bool:
    return action.action_type in MEMBERSHIP_ACTION_TYPES


def _is_hard_hourly_membership_action(session: Session, action: Action) -> bool:
    if not _is_membership_action(action):
        return False
    task = session.get(Task, action.task_id) if action.task_id else None
    config = task.type_config if task and isinstance(task.type_config, dict) else {}
    return (
        task is not None
        and task.type == "group_ai_chat"
        and bool(config.get("hard_hourly_target_enabled"))
        and int(config.get("hourly_min_messages") or 0) > 0
    )


def _replacement_for_lost_group_send_permission(session: Session, action: Action, account: TgAccount) -> TgAccount | None:
    group_id = _action_group_id(action)
    if not group_id or _account_can_send_group(session, action, account.id, group_id):
        return None
    return _replacement_account_for_action(session, action, account)


def _action_group_id(action: Action) -> int | None:
    if action.action_type != "send_message":
        return None
    payload = action.payload if isinstance(action.payload, dict) else {}
    group_id = int(payload.get("group_id") or 0)
    return group_id or None


def _account_can_send_group(session: Session, action: Action, account_id: int, group_id: int) -> bool:
    return bool(
        session.scalar(
            select(TgGroupAccount.id).where(
                TgGroupAccount.tenant_id == action.tenant_id,
                TgGroupAccount.group_id == group_id,
                TgGroupAccount.account_id == account_id,
                TgGroupAccount.can_send.is_(True),
            )
        )
    )




def _legacy_review_enabled() -> bool:
    return _setting(get_settings(), "enable_legacy_review_dispatch_gate", False)


def _setting(settings, name: str, default):
    return getattr(settings, name, default)


def _ensure_send_message_content(session: Session, action: Action, account: TgAccount, payload: SendMessagePayload) -> SendMessagePayload:
    if payload.message_text.strip():
        return payload
    if payload.ai_generation_status != "pending":
        raise AiGenerationUnavailable("send_message action 缺少可发送文案")
    task = session.get(Task, action.task_id) if action.task_id else None
    if not task:
        raise AiGenerationUnavailable("AI 生成缺少任务配置")
    batch = _pending_ai_generation_batch(session, action, payload)
    contents, tokens = generate_group_messages(
        session,
        action.tenant_id,
        _runtime_group_ai_config(task, batch),
        count=len(batch),
        target_label=payload.target_display,
        history=payload.ai_generation_history,
    )
    _store_generated_send_payloads(batch, contents, tokens)
    refreshed = SendMessagePayload.model_validate(action.payload or {})
    if not refreshed.message_text.strip():
        raise AiGenerationUnavailable(AI_GENERATION_UNAVAILABLE_MESSAGE)
    return refreshed


def _pending_ai_generation_batch(session: Session, action: Action, payload: SendMessagePayload) -> list[tuple[Action, SendMessagePayload]]:
    rows: list[tuple[Action, SendMessagePayload]] = [(action, payload)]
    sibling_limit = AI_DISPATCH_GENERATION_BATCH_SIZE - 1
    if sibling_limit <= 0:
        return rows
    generation_id = str(payload.ai_generation_id or "").strip()
    if not generation_id:
        return rows
    siblings = session.scalars(_pending_ai_generation_sibling_query(action, generation_id, sibling_limit))
    for sibling in siblings:
        rows.append((sibling, SendMessagePayload.model_validate(sibling.payload or {})))
    return rows


def _pending_ai_generation_sibling_query(action: Action, generation_id: str, limit: int):
    return (
        select(Action)
        .where(
            Action.id != action.id,
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.action_type == "send_message",
            Action.status == "pending",
            Action.payload["ai_generation_status"].as_string() == "pending",
            Action.payload["ai_generation_id"].as_string() == generation_id,
        )
        .order_by(Action.scheduled_at.asc(), Action.created_at.asc())
        .limit(max(1, int(limit or 1)))
    )


def _runtime_group_ai_config(task: Task, batch: list[tuple[Action, SendMessagePayload]]) -> dict:
    config = dict(task.type_config or {})
    config["account_personas"] = _payload_map(batch, "account_role")
    config["account_memories"] = _payload_map(batch, "account_memory")
    config["account_profiles"] = _payload_map(batch, "account_profile")
    first_payload = batch[0][1]
    if first_payload.topic_thread:
        config["topic_thread"] = first_payload.topic_thread
    if first_payload.topic_plan:
        config["topic_plan"] = first_payload.topic_plan
    return config


def _payload_map(batch: list[tuple[Action, SendMessagePayload]], attr: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for action, payload in batch:
        value = str(getattr(payload, attr) or "").strip()
        if value and action.account_id:
            values[str(action.account_id)] = value
    return values


def _store_generated_send_payloads(batch: list[tuple[Action, SendMessagePayload]], contents: list[str], tokens: int) -> None:
    for index, ((action, payload), content) in enumerate(zip(batch, contents, strict=False)):
        payload_data = payload.model_dump(mode="json")
        payload_data["message_text"] = str(content or "").strip()
        payload_data["ai_generation_status"] = "success"
        payload_data["ai_generation_tokens"] = int(tokens or 0) if index == 0 else 0
        action.payload = payload_data


def _dispatch_send_message(session: Session, action: Action, account: TgAccount, credentials, payload: SendMessagePayload) -> bool:
    group_id = payload.group_id
    if group_id:
        group = session.get(TgGroup, group_id)
        if not group:
            _fail(action, FailureType.PEER_INVALID.value, "目标群不存在", auto_check="拦截", validation_stage="target")
            return True
        try:
            payload = _ensure_send_message_content(session, action, account, payload)
        except AiGenerationUnavailable as exc:
            _fail_with_policy(action, FailureType.UNKNOWN.value, str(exc) or AI_GENERATION_UNAVAILABLE_MESSAGE, auto_check="失败", validation_stage="ai_generation")
            return True
        content = payload.message_text
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id))
        if not link or not link.can_send:
            if link and _defer_send_for_required_channel_admission(action, link):
                return True
            _fail_with_policy(
                action,
                FailureType.ACCOUNT_UNAVAILABLE.value,
                "该账号不可向此群发送",
                auto_check="拦截",
                validation_stage="account_target_permission",
            )
            return True
        prompt_ctx = PreSendRequiredChannelContext(session, action, account, credentials, group, link, payload)
        if _recover_pre_send_required_channel_prompt(prompt_ctx):
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
            _skip_context_expired_cycle(session, action, payload)
            _skip(action, "context_expired", "上下文已过期，跳过本轮剩余发言")
            return True
        account_id = account.id
        group_peer = group.tg_peer_id
        group_pk = group.id
        session_ciphertext = account.session_ciphertext
        attempt = _begin_execution_attempt(session, action, account)
        _mark_executing(action)
        session.commit()
        _mark_gateway_call_started(session, attempt)
        send_kwargs = {"reply_to_message_id": payload.reply_to_message_id} if payload.reply_to_message_id else {}
        result = gateway.send_message(
            account_id,
            group_pk,
            content,
            _outbound_segments(payload),
            session_ciphertext,
            group_peer,
            credentials,
            **send_kwargs,
        )
        if _recover_send_message_required_channel(session, action, account, credentials, group, payload, result, attempt):
            return True
        _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "", attempt=attempt)
        if result.ok:
            link.last_sent_at = _now()
        return True
    payload = _ensure_send_message_content(session, action, account, payload)
    content = payload.message_text
    target_peer = payload.chat_id
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.send_message_to_target(account_id, target_peer, content, "channel", None, session_ciphertext, credentials)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "", attempt=attempt)
    return True


def _dispatch_delete_message(session: Session, action: Action, account: TgAccount, credentials, payload: DeleteMessagePayload) -> bool:
    group = session.get(TgGroup, payload.group_id) if payload.group_id else None
    target_peer = group.tg_peer_id if group else payload.chat_id
    if not target_peer:
        _fail(action, FailureType.PEER_INVALID.value, "删除消息缺少目标群")
        return True
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.delete_message(
        account.id,
        target_peer,
        payload.message_id,
        account.session_ciphertext,
        credentials,
    )
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail, attempt=attempt)
    return True


def _dispatch_invite_group_account(session: Session, action: Action, account: TgAccount, credentials, payload: InviteGroupAccountPayload) -> bool:
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.invite_account_to_group(
        account.id,
        payload.group_peer_id,
        payload.target_account_ref,
        account.session_ciphertext,
        credentials,
    )
    if not result.ok and _requires_invite_link_join(result):
        result = _invite_group_account_with_link(InviteGroupAccountContext(session, account, credentials, payload))
    if result.ok:
        _mark_rescued_group_account_joined(session, action, payload)
    _apply_rescue_invite_result(action, account, result, attempt=attempt)
    return True


def _requires_invite_link_join(result: OperationResult) -> bool:
    detail = f"{result.failure_type or ''} {result.detail or ''}".lower()
    return "not a mutual contact" in detail or "not mutual contact" in detail


def _invite_group_account_with_link(ctx: InviteGroupAccountContext) -> OperationResult:
    target = ctx.session.get(TgAccount, ctx.payload.target_account_id or 0)
    if not target or target.status != AccountStatus.ACTIVE.value or not target.session_ciphertext:
        return OperationResult(False, "失败", FailureType.ACCOUNT_UNAVAILABLE.value, "被救援账号不可用，无法通过邀请链接入群")
    link = gateway.export_group_invite_link(ctx.account.id, ctx.payload.group_peer_id, ctx.account.session_ciphertext, ctx.credentials)
    if not link.ok or not link.invite_link:
        return OperationResult(False, "失败", link.failure_type or "invite_link_export_failed", link.detail or "管理员导出邀请链接失败")
    joined = gateway.ensure_channel_membership(
        target.id,
        ctx.payload.group_peer_id,
        target.session_ciphertext,
        ctx.credentials,
        invite_link=link.invite_link,
    )
    if not joined.ok:
        if _invite_link_unusable(joined.detail):
            return _retry_invite_group_account_after_lifting_restrictions(ctx, target, joined.detail)
        return _rescue_invite_link_join_failure(joined.detail, joined.failure_type)
    return OperationResult(True, "已处理", detail=f"invite_link_{joined.membership_status or 'joined'}")


def _retry_invite_group_account_after_lifting_restrictions(ctx: InviteGroupAccountContext, target: TgAccount, first_detail: str) -> OperationResult:
    lifted = gateway.lift_group_account_restrictions(
        ctx.account.id,
        ctx.payload.group_peer_id,
        ctx.payload.target_account_ref,
        ctx.account.session_ciphertext,
        ctx.credentials,
    )
    if not lifted.ok:
        return OperationResult(False, "失败", lifted.failure_type or "target_group_restriction_unresolved", f"管理员解除目标账号群限制失败：{lifted.detail or lifted.failure_type}")
    link = gateway.export_group_invite_link(ctx.account.id, ctx.payload.group_peer_id, ctx.account.session_ciphertext, ctx.credentials)
    if not link.ok or not link.invite_link:
        return OperationResult(False, "失败", link.failure_type or "invite_link_export_failed", f"解除限制后导出邀请链接失败：{link.detail or link.failure_type}")
    joined = gateway.ensure_channel_membership(
        target.id,
        ctx.payload.group_peer_id,
        target.session_ciphertext,
        ctx.credentials,
        invite_link=link.invite_link,
    )
    if joined.ok:
        return OperationResult(True, "已处理", detail=f"unban_invite_link_{joined.membership_status or 'joined'}")
    return _rescue_invite_link_join_failure(joined.detail or first_detail, joined.failure_type)


def _invite_link_unusable(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return "expired" in normalized and "not valid" in normalized


def _rescue_invite_link_join_failure(detail: str, failure_type: str = "") -> OperationResult:
    text = detail or "被救援账号通过邀请链接入群失败"
    if _invite_link_unusable(text):
        return OperationResult(False, "失败", "target_invite_link_unusable", f"邀请链接对目标账号不可用，疑似账号被群限制或群邀请策略拒绝：{text}")
    return OperationResult(False, "失败", failure_type or FailureType.UNKNOWN.value, text)


def _mark_rescued_group_account_joined(session: Session, action: Action, payload: InviteGroupAccountPayload) -> None:
    if not payload.operation_target_id or not payload.target_account_id:
        return
    mark_channel_membership_joined(
        session,
        action.tenant_id,
        payload.operation_target_id,
        payload.target_account_id,
        permission_label="群聊救援已入群",
    )


def _apply_rescue_invite_result(action: Action, account: TgAccount, result: OperationResult, *, attempt: ExecutionAttempt | None) -> None:
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail, attempt=attempt)
    status = "invite_success" if result.ok else "invite_failed"
    action.result = {**(action.result or {}), "rescue_status": status, "rescue_detail": result.detail or result.failure_type}


def _dispatch_channel_membership(session: Session, action: Action, account: TgAccount, credentials, payload: EnsureChannelMembershipPayload) -> bool:
    lookup_ctx = MembershipDispatchContext(session, action, account, credentials, payload, None)
    payload, existing_group = _membership_existing_group_for_account(lookup_ctx)
    if existing_group:
        existing_link = session.scalar(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == action.tenant_id,
                TgGroupAccount.group_id == existing_group.id,
                TgGroupAccount.account_id == account.id,
            )
        )
        if existing_link and _dispatch_existing_membership(session, action, account, credentials, payload, existing_link):
            return True
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    ctx = MembershipDispatchContext(session, action, account, credentials, payload, attempt)
    result, payload, fallback_ref = _ensure_membership_with_peer_candidates(ctx)
    runtime_ctx = MembershipDispatchContext(session, action, account, credentials, payload, attempt)
    _record_membership_peer_ref(action, payload, fallback_ref)
    if result.ok:
        probe_result = _probe_joined_group_send_permission(session, action, account, credentials, payload)
        if probe_result is not None and not probe_result.ok:
            return _handle_group_send_permission_denied(runtime_ctx, probe_result, membership_status="joined", skip_on_failure=False)
        _mark_membership_joined(session, action, account, payload)
    elif result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        return _handle_group_send_permission_denied(runtime_ctx, result, membership_status="joined", skip_on_failure=True)
    result_detail = _membership_result_detail(result)
    failure_type = _classify_membership_failure(result.failure_type, result_detail)
    _apply_operation_result(action, account, result.ok, failure_type, result_detail, attempt=attempt)
    if result.ok:
        membership_status = getattr(result, "membership_status", "") or "joined"
        action.result = {**(action.result or {}), "membership_status": membership_status}
    return True


def _membership_existing_group_for_account(ctx: MembershipDispatchContext) -> tuple[EnsureChannelMembershipPayload, TgGroup | None]:
    for group in _membership_candidate_groups(ctx.session, ctx.action.tenant_id, ctx.payload):
        if _requires_group_send_probe(ctx.payload) and not _is_stable_telegram_peer(group.tg_peer_id):
            continue
        link = _channel_account_link(ctx.session, ctx.action.tenant_id, group.id, ctx.account.id)
        if link:
            return _payload_with_channel_ref(ctx.payload, group.tg_peer_id, group.title), group
    return ctx.payload, None


def _membership_result_detail(result) -> str:
    return result.detail or getattr(result, "membership_status", "")


def _ensure_membership_with_peer_candidates(ctx: MembershipDispatchContext):
    result, next_payload, fallback_ref = _ensure_membership_refs(ctx, _membership_static_refs(ctx.session, ctx.action, ctx.payload))
    if result.ok or not _membership_peer_ref_invalid(result):
        return result, next_payload, fallback_ref
    dialog_refs = _membership_dialog_refs(ctx)
    if not dialog_refs:
        return result, next_payload, fallback_ref
    return _ensure_membership_refs(ctx, dialog_refs, fallback_ref=fallback_ref)


def _ensure_membership_refs(
    ctx: MembershipDispatchContext,
    refs: list[str],
    *,
    fallback_ref: str = "",
):
    result = OperationResult(False, "失败", FailureType.PEER_INVALID.value, "缺少可用目标准入引用")
    selected_payload = ctx.payload
    for ref in _dedupe_refs(refs):
        candidate = _payload_with_channel_ref(ctx.payload, ref, ctx.payload.target_display)
        _record_membership_attempted_ref(ctx.action, ref)
        result = gateway.ensure_channel_membership(
            ctx.account.id,
            candidate.channel_id,
            ctx.account.session_ciphertext,
            ctx.credentials,
            invite_link=_membership_invite_for_ref(ctx.payload, ref),
        )
        selected_payload = candidate
        fallback_ref = fallback_ref or (ref if ref != ctx.payload.channel_id else "")
        if result.ok or not _membership_peer_ref_invalid(result):
            return result, selected_payload, fallback_ref
    return result, selected_payload, fallback_ref


def _membership_static_refs(session: Session, action: Action, payload: EnsureChannelMembershipPayload) -> list[str]:
    target = session.get(OperationTarget, payload.channel_target_id)
    candidate_refs = [group.tg_peer_id for group in _membership_candidate_groups(session, action.tenant_id, payload)]
    verified_refs = _membership_verified_peer_refs(session, action, target, payload)
    username = payload.target_username or ""
    if target and target.tenant_id == action.tenant_id:
        username = username or target.username or ""
    if payload.target_type == "group" and payload.require_send:
        return _membership_send_required_refs(payload, target, candidate_refs, verified_refs, username)
    refs = [payload.invite_link]
    if username and not (payload.target_type == "group" and payload.require_send):
        refs.extend([username, f"https://t.me/{username.lstrip('@')}"])
    refs.append(payload.channel_id)
    if target and target.tenant_id == action.tenant_id:
        refs.append(target.tg_peer_id)
    refs.extend(candidate_refs)
    refs.extend(verified_refs)
    return _dedupe_refs(refs)


def _membership_send_required_refs(
    payload: EnsureChannelMembershipPayload,
    target: OperationTarget | None,
    candidate_refs: list[str],
    verified_refs: list[str],
    username: str,
) -> list[str]:
    raw_refs = [payload.channel_id]
    if target:
        raw_refs.append(target.tg_peer_id)
    raw_refs.extend(candidate_refs)
    stable_refs = [ref for ref in candidate_refs + verified_refs + raw_refs if _is_stable_telegram_peer(ref)]
    public_refs = [payload.invite_link]
    if username:
        if _looks_like_invite_ref(username):
            public_refs.append(username)
        else:
            public_refs.extend([username, f"https://t.me/{username.lstrip('@')}"])
    public_refs.extend([ref for ref in raw_refs if ref and not _is_stable_telegram_peer(ref)])
    return _dedupe_refs(stable_refs + public_refs)


def _membership_verified_peer_refs(
    session: Session,
    action: Action,
    target: OperationTarget | None,
    payload: EnsureChannelMembershipPayload,
) -> list[str]:
    names = [payload.target_display]
    task = session.get(Task, action.task_id) if action.task_id else None
    if target and target.tenant_id == action.tenant_id:
        names.append(target.title)
    if task and task.tenant_id == action.tenant_id:
        names.append(task.name)
    clean_names = [name for name in _dedupe_refs(names) if name]
    if not clean_names:
        return []
    refs = session.scalars(
        select(VerificationTask.target_peer_id)
        .where(
            VerificationTask.tenant_id == action.tenant_id,
            VerificationTask.target_display.in_(clean_names),
        )
        .order_by(VerificationTask.id.desc())
        .limit(10)
    )
    return [ref for ref in _dedupe_refs([str(ref or "") for ref in refs]) if _is_stable_telegram_peer(ref)]


def _membership_dialog_refs(ctx: MembershipDispatchContext) -> list[str]:
    list_groups = getattr(gateway, "list_groups", None)
    if not callable(list_groups):
        return []
    try:
        groups = list_groups(ctx.account.id, ctx.account.session_ciphertext, ctx.credentials)
    except Exception as exc:  # noqa: BLE001 - expose lookup failure without hiding original join error.
        ctx.action.result = {**(ctx.action.result or {}), "membership_dialog_lookup_error": str(exc)[:200]}
        return []
    names = {name for name in (ctx.payload.target_display, _target_title_from_action(ctx.action, ctx.payload)) if name}
    return _dedupe_refs([group.tg_peer_id for group in groups if getattr(group, "title", "") in names])


def _target_title_from_action(action: Action, payload: EnsureChannelMembershipPayload) -> str:
    if payload.target_display:
        return payload.target_display
    return str((action.payload or {}).get("target_display") or "")


def _membership_candidate_groups(session: Session, tenant_id: int, payload: EnsureChannelMembershipPayload) -> list[TgGroup]:
    names = [payload.target_display]
    target = session.get(OperationTarget, payload.channel_target_id)
    if target and target.tenant_id == tenant_id:
        names.append(target.title)
    ref_filters = [TgGroup.tg_peer_id == payload.channel_id]
    clean_names = [name for name in _dedupe_refs(names) if name]
    if clean_names:
        ref_filters.append(TgGroup.title.in_(clean_names))
    groups = list(session.scalars(select(TgGroup).where(TgGroup.tenant_id == tenant_id, or_(*ref_filters))))
    return sorted(groups, key=_membership_group_rank)


def _membership_group_rank(group: TgGroup) -> tuple[int, int, int]:
    send_rank = 0 if group.can_send else 1
    stable_rank = 0 if _is_stable_telegram_peer(group.tg_peer_id) else 1
    return (send_rank, stable_rank, int(group.id or 0))


def _payload_with_channel_ref(payload: EnsureChannelMembershipPayload, ref: str, display: str) -> EnsureChannelMembershipPayload:
    return payload.model_copy(update={"channel_id": ref, "target_display": display or payload.target_display})


def _membership_invite_for_ref(payload: EnsureChannelMembershipPayload, ref: str) -> str:
    if ref == payload.channel_id:
        return payload.invite_link
    if _looks_like_invite_ref(ref):
        return ref
    return ""


def _looks_like_invite_ref(ref: str) -> bool:
    value = (ref or "").strip()
    return value.startswith(("+", "https://t.me/+", "http://t.me/+", "t.me/+", "https://telegram.me/+", "telegram.me/+"))


def _dedupe_refs(refs) -> list[str]:
    result: list[str] = []
    for raw in refs:
        ref = str(raw or "").strip()
        if ref and ref not in result:
            result.append(ref)
    return result


def _membership_peer_ref_invalid(result) -> bool:
    failure_type = result.failure_type or ""
    detail = (result.detail or failure_type).lower()
    if failure_type == FailureType.PEER_INVALID.value:
        return True
    return failure_type == FailureType.UNKNOWN.value and any(marker in detail for marker in _PEER_REF_INVALID_MARKERS)


def _record_membership_peer_ref(action: Action, payload: EnsureChannelMembershipPayload, fallback_ref: str) -> None:
    result = {**(action.result or {}), "membership_peer_ref": payload.channel_id}
    if fallback_ref:
        result["membership_fallback_ref"] = fallback_ref
    action.result = result


def _record_membership_attempted_ref(action: Action, ref: str) -> None:
    result = action.result if isinstance(action.result, dict) else {}
    attempted = [str(item) for item in result.get("membership_attempted_refs") or []]
    action.result = {**result, "membership_attempted_refs": _dedupe_refs([*attempted, ref])}


def _is_stable_telegram_peer(peer_id: str) -> bool:
    value = str(peer_id or "").strip()
    return value.lstrip("-").isdigit()


def _dispatch_existing_membership(
    session: Session,
    action: Action,
    account: TgAccount,
    credentials,
    payload: EnsureChannelMembershipPayload,
    link: TgGroupAccount,
) -> bool:
    if payload.target_type == "channel" and payload.require_send:
        return False
    if not _requires_group_send_probe(payload):
        _skip_membership_already_joined(action)
        return True
    if not link.can_send:
        return False
    attempt = _begin_execution_attempt(session, action, account)
    ctx = MembershipDispatchContext(session, action, account, credentials, payload, attempt)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)
    if result.ok:
        _record_group_send_permission_allowed(session, action, account, payload)
        _apply_operation_result(action, account, True, "", "already_joined", attempt=attempt)
        action.result = {**(action.result or {}), "membership_status": "already_joined"}
        return True
    handled, result = _handle_existing_membership_probe_denied(ctx, result)
    if handled:
        return True
    recovered = _recover_group_send_permission_with_linked_channel(session, action, account, credentials, payload, result)
    if recovered.ok:
        _record_group_send_permission_allowed(session, action, account, payload)
        _apply_operation_result(action, account, True, "", recovered.detail or "linked_channel_joined", attempt=attempt)
        action.result = {**(action.result or {}), "membership_status": "already_joined", "prerequisite_channel_followed": True}
        return True
    result = recovered
    verification = _record_group_send_permission_denied(session, action, account, payload, result.detail or result.failure_type)
    if _auto_verify_and_apply_group_send(ctx, verification, membership_status="joined"):
        return True
    if result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        _skip_membership_permission_denied(action, result.detail or result.failure_type)
        _maybe_trigger_membership_permission_rescue(ctx, result.detail or result.failure_type)
        _finish_execution_attempt(attempt, action, failure_type=result.failure_type, detail=result.detail or result.failure_type)
        _release_runtime_resources(action)
        return True
    _apply_operation_result(action, account, False, result.failure_type, result.detail or result.failure_type, attempt=attempt)
    return True


def _handle_existing_membership_probe_denied(ctx: MembershipDispatchContext, probe_result) -> tuple[bool, object]:
    refreshed = _refresh_joined_group_membership_before_verification(ctx, probe_result)
    if refreshed is None or not refreshed.ok:
        return False, refreshed or probe_result
    _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
    _apply_operation_result(ctx.action, ctx.account, True, "", refreshed.detail or "rejoined", attempt=ctx.attempt)
    ctx.action.result = {**(ctx.action.result or {}), "membership_status": "already_joined", "membership_rejoined": True}
    return True, refreshed


def _refresh_joined_group_membership_before_verification(ctx: MembershipDispatchContext, probe_result):
    detail = probe_result.detail or probe_result.failure_type or ""
    if not _group_send_probe_should_rejoin(detail):
        return None
    join_result, joined_payload, fallback_ref = _ensure_membership_with_peer_candidates(ctx)
    _record_membership_peer_ref(ctx.action, joined_payload, fallback_ref)
    if not join_result.ok:
        return join_result
    reprobe = gateway.probe_target_capabilities(
        ctx.account.id,
        joined_payload.channel_id,
        joined_payload.target_type,
        ctx.account.session_ciphertext,
        ctx.credentials,
    )
    return OperationResult(True, detail=join_result.detail or reprobe.detail or "重新入群后可发言") if reprobe.ok else reprobe


def _group_send_probe_should_rejoin(detail: str) -> bool:
    normalized = str(detail or "").lower()
    return any(marker.lower() in normalized for marker in _GROUP_SEND_RETRYABLE_VERIFICATION_MARKERS)


def _requires_group_send_probe(payload: EnsureChannelMembershipPayload) -> bool:
    return payload.target_type == "group" and bool(payload.require_send)


def _probe_joined_group_send_permission(session: Session, action: Action, account: TgAccount, credentials, payload: EnsureChannelMembershipPayload):
    if not _requires_group_send_probe(payload):
        return None
    return gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)


def _recover_group_send_permission_with_linked_channel(
    session: Session,
    action: Action,
    account: TgAccount,
    credentials,
    payload: EnsureChannelMembershipPayload,
    probe_result,
    *,
    retry_target_membership: bool = False,
):
    detail = probe_result.detail or probe_result.failure_type or ""
    if not _group_send_permission_needs_linked_channel(detail):
        return probe_result
    if not _auto_follow_required_channel_enabled(session, action):
        return probe_result
    required_channels = required_channel_references(detail)
    if required_channels:
        return _follow_required_channels_and_reprobe(
            session,
            action,
            account,
            credentials,
            payload,
            probe_result,
            required_channels,
            retry_target_membership=retry_target_membership,
        )
    follow = getattr(gateway, "ensure_linked_channel_membership", None)
    if not callable(follow):
        return probe_result
    followed = follow(account.id, payload.channel_id, account.session_ciphertext, credentials)
    if not followed.ok:
        return OperationResult(False, "失败", followed.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, followed.detail or detail)
    if retry_target_membership:
        ctx = MembershipDispatchContext(session, action, account, credentials, payload, None)
        refreshed = _retry_target_membership_after_required_channel(ctx)
        if not refreshed.ok:
            return refreshed
    reprobe = gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)
    if reprobe.ok:
        return OperationResult(True, detail=followed.detail or "已关注关联频道并通过群发言验证")
    return OperationResult(False, "失败", reprobe.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, reprobe.detail or detail)


def _follow_required_channels_and_reprobe(
    session: Session,
    action: Action,
    account: TgAccount,
    credentials,
    payload: EnsureChannelMembershipPayload,
    probe_result,
    required_channels: list[str],
    *,
    retry_target_membership: bool = False,
):
    for channel_ref in required_channels:
        followed = gateway.ensure_channel_membership(account.id, channel_ref, account.session_ciphertext, credentials, invite_link=channel_ref)
        if not followed.ok:
            detail = followed.detail or followed.failure_type or probe_result.detail
            return OperationResult(False, "失败", followed.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)
    if retry_target_membership:
        ctx = MembershipDispatchContext(session, action, account, credentials, payload, None)
        refreshed = _retry_target_membership_after_required_channel(ctx)
        if not refreshed.ok:
            return refreshed
    reprobe = gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)
    if reprobe.ok:
        action.result = {**(action.result or {}), "required_channels_followed": required_channels}
        return OperationResult(True, detail=f"已关注 {len(required_channels)} 个必需频道并通过群发言验证")
    detail = reprobe.detail or reprobe.failure_type or probe_result.detail
    return OperationResult(False, "失败", reprobe.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)


def _retry_target_membership_after_required_channel(ctx: MembershipDispatchContext):
    if ctx.action.action_type not in MEMBERSHIP_ACTION_TYPES:
        return OperationResult(True, detail="send_action_no_target_membership_retry")
    result, joined_payload, fallback_ref = _ensure_membership_with_peer_candidates(ctx)
    _record_membership_peer_ref(ctx.action, joined_payload, fallback_ref)
    if result.ok:
        ctx.action.result = {**(ctx.action.result or {}), "target_membership_retried_after_required_channel": True}
        return OperationResult(True, detail=result.detail or "target_membership_retried")
    detail = result.detail or result.failure_type or "关注必需频道后仍无法加入目标群"
    return OperationResult(False, "失败", result.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)


def _recover_send_message_required_channel(
    session: Session,
    action: Action,
    account: TgAccount,
    credentials,
    group: TgGroup,
    payload: SendMessagePayload,
    send_result,
    attempt: ExecutionAttempt | None,
) -> bool:
    detail = send_result.detail or send_result.failure_type or ""
    if send_result.ok or send_result.failure_type != FailureType.GROUP_PERMISSION_DENIED.value:
        return False
    membership_payload = _group_send_membership_payload(session, action, group, payload)
    if membership_payload is None:
        return False
    recovered = _recover_group_send_permission_with_linked_channel(session, action, account, credentials, membership_payload, send_result)
    if recovered.ok:
        _record_group_send_permission_allowed(
            session,
            action,
            account,
            membership_payload,
            permission_label=REQUIRED_CHANNEL_PERMISSION_LABEL,
        )
        _requeue_send_after_required_channel_follow(action, recovered.detail or "已关注必需频道，等待重新发送")
        _finish_execution_attempt(attempt, action, failure_type=send_result.failure_type, detail=detail)
        _release_runtime_resources(action)
        return True
    verification = _record_group_send_permission_denied(
        session,
        action,
        account,
        membership_payload,
        recovered.detail or recovered.failure_type or detail,
    )
    ctx = MembershipDispatchContext(session, action, account, credentials, membership_payload, attempt)
    auto_verified = _auto_verify_send_permission(ctx, verification)
    if auto_verified.ok:
        _requeue_send_after_permission_recovery(action, auto_verified.detail or "群发言权限已恢复，等待重新发送", verification)
    else:
        snapshot = _verification_result_snapshot(action)
        failure_detail = recovered.detail or recovered.failure_type or detail
        _fail(action, send_result.failure_type, failure_detail, auto_check="失败", validation_stage="send_permission")
        action.result = {**(action.result or {}), **snapshot, "membership_status": "permission_denied"}
        _maybe_trigger_send_permission_rescue(action, account, failure_detail)
    _finish_execution_attempt(attempt, action, failure_type=send_result.failure_type, detail=detail)
    _release_runtime_resources(action)
    return True


def _recover_pre_send_required_channel_prompt(ctx: PreSendRequiredChannelContext) -> bool:
    if _required_channel_prompt_already_resolved(ctx.action, ctx.link):
        return False
    detail = _recent_required_channel_prompt_for_send(ctx)
    if not detail:
        return False
    send_result = OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, detail)
    return _recover_send_message_required_channel(
        ctx.session,
        ctx.action,
        ctx.account,
        ctx.credentials,
        ctx.group,
        ctx.payload,
        send_result,
        None,
    )


def _defer_send_for_required_channel_admission(action: Action, link: TgGroupAccount) -> bool:
    if REQUIRED_CHANNEL_BLOCKED_LABEL not in str(link.permission_label or ""):
        return False
    action.status = "pending"
    action.scheduled_at = _now() + timedelta(seconds=REQUIRED_CHANNEL_ADMISSION_RETRY_SECONDS)
    action.executed_at = None
    _clear_action_lease(action)
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": "required_channel_admission_pending",
        "error_message": "账号需要先关注必需频道并复检群发言权限",
        "auto_check": "等待准入",
        "validation_stage": "required_channel_follow",
    }
    return True


def _required_channel_prompt_already_resolved(action: Action, link: TgGroupAccount) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    if result.get("prerequisite_channel_followed"):
        return True
    return bool(link.can_send and link.permission_label == REQUIRED_CHANNEL_PERMISSION_LABEL)


def _recent_required_channel_prompt_for_send(ctx: PreSendRequiredChannelContext) -> str:
    cutoff = _now() - timedelta(hours=RECENT_REQUIRED_CHANNEL_PROMPT_LOOKBACK_HOURS)
    observed_at = func.coalesce(GroupContextMessage.sent_at, GroupContextMessage.created_at)
    rows = ctx.session.scalars(
        select(GroupContextMessage)
        .where(
            GroupContextMessage.tenant_id == ctx.action.tenant_id,
            GroupContextMessage.group_id == ctx.group.id,
            observed_at >= cutoff,
        )
        .order_by(observed_at.desc(), GroupContextMessage.id.desc())
        .limit(RECENT_REQUIRED_CHANNEL_PROMPT_LIMIT)
    )
    for row in rows:
        text = str(row.content or "").strip()
        if required_channel_prompt_applies_to_send(text, ctx.account, allow_global=True):
            return text[:REQUIRED_CHANNEL_PROMPT_PREVIEW_LENGTH]
    return ""


def _auto_verify_send_permission(ctx: MembershipDispatchContext, verification_task):
    if not _auto_verification_enabled(ctx.session, ctx.action):
        return OperationResult(False, "需人工处理", FailureType.GROUP_PERMISSION_DENIED.value, "任务未启用自动验证")
    return _try_auto_group_send_verification(ctx, verification_task)


def _group_send_membership_payload(
    session: Session,
    action: Action,
    group: TgGroup,
    payload: SendMessagePayload,
) -> EnsureChannelMembershipPayload | None:
    target = session.get(OperationTarget, int(payload.operation_target_id or 0)) if payload.operation_target_id else None
    if not target:
        target = session.scalar(
            select(OperationTarget).where(
                OperationTarget.tenant_id == action.tenant_id,
                OperationTarget.target_type == "group",
                OperationTarget.tg_peer_id == group.tg_peer_id,
            )
        )
    if not target or target.tenant_id != action.tenant_id:
        return None
    return EnsureChannelMembershipPayload(
        channel_id=group.tg_peer_id,
        channel_target_id=target.id,
        target_type="group",
        target_display=payload.target_display or group.title or target.title,
        require_send=True,
    )


def _requeue_send_after_required_channel_follow(action: Action, detail: str) -> None:
    previous = dict(action.result or {})
    action.status = "pending"
    action.scheduled_at = _now()
    action.executed_at = None
    _clear_action_lease(action)
    action.result = {
        **previous,
        "success": False,
        "error_code": "required_channel_followed_retry",
        "error_message": detail,
        "auto_check": "等待重发",
        "validation_stage": "required_channel_follow",
        "prerequisite_channel_followed": True,
    }


def _requeue_send_after_permission_recovery(action: Action, detail: str, verification_task) -> None:
    previous = dict(action.result or {})
    action.status = "pending"
    action.scheduled_at = _now()
    action.executed_at = None
    _clear_action_lease(action)
    action.result = {
        **previous,
        "success": False,
        "error_code": "send_permission_recovered_retry",
        "error_message": detail,
        "auto_check": "等待重发",
        "validation_stage": "send_permission_recovered",
        "verification_task_id": verification_task.id,
        "verification_status": verification_task.status,
        "verification_action": verification_task.suggested_action,
    }


def _handle_group_send_permission_denied(
    ctx: MembershipDispatchContext,
    probe_result,
    *,
    membership_status: str,
    skip_on_failure: bool,
) -> bool:
    recovered = _recover_group_send_permission_with_linked_channel(
        ctx.session,
        ctx.action,
        ctx.account,
        ctx.credentials,
        ctx.payload,
        probe_result,
        retry_target_membership=True,
    )
    if recovered.ok:
        _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
        _apply_operation_result(ctx.action, ctx.account, True, "", recovered.detail or "linked_channel_joined", attempt=ctx.attempt)
        ctx.action.result = {**(ctx.action.result or {}), "membership_status": membership_status, "prerequisite_channel_followed": True}
        return True
    detail = recovered.detail or recovered.failure_type
    verification = _record_group_send_permission_denied(ctx.session, ctx.action, ctx.account, ctx.payload, detail)
    if _auto_verify_and_apply_group_send(ctx, verification, membership_status=membership_status):
        return True
    if skip_on_failure or recovered.failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        _skip_membership_permission_denied(ctx.action, detail)
        _maybe_trigger_membership_permission_rescue(ctx, detail)
        _finish_execution_attempt(ctx.attempt, ctx.action, failure_type=recovered.failure_type, detail=detail)
        _release_runtime_resources(ctx.action)
        return True
    _apply_operation_result(ctx.action, ctx.account, False, recovered.failure_type, detail, attempt=ctx.attempt)
    return True


def _group_send_permission_needs_linked_channel(detail: str) -> bool:
    normalized = detail.lower()
    return any(marker.lower() in normalized for marker in _GROUP_SEND_LINKED_CHANNEL_REQUIRED_MARKERS)


def _skip_membership_already_joined(action: Action) -> None:
    _skip(action, "already_joined", "账号已满足目标准入")
    action.result = {**(action.result or {}), "success": True, "membership_status": "already_joined"}


def _skip_membership_permission_denied(action: Action, detail: str) -> None:
    verification_result = _verification_result_snapshot(action)
    _skip(action, "membership_permission_denied", f"账号无法加入/访问目标：{detail}")
    action.result = {
        **(action.result or {}),
        **verification_result,
        "membership_status": "permission_denied",
        "validation_stage": "target_membership_runtime",
    }


def _maybe_trigger_membership_permission_rescue(ctx: MembershipDispatchContext, detail: str) -> None:
    if ctx.action.task_type != "group_ai_chat" or ctx.payload.target_type != "group":
        return
    task = ctx.session.get(Task, ctx.action.task_id) if ctx.action.task_id else None
    target = ctx.session.get(OperationTarget, ctx.payload.channel_target_id)
    if not task or not target or target.tenant_id != ctx.action.tenant_id:
        return
    group = _membership_group_for_payload(ctx.session, target, ctx.payload, create=True)
    result = trigger_group_rescue(
        ctx.session,
        task,
        group,
        trigger_account_id=ctx.account.id,
        trigger_reason=detail,
        operation_target_id=target.id,
    )
    _record_group_rescue_result(ctx.action, result)


def _verification_result_snapshot(action: Action) -> dict[str, object]:
    result = action.result or {}
    return {key: result[key] for key in ("verification_task_id", "verification_status", "verification_action") if key in result}


def _mark_membership_joined(session: Session, action: Action, account: TgAccount, payload: EnsureChannelMembershipPayload) -> None:
    target = session.get(OperationTarget, payload.channel_target_id)
    if not target or target.tenant_id != action.tenant_id:
        raise ValueError("operation target not found")
    group = _membership_group_for_payload(session, target, payload, create=True)
    label = "已关注" if payload.target_type == "channel" else "可发言"
    target_can_send = True if payload.target_type == "group" else bool(target.can_send)
    link = _group_account_link(session, action.tenant_id, group.id, account.id, create=True)
    link.permission_label = label
    link.can_send = True
    group.auth_status = GroupAuthStatus.AUTHORIZED.value
    group.can_send = bool(group.can_send or target_can_send)
    target.auth_status = GroupAuthStatus.AUTHORIZED.value
    target.can_send = bool(target.can_send or target_can_send)
    target.updated_at = _now()


def _membership_group_for_payload(
    session: Session,
    target: OperationTarget,
    payload: EnsureChannelMembershipPayload,
    *,
    create: bool,
) -> TgGroup:
    group_peer = _membership_group_peer(target, payload)
    group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == target.tenant_id, TgGroup.tg_peer_id == group_peer))
    preferred_group = _send_ready_title_group(session, target, payload)
    if create and preferred_group and (group is None or not group.can_send):
        return preferred_group
    if group or not create:
        return group
    group = TgGroup(
        tenant_id=target.tenant_id,
        tg_peer_id=group_peer,
        title=payload.target_display or target.title,
        group_type="channel" if payload.target_type == "channel" else "supergroup",
        member_count=target.member_count,
        auth_status=target.auth_status,
        can_send=target.can_send,
    )
    session.add(group)
    session.flush()
    return group


def _send_ready_title_group(session: Session, target: OperationTarget, payload: EnsureChannelMembershipPayload) -> TgGroup | None:
    if payload.target_type != "group":
        return None
    names = [name for name in _dedupe_refs([payload.target_display, target.title]) if name]
    if not names:
        return None
    groups = list(
        session.scalars(
            select(TgGroup).where(
                TgGroup.tenant_id == target.tenant_id,
                TgGroup.title.in_(names),
                TgGroup.can_send.is_(True),
            )
        )
    )
    return sorted(groups, key=_membership_group_rank)[0] if groups else None


def _membership_group_peer(target: OperationTarget, payload: EnsureChannelMembershipPayload) -> str:
    ref = str(payload.channel_id or "").strip()
    target_peer = str(target.tg_peer_id or "").strip()
    public_ref = _public_group_ref(ref) if payload.target_type == "group" else ""
    if public_ref:
        return public_ref
    if _is_join_link_ref(ref) and target_peer:
        return target_peer
    if not _is_stable_telegram_peer(ref) and _is_stable_telegram_peer(target_peer):
        return target_peer
    return ref or target_peer


def _public_group_ref(ref: str) -> str:
    value = str(ref or "").strip()
    if not value or _is_stable_telegram_peer(value) or _looks_like_invite_ref(value):
        return ""
    lowered = value.lower()
    for prefix in ("https://t.me/", "http://t.me/", "t.me/", "https://telegram.me/", "http://telegram.me/", "telegram.me/"):
        if lowered.startswith(prefix):
            return value[len(prefix):].strip("/").lstrip("@")
    return value.lstrip("@")


def _is_join_link_ref(ref: str) -> bool:
    value = (ref or "").strip().lower()
    return value.startswith(("http://t.me/", "https://t.me/", "t.me/", "http://telegram.me/", "https://telegram.me/", "telegram.me/", "+"))


def _record_group_send_permission_allowed(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: EnsureChannelMembershipPayload,
    *,
    permission_label: str = "可发言",
) -> None:
    target = session.get(OperationTarget, payload.channel_target_id)
    if not target:
        return
    group = _membership_group_for_payload(session, target, payload, create=True)
    link = _group_account_link(session, action.tenant_id, group.id, account.id, create=True)
    link.can_send = True
    link.permission_label = permission_label
    _sync_group_target_send_state(session, group, target)


def _record_group_send_permission_denied(session: Session, action: Action, account: TgAccount, payload: EnsureChannelMembershipPayload, detail: str):
    target = session.get(OperationTarget, payload.channel_target_id)
    if not target:
        return
    group = _membership_group_for_payload(session, target, payload, create=True)
    link = _group_account_link(session, action.tenant_id, group.id, account.id, create=True)
    link.can_send = False
    link.permission_label = (detail or FailureType.GROUP_PERMISSION_DENIED.value)[:80]
    _sync_group_target_send_state(session, group, target)
    verification = create_verification_task(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        group_id=group.id,
        message_task_id=None,
        verification_type="群发言权限",
        detected_reason=detail or "账号已加入但没有群发言权限",
        suggested_action=_group_send_verification_action(detail),
        target_peer_id=_verification_target_peer_ref(payload, group),
        target_display=group.title,
    )
    action.result = {
        **(action.result or {}),
        "verification_task_id": verification.id,
        "verification_status": verification.status,
        "verification_action": verification.suggested_action,
    }
    return verification


def _verification_target_peer_ref(payload: EnsureChannelMembershipPayload, group: TgGroup) -> str:
    if payload.target_type == "group" and _is_stable_telegram_peer(group.tg_peer_id):
        return group.tg_peer_id
    if payload.target_type == "group":
        public_ref = _public_group_ref(payload.channel_id)
        if public_ref:
            return public_ref
    return group.tg_peer_id


def _auto_verify_and_apply_group_send(ctx: MembershipDispatchContext, verification_task, *, membership_status: str) -> bool:
    if not _auto_verification_enabled(ctx.session, ctx.action):
        return False
    auto_verified = _try_auto_group_send_verification(ctx, verification_task)
    if not auto_verified.ok:
        return False
    _apply_operation_result(ctx.action, ctx.account, True, "", auto_verified.detail or "verification_resolved", attempt=ctx.attempt)
    ctx.action.result = {
        **(ctx.action.result or {}),
        "membership_status": membership_status,
        "verification_status": verification_task.status,
        "verification_action": verification_task.suggested_action,
    }
    return True


def _auto_verification_enabled(session: Session, action: Action) -> bool:
    task = session.get(Task, action.task_id) if action.task_id else None
    config = task.type_config if task else {}
    return bool((config or {}).get("auto_resolve_verification", True))


def _ai_assisted_verification_enabled(session: Session, action: Action) -> bool:
    task = session.get(Task, action.task_id) if action.task_id else None
    config = task.type_config if task else {}
    return bool((config or {}).get("ai_assisted_verification", True))


def _auto_follow_required_channel_enabled(session: Session, action: Action) -> bool:
    task = session.get(Task, action.task_id) if action.task_id else None
    config = task.type_config if task else {}
    return bool((config or {}).get("auto_follow_required_channel", True))


def _try_auto_group_send_verification(ctx: MembershipDispatchContext, verification_task):
    if verification_task is None:
        return OperationResult(False, "需人工处理", FailureType.GROUP_PERMISSION_DENIED.value, "未生成验证辅助任务")
    if not getattr(verification_task, "can_auto_resolve", False):
        return OperationResult(False, "需人工处理", FailureType.GROUP_PERMISSION_DENIED.value, verification_task.detected_reason)
    if verification_task.suggested_action == "识别图形验证码":
        if not _ai_assisted_verification_enabled(ctx.session, ctx.action):
            detail = "任务未启用 AI 辅助验证，图形验证码转人工处理"
            verification_task.status = "需人工处理"
            verification_task.failure_detail = detail
            audit(ctx.session, tenant_id=ctx.action.tenant_id, actor="system", action="图形验证码转人工", target_type="verification_task", target_id=str(verification_task.id), detail=detail)
            return OperationResult(False, "需人工处理", FailureType.GROUP_PERMISSION_DENIED.value, detail)
        return _try_auto_image_verification(ctx, verification_task)
    if verification_task.suggested_action == "发送验证回复":
        return _try_auto_text_verification(ctx, verification_task)
    if verification_task.suggested_action == "关注频道":
        return _try_auto_follow_verification(ctx, verification_task)
    result = gateway.resolve_verification_task(
        ctx.account.id,
        verification_task.suggested_action,
        verification_task.target_peer_id or ctx.payload.channel_id,
        ctx.account.session_ciphertext,
        ctx.credentials,
    )
    verification_task.status = result.status
    verification_task.failure_detail = result.detail
    if result.status != "需人工处理":
        verification_task.handled_at = _now()
    audit(ctx.session, tenant_id=ctx.action.tenant_id, actor="system", action="自动处理验证辅助任务", target_type="verification_task", target_id=str(verification_task.id), detail=f"{result.status}:{result.detail}")
    if not result.ok:
        return OperationResult(False, result.status, FailureType.GROUP_PERMISSION_DENIED.value, result.detail or result.failure_type)
    reprobe = gateway.probe_target_capabilities(ctx.account.id, ctx.payload.channel_id, ctx.payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    if reprobe.ok:
        _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
        return OperationResult(True, "已完成", detail=result.detail or reprobe.detail or "verification_resolved")
    verification_task.status = "失败"
    verification_task.failure_detail = reprobe.detail or reprobe.failure_type
    return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)


def _try_auto_text_verification(ctx: MembershipDispatchContext, verification_task):
    readers = _image_verification_reader_candidates(ctx.session, verification_task, ctx.account)
    result = auto_resolve_text_verification(
        ctx.session,
        verification_task,
        ctx.account,
        ctx.credentials,
        reader_candidates=readers,
    )
    verification_task.status = result.status
    verification_task.failure_detail = result.detail or result.failure_type
    if result.status != "需人工处理":
        verification_task.handled_at = _now()
    audit(ctx.session, tenant_id=ctx.action.tenant_id, actor="system", action="自动处理文本验证", target_type="verification_task", target_id=str(verification_task.id), detail=f"{result.status}:{verification_task.failure_detail}")
    if not result.ok:
        return OperationResult(False, result.status, FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)
    reprobe = gateway.probe_target_capabilities(ctx.account.id, ctx.payload.channel_id, ctx.payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    if reprobe.ok:
        _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
        return OperationResult(True, "已完成", detail=result.detail or reprobe.detail or "text_verification_resolved")
    verification_task.status = "失败"
    verification_task.failure_detail = reprobe.detail or reprobe.failure_type
    return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)


def _try_auto_image_verification(ctx: MembershipDispatchContext, verification_task):
    readers = _image_verification_reader_candidates(ctx.session, verification_task, ctx.account)
    result = auto_resolve_image_verification(
        ctx.session,
        verification_task,
        ctx.account,
        ctx.credentials,
        reader_candidates=readers,
    )
    verification_task.status = result.status
    verification_task.failure_detail = result.detail or result.failure_type
    if result.status != "需人工处理":
        verification_task.handled_at = _now()
    audit(ctx.session, tenant_id=ctx.action.tenant_id, actor="system", action="自动处理图形验证码", target_type="verification_task", target_id=str(verification_task.id), detail=f"{result.status}:{verification_task.failure_detail}")
    if not result.ok:
        context_result = _try_context_verification_fallback(ctx, verification_task, result)
        if context_result is not None:
            return context_result
        return OperationResult(False, result.status, FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)
    reprobe = gateway.probe_target_capabilities(ctx.account.id, ctx.payload.channel_id, ctx.payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    if reprobe.ok:
        _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
        _record_image_reprobe_attempt(ctx, verification_task, result, "reprobe_ok", reprobe.detail or "复检可发言")
        return OperationResult(True, "已完成", detail=result.detail or reprobe.detail or "image_verification_resolved")
    verification_task.status = "失败"
    verification_task.failure_detail = reprobe.detail or reprobe.failure_type
    _record_image_reprobe_attempt(ctx, verification_task, result, "reprobe_failed", verification_task.failure_detail)
    return OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)


def _try_auto_follow_verification(ctx: MembershipDispatchContext, verification_task):
    detail = _auto_follow_detail_text(ctx.action, verification_task)
    payload = _verification_probe_payload(ctx.payload, verification_task)
    probe_result = OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, detail)
    result = _recover_group_send_permission_with_linked_channel(
        ctx.session,
        ctx.action,
        ctx.account,
        ctx.credentials,
        payload,
        probe_result,
        retry_target_membership=ctx.action.action_type in MEMBERSHIP_ACTION_TYPES,
    )
    if not result.ok:
        required_channels = _required_channels_from_verification_context(ctx, verification_task)
        if required_channels and _auto_follow_required_channel_enabled(ctx.session, ctx.action):
            result = _follow_required_channels_and_reprobe(
                ctx.session,
                ctx.action,
                ctx.account,
                ctx.credentials,
                payload,
                probe_result,
                required_channels,
                retry_target_membership=ctx.action.action_type in MEMBERSHIP_ACTION_TYPES,
            )
    return _apply_context_fallback_result(ctx, verification_task, payload, result)


def _auto_follow_detail_text(action: Action, verification_task) -> str:
    result = action.result if isinstance(action.result, dict) else {}
    return "\n".join(
        text
        for text in (
            verification_task.detected_reason,
            verification_task.failure_detail,
            result.get("error_message"),
            result.get("detail"),
            result.get("failure_detail"),
        )
        if text
    )


def _required_channels_from_verification_context(ctx: MembershipDispatchContext, verification_task) -> list[str]:
    readers = _image_verification_reader_candidates(ctx.session, verification_task, ctx.account)
    read_result = read_challenge_context_with_fallback(
        ctx.session,
        verification_task,
        ctx.account,
        ctx.credentials,
        reader_candidates=readers,
    )
    return required_channel_references(_verification_context_text(read_result.context))


def _try_context_verification_fallback(ctx: MembershipDispatchContext, verification_task, image_result):
    context_text = _verification_context_text(getattr(image_result, "attempt_context", None) or {})
    if not context_text:
        return None
    payload = _verification_probe_payload(ctx.payload, verification_task)
    required_channels = required_channel_references(context_text)
    if required_channels and _auto_follow_required_channel_enabled(ctx.session, ctx.action):
        verification_task.suggested_action = "关注频道"
        followed = _follow_required_channels_and_reprobe(
            ctx.session,
            ctx.action,
            ctx.account,
            ctx.credentials,
            payload,
            image_result,
            required_channels,
            retry_target_membership=ctx.action.action_type in MEMBERSHIP_ACTION_TYPES,
        )
        return _apply_context_fallback_result(ctx, verification_task, payload, followed)
    if not _context_requires_button_click(context_text):
        return None
    verification_task.suggested_action = "点击按钮"
    clicked = gateway.resolve_verification_task(
        ctx.account.id,
        "点击按钮",
        verification_task.target_peer_id or payload.channel_id,
        ctx.account.session_ciphertext,
        ctx.credentials,
    )
    if not clicked.ok:
        return _apply_context_fallback_result(ctx, verification_task, payload, clicked)
    reprobe = gateway.probe_target_capabilities(ctx.account.id, payload.channel_id, payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    return _apply_context_fallback_result(ctx, verification_task, payload, reprobe, success_detail=clicked.detail)


def _verification_context_text(context: dict[str, object]) -> str:
    messages = context.get("messages") if isinstance(context, dict) else []
    texts = [str(message.get("text") or "") for message in messages or [] if isinstance(message, dict)]
    return "\n".join(text for text in texts if text)


def _verification_probe_payload(payload: EnsureChannelMembershipPayload, verification_task) -> EnsureChannelMembershipPayload:
    target_peer = str(getattr(verification_task, "target_peer_id", "") or "")
    if target_peer and _is_stable_telegram_peer(target_peer):
        return _payload_with_channel_ref(payload, target_peer, getattr(verification_task, "target_display", "") or payload.target_display)
    return payload


def _context_requires_button_click(context_text: str) -> bool:
    normalized = context_text.lower()
    return any(marker.lower() in normalized for marker in _GROUP_SEND_BUTTON_VERIFICATION_MARKERS)


def _apply_context_fallback_result(
    ctx: MembershipDispatchContext,
    verification_task,
    payload: EnsureChannelMembershipPayload,
    result,
    *,
    success_detail: str = "",
):
    if result.ok:
        verification_task.status = "已处理"
        verification_task.failure_detail = success_detail or result.detail or "verification_context_fallback_resolved"
        verification_task.handled_at = _now()
        _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, payload)
        return OperationResult(True, "已完成", detail=verification_task.failure_detail)
    verification_task.status = result.status or "需人工处理"
    verification_task.failure_detail = result.detail or result.failure_type or "verification_context_fallback_failed"
    return OperationResult(False, verification_task.status, FailureType.GROUP_PERMISSION_DENIED.value, verification_task.failure_detail)


def _record_image_reprobe_attempt(ctx: MembershipDispatchContext, verification_task, image_result, status: str, detail: str) -> None:
    record_challenge_attempt(
        ctx.session,
        verification_task,
        ctx.account,
        getattr(image_result, "attempt_context", None) or {},
        image_message=getattr(image_result, "image_message", None),
        answer_text=getattr(image_result, "answer_text", ""),
        answer_source=getattr(image_result, "answer_source", ""),
        confidence=float(getattr(image_result, "confidence", 0.0) or 0.0),
        model_name=getattr(image_result, "model_name", ""),
        status=status,
        result_detail=detail,
    )


def _group_send_verification_action(detail: str) -> str:
    normalized = str(detail or "").lower()
    if any(marker.lower() in normalized for marker in _GROUP_SEND_IMAGE_VERIFICATION_MARKERS):
        return "识别图形验证码"
    if any(marker.lower() in normalized for marker in _GROUP_SEND_REPLY_VERIFICATION_MARKERS):
        return "发送验证回复"
    if any(marker.lower() in normalized for marker in _GROUP_SEND_TEXT_VERIFICATION_MARKERS):
        return "发送验证回复"
    if any(marker.lower() in normalized for marker in _GROUP_SEND_LINKED_CHANNEL_REQUIRED_MARKERS):
        return "关注频道"
    if any(marker.lower() in normalized for marker in _GROUP_SEND_BUTTON_VERIFICATION_MARKERS):
        return "点击按钮"
    if any(marker.lower() in normalized for marker in _GROUP_SEND_RETRYABLE_VERIFICATION_MARKERS):
        return "识别图形验证码"
    return "人工处理"


def _image_verification_reader_candidates(session: Session, verification_task, submit_account: TgAccount) -> list[tuple[TgAccount, object]]:
    if not verification_task or not getattr(verification_task, "group_id", None):
        return []
    links = list(
        session.scalars(
            select(TgGroupAccount)
            .where(
                TgGroupAccount.tenant_id == submit_account.tenant_id,
                TgGroupAccount.group_id == verification_task.group_id,
                TgGroupAccount.account_id != submit_account.id,
                TgGroupAccount.can_send.is_(True),
            )
            .limit(VERIFICATION_READER_CANDIDATE_LIMIT)
        )
    )
    account_ids = [link.account_id for link in links]
    if not account_ids:
        return []
    accounts = list(
        session.scalars(
            select(TgAccount).where(
                TgAccount.tenant_id == submit_account.tenant_id,
                TgAccount.id.in_(account_ids),
                TgAccount.status == AccountStatus.ACTIVE.value,
                TgAccount.deleted_at.is_(None),
            )
        )
    )
    by_id = {account.id: account for account in accounts}
    candidates: list[tuple[TgAccount, object]] = []
    for account_id in account_ids:
        account = by_id.get(account_id)
        if not account:
            continue
        credentials = credentials_for_account(session, account)
        candidates.append((account, credentials))
    return candidates


def _group_account_link(session: Session, tenant_id: int, group_id: int, account_id: int, *, create: bool) -> TgGroupAccount:
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.account_id == account_id,
        )
    )
    if link or not create:
        return link
    link = TgGroupAccount(tenant_id=tenant_id, group_id=group_id, account_id=account_id)
    session.add(link)
    session.flush()
    return link


def _sync_group_target_send_state(session: Session, group: TgGroup, target: OperationTarget) -> None:
    can_send = bool(
        session.scalar(
            select(func.count(TgGroupAccount.id)).where(
                TgGroupAccount.tenant_id == group.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.can_send.is_(True),
            )
        )
    )
    group.can_send = can_send
    target.can_send = can_send
    if can_send:
        group.auth_status = GroupAuthStatus.AUTHORIZED.value
        target.auth_status = GroupAuthStatus.AUTHORIZED.value
    elif group.auth_status == GroupAuthStatus.AUTHORIZED.value:
        group.auth_status = GroupAuthStatus.READONLY.value
    if not can_send and target.auth_status == GroupAuthStatus.AUTHORIZED.value:
        target.auth_status = GroupAuthStatus.READONLY.value
    target.updated_at = _now()


def _outbound_segments(payload: SendMessagePayload) -> list[OutboundSegment]:
    segments = [OutboundSegment(segment_type="文本", content=payload.message_text)]
    for item in payload.media_segments or []:
        if not isinstance(item, dict):
            continue
        source = str(item.get("source") or "")
        if not source:
            continue
        segments.append(
            OutboundSegment(
                segment_type=str(item.get("segment_type") or item.get("type") or "图片"),
                content=str(item.get("content") or ""),
                source=source,
                caption=str(item.get("caption") or ""),
            )
        )
    return segments


def _dispatch_view(action: Action, account: TgAccount, credentials, session: Session, payload: ViewMessagePayload) -> bool:
    if not _ensure_channel_action_membership(session, action, account, payload.channel_target_id):
        return True
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.view_channel_message(account_id, channel_peer, message_id, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail, attempt=attempt)
    return True


def _dispatch_like(action: Action, account: TgAccount, credentials, session: Session, payload: LikeMessagePayload) -> bool:
    if not _ensure_channel_action_membership(session, action, account, payload.channel_target_id):
        return True
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    reaction = payload.reaction_emoji
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.send_channel_reaction(account_id, channel_peer, message_id, reaction, session_ciphertext, credentials)
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail, attempt=attempt)
    return True


def _dispatch_comment(action: Action, account: TgAccount, credentials, session: Session, payload: PostCommentPayload) -> bool:
    if _comment_total_limit_reached(session, action):
        _skip(action, "comment_task_total_reached", "频道评论任务总上限已达到，跳过旧计划")
        return True
    if _comment_success_limit_reached(session, action, payload):
        _skip(action, "comment_target_reached", "频道消息评论已达到当前上限，跳过旧计划")
        return True
    if not _ensure_channel_action_membership(session, action, account, payload.channel_target_id):
        return True
    account_id = account.id
    session_ciphertext = account.session_ciphertext
    channel_peer = payload.channel_id
    message_id = payload.message_id
    content = payload.comment_text
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.reply_channel_message(account_id, channel_peer, message_id, content, session_ciphertext, credentials, reply_to_message_id=payload.reply_to_message_id)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "", attempt=attempt)
    return True


def _comment_total_limit_reached(session: Session, action: Action) -> bool:
    task = session.get(Task, action.task_id)
    if not task or task.type != "channel_comment":
        return False
    config = task.type_config if isinstance(task.type_config, dict) else {}
    limit = _resolved_total_comment_limit(task, config)
    used = _total_comment_action_count(session, task, exclude_action_id=action.id)
    return used >= limit


def _comment_success_limit_reached(session: Session, action: Action, payload: PostCommentPayload) -> bool:
    task = session.get(Task, action.task_id)
    config = task.type_config if task and isinstance(task.type_config, dict) else {}
    target = int(config.get("target_comments_per_message") or 0)
    if not task or task.type != "channel_comment" or target <= 0:
        return False
    _lower, limit = quantity_jitter_bounds(target, float(config.get("comment_count_jitter") or 0))
    success_count = 0
    rows = session.scalars(
        select(Action.payload).where(
            Action.id != action.id,
            Action.task_id == action.task_id,
            Action.action_type == "post_comment",
            Action.status == "success",
        )
    )
    for existing_payload in rows:
        if _same_comment_message(existing_payload, payload):
            success_count += 1
    return success_count >= limit


def _same_comment_message(existing_payload: object, payload: PostCommentPayload) -> bool:
    if not isinstance(existing_payload, dict):
        return False
    channel_message_id = _payload_int(existing_payload, "channel_message_id")
    message_id = _payload_int(existing_payload, "message_id")
    return (bool(payload.channel_message_id) and channel_message_id == payload.channel_message_id) or message_id == payload.message_id


def _payload_int(payload: dict, key: str) -> int:
    raw = payload.get(key)
    if isinstance(raw, int):
        return raw
    text = str(raw or "").strip()
    return int(text) if text.isdigit() else 0


def _ensure_channel_action_membership(session: Session, action: Action, account: TgAccount, channel_target_id: int | None) -> bool:
    if not channel_target_id:
        _fail_with_policy(
            action,
            FailureType.PEER_INVALID.value,
            "频道互动缺少频道目标标识",
            auto_check="拦截",
            validation_stage="account_channel_membership",
        )
        return False
    channel = session.get(OperationTarget, int(channel_target_id))
    if action.action_type == "post_comment":
        return _ensure_post_comment_membership(session, action, account, channel)
    if channel and channel.tenant_id == action.tenant_id and channel.target_type == "channel" and account_satisfies_authorized_target(channel, account):
        return True
    if _channel_action_has_membership_link(session, action, account, channel):
        return True
    if action.action_type == "like_message" and channel and channel.tenant_id == action.tenant_id and channel.target_type == "channel":
        _defer_channel_action_for_membership(
            session,
            action,
            account,
            channel,
            "账号未关注目标频道，等待准入后继续点赞",
            error_code="channel_membership_required",
            require_send=False,
        )
        return False
    _fail_with_policy(
        action,
        FailureType.ACCOUNT_UNAVAILABLE.value,
        "账号未关注目标频道，已拦截主互动动作",
        auto_check="拦截",
        validation_stage="account_channel_membership",
    )
    return False


def _channel_action_has_membership_link(
    session: Session,
    action: Action,
    account: TgAccount,
    channel: OperationTarget | None,
) -> bool:
    if not channel or channel.tenant_id != action.tenant_id or channel.target_type != "channel":
        return False
    group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.tg_peer_id == channel.tg_peer_id))
    return bool(group and _channel_account_link(session, action.tenant_id, group.id, account.id))


def _ensure_post_comment_membership(session: Session, action: Action, account: TgAccount, channel: OperationTarget | None) -> bool:
    if not channel or channel.tenant_id != action.tenant_id or channel.target_type != "channel":
        _fail_with_policy(action, FailureType.PEER_INVALID.value, "频道评论目标不存在", auto_check="拦截", validation_stage="account_channel_membership")
        return False
    group = linked_channel_group(session, channel, create=False)
    link = _channel_account_link(session, action.tenant_id, group.id, account.id) if group else None
    if link and link.can_send:
        return True
    if link and link.can_send is False:
        _skip_comment_account_permission_denied(action, "该账号对频道评论区不可发言")
        return False
    _defer_comment_for_membership(session, action, account, channel, "账号未关注 / 加入目标频道，等待准入后继续评论")
    return False


def _defer_comment_for_membership(session: Session, action: Action, account: TgAccount, channel: OperationTarget, detail: str) -> None:
    _defer_channel_action_for_membership(
        session,
        action,
        account,
        channel,
        detail,
        error_code="comment_membership_required",
        require_send=True,
    )


def _defer_channel_action_for_membership(
    session: Session,
    action: Action,
    account: TgAccount,
    channel: OperationTarget,
    detail: str,
    *,
    error_code: str,
    require_send: bool,
) -> None:
    task = session.get(Task, action.task_id)
    if task and not _open_channel_membership_action(session, action, account.id, channel.id):
        create_membership_action(session, task, account.id, _now(), _channel_membership_payload(channel, require_send=require_send))
    action.status = "pending"
    action.scheduled_at = _now() + _COMMENT_MEMBERSHIP_RETRY_DELAY
    action.executed_at = None
    _clear_action_lease(action)
    action.result = {
        "success": False,
        "error_code": error_code,
        "error_message": detail,
        "auto_check": "等待准入",
        "validation_stage": "account_channel_membership",
    }
    _release_runtime_resources(action)


def _open_channel_membership_action(session: Session, action: Action, account_id: int, channel_target_id: int) -> Action | None:
    return session.scalar(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.action_type.in_(["ensure_channel_membership", "ensure_target_membership"]),
            Action.account_id == account_id,
            Action.status.in_(["pending", "claiming", "executing", "retryable_failed"]),
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
        )
    )


def _channel_membership_payload(channel: OperationTarget, *, require_send: bool) -> EnsureChannelMembershipPayload:
    return EnsureChannelMembershipPayload(
        channel_id=channel.tg_peer_id,
        channel_target_id=channel.id,
        target_type=channel.target_type,
        target_display=channel.title,
        target_username=channel.username or "",
        invite_link=channel.username or channel.tg_peer_id,
        require_send=require_send,
    )


def _apply_operation_result(action: Action, account: TgAccount, ok: bool, failure_type: str = "", detail: str = "", *, attempt: ExecutionAttempt | None = None) -> None:
    _apply_send_result(action, account, ok, "", failure_type, detail, attempt=attempt)


def _classify_membership_failure(failure_type: str, detail: str) -> str:
    if _is_account_frozen_failure(failure_type, detail):
        return FailureType.ACCOUNT_UNAVAILABLE.value
    return failure_type


def _apply_send_result(action: Action, account: TgAccount, ok: bool, remote_id: str = "", failure_type: str = "", detail: str = "", *, attempt: ExecutionAttempt | None = None) -> None:
    if ok:
        action.status = "success"
        action.result = {**(action.result or {}), "success": True, "telegram_msg_id": remote_id, "auto_check": "通过", "validation_stage": "sent"}
        _clear_action_lease(action)
        account.last_active_at = _now()
        _release_runtime_resources(action)
    else:
        _fail(action, failure_type or FailureType.UNKNOWN.value, detail or "执行失败", auto_check="失败", validation_stage="telegram_api")
        if failure_type == FailureType.ACCOUNT_LIMITED.value:
            account.status = AccountStatus.LIMITED.value
            account.health_score = min(account.health_score, 55)
        if _is_account_frozen_failure(failure_type, detail):
            _mark_account_frozen(account)
        elif _is_account_proxy_failure(failure_type, detail):
            _recover_account_proxy_after_failure(action, account, detail or failure_type)
        elif _is_account_session_failure(failure_type, detail):
            _recover_account_session_after_failure(action, account, detail or failure_type)
        if _is_target_send_permission_failure(failure_type):
            if not _defer_comment_membership_from_gateway_failure(action, account, detail or failure_type):
                _mark_group_account_cannot_send(action, account, detail or failure_type)
                _mark_channel_comment_account_cannot_send(action, account, detail or failure_type)
                _maybe_trigger_send_permission_rescue(action, account, detail or failure_type)
        if failure_type in _COMMENT_THREAD_UNAVAILABLE_FAILURES:
            _close_unavailable_comment_thread(action, failure_type, detail or failure_type)
        if failure_type == FailureType.REACTION_UNAVAILABLE.value:
            _close_unavailable_reaction(action, detail or failure_type)
        if action.status == "failed":
            _apply_default_failure_policy(action, failure_type or FailureType.UNKNOWN.value)
    action.executed_at = None if action.status == "pending" else _now()
    _update_reply_result_stats(action, ok, failure_type or "")
    _finish_execution_attempt(attempt, action, remote_id=remote_id, failure_type=failure_type or "", detail=detail or "")


def _update_reply_result_stats(action: Action, ok: bool, failure_type: str) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if not payload.get("reply_to_message_id"):
        return
    from sqlalchemy.orm import object_session

    session = object_session(action)
    task = session.get(Task, action.task_id) if session and action.task_id else None
    if not task:
        return
    stats = dict(task.stats or {})
    key = "reply_success_count" if ok else "reply_failure_count"
    stats[key] = int(stats.get(key) or 0) + 1
    if not ok:
        stats["telegram_reply_failure_count"] = int(stats.get("telegram_reply_failure_count") or 0) + 1
        stats["last_reply_failure_type"] = failure_type
    task.stats = stats


def _maybe_trigger_send_permission_rescue(action: Action, account: TgAccount, detail: str) -> None:
    if action.task_type != "group_ai_chat":
        return
    from sqlalchemy.orm import object_session

    session = object_session(action)
    group = session.get(TgGroup, _action_group_id(action)) if session else None
    task = session.get(Task, action.task_id) if session and action.task_id else None
    if not group or not task:
        return
    if permission_failure_count_for_send_action(session, action) <= GROUP_RESCUE_FAILURE_THRESHOLD:
        return
    target_id = _payload_int(action.payload if isinstance(action.payload, dict) else {}, "operation_target_id")
    result = trigger_group_rescue(session, task, group, trigger_account_id=account.id, trigger_reason=detail, operation_target_id=target_id or None)
    _record_group_rescue_result(action, result)


def _record_group_rescue_result(action: Action, result) -> None:
    action.result = {**(action.result or {}), "group_rescue_status": result.status, "group_rescue_detail": result.detail}
    if result.action:
        if result.status == "pending":
            result.action.status = "pending"
            result.action.result = {"rescue_status": "pending"}
        action.result = {**(action.result or {}), "group_rescue_action_id": result.action.id}


def _update_reply_payload_error_stats(action: Action) -> None:
    payload = action.payload if isinstance(action.payload, dict) else {}
    has_reply_meta = any(payload.get(key) for key in ["reply_target_label", "reply_target_author", "reply_target_preview", "reply_target_source"])
    if payload.get("reply_to_message_id") or not (has_reply_meta or payload.get("comment_mode") == "reply"):
        return
    from sqlalchemy.orm import object_session

    session = object_session(action)
    task = session.get(Task, action.task_id) if session and action.task_id else None
    if not task:
        return
    stats = dict(task.stats or {})
    stats["reply_payload_error_count"] = int(stats.get("reply_payload_error_count") or 0) + 1
    task.stats = stats


def _is_account_session_failure(failure_type: str, detail: str) -> bool:
    if failure_type != FailureType.ACCOUNT_UNAVAILABLE.value:
        return False
    text = f"{failure_type} {detail}".lower()
    return any(marker.lower() in text for marker in _ACCOUNT_SESSION_FAILURE_MARKERS)


def _is_account_frozen_failure(failure_type: str, detail: str) -> bool:
    text = f"{failure_type} {detail}".lower()
    return any(marker in text for marker in _ACCOUNT_FROZEN_FAILURE_MARKERS)


def _mark_account_frozen(account: TgAccount) -> None:
    account.status = AccountStatus.SUSPECTED_BANNED.value
    account.health_score = min(account.health_score, FROZEN_ACCOUNT_HEALTH_SCORE)


def _is_account_proxy_failure(failure_type: str, detail: str) -> bool:
    if failure_type != FailureType.ACCOUNT_UNAVAILABLE.value:
        return False
    text = f"{failure_type} {detail}".lower()
    return any(marker.lower() in text for marker in _ACCOUNT_PROXY_FAILURE_MARKERS)


def _is_target_send_permission_failure(failure_type: str) -> bool:
    return failure_type in {FailureType.GROUP_PERMISSION_DENIED.value, FailureType.PEER_INVALID.value}


def _recover_account_proxy_after_failure(action: Action, account: TgAccount, reason: str) -> None:
    from sqlalchemy.orm import object_session

    session = object_session(account) or object_session(action)
    if session is None:
        return
    recovered = attempt_primary_proxy_recovery(session, account, actor="task-dispatcher", reason=reason)
    if recovered is None:
        return
    action.result = {
        **(action.result or {}),
        "proxy_recovered": True,
        "recovered_proxy_id": recovered.id,
    }
    action.status = "pending"
    action.executed_at = None


def _recover_account_session_after_failure(action: Action, account: TgAccount, reason: str) -> None:
    from sqlalchemy.orm import object_session

    session = object_session(account) or object_session(action)
    if session is None:
        return
    recovered = attempt_standby_authorization_recovery(session, account, actor="task-dispatcher", reason=reason)
    if recovered is None:
        account.status = AccountStatus.NEED_RELOGIN.value
        account.health_score = min(account.health_score, 45)
        return
    action.result = {
        **(action.result or {}),
        "account_recovered": True,
        "recovered_authorization_id": recovered.id,
    }
    action.status = "pending"
    action.executed_at = None
    _release_runtime_resources(action)


def _defer_comment_membership_from_gateway_failure(action: Action, account: TgAccount, detail: str) -> bool:
    from sqlalchemy.orm import object_session

    if action.action_type != "post_comment" or not _comment_failure_requires_membership(detail):
        return False
    session = object_session(action)
    if not session:
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel = session.get(OperationTarget, int(payload.get("channel_target_id") or 0))
    if not channel or channel.tenant_id != action.tenant_id or channel.target_type != "channel":
        return False
    _defer_comment_for_membership(session, action, account, channel, "账号未关注 / 加入目标频道，等待准入后继续评论")
    return True


def _comment_failure_requires_membership(detail: str) -> bool:
    lowered = str(detail or "").lower()
    return any(marker in lowered for marker in _COMMENT_MEMBERSHIP_REQUIRED_MARKERS)


def _mark_group_account_cannot_send(action: Action, account: TgAccount, detail: str) -> None:
    from sqlalchemy.orm import object_session

    session = object_session(action)
    if not session:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    group_id = int(payload.get("group_id") or 0)
    if not group_id:
        return
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == action.tenant_id,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.account_id == account.id,
        )
    )
    if not link:
        return
    link.can_send = False
    link.permission_label = detail[:80] or "群无权限或账号不可发言"


def _mark_channel_comment_account_cannot_send(action: Action, account: TgAccount, detail: str) -> None:
    from sqlalchemy.orm import object_session

    if action.action_type != "post_comment":
        return
    session = object_session(action)
    if not session:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id = int(payload.get("channel_target_id") or 0)
    channel = session.get(OperationTarget, channel_target_id) if channel_target_id else None
    if not channel or channel.tenant_id != action.tenant_id or channel.target_type != "channel":
        return
    group = linked_channel_group(session, channel, create=True)
    link = _channel_account_link(session, action.tenant_id, group.id, account.id)
    if link is None:
        link = TgGroupAccount(tenant_id=action.tenant_id, group_id=group.id, account_id=account.id)
        session.add(link)
    link.can_send = False
    link.permission_label = (detail or "频道评论区不可发言")[:80]
    _skip_comment_account_permission_denied(action, detail or "频道评论区不可发言")
    _skip_channel_comment_account_siblings(session, action, channel_target_id, account.id, detail)


def _channel_account_link(session: Session, tenant_id: int, group_id: int, account_id: int) -> TgGroupAccount | None:
    return session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == tenant_id,
            TgGroupAccount.group_id == group_id,
            TgGroupAccount.account_id == account_id,
        )
    )


def _skip_channel_comment_account_siblings(session: Session, action: Action, channel_target_id: int, account_id: int, detail: str) -> None:
    reason = f"该账号对频道评论区不可发言，已跳过该账号后续频道评论：{detail}"
    siblings = session.scalars(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.id != action.id,
            Action.action_type == "post_comment",
            Action.account_id == account_id,
            Action.status.in_(["pending", "claiming", "retryable_failed"]),
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
        )
    )
    for sibling in siblings:
        _skip(sibling, "comment_account_permission_denied", reason)
        sibling.result = {**(sibling.result or {}), "validation_stage": "channel_comment_runtime"}


def _skip_comment_account_permission_denied(action: Action, detail: str) -> None:
    _skip(action, "comment_account_permission_denied", f"该账号对频道评论区不可发言：{detail}")
    action.result = {**(action.result or {}), "validation_stage": "channel_comment_runtime"}


def _close_unavailable_comment_thread(action: Action, failure_type: str, detail: str) -> None:
    from sqlalchemy.orm import object_session

    if action.action_type != "post_comment":
        return
    session = object_session(action)
    if not session:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id, channel_message_id = _comment_thread_identity(payload)
    if not channel_target_id or not channel_message_id:
        return
    message = session.get(ChannelMessage, channel_message_id)
    if message and message.tenant_id == action.tenant_id and message.channel_target_id == channel_target_id:
        message.comment_available = False
    _skip_comment_unavailable_message(action, detail)
    _skip_unavailable_comment_siblings(session, action, channel_target_id, channel_message_id, failure_type, detail)


def _comment_thread_identity(payload: dict) -> tuple[int, int]:
    channel_target_id = int(payload.get("channel_target_id") or 0)
    channel_message_id = int(payload.get("channel_message_id") or 0)
    return channel_target_id, channel_message_id


def _skip_unavailable_comment_siblings(
    session: Session,
    action: Action,
    channel_target_id: int,
    channel_message_id: int,
    failure_type: str,
    detail: str,
) -> None:
    code = _COMMENT_THREAD_SKIP_CODES.get(failure_type, "comment_unavailable_sibling")
    reason = _comment_thread_skip_reason(failure_type, detail)
    siblings = session.scalars(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.id != action.id,
            Action.action_type == "post_comment",
            Action.status.in_(["pending", "claiming", "retryable_failed"]),
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
            Action.payload["channel_message_id"].as_integer() == channel_message_id,
        )
    )
    for sibling in siblings:
        _skip(sibling, code, reason)
        sibling.result = {**(sibling.result or {}), "validation_stage": "channel_comment_runtime"}


def _comment_thread_skip_reason(failure_type: str, detail: str) -> str:
    if failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        return f"评论目标权限不可用，已跳过同帖待执行评论：{detail}"
    return f"评论区不可用，已跳过同帖待执行评论：{detail}"


def _skip_comment_unavailable_message(action: Action, detail: str) -> None:
    _skip(action, "comment_unavailable_message", f"该消息无法评论：{detail}")
    action.result = {**(action.result or {}), "validation_stage": "channel_comment_runtime"}


def _close_unavailable_reaction(action: Action, detail: str) -> None:
    from sqlalchemy.orm import object_session

    if action.action_type != "like_message":
        return
    session = object_session(action)
    if not session:
        return
    payload = action.payload if isinstance(action.payload, dict) else {}
    channel_target_id = int(payload.get("channel_target_id") or 0)
    channel_message_id = int(payload.get("channel_message_id") or 0)
    if not channel_target_id or not channel_message_id:
        return
    _skip_like_unavailable_message(action, detail)
    siblings = _unavailable_reaction_siblings(session, action, channel_target_id, channel_message_id)
    for sibling in siblings:
        _skip(sibling, _REACTION_UNAVAILABLE_SKIP_CODE, f"频道消息不可点赞，已跳过同帖待执行点赞：{detail}")
        sibling.result = {**(sibling.result or {}), "validation_stage": "channel_like_runtime"}


def _unavailable_reaction_siblings(session: Session, action: Action, channel_target_id: int, channel_message_id: int):
    return session.scalars(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.id != action.id,
            Action.action_type == "like_message",
            Action.status.in_(["pending", "claiming", "retryable_failed"]),
            Action.payload["channel_target_id"].as_integer() == channel_target_id,
            Action.payload["channel_message_id"].as_integer() == channel_message_id,
        )
    )


def _skip_like_unavailable_message(action: Action, detail: str) -> None:
    _skip(action, "reaction_unavailable_message", f"该消息无法点赞：{detail}")
    action.result = {**(action.result or {}), "validation_stage": "channel_like_runtime"}


def _fail(action: Action, failure_type: str, detail: str, *, auto_check: str = "失败", validation_stage: str = "") -> None:
    action.status = "failed"
    _clear_action_lease(action)
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": failure_type,
        "error_message": detail,
        "auto_check": auto_check,
        "validation_stage": validation_stage,
    }
    action.executed_at = _now()
    _release_runtime_resources(action)


def _mark_unknown_after_send(session: Session, action: Action, detail: str) -> None:
    action.status = "unknown_after_send"
    _clear_action_lease(action)
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": "unknown_after_send",
        "error_message": detail or "执行项已进入 TG 调用边界但本地结果未知，需人工或补偿确认",
        "auto_check": "结果未知",
        "validation_stage": "telegram_api",
    }
    action.executed_at = _now()
    attempt = _latest_open_gateway_attempt(session, action)
    if attempt:
        attempt.after_call_at = _now()
        attempt.status = "result_unknown"
        attempt.failure_type = "unknown_after_send"
        attempt.failure_detail = detail or ""
        attempt.result_snapshot = dict(action.result or {})
    _release_runtime_resources(action)


def _fail_with_policy(action: Action, failure_type: str, detail: str, *, auto_check: str = "失败", validation_stage: str = "") -> None:
    _fail(action, failure_type, detail, auto_check=auto_check, validation_stage=validation_stage)
    _apply_default_failure_policy(action, failure_type)


def _skip(action: Action, code: str, detail: str) -> None:
    action.status = "skipped"
    _clear_action_lease(action)
    action.result = {**(action.result or {}), "success": False, "error_code": code, "error_message": detail, "auto_check": "跳过", "validation_stage": "context"}
    action.executed_at = _now()
    _release_runtime_resources(action)


def _defer(action: Action, scheduled_at, code: str, detail: str) -> None:
    action.status = "pending"
    action.scheduled_at = scheduled_at
    _clear_action_lease(action)
    action.result = {**(action.result or {}), "success": False, "error_code": code, "error_message": detail, "auto_check": "延后", "validation_stage": "account_policy"}
    _release_runtime_resources(action)


def _account_after_global_policy(session: Session, action: Action, account: TgAccount, *, allow_reassign: bool = True) -> TgAccount | None:
    if _is_hard_hourly_membership_action(session, action):
        return account
    if _is_hard_hourly_send_action(action):
        _record_hard_hourly_capacity_override(action)
        return account
    decision = account_capacity_decision(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        scheduled_at=_capacity_check_at(action),
        exclude_action_ids=_capacity_excluded_action_ids(session, action, account.id),
    )
    if decision.available:
        return account
    replacement = _replacement_account_for_action(session, action, account) if allow_reassign else None
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


def _capacity_excluded_action_ids(session: Session, action: Action, account_id: int) -> set[str]:
    excluded = {action.id}
    if not _is_hard_hourly_send_action(action):
        return excluded
    rows = session.scalars(
        select(Action.id).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.action_type == "send_message",
            Action.account_id == account_id,
            Action.status == "pending",
            Action.scheduled_at <= _now(),
            Action.payload["hard_hourly_target"].as_boolean().is_(True),
            Action.id != action.id,
        )
    )
    excluded.update(str(action_id) for action_id in rows)
    return excluded


def _is_hard_hourly_send_action(action: Action) -> bool:
    payload = action.payload if isinstance(action.payload, dict) else {}
    return action.action_type == "send_message" and bool(payload.get("hard_hourly_target"))


def _record_hard_hourly_capacity_override(action: Action) -> None:
    action.result = {
        **(action.result or {}),
        "account_policy_action": "hard_hourly_capacity_override",
        "account_policy_reason": "hard_hourly_target",
    }


def _skip_expired_hard_hourly_action(session: Session, action: Action) -> bool:
    if not _hard_hourly_bucket_expired(action):
        return False
    _skip(action, "hard_hourly_bucket_expired", "硬目标小时窗口已结束，过期补量已跳过")
    task = session.get(Task, action.task_id) if action.task_id else None
    if task:
        task.next_run_at = _now()
    return True


def _hard_hourly_bucket_expired(action: Action) -> bool:
    if not _is_hard_hourly_send_action(action):
        return False
    payload = action.payload if isinstance(action.payload, dict) else {}
    bucket_value = str(payload.get("hard_hourly_bucket") or "").strip()
    if not bucket_value:
        return False
    try:
        bucket_start = datetime.fromisoformat(bucket_value)
    except ValueError:
        return False
    now_value = _now()
    if bucket_start.tzinfo is None:
        now_value = now_value.replace(tzinfo=None)
    else:
        now_value = now_value.replace(tzinfo=BEIJING_TZ).astimezone(bucket_start.tzinfo)
    return bucket_start + timedelta(hours=1) <= now_value


def _replacement_account_for_action(session: Session, action: Action, account: TgAccount) -> TgAccount | None:
    task = session.get(Task, action.task_id)
    if not task:
        return None
    payload = action.payload if isinstance(action.payload, dict) else {}
    scheduled_at = _capacity_check_at(action)
    channel_target_id = int(payload.get("channel_target_id") or 0)
    if channel_target_id:
        channel = session.get(OperationTarget, channel_target_id)
        group = (
            session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.tg_peer_id == channel.tg_peer_id))
            if channel and channel.tenant_id == action.tenant_id
            else None
        )
        if not group:
            return None
        member_ids = list(
            session.scalars(
                select(TgGroupAccount.account_id).where(
                    TgGroupAccount.tenant_id == action.tenant_id,
                    TgGroupAccount.group_id == group.id,
                )
            )
        )
        if not member_ids:
            return None
        candidates = select_task_accounts(
            session,
            action.tenant_id,
            task.account_config or {},
            scheduled_at=scheduled_at,
            limit=_replacement_scan_limit(action, task),
            enforce_shard=True,
        )
        return next((candidate for candidate in candidates if candidate.id != account.id and candidate.id in member_ids), None)
    group_id = int(payload.get("group_id") or 0) or None
    candidates = select_task_accounts(
        session,
        action.tenant_id,
        task.account_config or {},
        target_group_id=group_id,
        scheduled_at=scheduled_at,
        limit=_replacement_scan_limit(action, task),
        enforce_shard=True,
    )
    return next((candidate for candidate in candidates if candidate.id != account.id), None)


def _replacement_scan_limit(action: Action, task: Task) -> int:
    payload = action.payload if isinstance(action.payload, dict) else {}
    if not bool(payload.get("hard_hourly_target")):
        return 10
    goal = int((task.type_config or {}).get("hourly_min_messages") or 0)
    planned_deficit = int(payload.get("hard_hourly_deficit_at_plan") or 0)
    return max(10, goal, planned_deficit)


def _capacity_check_at(action: Action) -> datetime:
    scheduled_at = _naive_datetime(action.scheduled_at)
    now_value = _now()
    if scheduled_at is None:
        return now_value
    if _released_before(action) and scheduled_at < now_value - timedelta(seconds=1):
        return now_value
    return scheduled_at


def _naive_datetime(value: datetime | None) -> datetime | None:
    return value.replace(tzinfo=None) if value and value.tzinfo is not None else value


def _released_before(action: Action) -> bool:
    result = action.result if isinstance(action.result, dict) else {}
    return bool(result.get("claim_released_reason") or result.get("claim_released_at"))


def _mark_executing(action: Action, *, lease_seconds: int = 1800) -> None:
    action.status = "executing"
    action.lease_owner = _lease_owner()
    action.lease_expires_at = _now() + timedelta(seconds=max(60, int(lease_seconds or 1800)))
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None


def _clear_action_lease(action: Action) -> None:
    action.lease_owner = ""
    action.lease_expires_at = None
    action.claim_owner = ""
    action.claim_token = ""
    action.claim_expires_at = None


def _lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


def _begin_execution_attempt(session: Session, action: Action, account: TgAccount) -> ExecutionAttempt:
    attempt_no = (
        session.scalar(select(func.max(ExecutionAttempt.attempt_no)).where(ExecutionAttempt.action_id == action.id))
        or 0
    ) + 1
    attempt = ExecutionAttempt(
        tenant_id=action.tenant_id,
        action_id=action.id,
        worker_id=_lease_owner(),
        account_id=account.id,
        attempt_no=attempt_no,
        status="before_call",
        before_call_at=_now(),
        result_snapshot={},
    )
    session.add(attempt)
    session.flush()
    return attempt


def _mark_gateway_call_started(session: Session, attempt: ExecutionAttempt) -> None:
    attempt.gateway_call_started_at = _now()
    attempt.status = "gateway_call_started"
    session.commit()


def _gateway_call_started(session: Session, action: Action) -> bool:
    return _latest_open_gateway_attempt(session, action) is not None


def _latest_open_gateway_attempt(session: Session, action: Action) -> ExecutionAttempt | None:
    return session.scalar(
        select(ExecutionAttempt)
        .where(
            ExecutionAttempt.action_id == action.id,
            ExecutionAttempt.gateway_call_started_at.is_not(None),
            ExecutionAttempt.after_call_at.is_(None),
        )
        .order_by(ExecutionAttempt.attempt_no.desc())
        .limit(1)
    )


def _finish_execution_attempt(attempt: ExecutionAttempt | None, action: Action, *, remote_id: str = "", failure_type: str = "", detail: str = "") -> None:
    if not attempt:
        return
    attempt.after_call_at = _now()
    attempt.remote_message_id = remote_id or ""
    attempt.failure_type = failure_type or ""
    attempt.failure_detail = detail or ""
    attempt.status = "success" if action.status == "success" else "failed" if action.status in {"failed", "retryable_failed"} else action.status
    attempt.result_snapshot = dict(action.result or {})


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
                next_retry_at = _now() + timedelta(seconds=retry_after)
                action.status = "pending"
                action.scheduled_at = next_retry_at
                _clear_action_lease(action)
                action.result = {
                    **(action.result or {}),
                    "auto_check": "延后",
                    "validation_stage": "failure_policy",
                    "retry_after_seconds": retry_after,
                    "next_retry_at": next_retry_at.isoformat(),
                    "rate_limit_source": "telegram",
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
    if payload.hard_hourly_target and not payload.reply_to_message_id:
        return False
    return _newer_context_count(session, payload) >= payload.context_expire_after_messages


def _newer_context_count(session: Session, payload: SendMessagePayload) -> int:
    snapshot = session.get(GroupContextMessage, payload.context_snapshot_message_id)
    filters = [
        GroupContextMessage.group_id == payload.group_id,
        GroupContextMessage.is_bot.is_(False),
    ]
    if snapshot and snapshot.sent_at:
        filters.extend(
            [
                GroupContextMessage.sent_at.is_not(None),
                GroupContextMessage.sent_at > snapshot.sent_at,
            ]
        )
    else:
        filters.append(GroupContextMessage.id > payload.context_snapshot_message_id)
    newer_count = session.scalar(select(func.count(GroupContextMessage.id)).where(*filters)) or 0
    return int(newer_count)


def _skip_context_expired_cycle(session: Session, current: Action, payload: SendMessagePayload) -> None:
    if not payload.cycle_id:
        return
    pending_actions = list(
        session.scalars(
            select(Action).where(
                Action.id != current.id,
                Action.tenant_id == current.tenant_id,
                Action.task_id == current.task_id,
                Action.action_type == "send_message",
                Action.status == "pending",
            )
        )
    )
    for action in pending_actions:
        action_payload = action.payload if isinstance(action.payload, dict) else {}
        if _same_context_cycle(action_payload, payload):
            _skip(action, "context_expired", "上下文已过期，跳过本轮剩余发言")
    task = session.get(Task, current.task_id)
    if task:
        task.next_run_at = _now()


def _same_context_cycle(action_payload: dict, payload: SendMessagePayload) -> bool:
    return (
        action_payload.get("cycle_id") == payload.cycle_id
        and action_payload.get("group_id") == payload.group_id
        and action_payload.get("context_snapshot_message_id") == payload.context_snapshot_message_id
    )


__all__ = ["claim_actions", "dispatch_action", "due_actions", "recover_expired_claims", "recover_expired_hard_hourly_actions"]
