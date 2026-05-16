from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from uuid import uuid4
from datetime import timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.gateways import OutboundSegment
from app.config import get_settings
from app.models import AccountStatus, Action, ExecutionAttempt, FailureType, GroupContextMessage, OperationTarget, ReviewQueue, Task, TgAccount, TgGroup, TgGroupAccount
from app.services._common import _now, gateway
from app.services.account_capacity import account_capacity_decision
from app.services.content_filters import filter_outbound_content, rewrite_rejected_content
from app.services.developer_apps import credentials_for_account
from app.services.ai_config import get_scheduling_setting

from .account_pool import account_matches_current_shard, current_account_shard, select_task_accounts
from .payloads import LikeMessagePayload, PostCommentPayload, SendMessagePayload, ViewMessagePayload, payload_error_message, validate_action_payload
from .policies import validate_group_send_policy
from .review import has_pending_review


_IN_FLIGHT_LOCK = threading.Lock()
_IN_FLIGHT_ACCOUNTS: set[int] = set()
_ACTION_RESERVATIONS: dict[str, "_RuntimeReservation"] = {}


@dataclass(frozen=True)
class _RuntimeReservation:
    account_id: int | None
    redis_reservations: tuple[tuple[str, str], ...] = ()
    redis_account_lock: tuple[str, str] | None = None


@dataclass(frozen=True)
class _RateBucket:
    key: str
    rate_per_second: float
    capacity: float
    cost: float = 1.0


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


def _reserve_runtime_resources(action: Action) -> bool:
    account_id = int(action.account_id) if action.account_id is not None else None
    if account_id is not None:
        with _IN_FLIGHT_LOCK:
            if account_id in _IN_FLIGHT_ACCOUNTS:
                action.result = {
                    **(action.result or {}),
                    "runtime_resource_reason": "account_inflight_conflict",
                    "runtime_resource_wait_seconds": 1,
                }
                return False
            _IN_FLIGHT_ACCOUNTS.add(account_id)
    redis_reservation = _reserve_redis_token(action)
    if redis_reservation is False:
        _release_runtime_resources(action)
        return False
    redis_account_lock = _reserve_redis_account_lock(action, account_id)
    if redis_account_lock is False:
        if redis_reservation:
            for redis_key, redis_token in redis_reservation:
                _release_redis_reservation(redis_key, redis_token)
        _release_runtime_resources(action)
        return False
    with _IN_FLIGHT_LOCK:
        _ACTION_RESERVATIONS[action.id] = _RuntimeReservation(
            account_id=account_id,
            redis_reservations=redis_reservation if redis_reservation else (),
            redis_account_lock=redis_account_lock if redis_account_lock else None,
        )
    return True


def _release_runtime_resources(action: Action) -> None:
    with _IN_FLIGHT_LOCK:
        reservation = _ACTION_RESERVATIONS.pop(action.id, None)
    if reservation:
        for redis_key, redis_token in reservation.redis_reservations:
            _release_redis_reservation(redis_key, redis_token)
        if reservation.redis_account_lock:
            _release_redis_reservation(*reservation.redis_account_lock)
    account_id = reservation.account_id if reservation else (int(action.account_id) if action.account_id is not None else None)
    if account_id is None:
        return
    with _IN_FLIGHT_LOCK:
        _IN_FLIGHT_ACCOUNTS.discard(account_id)


def _reserve_redis_token(action: Action) -> tuple[tuple[str, str], ...] | None | bool:
    settings = get_settings()
    if not _setting(settings, "enable_redis_token_bucket", False):
        return None
    reservations: list[tuple[str, str]] = []
    try:
        client = _redis_client(settings.redis_url)
        for index, bucket in enumerate(_redis_rate_buckets(action, settings)):
            request_id = str(uuid4())
            reservation_key = f"{bucket.key}:reservation:{action.id}:{index}:{request_id}"
            wait_seconds = _try_consume_bucket(client, bucket, reservation_key, request_id)
            if wait_seconds > 0:
                for redis_key, redis_token in reservations:
                    _release_redis_reservation(redis_key, redis_token)
                action.result = {
                    **(action.result or {}),
                    "runtime_resource_reason": "redis_token_bucket_limited",
                    "rate_limit_key": bucket.key,
                    "rate_limit_wait_seconds": wait_seconds,
                }
                return False
            reservations.append((reservation_key, request_id))
        return tuple(reservations)
    except Exception:
        for redis_key, redis_token in reservations:
            _release_redis_reservation(redis_key, redis_token)
        if not _setting(settings, "redis_token_fail_closed", True):
            return None
        action.result = {
            **(action.result or {}),
            "runtime_resource_reason": "redis_token_bucket_unavailable",
            "runtime_resource_wait_seconds": 5,
        }
        return False


