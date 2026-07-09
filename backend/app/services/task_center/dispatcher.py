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

from app.admin_chats import send_admin_chat_broadcast
from app.integrations.telegram import DeveloperAppCredentials, OperationResult, OutboundSegment
from app.config import get_settings
from app.models import AccountStatus, Action, ChannelMessage, ExecutionAttempt, FailureType, GroupAuthStatus, GroupContextMessage, OperationTarget, ReviewQueue, Task, Tenant, TgAccount, TgGroup, TgGroupAccount, VerificationTask
from app.models import AccountEnvironmentBinding, AccountProxy, AccountProxyBinding, TelegramDeveloperApp, TgAccountAuthorization
from app.security import decrypt_secret
from app.services._common import _now, audit, gateway
from app.services.account_online_state import is_account_online_ready
from app.services.account_authorizations import attempt_primary_proxy_recovery, attempt_standby_authorization_recovery
from app.services.account_capacity import account_capacity_decision
from app.services.content_filters import filter_outbound_content, rewrite_rejected_content
from app.services.developer_apps import DIRECT_ONLY_TASK_TYPES, credentials_for_account, credentials_for_developer_app
from app.services.ai_config import get_scheduling_setting
from app.services.membership_challenges import auto_resolve_image_verification, auto_resolve_text_verification, read_challenge_context_with_fallback, record_challenge_attempt
from app.services.notifications import NotificationResult, send_telegram_bot_message
from app.services.proxy_airport_subscription import failover_proxy_airport_node_binding
from app.services.required_channel_prompts import (
    REQUIRED_CHANNEL_BLOCKED_LABEL,
    REQUIRED_CHANNEL_PERMISSION_LABEL,
    REQUIRED_CHANNEL_PROMPT_PREVIEW_LENGTH,
    required_channel_prompt_applies_to_send,
    required_channel_references,
)
from app.services.verification import create_verification_task
from app.timezone import BEIJING_TZ, as_beijing

from .account_pool import account_matches_current_shard, current_account_shard, select_task_accounts
from .account_voice_profiles import upsert_group_stance_memory
from .ai_generator import AI_GENERATION_UNAVAILABLE_MESSAGE, AiGenerationUnavailable, generate_group_messages
from .ai_message_memory import DuplicateMessageReservation, ensure_group_ai_message_sendable, mark_group_ai_message_result, reserve_group_ai_message
from .channel_membership import account_satisfies_authorized_target, linked_channel_group, mark_channel_membership_joined
from .executors.common import quantity_jitter_bounds, stats_inc
from .executors.channel_comment import _resolved_total_comment_limit, _total_comment_action_count
from .group_rescue import GROUP_RESCUE_FAILURE_THRESHOLD, infer_rescue_admin_rate_limit, permission_failure_count_for_send_action, refresh_group_rescue_action, trigger_group_rescue
from .payloads import DeprecatedGroupRescuePayload, DeleteMessagePayload, EnsureChannelMembershipPayload, InviteGroupAccountPayload, LikeMessagePayload, PostCommentPayload, SearchJoinPayload, SearchRankDeboostPayload, SendMessagePayload, ViewMessagePayload, create_membership_action, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review
from .search_join_linking import create_linked_dispatch_if_membership_observed
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
DISPATCHER_DB_ERROR_RETRY_DELAY_SECONDS = 10
_COMMENT_MEMBERSHIP_REQUIRED_MARKERS = (
    "not participant",
    "not a participant",
    "usernotparticipant",
    "未关注",
    "未加入",
    "不在目标",
    "无法进入关联讨论区",
)
_SEARCH_JOIN_PROXY_FAILURE_CODES = {
    "proxy_auth_failed",
    "proxy_connect_timeout",
    "proxy_connection_failed",
    "proxy_egress_guard_failed",
    "proxy_node_unreachable",
}
_GROUP_SEND_LINKED_CHANNEL_REQUIRED_MARKERS = (
    "未关注",
    "关注",
    "follow",
    "subscribe",
    "未加入目标频道",
    "无法进入关联讨论区",
    "缓存频道不可访问",
    "共同群",
    "共同✈️群",
    "共同飞机群",
)
_GROUP_SEND_BUTTON_VERIFICATION_MARKERS = ("按钮", "button", "click", "点击")
_GROUP_SEND_CONFIRM_BUTTON_MARKERS = ("我已加入", "我已关注", "已关注", "完成验证", "完成关注", "确认")
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
ACTIVE_SEARCH_JOIN_AUTHORIZATION_STATUSES = {"active", "standby"}


@dataclass(frozen=True)
class SearchJoinRuntimeAuthorization:
    session_ciphertext: str
    credentials: DeveloperAppCredentials
RECENT_REQUIRED_CHANNEL_PROMPT_LIMIT = 25
RECENT_REQUIRED_CHANNEL_PROMPT_LOOKBACK_HOURS = 6
REQUIRED_CHANNEL_ADMISSION_RETRY_SECONDS = 300
VERIFICATION_READER_CANDIDATE_LIMIT = 5
HARD_HOURLY_OVERDUE_SEND_PRIORITY_SECONDS = 300
GROUP_RESCUE_INFLIGHT_CONFLICT_BACKOFF_SECONDS = 30
AI_DISPATCH_GENERATION_BATCH_SIZE = 10
AI_DISPATCH_CANDIDATE_SHORTFALL_MESSAGE = "AI 普通发言候选不足，已跳过本批次发送"
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
class MembershipRateLimit:
    retry_at: datetime
    detail: str
    source: str
    retry_after: int


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
        if action.action_type == "search_join":
            return _dispatch_search_join(session, action, account, payload)
        if action.action_type == "search_rank_deboost":
            return _dispatch_search_rank_deboost(session, action, account, payload)
        if action.action_type in {"view_message", "like_message", "post_comment"} and not _ensure_channel_action_membership(session, action, account, payload.channel_target_id):
            return True
        credentials = credentials_for_account(session, account, use_proxy=action.task_type not in DIRECT_ONLY_TASK_TYPES)
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


def mark_dispatcher_db_error(session: Session, action_id: str, detail: str) -> bool:
    action = session.get(Action, action_id)
    if not action:
        return False
    if _latest_open_gateway_attempt(session, action):
        _mark_unknown_after_send(session, action, detail)
        return True
    _release_dispatcher_db_error(action, detail)
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
    candidates = _claimable_candidates(list(session.scalars(stmt)))
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
        if _skip_resolved_invite_group_account_action(session, action):
            session.commit()
            continue
        if not _reserve_runtime_resources(action):
            result = action.result or {}
            delay_seconds = _runtime_resource_retry_delay(action, result)
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


def _claimable_candidates(candidates: list[Action]) -> list[Action]:
    rescue_admins: set[int] = set()
    selected: list[Action] = []
    for action in candidates:
        if action.action_type != "invite_group_account" or action.account_id is None:
            selected.append(action)
            continue
        account_id = int(action.account_id)
        if account_id in rescue_admins:
            continue
        rescue_admins.add(account_id)
        selected.append(action)
    return selected


def _runtime_resource_retry_delay(action: Action, result: dict) -> int:
    delay_seconds = int(result.get("rate_limit_wait_seconds") or result.get("runtime_resource_wait_seconds") or 0)
    if action.action_type != "invite_group_account":
        return delay_seconds
    if str(result.get("runtime_resource_reason") or "") != "account_inflight_conflict":
        return delay_seconds
    return max(delay_seconds, GROUP_RESCUE_INFLIGHT_CONFLICT_BACKOFF_SECONDS)


