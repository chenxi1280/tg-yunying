from __future__ import annotations

import threading
from dataclasses import dataclass
from uuid import uuid4

from app.config import get_settings
from app.models import Action, TgAccount
from app.services._common import _now


def _setting(settings, name: str, default):
    return getattr(settings, name, default)


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


def _reserve_runtime_resources(action: Action) -> bool:
    account_id = int(action.account_id) if action.account_id is not None else None
    if account_id is not None:
        with _IN_FLIGHT_LOCK:
            if account_id in _IN_FLIGHT_ACCOUNTS:
                action.result = {
                    **(action.result or {}),
                    "runtime_resource_reason": "account_inflight_conflict",
                    "runtime_resource_wait_seconds": 0,
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