def _reserve_redis_account_lock(action: Action, account_id: int | None) -> tuple[str, str] | None | bool:
    settings = get_settings()
    if not account_id or not _setting(settings, "enable_redis_account_inflight", False):
        return None
    lock_key = f"inflight:account:{account_id}"
    lock_token = f"{action.id}:{uuid4()}"
    ttl_seconds = max(30, int(_setting(settings, "redis_account_inflight_seconds", 1800) or 1800))
    try:
        client = _redis_client(settings.redis_url)
        locked = client.set(lock_key, lock_token, nx=True, ex=ttl_seconds)
    except Exception:
        action.result = {
            **(action.result or {}),
            "runtime_resource_reason": "redis_account_inflight_unavailable",
            "runtime_resource_wait_seconds": 5,
        }
        return False
    if locked:
        return lock_key, lock_token
    action.result = {
        **(action.result or {}),
        "runtime_resource_reason": "account_inflight_conflict",
        "runtime_resource_wait_seconds": 1,
    }
    return False


def _redis_client(redis_url: str):
    import redis

    return redis.Redis.from_url(redis_url, socket_connect_timeout=1, socket_timeout=1)


_TOKEN_BUCKET_LUA = """
local bucket_key = KEYS[1]
local reservation_key = KEYS[2]
local now_ms = tonumber(ARGV[1])
local rate_per_second = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local ttl_seconds = tonumber(ARGV[5])
local request_id = ARGV[6]
if rate_per_second <= 0 or capacity <= 0 or cost <= 0 then
  redis.call('SETEX', reservation_key, ttl_seconds, request_id)
  return {1, 0}
end
local current_tokens = tonumber(redis.call('HGET', bucket_key, 'tokens'))
local updated_at = tonumber(redis.call('HGET', bucket_key, 'updated_at'))
if current_tokens == nil then current_tokens = capacity end
if updated_at == nil then updated_at = now_ms end
local elapsed = math.max(0, now_ms - updated_at) / 1000.0
local tokens = math.min(capacity, current_tokens + elapsed * rate_per_second)
if tokens < cost then
  local wait_seconds = math.ceil((cost - tokens) / rate_per_second)
  redis.call('HSET', bucket_key, 'tokens', tokens, 'updated_at', now_ms)
  redis.call('EXPIRE', bucket_key, ttl_seconds)
  return {0, wait_seconds}
end
tokens = tokens - cost
redis.call('HSET', bucket_key, 'tokens', tokens, 'updated_at', now_ms)
redis.call('EXPIRE', bucket_key, ttl_seconds)
redis.call('SETEX', reservation_key, ttl_seconds, request_id)
return {1, 0}
"""


def _try_consume_bucket(client, bucket: _RateBucket, reservation_key: str, request_id: str) -> int:
    now_ms = int(_now().timestamp() * 1000)
    ttl_seconds = max(30, int((bucket.capacity / max(bucket.rate_per_second, 0.001)) * 2))
    result = client.eval(
        _TOKEN_BUCKET_LUA,
        2,
        bucket.key,
        reservation_key,
        now_ms,
        float(bucket.rate_per_second),
        float(bucket.capacity),
        float(bucket.cost),
        ttl_seconds,
        request_id,
    )
    allowed = int(result[0]) if isinstance(result, (list, tuple)) else int(result)
    wait_seconds = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else 0
    return 0 if allowed == 1 else max(1, wait_seconds)


