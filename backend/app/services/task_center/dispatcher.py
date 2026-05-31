from __future__ import annotations

import os
import socket
from uuid import uuid4
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.integrations.telegram import OutboundSegment
from app.config import get_settings
from app.models import AccountStatus, Action, ChannelMessage, ExecutionAttempt, FailureType, GroupAuthStatus, GroupContextMessage, OperationTarget, ReviewQueue, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now, gateway
from app.services.account_capacity import account_capacity_decision
from app.services.content_filters import filter_outbound_content, rewrite_rejected_content
from app.services.developer_apps import credentials_for_account
from app.services.ai_config import get_scheduling_setting
from app.services.verification import create_verification_task

from .account_pool import account_matches_current_shard, current_account_shard, select_task_accounts
from .channel_membership import account_satisfies_authorized_target, linked_channel_group, mark_channel_membership_joined
from .payloads import EnsureChannelMembershipPayload, LikeMessagePayload, PostCommentPayload, SendMessagePayload, ViewMessagePayload, create_membership_action, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review
from . import runtime_resources as _runtime_resources

_ACTION_RESERVATIONS = _runtime_resources._ACTION_RESERVATIONS
_IN_FLIGHT_ACCOUNTS = _runtime_resources._IN_FLIGHT_ACCOUNTS
_redis_client = _runtime_resources._redis_client
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
    account = session.get(TgAccount, action.account_id) if action.account_id else None
    if not account or account.deleted_at is not None or account.status != AccountStatus.ACTIVE.value:
        _fail_with_policy(action, FailureType.ACCOUNT_UNAVAILABLE.value, "账号不可用", auto_check="拦截", validation_stage="account")
        return True
    account = _account_after_global_policy(session, action, account, allow_reassign=action.status != "executing")
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
        if _gateway_call_started(session, action):
            _mark_unknown_after_send(session, action, str(exc))
        else:
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
            .order_by(Task.priority.asc(), Action.scheduled_at.asc(), Action.created_at.asc())
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
        .order_by(Task.priority.asc(), Action.scheduled_at.asc(), Action.created_at.asc())
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


def recover_expired_claims(session: Session) -> int:
    now_value = _now()
    rows = list(session.scalars(select(Action).where(Action.status == "claiming", Action.claim_expires_at <= now_value).order_by(Action.scheduled_at.asc())))
    for action in rows:
        _release_claim(action, delay_seconds=0, reason="claim_expired")
    return len(rows)


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
    decision = account_capacity_decision(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        scheduled_at=action.scheduled_at,
        exclude_action_id=action.id,
    )
    if decision.available:
        return True
    replacement = _replacement_account_for_action(session, action, account)
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
        result = gateway.send_message(
            account_id,
            group_pk,
            content,
            _outbound_segments(payload),
            session_ciphertext,
            group_peer,
            credentials,
        )
        _apply_send_result(action, account, result.ok, result.remote_message_id or "", result.failure_type or "", result.detail or "", attempt=attempt)
        if result.ok:
            link.last_sent_at = _now()
        return True
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


def _dispatch_channel_membership(session: Session, action: Action, account: TgAccount, credentials, payload: EnsureChannelMembershipPayload) -> bool:
    existing_group = session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.tg_peer_id == payload.channel_id))
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
    result = gateway.ensure_channel_membership(
        account.id,
        payload.channel_id,
        account.session_ciphertext,
        credentials,
        invite_link=payload.invite_link,
    )
    if result.ok:
        probe_result = _probe_joined_group_send_permission(session, action, account, credentials, payload)
        if probe_result is not None and not probe_result.ok:
            _record_group_send_permission_denied(session, action, account, payload, probe_result.detail or probe_result.failure_type)
            _apply_operation_result(action, account, False, probe_result.failure_type, probe_result.detail or probe_result.failure_type, attempt=attempt)
            return True
        _mark_membership_joined(session, action, account, payload)
    elif result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        _record_group_send_permission_denied(session, action, account, payload, result.detail or result.failure_type)
        _skip_membership_permission_denied(action, result.detail or result.failure_type)
        _finish_execution_attempt(attempt, action, failure_type=result.failure_type, detail=result.detail or result.failure_type)
        _release_runtime_resources(action)
        return True
    _apply_operation_result(action, account, result.ok, result.failure_type, result.detail or result.membership_status, attempt=attempt)
    if result.ok:
        action.result = {**(action.result or {}), "membership_status": result.membership_status or "joined"}
    return True


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
    _mark_executing(action)
    session.commit()
    _mark_gateway_call_started(session, attempt)
    result = gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)
    if result.ok:
        _record_group_send_permission_allowed(session, action, account, payload)
        _apply_operation_result(action, account, True, "", "already_joined", attempt=attempt)
        action.result = {**(action.result or {}), "membership_status": "already_joined"}
        return True
    _record_group_send_permission_denied(session, action, account, payload, result.detail or result.failure_type)
    if result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
        _skip_membership_permission_denied(action, result.detail or result.failure_type)
        _finish_execution_attempt(attempt, action, failure_type=result.failure_type, detail=result.detail or result.failure_type)
        _release_runtime_resources(action)
        return True
    _apply_operation_result(action, account, False, result.failure_type, result.detail or result.failure_type, attempt=attempt)
    return True


