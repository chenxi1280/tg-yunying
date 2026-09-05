from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.dispatcher_lifecycle import (
    DispatcherLifecycle,
    DispatcherMetrics,
    DispatcherSafetySnapshot,
    RedisRecycleLease,
)


class _Lease:
    def __init__(self, acquire_results: list[bool] | None = None) -> None:
        self.acquire_results = acquire_results or [True]
        self.acquire_calls = 0
        self.renew_calls = 0
        self.release_calls = 0

    def acquire(self) -> bool:
        self.acquire_calls += 1
        if len(self.acquire_results) > 1:
            return self.acquire_results.pop(0)
        return self.acquire_results[0]

    def renew(self) -> bool:
        self.renew_calls += 1
        return True

    def release(self) -> bool:
        self.release_calls += 1
        return True

    def acknowledge_successor(self) -> bool:
        return False


class _FlakySuccessorLease(_Lease):
    def __init__(self) -> None:
        super().__init__()
        self.successor_results: list[bool | None] = [None, True]
        self.successor_calls = 0

    def acknowledge_successor(self) -> bool | None:
        self.successor_calls += 1
        return self.successor_results.pop(0)


class _Probe:
    def __init__(self, active_operations: list[int] | None = None) -> None:
        self.active_operations = active_operations or [0]
        self.calls = 0

    def snapshot(self, *, gateway_open: bool) -> DispatcherSafetySnapshot:
        self.calls += 1
        if len(self.active_operations) > 1:
            active = self.active_operations.pop(0)
        else:
            active = self.active_operations[0]
        return DispatcherSafetySnapshot(
            active_operations=active,
            owned_actions=0,
            unfinished_attempts=0,
            gateway_open=gateway_open,
        )


def _settings(**overrides):
    values = {
        "dispatcher_recycle_enabled": True,
        "dispatcher_recycle_soft_rss_bytes": 100,
        "dispatcher_recycle_soft_cgroup_bytes": 0,
        "dispatcher_recycle_ocr_attempt_limit": 0,
        "dispatcher_recycle_max_uptime_seconds": 0,
        "dispatcher_gateway_shutdown_timeout_seconds": 2,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _metrics(rss: int = 101) -> DispatcherMetrics:
    return DispatcherMetrics(
        rss_bytes=rss,
        cgroup_bytes=0,
        uptime_seconds=1,
        ocr_attempts=0,
    )


@pytest.mark.no_postgres
def test_threshold_requests_automatic_recycle() -> None:
    lifecycle = DispatcherLifecycle(
        settings=_settings(),
        safety_probe=_Probe(),
        lease=_Lease(),
        disconnect_gateway=lambda _timeout: 0,
        metrics_reader=_metrics,
    )

    lifecycle.observe_after_batch()

    assert lifecycle.state == "recycle_requested"
    assert lifecycle.trigger == "rss"
    assert lifecycle.automatic is True


@pytest.mark.no_postgres
def test_automatic_recycle_waits_while_active_then_drains_current_operations() -> None:
    lease = _Lease([False, True])
    probe = _Probe([1, 0, 0])
    disconnects: list[float] = []
    heartbeats: list[dict[str, object]] = []
    lifecycle = DispatcherLifecycle(
        settings=_settings(),
        safety_probe=probe,
        lease=lease,
        disconnect_gateway=lambda timeout: disconnects.append(timeout) or 1,
        metrics_reader=_metrics,
    )
    lifecycle.request_stop("rss", automatic=True)
    assert lifecycle.state == "active"
    lifecycle.observe_after_batch()

    snapshot = lifecycle.drain_until_safe(heartbeats.append)

    assert snapshot.blockers() == ()
    assert lifecycle.state == "safe_to_exit"
    assert lease.acquire_calls == 2
    assert lease.release_calls == 0
    assert disconnects == [2]
    assert any(item["safety"].get("active_operations") == 1 for item in heartbeats)


@pytest.mark.no_postgres
def test_planned_sigterm_drain_does_not_acquire_runtime_lease() -> None:
    lease = _Lease()
    lifecycle = DispatcherLifecycle(
        settings=_settings(),
        safety_probe=_Probe(),
        lease=lease,
        disconnect_gateway=lambda _timeout: 0,
        metrics_reader=_metrics,
    )
    lifecycle.request_stop("sigterm", automatic=False)

    lifecycle.drain_until_safe(lambda _metadata: None)

    assert lifecycle.state == "safe_to_exit"
    assert lease.acquire_calls == 0
    assert lease.renew_calls == 0
    assert lease.release_calls == 0


@pytest.mark.no_postgres
def test_safe_to_exit_waits_until_heartbeat_is_persisted() -> None:
    lifecycle = DispatcherLifecycle(
        settings=_settings(),
        safety_probe=_Probe([0, 0, 0]),
        lease=_Lease(),
        disconnect_gateway=lambda _timeout: 0,
        metrics_reader=_metrics,
    )
    lifecycle.request_stop("sigterm", automatic=False)
    safe_heartbeat_attempts = 0

    def heartbeat(metadata) -> None:
        nonlocal safe_heartbeat_attempts
        if metadata["lifecycle_state"] != "safe_to_exit":
            return
        safe_heartbeat_attempts += 1
        if safe_heartbeat_attempts == 1:
            raise RuntimeError("database unavailable")

    lifecycle.drain_until_safe(heartbeat)

    assert lifecycle.state == "safe_to_exit"
    assert safe_heartbeat_attempts == 2


@pytest.mark.no_postgres
def test_successor_ack_retries_after_transient_lease_error() -> None:
    lease = _FlakySuccessorLease()
    lifecycle = DispatcherLifecycle(
        settings=_settings(),
        safety_probe=_Probe(),
        lease=lease,
        disconnect_gateway=lambda _timeout: 0,
        metrics_reader=_metrics,
    )

    assert lifecycle.acknowledge_successor() is False
    assert lifecycle.acknowledge_successor() is True
    assert lease.successor_calls == 2


class _Redis:
    def __init__(self) -> None:
        self.value = None

    def set(self, _key, value, *, nx, ex):
        del ex
        if nx and self.value is not None:
            return False
        self.value = value
        return True

    def get(self, _key):
        return self.value

    def eval(self, script, _keys, _key, value, *args):
        del args
        if self.value != value:
            return 0
        if "DEL" in script:
            self.value = None
        return 1


@pytest.mark.no_postgres
def test_redis_lease_owner_token_prevents_cross_release() -> None:
    redis = _Redis()
    first = RedisRecycleLease(
        redis,
        worker_instance_id="worker-1",
        shard_index=0,
        ttl_seconds=30,
    )
    second = RedisRecycleLease(
        redis,
        worker_instance_id="worker-2",
        shard_index=1,
        ttl_seconds=30,
    )

    assert first.acquire() is True
    assert second.acquire() is False
    assert second.release() is False
    assert first.renew() is True
    assert first.release() is True


@pytest.mark.no_postgres
def test_new_same_shard_instance_releases_predecessor_lease() -> None:
    redis = _Redis()
    predecessor = RedisRecycleLease(
        redis,
        worker_instance_id="worker-old",
        shard_index=0,
        ttl_seconds=30,
    )
    successor = RedisRecycleLease(
        redis,
        worker_instance_id="worker-new",
        shard_index=0,
        ttl_seconds=30,
    )

    assert predecessor.acquire() is True
    assert successor.acknowledge_successor() is True
    assert redis.value is None