def _skip_resolved_invite_group_account_action(session: Session, action: Action) -> bool:
    if action.action_type != "invite_group_account":
        return False
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
    except (ValidationError, ValueError):
        return False
    target = session.get(TgAccount, payload.target_account_id or 0)
    if not target:
        return False
    if target.deleted_at is not None or target.status != AccountStatus.ACTIVE.value:
        _skip(action, "admission_retry_target_inactive", "被救援账号已不是当前在线账号，跳过过期入群邀请")
        action.result = {**(action.result or {}), "rescue_status": "stale_skipped"}
        return True
    link = session.scalar(
        select(TgGroupAccount).where(
            TgGroupAccount.tenant_id == action.tenant_id,
            TgGroupAccount.group_id == payload.group_id,
            TgGroupAccount.account_id == target.id,
        )
    )
    if not link or not link.can_send:
        return False
    _skip(action, "admission_retry_target_already_joined", "被救援账号已在目标群可发言，跳过过期入群邀请")
    action.result = {**(action.result or {}), "rescue_status": "already_joined_skipped"}
    return True


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
    if account.account_identity == "code_receiver":
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "接码专用账号不参与任务执行", auto_check="拦截", validation_stage="account_identity")
        return False
    if account.account_identity == "rank_deboost" and action.action_type != "search_rank_deboost":
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "降权专用账号不参与其他任务执行", auto_check="拦截", validation_stage="account_identity")
        _record_rank_deboost_isolation_alert(session, action, account, "rank_deboost_account_used_by_other")
        return False
    if account.account_identity != "rank_deboost" and action.action_type == "search_rank_deboost":
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "搜索排名观察任务只能使用专用账号", auto_check="拦截", validation_stage="account_identity")
        _record_rank_deboost_isolation_alert(session, action, account, "deboost_task_used_normal_account")
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
    if _is_group_rescue_action(action):
        _record_group_rescue_capacity_override(action)
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
    detail = decision.reason or "账号全局限额或冷却中，已延后执行"
    _defer(
        action,
        decision.defer_until or (_now() + timedelta(seconds=60)),
        "global_account_policy",
        detail,
    )
    _maybe_trigger_deferred_membership_rescue(session, action, account, detail)
    return False


def _record_rank_deboost_isolation_alert(session: Session, action: Action, account: TgAccount, violation: str) -> None:
    """账号组隔离校验失败时生成告警（不阻断已有失败流程）。"""
    from app.services.search_rank_deboost_alerts import record_account_isolation_violation_alert

    record_account_isolation_violation_alert(
        session,
        tenant_id=action.tenant_id,
        task_id=action.task_id,
        action_id=action.id,
        account_id=int(account.id),
        violation=violation,
    )


def _is_membership_action(action: Action) -> bool:
    return action.action_type in MEMBERSHIP_ACTION_TYPES


def _is_group_rescue_action(action: Action) -> bool:
    return action.action_type == "invite_group_account"


def _record_group_rescue_capacity_override(action: Action) -> None:
    action.result = {
        **(action.result or {}),
        "account_policy_action": "group_rescue_capacity_override",
        "account_policy_reason": "rescue_admin_invite",
    }


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
    if len(contents) < len(batch):
        stats_inc(task, "normal_candidate_shortfall_count")
        if not contents:
            raise AiGenerationUnavailable(AI_DISPATCH_CANDIDATE_SHORTFALL_MESSAGE)
    _store_generated_send_payloads(session, batch, contents, tokens)
    refreshed = SendMessagePayload.model_validate(action.payload or {})
    if refreshed.quality_skip_reason == "duplicate_message":
        raise AiGenerationUnavailable("AI 活群生成内容重复，已拦截")
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
    config["generation_slots"] = _payload_generation_slots(batch)
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


def _payload_generation_slots(batch: list[tuple[Action, SendMessagePayload]]) -> list[dict]:
    slots: list[dict] = []
    for index, (action, payload) in enumerate(batch, start=1):
        slot = _payload_generation_slot(action, payload, index)
        if slot:
            slots.append(slot)
    return slots


def _payload_generation_slot(action: Action, payload: SendMessagePayload, index: int) -> dict:
    slot_id = str(payload.slot_id or "").strip()
    if not slot_id:
        return {}
    slot = {
        "slot_id": slot_id,
        "sequence_index": int(payload.turn_index or index),
        "account_id": action.account_id,
        "act_type": payload.act_type,
        "account_profile": payload.account_profile,
        "reply_to_message_id": payload.reply_to_message_id,
        "reply_to_content": payload.reply_target_preview,
    }
    if payload.topic_direction:
        slot["topic_direction"] = dict(payload.topic_direction)
    if payload.teacher_target:
        slot["teacher_target"] = dict(payload.teacher_target)
    return slot


def _store_generated_send_payloads(session: Session, batch: list[tuple[Action, SendMessagePayload]], contents: list[str], tokens: int) -> None:
    for index, ((action, payload), content) in enumerate(zip(batch, contents, strict=False)):
        payload_data = payload.model_dump(mode="json")
        payload_data["message_text"] = str(content or "").strip()
        payload_data["ai_generation_status"] = "success"
        payload_data["ai_generation_tokens"] = int(tokens or 0) if index == 0 else 0
        _attach_generated_message_memory(session, action, payload, payload_data)
        action.payload = payload_data


def _attach_generated_message_memory(session: Session, action: Action, payload: SendMessagePayload, payload_data: dict) -> None:
    content = str(payload_data.get("message_text") or "").strip()
    if action.task_type != "group_ai_chat" or not payload.group_id or not content:
        return
    try:
        memory = reserve_group_ai_message(
            session,
            tenant_id=action.tenant_id,
            group_id=int(payload.group_id),
            task_id=action.task_id,
            account_id=action.account_id,
            raw_text=content,
            topic_direction=_target_label(payload.topic_direction, "title"),
            teacher_target=_target_label(payload.teacher_target, "name"),
            profile_version=payload.profile_version or None,
            profile_match_score=payload.profile_match_score or None,
            profile_match_reason=payload.profile_match_reason,
        )
    except DuplicateMessageReservation as exc:
        _mark_generated_duplicate(action, payload_data, exc)
        return
    mark_group_ai_message_result(session, memory.id, status="reserved", action_id=action.id)
    payload_data["ai_message_memory_id"] = memory.id
    payload_data["semantic_cluster"] = payload_data.get("semantic_cluster") or memory.semantic_cluster


