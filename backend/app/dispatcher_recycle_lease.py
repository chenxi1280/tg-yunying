from __future__ import annotations

import json
from datetime import UTC, datetime
from time import monotonic
from typing import Protocol
from uuid import uuid4

RECYCLE_LEASE_KEY = "tgyunying:dispatcher:rolling-recycle"
LEASE_RENEW_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""
LEASE_RELEASE_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


class RecycleLease(Protocol):
    def acquire(self) -> bool: ...

    def renew(self) -> bool: ...

    def release(self) -> bool: ...

    def acknowledge_successor(self) -> bool | None: ...


class RedisRecycleLease:
    def __init__(
        self,
        client: object,
        *,
        worker_instance_id: str,
        shard_index: int,
        ttl_seconds: int,
    ) -> None:
        self._client = client
        self._ttl_seconds = ttl_seconds
        self._worker_instance_id = worker_instance_id
        self._shard_index = shard_index
        self._next_renew_at = 0.0
        now = datetime.now(UTC)
        self._value = json.dumps(
            {
                "token": str(uuid4()),
                "worker_instance_id": worker_instance_id,
                "shard_index": shard_index,
                "requested_at": now.isoformat(),
            },
            sort_keys=True,
        )

    def acquire(self) -> bool:
        try:
            acquired = bool(
                self._client.set(
                    RECYCLE_LEASE_KEY,
                    self._value,
                    nx=True,
                    ex=self._ttl_seconds,
                )
            )
            if acquired:
                self._next_renew_at = monotonic() + (
                    self._ttl_seconds / 3
                )
            return acquired
        except Exception:  # noqa: BLE001 - unavailable is a drain blocker.
            return False

    def renew(self) -> bool:
        if monotonic() < self._next_renew_at:
            return True
        try:
            renewed = bool(
                self._client.eval(
                    LEASE_RENEW_LUA,
                    1,
                    RECYCLE_LEASE_KEY,
                    self._value,
                    self._ttl_seconds,
                )
            )
            if renewed:
                self._next_renew_at = monotonic() + (
                    self._ttl_seconds / 3
                )
            return renewed
        except Exception:  # noqa: BLE001 - unavailable is a drain blocker.
            return False

    def release(self) -> bool:
        try:
            return bool(
                self._client.eval(
                    LEASE_RELEASE_LUA,
                    1,
                    RECYCLE_LEASE_KEY,
                    self._value,
                )
            )
        except Exception:  # noqa: BLE001 - release failure must stay visible.
            return False

    def acknowledge_successor(self) -> bool | None:
        try:
            raw = self._client.get(RECYCLE_LEASE_KEY)
            value = raw.decode() if isinstance(raw, bytes) else str(raw or "")
            payload = json.loads(value) if value else {}
            is_predecessor = (
                int(payload.get("shard_index", -1)) == self._shard_index
                and str(payload.get("worker_instance_id") or "")
                != self._worker_instance_id
            )
            if not is_predecessor:
                return False
            return bool(
                self._client.eval(
                    LEASE_RELEASE_LUA,
                    1,
                    RECYCLE_LEASE_KEY,
                    value,
                )
            )
        except Exception:  # noqa: BLE001 - caller retries transient ack failure.
            return None