def _redis_rate_buckets(action: Action, settings) -> list[_RateBucket]:
    payload = action.payload if isinstance(action.payload, dict) else {}
    cost = _task_type_weight(action.task_type or action.action_type, settings)
    buckets: list[_RateBucket] = []
    _add_bucket(buckets, "rate:global:tg_api", float(_setting(settings, "global_tg_rate_per_second", 0) or 0), max(float(_setting(settings, "global_tg_rate_per_second", 0) or 0) * 2, 1), cost)
    _add_bucket(buckets, f"rate:task:{action.task_id}", float(_setting(settings, "task_rate_per_minute", 0) or 0) / 60.0, max(float(_setting(settings, "task_rate_per_minute", 0) or 0), 1), cost)
    _add_bucket(buckets, f"rate:task_type:{action.task_type or action.action_type}", float(_setting(settings, "task_type_rate_per_minute", 0) or 0) / 60.0, max(float(_setting(settings, "task_type_rate_per_minute", 0) or 0), 1), cost)
    if action.account_id is not None:
        _add_bucket(buckets, f"rate:account:{action.account_id}", float(_setting(settings, "account_rate_per_hour", 0) or 0) / 3600.0, max(float(_setting(settings, "account_rate_per_hour", 0) or 0), 1), 1)
    proxy_id = getattr(action, "proxy_id", None)
    if proxy_id is None:
        account = getattr(action, "account", None)
        proxy_id = getattr(account, "proxy_id", None) if account is not None else None
    if proxy_id is None and action.account_id is not None:
        from sqlalchemy.orm import object_session

        session = object_session(action)
        account = session.get(TgAccount, action.account_id) if session else None
        proxy_id = getattr(account, "proxy_id", None) if account is not None else None
    if proxy_id:
        _add_bucket(buckets, f"rate:proxy:{proxy_id}", float(_setting(settings, "proxy_rate_per_minute", 0) or 0) / 60.0, max(float(_setting(settings, "proxy_rate_per_minute", 0) or 0), 1), cost)
    target_id = payload.get("group_id") or payload.get("chat_id") or payload.get("channel_id") or payload.get("target_peer_id")
    if target_id:
        _add_bucket(buckets, f"rate:target:{target_id}", float(_setting(settings, "target_rate_per_minute", 0) or 0) / 60.0, max(float(_setting(settings, "target_rate_per_minute", 0) or 0), 1), cost)
    if payload.get("media_segments"):
        _add_bucket(buckets, "rate:media", float(_setting(settings, "media_rate_per_minute", 0) or 0) / 60.0, max(float(_setting(settings, "media_rate_per_minute", 0) or 0), 1), cost)
    return buckets


def _add_bucket(buckets: list[_RateBucket], key: str, rate_per_second: float, capacity: float, cost: float) -> None:
    if rate_per_second <= 0 or capacity <= 0:
        return
    buckets.append(_RateBucket(key=key, rate_per_second=rate_per_second, capacity=max(capacity, cost), cost=max(cost, 1.0)))


def _task_type_weight(task_type: str, settings) -> float:
    raw = str(_setting(settings, "task_type_token_weights", "") or "")
    weights: dict[str, float] = {}
    for item in raw.split(","):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        try:
            weights[key.strip()] = max(1.0, float(value.strip()))
        except ValueError:
            continue
    return weights.get(task_type, 1.0)


def _release_redis_reservation(key: str, token: str) -> None:
    try:
        client = _redis_client(get_settings().redis_url)
        script = "if redis.call('get', KEYS[1]) == ARGV[1] then return redis.call('del', KEYS[1]) else return 0 end"
        client.eval(script, 1, key, token)
    except Exception:
        return


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
        _apply_default_failure_policy(action, failure_type or FailureType.UNKNOWN.value)
    action.executed_at = None if action.status == "pending" else _now()
    _finish_execution_attempt(attempt, action, remote_id=remote_id, failure_type=failure_type or "", detail=detail or "")
    _release_runtime_resources(action)


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


__all__ = ["claim_actions", "dispatch_action", "due_actions", "recover_expired_claims"]