def _mark_generated_duplicate(action: Action, payload_data: dict, exc: DuplicateMessageReservation) -> None:
    payload_data["ai_generation_status"] = "duplicate_rejected"
    payload_data["quality_skip_reason"] = "duplicate_message"
    payload_data["duplicate_risk"] = exc.duplicate_window
    action.payload = payload_data
    _fail(action, "duplicate_message", f"AI 活群生成内容重复：{exc.duplicate_window}", auto_check="拦截", validation_stage="ai_message_memory")
    action.result = {
        **(action.result or {}),
        "duplicate_reference_id": exc.reference_id,
        "duplicate_window": exc.duplicate_window,
    }


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
            if action.status == "failed" and (action.result or {}).get("validation_stage") == "ai_message_memory":
                return True
            _fail_group_ai_send_before_gateway(
                session,
                action,
                payload,
                FailureType.UNKNOWN.value,
                str(exc) or AI_GENERATION_UNAVAILABLE_MESSAGE,
                auto_check="失败",
                validation_stage="ai_generation",
            )
            return True
        content = payload.message_text
        link = session.scalar(select(TgGroupAccount).where(TgGroupAccount.group_id == group.id, TgGroupAccount.account_id == account.id))
        if not link or not link.can_send:
            if link and _defer_send_for_required_channel_admission(action, link):
                return True
            _fail_group_ai_send_before_gateway(
                session,
                action,
                payload,
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
            _fail_group_ai_send_before_gateway(
                session,
                action,
                payload,
                failure_type,
                failure_detail or failure_type,
                auto_check="拦截",
                validation_stage="content_policy",
            )
            return True
        filtered = filter_outbound_content(session, tenant_id=action.tenant_id, group=group, content=content)
        if not filtered.ok:
            _fail_group_ai_send_before_gateway(
                session,
                action,
                payload,
                FailureType.CONTENT_REJECTED.value,
                filtered.reason,
                auto_check="拦截",
                validation_stage="content_policy",
            )
            return True
        if _context_expired(session, payload):
            _skip_context_expired_cycle(session, action, payload)
            _skip(action, "context_expired", "上下文已过期，跳过本轮剩余发言")
            return True
        if not _group_ai_account_online_ready(session, action, account, payload):
            _fail_with_policy(
                action,
                FailureType.ACCOUNT_UNAVAILABLE.value,
                "账号在线状态不可用，等待账号恢复在线后继续执行",
                auto_check="拦截",
                validation_stage="account_online",
            )
            _mark_ai_message_memory_result(
                session,
                payload,
                status="account_offline",
                action_id=action.id,
                result={"error_code": FailureType.ACCOUNT_UNAVAILABLE.value, "validation_stage": "account_online"},
            )
            return True
        if not _group_ai_message_memory_sendable(session, action, payload):
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
        _mark_ai_message_memory_result(
            session,
            payload,
            status="success" if result.ok else "failed",
            action_id=action.id,
            sent_at=_now() if result.ok else None,
            result=dict(action.result or {}),
        )
        if result.ok:
            _update_group_ai_stance_memory(session, action, account, payload, result.remote_message_id or "")
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


def _group_ai_account_online_ready(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SendMessagePayload,
) -> bool:
    if action.task_type != "group_ai_chat":
        return True
    return is_account_online_ready(session, tenant_id=action.tenant_id, account_id=account.id)


def _group_ai_message_memory_sendable(session: Session, action: Action, payload: SendMessagePayload) -> bool:
    if action.task_type != "group_ai_chat":
        return True
    if not payload.ai_message_memory_id:
        result = {
            "error_code": "ai_message_memory_missing",
            "validation_stage": "ai_message_memory",
        }
        _fail(
            action,
            "ai_message_memory_missing",
            "AI 活群发言缺少消息记忆预占，已拦截等待重新规划",
            auto_check="拦截",
            validation_stage="ai_message_memory",
        )
        action.result = {**(action.result or {}), **result}
        return False
    try:
        ensure_group_ai_message_sendable(session, payload.ai_message_memory_id)
    except DuplicateMessageReservation as exc:
        result = {
            "error_code": "duplicate_message",
            "validation_stage": "ai_message_memory",
            "duplicate_reference_id": exc.reference_id,
            "duplicate_window": exc.duplicate_window,
        }
        _fail(action, "duplicate_message", f"AI 活群发送前重复拦截：{exc.duplicate_window}", auto_check="拦截", validation_stage="ai_message_memory")
        action.result = {**(action.result or {}), **result}
        _mark_ai_message_memory_result(
            session,
            payload,
            status="duplicate_before_send",
            action_id=action.id,
            result=result,
        )
        return False
    return True


def _fail_group_ai_send_before_gateway(
    session: Session,
    action: Action,
    payload: SendMessagePayload,
    failure_type: str,
    detail: str,
    *,
    auto_check: str,
    validation_stage: str,
) -> None:
    _fail_with_policy(action, failure_type, detail, auto_check=auto_check, validation_stage=validation_stage)
    _mark_ai_message_memory_result(
        session,
        payload,
        status="failed",
        action_id=action.id,
        result=dict(action.result or {}),
    )


def _mark_ai_message_memory_result(
    session: Session,
    payload: SendMessagePayload,
    *,
    status: str,
    action_id: str,
    sent_at: datetime | None = None,
    result: dict | None = None,
) -> None:
    if not payload.ai_message_memory_id:
        return
    mark_group_ai_message_result(
        session,
        payload.ai_message_memory_id,
        status=status,
        action_id=action_id,
        sent_at=sent_at,
        result=result,
    )


def _update_group_ai_stance_memory(
    session: Session,
    action: Action,
    account: TgAccount,
    payload: SendMessagePayload,
    remote_message_id: str,
) -> None:
    if action.task_type != "group_ai_chat" or not payload.slot_id or not payload.group_id:
        return
    upsert_group_stance_memory(
        session,
        tenant_id=action.tenant_id,
        group_id=int(payload.group_id),
        account_id=account.id,
        topic_direction=_target_label(payload.topic_direction, "title"),
        teacher_target=_target_label(payload.teacher_target, "name"),
        stance="sent",
        act_type=payload.act_type,
        semantic_cluster=payload.semantic_cluster,
        message_id=remote_message_id,
        summary=_stance_summary(payload),
    )


def _target_label(value: dict | None, key: str) -> str:
    if not isinstance(value, dict):
        return ""
    return str(value.get(key) or "").strip()


def _stance_summary(payload: SendMessagePayload) -> str:
    act = payload.act_type or "发言"
    text = (payload.message_text or "").strip()
    return f"{act}：{text[:80]}"


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
    if _defer_existing_group_rescue_admin_rate_limit(session, action):
        return True
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
    _record_group_rescue_admin_rate_limit(action, result)
    status = "invite_success" if result.ok else "invite_failed"
    action.result = {**(action.result or {}), "rescue_status": status, "rescue_detail": result.detail or result.failure_type}


def _record_group_rescue_admin_rate_limit(action: Action, result: OperationResult) -> None:
    detail = result.detail or result.failure_type or ""
    if result.failure_type != FailureType.FLOOD_WAIT.value and "floodwait" not in detail.lower():
        return
    retry_after = _retry_after_seconds(detail)
    if retry_after <= 0:
        return
    from sqlalchemy.orm import object_session

    session = object_session(action)
    task = session.get(Task, action.task_id) if session and action.task_id else None
    if not task:
        return
    retry_at = _now() + timedelta(seconds=retry_after)
    stats = dict(task.stats or {})
    stats["group_rescue_admin_rate_limited_until"] = retry_at.isoformat()
    stats["group_rescue_admin_rate_limit_detail"] = detail
    task.stats = stats


def _defer_existing_group_rescue_admin_rate_limit(session: Session, action: Action) -> bool:
    task = session.get(Task, action.task_id) if action.task_id else None
    retry_at = _task_group_rescue_admin_rate_limited_until(task)
    if (not retry_at or retry_at <= _now()) and task:
        inferred = infer_rescue_admin_rate_limit(session, task, action.account_id)
        if inferred:
            retry_at, detail = inferred
        else:
            detail = ""
    else:
        detail = ""
    if not retry_at or retry_at <= _now():
        return False
    stats = task.stats or {}
    detail = detail or str(stats.get("group_rescue_admin_rate_limit_detail") or "Telegram 救援管理员触发 FloodWait，已延后重试")
    _defer(action, retry_at, FailureType.FLOOD_WAIT.value, detail)
    action.result = {
        **(action.result or {}),
        "validation_stage": "group_rescue_admin_rate_limit",
        "rescue_status": "invite_rate_limited",
        "rescue_detail": detail,
        "retry_after_seconds": max(1, int((retry_at - _now()).total_seconds())),
        "next_retry_at": retry_at.isoformat(),
    }
    return True


def _task_group_rescue_admin_rate_limited_until(task: Task | None) -> datetime | None:
    raw = str((task.stats or {}).get("group_rescue_admin_rate_limited_until") if task else "")
    if not raw:
        return None
    try:
        return _naive_datetime(datetime.fromisoformat(raw))
    except ValueError:
        return None


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
    elif _membership_requires_admin_rescue(result):
        rescued = _try_admin_lift_restriction_and_join(runtime_ctx, _membership_result_detail(result))
        if rescued.ok:
            _apply_operation_result(action, account, True, "", rescued.detail or "admin_rescue_joined", attempt=attempt)
            action.result = {**(action.result or {}), "membership_status": "joined", "admin_restriction_lifted": True}
            return True
        result = rescued
    result_detail = _membership_result_detail(result)
    if result.failure_type == FailureType.FLOOD_WAIT.value:
        _maybe_trigger_membership_rate_limit_rescue(runtime_ctx, result_detail)
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


def _membership_requires_admin_rescue(result) -> bool:
    detail = _membership_result_detail(result).lower()
    return "getchannelsrequest" in detail and ("private" in detail or "banned" in detail)


def _try_admin_lift_restriction_and_join(ctx: MembershipDispatchContext, first_detail: str) -> OperationResult:
    admin = _tenant_rescue_admin(ctx.session, ctx.action.tenant_id)
    if not admin:
        return OperationResult(False, "需人工处理", FailureType.ACCOUNT_UNAVAILABLE.value, "未配置可用救援管理员账号")
    target_ref = _account_invite_ref(ctx.account)
    if not target_ref:
        return OperationResult(False, "需人工处理", FailureType.ACCOUNT_UNAVAILABLE.value, "目标账号缺少 username 或手机号，无法解除群限制")
    credentials = credentials_for_account(ctx.session, admin)
    lifted = gateway.lift_group_account_restrictions(admin.id, ctx.payload.channel_id, target_ref, admin.session_ciphertext, credentials)
    ctx.action.result = {**(ctx.action.result or {}), "admin_restriction_lift_detail": lifted.detail or lifted.failure_type}
    if not lifted.ok:
        return OperationResult(False, "失败", lifted.failure_type or FailureType.UNKNOWN.value, lifted.detail or first_detail)
    return _join_after_admin_restriction_lift(ctx, admin, credentials, first_detail)


def _join_after_admin_restriction_lift(
    ctx: MembershipDispatchContext,
    admin: TgAccount,
    credentials,
    first_detail: str,
) -> OperationResult:
    link = gateway.export_group_invite_link(admin.id, ctx.payload.channel_id, admin.session_ciphertext, credentials)
    if not link.ok or not link.invite_link:
        return OperationResult(False, "失败", link.failure_type or "invite_link_export_failed", link.detail or first_detail)
    joined = gateway.ensure_channel_membership(
        ctx.account.id,
        ctx.payload.channel_id,
        ctx.account.session_ciphertext,
        ctx.credentials,
        invite_link=link.invite_link,
    )
    if not joined.ok:
        return OperationResult(False, "失败", joined.failure_type or FailureType.UNKNOWN.value, joined.detail or first_detail)
    reprobe = gateway.probe_target_capabilities(ctx.account.id, ctx.payload.channel_id, ctx.payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    if not reprobe.ok:
        return OperationResult(False, "失败", reprobe.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, reprobe.detail or first_detail)
    _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
    return OperationResult(True, "已处理", detail=f"admin_rescue_{joined.membership_status or 'joined'}")


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
            confirmation_detail=detail,
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
    confirmation_detail: str = "",
):
    ctx = MembershipDispatchContext(session, action, account, credentials, payload, None)
    followed_refs: list[str] = []
    skipped_refs: list[dict[str, str]] = []
    for channel_ref in required_channels:
        followed = gateway.ensure_channel_membership(account.id, channel_ref, account.session_ciphertext, credentials, invite_link=channel_ref)
        if not followed.ok:
            detail = followed.detail or followed.failure_type or probe_result.detail
            if _membership_peer_ref_invalid(followed):
                skipped_refs.append({"ref": channel_ref, "detail": detail or "peer_ref_invalid"})
                continue
            return OperationResult(False, "失败", followed.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)
        followed_refs.append(channel_ref)
    if not followed_refs:
        action.result = {**(action.result or {}), "required_channels_skipped": skipped_refs}
        detail = "; ".join(f"{item['ref']}:{item['detail']}" for item in skipped_refs) or probe_result.detail
        return OperationResult(False, "失败", FailureType.PEER_INVALID.value, detail)
    if retry_target_membership:
        refreshed = _retry_target_membership_after_required_channel(ctx)
        if not refreshed.ok:
            return refreshed
    confirmed = _resolve_required_channel_confirmation(ctx, confirmation_detail)
    if not confirmed.ok:
        return confirmed
    reprobe = gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)
    if reprobe.ok:
        result = {**(action.result or {}), "required_channels_followed": followed_refs}
        if skipped_refs:
            result["required_channels_skipped"] = skipped_refs
        action.result = result
        return OperationResult(True, detail=f"已关注 {len(followed_refs)} 个必需频道并通过群发言验证")
    detail = reprobe.detail or reprobe.failure_type or probe_result.detail
    return OperationResult(False, "失败", reprobe.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)