def _requires_group_send_probe(payload: EnsureChannelMembershipPayload) -> bool:
    return payload.target_type == "group" and bool(payload.require_send)


def _probe_joined_group_send_permission(session: Session, action: Action, account: TgAccount, credentials, payload: EnsureChannelMembershipPayload):
    if not _requires_group_send_probe(payload):
        return None
    return gateway.probe_target_capabilities(account.id, payload.channel_id, payload.target_type, account.session_ciphertext, credentials)


def _skip_membership_already_joined(action: Action) -> None:
    _skip(action, "already_joined", "账号已满足目标准入")
    action.result = {**(action.result or {}), "success": True, "membership_status": "already_joined"}


def _skip_membership_permission_denied(action: Action, detail: str) -> None:
    _skip(action, "membership_permission_denied", f"账号无法加入/访问目标：{detail}")
    action.result = {**(action.result or {}), "membership_status": "permission_denied", "validation_stage": "target_membership_runtime"}


def _mark_membership_joined(session: Session, action: Action, account: TgAccount, payload: EnsureChannelMembershipPayload) -> None:
    mark_channel_membership_joined(
        session,
        action.tenant_id,
        payload.channel_target_id,
        account.id,
        permission_label="已关注" if payload.target_type == "channel" else "可发言",
    )


def _record_group_send_permission_allowed(session: Session, action: Action, account: TgAccount, payload: EnsureChannelMembershipPayload) -> None:
    target = session.get(OperationTarget, payload.channel_target_id)
    if not target:
        return
    group = linked_channel_group(session, target, create=True)
    link = _group_account_link(session, action.tenant_id, group.id, account.id, create=True)
    link.can_send = True
    link.permission_label = "可发言"
    _sync_group_target_send_state(session, group, target)


def _record_group_send_permission_denied(session: Session, action: Action, account: TgAccount, payload: EnsureChannelMembershipPayload, detail: str) -> None:
    target = session.get(OperationTarget, payload.channel_target_id)
    if not target:
        return
    group = linked_channel_group(session, target, create=True)
    link = _group_account_link(session, action.tenant_id, group.id, account.id, create=True)
    link.can_send = False
    link.permission_label = (detail or FailureType.GROUP_PERMISSION_DENIED.value)[:80]
    _sync_group_target_send_state(session, group, target)
    create_verification_task(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        group_id=group.id,
        message_task_id=None,
        verification_type="群发言权限",
        detected_reason=detail or "账号已加入但没有群发言权限",
        suggested_action="人工处理",
        target_peer_id=group.tg_peer_id,
        target_display=group.title,
    )


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
    group = (
        session.scalar(select(TgGroup).where(TgGroup.tenant_id == action.tenant_id, TgGroup.tg_peer_id == channel.tg_peer_id))
        if channel and channel.tenant_id == action.tenant_id and channel.target_type == "channel"
        else None
    )
    link = (
        session.scalar(
            select(TgGroupAccount).where(
                TgGroupAccount.tenant_id == action.tenant_id,
                TgGroupAccount.group_id == group.id,
                TgGroupAccount.account_id == account.id,
            )
        )
        if group
        else None
    )
    if link:
        return True
    _fail_with_policy(
        action,
        FailureType.ACCOUNT_UNAVAILABLE.value,
        "账号未关注目标频道，已拦截主互动动作",
        auto_check="拦截",
        validation_stage="account_channel_membership",
    )
    return False


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
    task = session.get(Task, action.task_id)
    if task and not _open_comment_membership_action(session, action, account.id, channel.id):
        create_membership_action(session, task, account.id, _now(), _comment_membership_payload(channel))
    action.status = "pending"
    action.scheduled_at = _now() + _COMMENT_MEMBERSHIP_RETRY_DELAY
    action.executed_at = None
    _clear_action_lease(action)
    action.result = {"success": False, "error_code": "comment_membership_required", "error_message": detail, "auto_check": "等待准入", "validation_stage": "account_channel_membership"}
    _release_runtime_resources(action)


