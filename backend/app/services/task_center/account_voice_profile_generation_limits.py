from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from app.config import get_settings
from app.services._common import _now


MIN_REDIS_TIMEOUT_SECONDS = 1
MIN_PROVIDER_LEASE_SECONDS = 30
DEFAULT_PROVIDER_WAIT_SECONDS = 1
TOKEN_BUCKET_TTL_SECONDS = 120
RELEASE_IF_OWNER_LUA = (
    "if redis.call('get', KEYS[1]) == ARGV[1] "
    "then return redis.call('del', KEYS[1]) else return 0 end"
)
TOKEN_BUCKET_LUA = """
local bucket_key = KEYS[1]
local now_ms = tonumber(ARGV[1])
local rate_per_second = tonumber(ARGV[2])
local capacity = tonumber(ARGV[3])
local ttl_seconds = tonumber(ARGV[4])
if rate_per_second <= 0 or capacity <= 0 then return {0, 60} end
local current_tokens = tonumber(redis.call('HGET', bucket_key, 'tokens'))
local updated_at = tonumber(redis.call('HGET', bucket_key, 'updated_at'))
if current_tokens == nil then current_tokens = capacity end
if updated_at == nil then updated_at = now_ms end
local elapsed = math.max(0, now_ms - updated_at) / 1000.0
local tokens = math.min(capacity, current_tokens + elapsed * rate_per_second)
if tokens < 1 then
  local wait_seconds = math.ceil((1 - tokens) / rate_per_second)
  redis.call('HSET', bucket_key, 'tokens', tokens, 'updated_at', now_ms)
  redis.call('EXPIRE', bucket_key, ttl_seconds)
  return {0, wait_seconds}
end
redis.call('HSET', bucket_key, 'tokens', tokens - 1, 'updated_at', now_ms)
redis.call('EXPIRE', bucket_key, ttl_seconds)
return {1, 0}
"""


class VoiceProfileProviderRateLimitedError(RuntimeError):
    def __init__(self, provider: str, wait_seconds: int) -> None:
        self.provider = provider
        self.wait_seconds = max(DEFAULT_PROVIDER_WAIT_SECONDS, int(wait_seconds))
        super().__init__(f"voice profile provider rate limited: provider={provider};wait_seconds={self.wait_seconds}")


class VoiceProfileProviderLimiterUnavailableError(RuntimeError):
    def __init__(self, provider: str, detail: str) -> None:
        self.provider = provider
        super().__init__(f"voice profile provider limiter unavailable: provider={provider};detail={detail}")


@dataclass(frozen=True)
class VoiceProfileProviderReservation:
    provider: str
    client: object
    slot_key: str
    slot_token: str

    def release(self) -> None:
        try:
            self.client.eval(RELEASE_IF_OWNER_LUA, 1, self.slot_key, self.slot_token)
        except Exception:
            return


def reserve_voice_profile_provider(*, tenant_id: int, provider_id: int) -> VoiceProfileProviderReservation:
    settings = get_settings()
    provider = str(int(provider_id))
    reservation: VoiceProfileProviderReservation | None = None
    try:
        client = _redis_client(settings.redis_url)
        reservation = _reserve_provider_slot(
            client,
            tenant_id=tenant_id,
            provider=provider,
            concurrency=int(settings.voice_profile_provider_concurrency),
            lease_seconds=int(settings.voice_profile_provider_lease_seconds),
        )
        _consume_provider_token(client, tenant_id, provider, int(settings.voice_profile_provider_rate_per_minute))
        return reservation
    except VoiceProfileProviderRateLimitedError:
        if reservation is not None:
            reservation.release()
        raise
    except Exception as exc:  # noqa: BLE001 - a distributed limiter outage must be surfaced to the durable item.
        if reservation is not None:
            reservation.release()
        raise VoiceProfileProviderLimiterUnavailableError(provider, str(exc)) from exc


def _reserve_provider_slot(
    client,
    *,
    tenant_id: int,
    provider: str,
    concurrency: int,
    lease_seconds: int,
) -> VoiceProfileProviderReservation:
    slot_count = max(1, concurrency)
    ttl_seconds = max(MIN_PROVIDER_LEASE_SECONDS, lease_seconds)
    token = str(uuid4())
    for slot in range(slot_count):
        key = f"inflight:ai:voice_profile:{tenant_id}:{provider}:{slot}"
        if client.set(key, token, nx=True, ex=ttl_seconds):
            return VoiceProfileProviderReservation(provider, client, key, token)
    raise VoiceProfileProviderRateLimitedError(provider, DEFAULT_PROVIDER_WAIT_SECONDS)


def _consume_provider_token(client, tenant_id: int, provider: str, rate_per_minute: int) -> None:
    rate = max(1, int(rate_per_minute))
    key = f"rate:ai:voice_profile:{tenant_id}:{provider}"
    result = client.eval(
        TOKEN_BUCKET_LUA,
        1,
        key,
        int(_now().timestamp() * 1000),
        rate / 60.0,
        rate,
        TOKEN_BUCKET_TTL_SECONDS,
    )
    allowed = int(result[0]) if isinstance(result, (list, tuple)) else int(result)
    wait_seconds = int(result[1]) if isinstance(result, (list, tuple)) and len(result) > 1 else 0
    if allowed:
        return
    raise VoiceProfileProviderRateLimitedError(provider, wait_seconds)


def _redis_client(redis_url: str):
    import redis

    return redis.Redis.from_url(
        redis_url,
        socket_connect_timeout=MIN_REDIS_TIMEOUT_SECONDS,
        socket_timeout=MIN_REDIS_TIMEOUT_SECONDS,
    )


__all__ = [
    "VoiceProfileProviderLimiterUnavailableError",
    "VoiceProfileProviderRateLimitedError",
    "VoiceProfileProviderReservation",
    "reserve_voice_profile_provider",
]