def _resolve_required_channel_confirmation(ctx: MembershipDispatchContext, detail: str):
    if not _required_channel_confirmation_needed(detail):
        return OperationResult(True, detail="required_channel_confirmation_not_needed")
    clicked = gateway.resolve_verification_task(
        ctx.account.id,
        "点击按钮",
        ctx.payload.channel_id,
        ctx.account.session_ciphertext,
        ctx.credentials,
    )
    if not clicked.ok:
        detail = clicked.detail or clicked.failure_type or "关注必需频道后确认按钮点击失败"
        return OperationResult(False, clicked.status or "失败", clicked.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, detail)
    ctx.action.result = {**(ctx.action.result or {}), "required_channel_confirmation_clicked": True}
    return OperationResult(True, detail=clicked.detail or "已点击必需频道确认按钮")


def _required_channel_confirmation_needed(detail: str) -> bool:
    if not _context_requires_button_click(detail):
        return False
    return any(marker in detail for marker in _GROUP_SEND_CONFIRM_BUTTON_MARKERS)


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
    if _defer_existing_membership_admin_rate_limit(ctx):
        return True
    approved = _try_admin_approve_join_request(ctx, detail)
    if approved.ok:
        _apply_operation_result(ctx.action, ctx.account, True, "", approved.detail or "join_request_approved", attempt=ctx.attempt)
        ctx.action.result = {**(ctx.action.result or {}), "membership_status": membership_status, "join_request_approved": True}
        return True
    if _defer_membership_admin_rate_limit(ctx, approved, "join_request_approval"):
        return True
    link_joined = _try_admin_link_join_after_approval_failure(ctx, detail)
    if link_joined.ok:
        _apply_operation_result(ctx.action, ctx.account, True, "", link_joined.detail or "join_request_link_joined", attempt=ctx.attempt)
        ctx.action.result = {**(ctx.action.result or {}), "membership_status": membership_status, "join_request_link_joined": True}
        return True
    if _defer_membership_admin_rate_limit(ctx, link_joined, "join_request_link_join"):
        return True
    admin_lifted = _try_admin_lift_restriction_and_join(ctx, detail)
    if admin_lifted.ok:
        _apply_group_send_admin_lift_result(ctx, admin_lifted, membership_status)
        return True
    if _defer_membership_admin_rate_limit(ctx, admin_lifted, "group_restriction_lift"):
        return True
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