def _open_comment_membership_action(session: Session, action: Action, account_id: int, channel_target_id: int) -> Action | None:
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


def _comment_membership_payload(channel: OperationTarget) -> EnsureChannelMembershipPayload:
    return EnsureChannelMembershipPayload(
        channel_id=channel.tg_peer_id,
        channel_target_id=channel.id,
        target_type=channel.target_type,
        target_display=channel.title,
        target_username=channel.username or "",
        invite_link=channel.username or channel.tg_peer_id,
        require_send=True,
    )


def _apply_operation_result(action: Action, account: TgAccount, ok: bool, failure_type: str = "", detail: str = "", *, attempt: ExecutionAttempt | None = None) -> None:
    _apply_send_result(action, account, ok, "", failure_type, detail, attempt=attempt)


def _apply_send_result(action: Action, account: TgAccount, ok: bool, remote_id: str = "", failure_type: str = "", detail: str = "", *, attempt: ExecutionAttempt | None = None) -> None:
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
        if failure_type == FailureType.GROUP_PERMISSION_DENIED.value:
            if not _defer_comment_membership_from_gateway_failure(action, account, detail or failure_type):
                _mark_group_account_cannot_send(action, account, detail or failure_type)
                _mark_channel_comment_account_cannot_send(action, account, detail or failure_type)
        if failure_type in _COMMENT_THREAD_UNAVAILABLE_FAILURES:
            _close_unavailable_comment_thread(action, failure_type, detail or failure_type)
        if failure_type == FailureType.REACTION_UNAVAILABLE.value:
            _close_unavailable_reaction(action, detail or failure_type)
        if action.status == "failed":
            _apply_default_failure_policy(action, failure_type or FailureType.UNKNOWN.value)
    action.executed_at = None if action.status == "pending" else _now()
    _finish_execution_attempt(attempt, action, remote_id=remote_id, failure_type=failure_type or "", detail=detail or "")
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
    action.result = {"success": False, "error_code": code, "error_message": detail, "auto_check": "跳过", "validation_stage": "context"}
    action.executed_at = _now()
    _release_runtime_resources(action)


def _defer(action: Action, scheduled_at, code: str, detail: str) -> None:
    action.status = "pending"
    action.scheduled_at = scheduled_at
    _clear_action_lease(action)
    action.result = {"success": False, "error_code": code, "error_message": detail, "auto_check": "延后", "validation_stage": "account_policy"}
    _release_runtime_resources(action)


def _account_after_global_policy(session: Session, action: Action, account: TgAccount, *, allow_reassign: bool = True) -> TgAccount | None:
    decision = account_capacity_decision(
        session,
        tenant_id=action.tenant_id,
        account_id=account.id,
        scheduled_at=action.scheduled_at,
        exclude_action_id=action.id,
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


def _replacement_account_for_action(session: Session, action: Action, account: TgAccount) -> TgAccount | None:
    task = session.get(Task, action.task_id)
    if not task:
        return None
    payload = action.payload if isinstance(action.payload, dict) else {}
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
        candidates = select_task_accounts(session, action.tenant_id, task.account_config or {}, scheduled_at=action.scheduled_at, limit=10)
        return next((candidate for candidate in candidates if candidate.id != account.id and candidate.id in member_ids), None)
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


__all__ = ["claim_actions", "dispatch_action", "due_actions", "recover_expired_claims"]
