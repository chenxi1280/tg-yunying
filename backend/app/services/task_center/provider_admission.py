from __future__ import annotations

import hashlib
import logging
import math
import time
from dataclasses import dataclass
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import AiProvider, AiProviderHealthStatus


ADMISSION_KEY_PREFIX = "ai:provider:admission"
ADMISSION_OPEN_TTL_SECONDS = 300
MIN_REDIS_TIMEOUT_SECONDS = 1
COOLDOWN_TTL_GRACE_SECONDS = 120

logger = logging.getLogger(__name__)

COOLDOWN_EXTEND_LUA = """
local key = KEYS[1]
local new_retry_at = tonumber(ARGV[1])
local max_retry_at = tonumber(ARGV[2])
local reason = ARGV[3]
local ttl = tonumber(ARGV[4])
local current = tonumber(redis.call('HGET', key, 'retry_at')) or 0
local target = math.min(math.max(current, new_retry_at), max_retry_at)
redis.call('HINCRBY', key, 'version', 1)
redis.call('HSET', key, 'retry_at', target, 'reason', reason, 'source_status', 'cooldown')
redis.call('EXPIRE', key, ttl)
return tostring(target)
"""

RELEASE_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)


class ProviderAdmissionBlocked(RuntimeError):
    def __init__(self, provider_key: str, wait_seconds: int, *, reason: str = "provider_cooldown") -> None:
        self.provider_key = provider_key
        self.wait_seconds = max(1, int(wait_seconds))
        self.reason = reason
        super().__init__(
            f"provider admission blocked: key={provider_key};reason={reason};wait_seconds={self.wait_seconds}"
        )


class ProviderAdmissionUnavailable(RuntimeError):
    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(f"provider_admission_unavailable: {detail}")


@dataclass(frozen=True)
class ProviderProbeLease:
    client: object
    admission_key: str
    probe_key: str
    token: str