def _apply_group_send_admin_lift_result(ctx: MembershipDispatchContext, result: OperationResult, membership_status: str) -> None:
    _apply_operation_result(ctx.action, ctx.account, True, "", result.detail or "admin_restriction_lifted", attempt=ctx.attempt)
    ctx.action.result = {
        **(ctx.action.result or {}),
        "membership_status": membership_status,
        "admin_restriction_lifted": True,
    }


def _try_admin_approve_join_request(ctx: MembershipDispatchContext, detail: str) -> OperationResult:
    if not _waiting_for_join_request_approval(detail):
        return OperationResult(False, "未执行", "", "不是入群申请审批场景")
    admin = _tenant_rescue_admin(ctx.session, ctx.action.tenant_id)
    if not admin:
        return OperationResult(False, "需人工处理", FailureType.ACCOUNT_UNAVAILABLE.value, "未配置可用救援管理员账号")
    target_ref = _account_invite_ref(ctx.account)
    if not target_ref:
        return OperationResult(False, "需人工处理", FailureType.ACCOUNT_UNAVAILABLE.value, "目标账号缺少 username 或手机号，无法审批入群申请")
    credentials = credentials_for_account(ctx.session, admin)
    approved = gateway.approve_group_join_request(admin.id, ctx.payload.channel_id, target_ref, admin.session_ciphertext, credentials)
    ctx.action.result = {**(ctx.action.result or {}), "join_request_approval_detail": approved.detail or approved.failure_type}
    if not approved.ok:
        return approved
    reprobe = gateway.probe_target_capabilities(ctx.account.id, ctx.payload.channel_id, ctx.payload.target_type, ctx.account.session_ciphertext, ctx.credentials)
    if not reprobe.ok:
        return OperationResult(False, "失败", reprobe.failure_type or FailureType.GROUP_PERMISSION_DENIED.value, reprobe.detail or "入群申请已审批但目标仍不可发言")
    _record_group_send_permission_allowed(ctx.session, ctx.action, ctx.account, ctx.payload)
    return OperationResult(True, "已处理", detail=approved.detail or reprobe.detail or "join_request_approved")


def _try_admin_link_join_after_approval_failure(ctx: MembershipDispatchContext, detail: str) -> OperationResult:
    if not _waiting_for_join_request_approval(detail):
        return OperationResult(False, "未执行", "", "不是入群申请审批场景")
    result = _try_admin_lift_restriction_and_join(ctx, detail)
    ctx.action.result = {**(ctx.action.result or {}), "join_request_link_join_detail": result.detail or result.failure_type}
    return result


def _waiting_for_join_request_approval(detail: str) -> bool:
    return "已提交入群申请" in str(detail or "")


def _defer_membership_admin_rate_limit(ctx: MembershipDispatchContext, result: OperationResult, source: str) -> bool:
    detail = result.detail or result.failure_type or ""
    if result.failure_type != FailureType.FLOOD_WAIT.value and "floodwait" not in detail.lower():
        return False
    retry_after = _retry_after_seconds(detail)
    retry_at = _now() + timedelta(seconds=retry_after)
    _record_task_membership_admin_rate_limit(ctx, retry_at, detail, source)
    _defer_membership_rate_limit_until(ctx, MembershipRateLimit(retry_at, detail, source, retry_after))
    return True


def _defer_existing_membership_admin_rate_limit(ctx: MembershipDispatchContext) -> bool:
    task = ctx.session.get(Task, ctx.action.task_id) if ctx.action.task_id else None
    retry_at = _task_membership_admin_rate_limited_until(task)
    if not retry_at or retry_at <= _now():
        return False
    stats = task.stats or {}
    detail = str(stats.get("membership_admin_rate_limit_detail") or "Telegram 管理员操作触发 FloodWait，已延后重试")
    source = str(stats.get("membership_admin_rate_limit_source") or "task_membership_admin_rate_limit")
    retry_after = max(1, int((retry_at - _now()).total_seconds()))
    _defer_membership_rate_limit_until(ctx, MembershipRateLimit(retry_at, detail, source, retry_after))
    return True


def _record_task_membership_admin_rate_limit(ctx: MembershipDispatchContext, retry_at: datetime, detail: str, source: str) -> None:
    task = ctx.session.get(Task, ctx.action.task_id) if ctx.action.task_id else None
    if not task:
        return
    stats = dict(task.stats or {})
    stats["membership_admin_rate_limited_until"] = retry_at.isoformat()
    stats["membership_admin_rate_limit_detail"] = detail
    stats["membership_admin_rate_limit_source"] = source
    task.stats = stats


def _task_membership_admin_rate_limited_until(task: Task | None) -> datetime | None:
    if not task:
        return None
    raw = str((task.stats or {}).get("membership_admin_rate_limited_until") or "")
    if not raw:
        return None
    try:
        return _naive_datetime(datetime.fromisoformat(raw))
    except ValueError:
        return None


def _defer_membership_rate_limit_until(ctx: MembershipDispatchContext, limit: MembershipRateLimit) -> None:
    ctx.action.status = "pending"
    ctx.action.scheduled_at = limit.retry_at
    ctx.action.executed_at = None
    _clear_action_lease(ctx.action)
    _release_runtime_resources(ctx.action)
    ctx.action.result = {
        **(ctx.action.result or {}),
        "success": False,
        "error_code": FailureType.FLOOD_WAIT.value,
        "error_message": limit.detail,
        "auto_check": "延后",
        "validation_stage": "membership_admin_rate_limit",
        "membership_status": "rate_limited",
        "membership_rate_limit_source": limit.source,
        "retry_after_seconds": limit.retry_after,
        "next_retry_at": limit.retry_at.isoformat(),
    }
    _finish_execution_attempt(ctx.attempt, ctx.action, failure_type=FailureType.FLOOD_WAIT.value, detail=limit.detail)


def _tenant_rescue_admin(session: Session, tenant_id: int) -> TgAccount | None:
    tenant = session.get(Tenant, tenant_id)
    account = session.get(TgAccount, tenant.group_rescue_admin_account_id) if tenant and tenant.group_rescue_admin_account_id else None
    if not account or account.status != AccountStatus.ACTIVE.value or not account.session_ciphertext:
        return None
    return account


def _account_invite_ref(account: TgAccount) -> str:
    if account.username:
        return f"@{account.username.lstrip('@')}"
    return account.phone_number or ""


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
    _trigger_membership_group_rescue(ctx, detail)


def _maybe_trigger_membership_rate_limit_rescue(ctx: MembershipDispatchContext, detail: str) -> None:
    if ctx.payload.target_type != "group":
        return
    _trigger_membership_group_rescue(ctx, detail)


def _maybe_trigger_deferred_membership_rescue(session: Session, action: Action, account: TgAccount, detail: str) -> None:
    if not _is_membership_action(action):
        return
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
    except ValidationError:
        return
    if not isinstance(payload, EnsureChannelMembershipPayload) or payload.target_type != "group":
        return
    ctx = MembershipDispatchContext(session, action, account, object(), payload, None)
    _trigger_membership_group_rescue(ctx, detail)


def _trigger_membership_group_rescue(ctx: MembershipDispatchContext, detail: str) -> None:
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
    if verification_task.suggested_action == "点击按钮":
        followed = _try_auto_follow_from_button_links(ctx, verification_task)
        if followed is not None:
            return followed
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
        context_text = _required_channel_context_text(ctx, verification_task)
        required_channels = required_channel_references(context_text)
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
                confirmation_detail=context_text,
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
    return required_channel_references(_required_channel_context_text(ctx, verification_task))


def _required_channel_context_text(ctx: MembershipDispatchContext, verification_task) -> str:
    readers = _image_verification_reader_candidates(ctx.session, verification_task, ctx.account)
    read_result = read_challenge_context_with_fallback(
        ctx.session,
        verification_task,
        ctx.account,
        ctx.credentials,
        reader_candidates=readers,
    )
    return _verification_context_text(read_result.context)


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
            confirmation_detail=context_text,
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


def _try_auto_follow_from_button_links(ctx: MembershipDispatchContext, verification_task):
    refs = _explicit_telegram_link_refs(_auto_follow_detail_text(ctx.action, verification_task))
    if not refs or not _auto_follow_required_channel_enabled(ctx.session, ctx.action):
        return None
    verification_task.suggested_action = "关注频道"
    payload = _verification_probe_payload(ctx.payload, verification_task)
    probe_result = OperationResult(False, "失败", FailureType.GROUP_PERMISSION_DENIED.value, verification_task.detected_reason)
    followed = _follow_required_channels_and_reprobe(
        ctx.session,
        ctx.action,
        ctx.account,
        ctx.credentials,
        payload,
        probe_result,
        refs,
        retry_target_membership=ctx.action.action_type in MEMBERSHIP_ACTION_TYPES,
        confirmation_detail=_auto_follow_detail_text(ctx.action, verification_task),
    )
    return _apply_context_fallback_result(ctx, verification_task, payload, followed)


def _explicit_telegram_link_refs(detail: str) -> list[str]:
    refs: list[str] = []
    text = detail or ""
    refs.extend(match.group("username") for match in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?!joinchat/|\+)(?P<username>[A-Za-z0-9_]{4,})", text))
    refs.extend(match.group(0) for match in re.finditer(r"(?:https?://)?(?:t\.me|telegram\.me)/(?:joinchat/|\+)[A-Za-z0-9_-]{8,}", text))
    return list(dict.fromkeys(refs))


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
    if _explicit_telegram_link_refs(detail):
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
    policy_group = _comment_content_policy_group(session, action, payload)
    if not policy_group:
        return True
    filtered = filter_outbound_content(session, tenant_id=action.tenant_id, group=policy_group, content=content)
    if not filtered.ok:
        _fail(action, FailureType.CONTENT_REJECTED.value, filtered.reason, auto_check="拦截", validation_stage="content_policy")
        return True
    attempt = _begin_execution_attempt(session, action, account)
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.reply_channel_message(account_id, channel_peer, message_id, content, session_ciphertext, credentials, reply_to_message_id=payload.reply_to_message_id)
    _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "", attempt=attempt)
    return True


def _comment_content_policy_group(session: Session, action: Action, payload: PostCommentPayload) -> TgGroup | None:
    channel_target_id = int(payload.channel_target_id or 0)
    channel = session.get(OperationTarget, channel_target_id) if channel_target_id else None
    group = linked_channel_group(session, channel, create=False) if channel else None
    if group:
        return group
    _fail(action, FailureType.PEER_INVALID.value, "频道评论缺少可校验的讨论组", auto_check="拦截", validation_stage="target")
    return None


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


def _dispatch_search_join(session: Session, action: Action, account: TgAccount, payload: SearchJoinPayload) -> bool:
    search_join = getattr(gateway, "execute_search_join", None)
    if not callable(search_join):
        _skip(action, "search_join_gateway_unavailable", "搜索入群 gateway 尚未接入真实 MTProto 执行器")
        action.result = {**(action.result or {}), "validation_stage": "search_join_gateway", "bot_username": payload.bot_username}
        return True
    if not _search_join_proxy_guard_verified(payload):
        _fail(action, "proxy_egress_guard_missing", "搜索入群缺少已验证代理出口 guard，禁止回退本机直连", validation_stage="search_join_proxy")
        return True
    if not _search_join_client_metadata_verified(payload):
        _fail(action, "client_metadata_missing", "搜索入群缺少已绑定客户端 metadata，禁止使用默认 MTProto 指纹", validation_stage="search_join_client_metadata")
        return True
    keyword_text = decrypt_secret(payload.keyword_text_ciphertext) or ""
    if not keyword_text.strip():
        _fail(action, "keyword_text_missing", "搜索入群缺少可执行关键词密文", validation_stage="search_join_payload")
        return True
    try:
        runtime_authorization = _search_join_runtime_authorization(session, account, payload)
    except ValueError as exc:
        _fail(action, str(exc), "搜索入群授权槽位不可用，禁止回退账号主授权", validation_stage="search_join_authorization")
        return True
    result = search_join(account.id, payload.model_dump(mode="json"), runtime_authorization.session_ciphertext, runtime_authorization.credentials, keyword_text)
    action.status = "success" if result.get("success") else "failed"
    action.result = {**(action.result or {}), **result}
    _record_search_join_proxy_failover(session, action, payload, result)
    action.executed_at = _now()
    _stop_search_join_task_if_target_exhausted(session, action, result)
    _create_search_join_linked_dispatches(session, action, payload)
    return True


def _dispatch_search_rank_deboost(session: Session, action: Action, account: TgAccount, payload: SearchRankDeboostPayload) -> bool:
    from .executors.search_rank_deboost import execute_search_rank_deboost

    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    binding_id = int(runtime.get("group_proxy_binding_id") or 0)
    if binding_id <= 0:
        _skip(action, "proxy_egress_guard_failed", "搜索排名观察任务缺少 group_proxy_binding_id，禁止回退本机直连")
        action.result = {**(action.result or {}), "validation_stage": "search_rank_deboost_proxy"}
        return True

    result = execute_search_rank_deboost(session, action, account, payload)
    skip_reason = result.get("skip_reason") or result.get("error_code")
    if skip_reason and not result.get("success"):
        _skip(action, skip_reason, result.get("error_message") or skip_reason)
        action.result = {
            **(action.result or {}),
            **result,
            "validation_stage": "search_rank_deboost_runtime",
            "bot_username": payload.bot_username,
        }
    else:
        action.status = "success" if result.get("success") else "failed"
        action.result = {**(action.result or {}), **result, "validation_stage": "search_rank_deboost_runtime"}
    action.executed_at = _now()
    return True