def provider_admission_key(provider: AiProvider) -> str:
    settings = get_settings()
    config_version = str(getattr(settings, "ai_provider_admission_config_version", "v1"))
    key_fingerprint = hashlib.sha256(str(provider.api_key_ciphertext or "").encode("utf-8")).hexdigest()[:12]
    identity = hashlib.sha256(
        "|".join(
            (
                config_version,
                str(provider.id),
                str(provider.base_url or "").strip().lower(),
                str(provider.model_name or "").strip().lower(),
                key_fingerprint,
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"{ADMISSION_KEY_PREFIX}:{config_version}:{identity}"


def ensure_claim_admission(session: Session) -> None:
    """领取 GenerationJob 前的共享 admission 检查。

    任一 active+healthy Provider 不在 cooldown 即允许领取；全部处于 cooldown 时
    停止领取；共享状态不可读时抛 ProviderAdmissionUnavailable（fail-closed）。
    """
    if not _admission_enabled():
        return
    providers = _active_providers(session)
    if not providers:
        return
    states: dict[int, dict[str, str]] = {}
    try:
        client = _redis_client(get_settings().redis_url)
        for provider in providers:
            states[provider.id] = _admission_state(client, provider_admission_key(provider))
    except ProviderAdmissionUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - 共享状态不可读必须显式停止领取
        raise ProviderAdmissionUnavailable(str(exc)) from exc
    now = _now_epoch()
    for provider in providers:
        retry_at = float(states[provider.id].get("retry_at") or 0)
        if retry_at <= now:
            return
    wait = max(
        1,
        math.ceil(max(float(states[p.id].get("retry_at") or 0) for p in providers) - now),
    )
    raise ProviderAdmissionBlocked(
        provider_admission_key(providers[0]),
        wait,
        reason="all_active_providers_cooldown",
    )


def begin_provider_call(provider: AiProvider) -> ProviderProbeLease | None:
    """真正发起 Provider HTTP 前的共享 admission 检查。

    cooldown 生效时抛 ProviderAdmissionBlocked；共享状态不可读时抛
    ProviderAdmissionUnavailable；cooldown key 缺失时只允许一个跨进程 probe
    token 通过，其他调用方等待。mock provider 与未启用时返回 None。
    """
    if not _admission_enabled() or _is_mock_provider(provider):
        return None
    settings = get_settings()
    key = provider_admission_key(provider)
    probe_ttl = max(10, int(getattr(settings, "ai_provider_probe_ttl_seconds", 60)))
    try:
        client = _redis_client(settings.redis_url)
        state = _admission_state(client, key)
        now = _now_epoch()
        retry_at = float(state.get("retry_at") or 0)
        if retry_at > now:
            raise ProviderAdmissionBlocked(
                key,
                math.ceil(retry_at - now),
                reason=str(state.get("reason") or "provider_cooldown"),
            )
        token = str(uuid4())
        if state.get("source_status") == "open":
            return ProviderProbeLease(client, key, "", token)
        probe_key = f"{key}:probe"
        if not client.set(probe_key, token, nx=True, ex=probe_ttl):
            raise ProviderAdmissionBlocked(key, probe_ttl, reason="provider_probe_in_flight")
        return ProviderProbeLease(client, key, probe_key, token)
    except ProviderAdmissionBlocked:
        raise
    except Exception as exc:  # noqa: BLE001 - 共享状态不可读必须显式停止调用
        raise ProviderAdmissionUnavailable(str(exc)) from exc


def settle_provider_success(lease: ProviderProbeLease | None) -> None:
    if lease is None:
        return
    try:
        lease.client.hset(
            lease.admission_key,
            mapping={"retry_at": "0", "reason": "", "source_status": "open"},
        )
        lease.client.hincrby(lease.admission_key, "version", 1)
        lease.client.expire(lease.admission_key, ADMISSION_OPEN_TTL_SECONDS)
        _release_probe(lease)
    except Exception:
        logger.exception(
            "provider admission success settlement failed key=%s",
            lease.admission_key,
        )


def release_provider_probe(lease: ProviderProbeLease | None) -> None:
    if lease is None:
        return
    try:
        _release_probe(lease)
    except Exception:
        logger.exception(
            "provider admission probe release failed key=%s",
            lease.admission_key,
        )


def extend_provider_cooldown(
    provider: AiProvider,
    retry_after_seconds: int | None,
    *,
    reason: str,
) -> int:
    settings = get_settings()
    key = provider_admission_key(provider)
    default_seconds = max(1, int(getattr(settings, "ai_provider_cooldown_default_seconds", 30)))
    max_seconds = max(default_seconds, int(getattr(settings, "ai_provider_cooldown_max_seconds", 3600)))
    wait = default_seconds if retry_after_seconds is None else max(1, int(retry_after_seconds))
    now = _now_epoch()
    try:
        client = _redis_client(settings.redis_url)
        target = client.eval(
            COOLDOWN_EXTEND_LUA,
            1,
            key,
            now + wait,
            now + max_seconds,
            reason[:200],
            max_seconds + COOLDOWN_TTL_GRACE_SECONDS,
        )
        return int(float(target))
    except Exception as exc:  # noqa: BLE001 - cooldown 写不进共享状态时禁止后续调用
        raise ProviderAdmissionUnavailable(f"cooldown_write_failed:{reason[:120]}") from exc


def _active_providers(session: Session) -> list[AiProvider]:
    return list(
        session.scalars(
            select(AiProvider)
            .where(
                AiProvider.is_active.is_(True),
                AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
            )
            .order_by(AiProvider.id.asc())
        )
    )


def _admission_enabled() -> bool:
    return bool(getattr(get_settings(), "ai_provider_admission_enabled", False))


def _is_mock_provider(provider: AiProvider) -> bool:
    return str(provider.base_url or "").startswith("mock://")


def _admission_state(client, key: str) -> dict[str, str]:  # noqa: ANN001
    raw = client.hgetall(key)
    if not raw:
        return {}

    def _decode(value: object) -> str:
        return value.decode("utf-8", "replace") if isinstance(value, bytes) else str(value)

    return {_decode(k): _decode(v) for k, v in raw.items()}


def _release_probe(lease: ProviderProbeLease) -> None:
    if not lease.probe_key:
        return
    lease.client.eval(RELEASE_IF_OWNER_LUA, 1, lease.probe_key, lease.token)


def _now_epoch() -> float:
    return time.time()


def _redis_client(redis_url: str):
    import redis

    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=MIN_REDIS_TIMEOUT_SECONDS,
        socket_timeout=MIN_REDIS_TIMEOUT_SECONDS,
    )


__all__ = [
    "ProviderAdmissionBlocked",
    "ProviderAdmissionUnavailable",
    "ProviderProbeLease",
    "begin_provider_call",
    "ensure_claim_admission",
    "extend_provider_cooldown",
    "provider_admission_key",
    "release_provider_probe",
    "settle_provider_success",
]