def _record_search_join_proxy_failover(
    session: Session,
    action: Action,
    payload: SearchJoinPayload,
    result: dict,
) -> None:
    if result.get("success") or not _is_search_join_proxy_failure(result):
        return
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    proxy_binding_id = int(runtime.get("proxy_binding_id") or 0)
    if proxy_binding_id <= 0:
        return
    reason = str(result.get("error_code") or result.get("failure_type") or "proxy_node_unreachable")
    observed_error = str(result.get("error_message") or result.get("failure_detail") or "")
    try:
        binding = failover_proxy_airport_node_binding(
            session,
            tenant_id=action.tenant_id,
            proxy_binding_id=proxy_binding_id,
            reason=reason,
            observed_error=observed_error,
        )
    except ValueError as exc:
        if str(exc) == "airport_all_subscriptions_unavailable":
            _record_runtime_all_subscriptions_unavailable_notice(session, action)
        action.result = {
            **(action.result or {}),
            "proxy_failover_status": "failed",
            "proxy_failover_error": str(exc),
        }
        return
    _retarget_pending_search_join_proxy_bindings(session, action, payload, binding)
    action.result = {
        **(action.result or {}),
        "proxy_failover_status": "switched",
        "proxy_failover_binding_id": binding.id,
        "proxy_failover_event_id": getattr(binding, "proxy_failover_event_id", None),
    }


def _is_search_join_proxy_failure(result: dict) -> bool:
    values = {str(result.get("error_code") or ""), str(result.get("failure_type") or "")}
    return bool(values & _SEARCH_JOIN_PROXY_FAILURE_CODES)


def _retarget_pending_search_join_proxy_bindings(
    session: Session,
    action: Action,
    payload: SearchJoinPayload,
    binding: AccountProxyBinding,
) -> None:
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    old_binding_id = str(runtime.get("proxy_binding_id") or "")
    environment_binding_id = str(runtime.get("environment_binding_id") or "")
    if not old_binding_id or not environment_binding_id:
        return
    rows = session.scalars(
        select(Action).where(
            Action.tenant_id == action.tenant_id,
            Action.task_id == action.task_id,
            Action.account_id == action.account_id,
            Action.action_type == "search_join",
            Action.status.in_(["pending", "retryable_failed"]),
            Action.id != action.id,
        )
    )
    for row in rows:
        _retarget_action_payload_proxy_binding(row, old_binding_id, environment_binding_id, binding)


def _retarget_action_payload_proxy_binding(
    action: Action,
    old_binding_id: str,
    environment_binding_id: str,
    new_binding: AccountProxyBinding,
) -> None:
    payload = dict(action.payload or {})
    runtime = dict(payload.get("runtime_environment") or {})
    if str(runtime.get("environment_binding_id") or "") != environment_binding_id:
        return
    if str(runtime.get("proxy_binding_id") or "") != old_binding_id:
        return
    runtime["proxy_binding_id"] = str(new_binding.id)
    runtime["proxy_id"] = str(new_binding.proxy_id or "")
    payload["runtime_environment"] = runtime
    action.payload = payload


def _record_runtime_all_subscriptions_unavailable_notice(session: Session, action: Action) -> None:
    task = session.get(Task, action.task_id)
    result = _notify_runtime_all_subscriptions_unavailable(session, action, task)
    action.result = {
        **(action.result or {}),
        "admin_notification_status": "sent" if result.ok else "admin_notification_failed",
        "admin_notification_detail": result.detail,
    }


def _notify_runtime_all_subscriptions_unavailable(
    session: Session,
    action: Action,
    task: Task | None,
) -> NotificationResult:
    tenant = session.get(Tenant, action.tenant_id)
    if not tenant or not tenant.admin_chat_id or not tenant.telegram_bot_token_ciphertext:
        result = NotificationResult(False, "Telegram Bot token or admin chat id not configured")
        _audit_runtime_subscription_notification(session, action, result)
        return result
    bot_token = decrypt_secret(tenant.telegram_bot_token_ciphertext)
    if not bot_token:
        result = NotificationResult(False, "Telegram Bot token decrypts to empty")
        _audit_runtime_subscription_notification(session, action, result)
        return result
    task_name = task.name if task else action.task_id
    summary = send_admin_chat_broadcast(
        bot_token=bot_token,
        raw_admin_chat_id=tenant.admin_chat_id,
        text=f"Clash 订阅源池全部不可用\n任务: {task_name}\n任务ID: {action.task_id}\n处理: 运行中代理切换失败，已阻断搜索目标群点击真实操作",
        sender=send_telegram_bot_message,
    )
    result = NotificationResult(summary.ok, summary.detail)
    _audit_runtime_subscription_notification(session, action, result)
    return result


def _audit_runtime_subscription_notification(session: Session, action: Action, result: NotificationResult) -> None:
    audit(
        session,
        tenant_id=action.tenant_id,
        actor="search-join-dispatcher",
        action="Clash订阅全部不可用通知" if result.ok else "Clash订阅全部不可用通知失败",
        target_type="action",
        target_id=str(action.id),
        detail=result.detail,
    )


def _search_join_proxy_guard_verified(payload: SearchJoinPayload) -> bool:
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    return runtime.get("proxy_egress_guard") == "verified"


def _search_join_client_metadata_verified(payload: SearchJoinPayload) -> bool:
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    metadata = payload.client_metadata if isinstance(payload.client_metadata, dict) else {}
    required = ("device_model", "system_version", "app_version", "platform", "client_identity_key")
    return runtime.get("client_metadata_guard") == "verified" and all(metadata.get(key) for key in required)


def _search_join_runtime_authorization(
    session: Session,
    account: TgAccount,
    payload: SearchJoinPayload,
) -> SearchJoinRuntimeAuthorization:
    authorization = session.get(TgAccountAuthorization, payload.authorization_id)
    if authorization is None or authorization.tenant_id != account.tenant_id:
        raise ValueError("search_join_authorization_not_found")
    if authorization.account_id != account.id or authorization.role != payload.session_role:
        raise ValueError("search_join_authorization_scope_mismatch")
    if authorization.disabled_at is not None or authorization.status not in ACTIVE_SEARCH_JOIN_AUTHORIZATION_STATUSES:
        raise ValueError("search_join_authorization_unavailable")
    if not authorization.session_ciphertext:
        raise ValueError("search_join_authorization_session_missing")
    app = _search_join_developer_app(session, authorization, payload)
    proxy = _search_join_proxy(session, account, authorization, payload)
    return SearchJoinRuntimeAuthorization(
        session_ciphertext=authorization.session_ciphertext,
        credentials=credentials_for_developer_app(app, proxy),
    )


def _search_join_developer_app(
    session: Session,
    authorization: TgAccountAuthorization,
    payload: SearchJoinPayload,
) -> TelegramDeveloperApp:
    app = session.get(TelegramDeveloperApp, int(authorization.developer_app_id or 0))
    if app is None:
        raise ValueError("search_join_developer_app_missing")
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    expected = int(runtime.get("developer_app_id") or 0)
    if expected and expected != app.id:
        raise ValueError("search_join_developer_app_scope_mismatch")
    return app


def _search_join_proxy(
    session: Session,
    account: TgAccount,
    authorization: TgAccountAuthorization,
    payload: SearchJoinPayload,
) -> AccountProxy:
    runtime = payload.runtime_environment if isinstance(payload.runtime_environment, dict) else {}
    binding = _search_join_environment_binding(session, account, authorization, runtime)
    proxy_binding = _search_join_proxy_binding(session, binding, runtime)
    proxy = session.get(AccountProxy, int(proxy_binding.proxy_id or 0)) if proxy_binding.proxy_id else None
    if proxy is None:
        raise ValueError("search_join_environment_proxy_missing")
    expected = int(runtime.get("proxy_id") or 0)
    if expected and expected != proxy.id:
        raise ValueError("search_join_proxy_scope_mismatch")
    if proxy.status != "healthy" or proxy.alert_status != "normal":
        raise ValueError("search_join_environment_proxy_unhealthy")
    if not _search_join_proxy_config_complete(proxy):
        raise ValueError("search_join_environment_proxy_config_missing")
    return proxy


def _search_join_proxy_config_complete(proxy: AccountProxy) -> bool:
    return bool(str(proxy.protocol or "").strip()) and bool(str(proxy.host or "").strip()) and int(proxy.port or 0) > 0


def _search_join_environment_binding(
    session: Session,
    account: TgAccount,
    authorization: TgAccountAuthorization,
    runtime: dict,
) -> AccountEnvironmentBinding:
    binding_id = str(runtime.get("environment_binding_id") or "").strip()
    if not binding_id:
        raise ValueError("search_join_environment_binding_missing")
    binding = session.get(AccountEnvironmentBinding, binding_id)
    if binding is None:
        raise ValueError("search_join_environment_binding_not_found")
    if binding.tenant_id != account.tenant_id or binding.account_id != account.id:
        raise ValueError("search_join_environment_binding_scope_mismatch")
    if binding.authorization_id != authorization.id or binding.session_role != authorization.role:
        raise ValueError("search_join_environment_authorization_mismatch")
    if int(binding.developer_app_id or 0) != int(authorization.developer_app_id or 0):
        raise ValueError("search_join_environment_developer_app_mismatch")
    if binding.status != "active" or binding.unbound_at is not None:
        raise ValueError("search_join_environment_binding_inactive")
    if not binding.proxy_binding_id:
        raise ValueError("search_join_proxy_binding_missing")
    return binding


def _search_join_proxy_binding(
    session: Session,
    binding: AccountEnvironmentBinding,
    runtime: dict,
) -> AccountProxyBinding:
    expected = int(runtime.get("proxy_binding_id") or 0)
    if expected and expected != int(binding.proxy_binding_id or 0):
        raise ValueError("search_join_proxy_binding_scope_mismatch")
    proxy_binding = session.get(AccountProxyBinding, int(binding.proxy_binding_id or 0))
    if proxy_binding is None:
        raise ValueError("search_join_proxy_binding_not_found")
    if not _proxy_binding_matches_environment(proxy_binding, binding):
        raise ValueError("search_join_proxy_binding_environment_mismatch")
    return proxy_binding


def _proxy_binding_matches_environment(proxy_binding: AccountProxyBinding, binding: AccountEnvironmentBinding) -> bool:
    return (
        proxy_binding.tenant_id == binding.tenant_id
        and proxy_binding.account_id == binding.account_id
        and proxy_binding.developer_app_id == binding.developer_app_id
        and proxy_binding.authorization_id == binding.authorization_id
        and proxy_binding.session_role == binding.session_role
        and proxy_binding.proxy_id == binding.proxy_id
        and proxy_binding.status == "active"
        and proxy_binding.unbound_at is None
    )


def _stop_search_join_task_if_target_exhausted(session: Session, action: Action, result: dict) -> None:
    if result.get("success") or result.get("error_code") != "target_not_in_results":
        return
    if result.get("pages_exhausted") is not True:
        return
    task = session.get(Task, action.task_id)
    if task is None:
        return
    max_pages = int(result.get("max_pages") or 0)
    task.status = "stopped"
    task.next_run_at = None
    task.last_error = f"搜索目标群点击任务找满 {max_pages} 页仍未命中目标群，已自动停止"
    for pending in _open_search_join_siblings(session, action):
        pending.status = "skipped"
        pending.executed_at = _now()
        _clear_action_lease(pending)
        pending.result = {
            "success": False,
            "error_code": "search_join_target_not_found_task_stopped",
            "error_message": task.last_error,
            "auto_check": "跳过",
            "validation_stage": "search_join_target_match",
        }


def _open_search_join_siblings(session: Session, action: Action):
    return session.scalars(
        select(Action).where(
            Action.task_id == action.task_id,
            Action.id != action.id,
            Action.action_type == "search_join",
            Action.status.in_(["pending", "claiming", "retryable_failed"]),
        )
    )


def _create_search_join_linked_dispatches(session: Session, action: Action, payload: SearchJoinPayload) -> None:
    if action.status != "success":
        return
    for policy in payload.linked_task_policy:
        linked_task_id = str(policy.get("linked_task_id") or "").strip()
        if not linked_task_id:
            continue
        create_linked_dispatch_if_membership_observed(
            session,
            action,
            linked_task_id=linked_task_id,
            activation_not_before=_linked_activation_not_before(policy),
            can_send_checked_at=_now() if policy.get("can_send_rechecked") else None,
        )


def _linked_activation_not_before(policy: dict) -> datetime | None:
    cooldown_minutes = int(policy.get("cooldown_minutes") or 0)
    if cooldown_minutes <= 0:
        return None
    return _now() + timedelta(minutes=cooldown_minutes)


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
    _mark_group_ai_unknown_side_effects(session, action)
    _release_runtime_resources(action)


def _mark_group_ai_unknown_side_effects(session: Session, action: Action) -> None:
    if action.task_type != "group_ai_chat" or action.action_type != "send_message":
        return
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    try:
        payload = validate_action_payload(action.action_type, action.payload or {})
    except (ValidationError, ValueError) as exc:
        action.result = {**(action.result or {}), "side_effect_error": payload_error_message(exc)}
        return
    _mark_ai_message_memory_result(
        session,
        payload,
        status="unknown_after_send",
        action_id=action.id,
        sent_at=_now(),
        result=dict(action.result or {}),
    )
    if account:
        _update_group_ai_stance_memory(session, action, account, payload, action.id)


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


def _release_dispatcher_db_error(action: Action, detail: str) -> None:
    retry_at = _now() + timedelta(seconds=DISPATCHER_DB_ERROR_RETRY_DELAY_SECONDS)
    action.status = "pending"
    action.scheduled_at = retry_at
    action.executed_at = None
    action.retry_count = int(action.retry_count or 0) + 1
    _clear_action_lease(action)
    action.result = {
        **(action.result or {}),
        "success": False,
        "error_code": "dispatcher_db_error",
        "error_message": detail,
        "auto_check": "延后",
        "validation_stage": "dispatcher_db",
        "retry_after_seconds": DISPATCHER_DB_ERROR_RETRY_DELAY_SECONDS,
        "next_retry_at": retry_at.isoformat(),
    }
    _release_runtime_resources(action)


def _account_after_global_policy(session: Session, action: Action, account: TgAccount, *, allow_reassign: bool = True) -> TgAccount | None:
    if _is_group_rescue_action(action):
        _record_group_rescue_capacity_override(action)
        return account
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
    detail = decision.reason or "账号全局限额或冷却中，已延后执行"
    _defer(
        action,
        decision.defer_until or (_now() + timedelta(seconds=60)),
        "global_account_policy",
        detail,
    )
    _maybe_trigger_deferred_membership_rescue(session, action, account, detail)
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
    return as_beijing(value)


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
