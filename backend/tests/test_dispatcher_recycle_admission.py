from __future__ import annotations

import pytest

from app import worker
from app.dispatcher_lifecycle import DispatcherLifecycle, RedisRecycleLease
from test_dispatcher_lifecycle import _Lease, _Probe, _Redis, _metrics, _settings

pytestmark = pytest.mark.no_postgres


def _lifecycle(lease, *, instance_id="test-worker", shard_index=0):
    return DispatcherLifecycle(
        settings=_settings(),
        safety_probe=_Probe(),
        lease=lease,
        disconnect_gateway=lambda _timeout: 0,
        metrics_reader=_metrics,
        instance_id=instance_id,
        shard_index=shard_index,
    )


def test_only_lease_winner_stops_claiming_when_both_reach_threshold() -> None:
    redis = _Redis()
    first = _lifecycle(RedisRecycleLease(
        redis, worker_instance_id="first", shard_index=0, ttl_seconds=30,
    ))
    second = _lifecycle(RedisRecycleLease(
        redis, worker_instance_id="second", shard_index=1, ttl_seconds=30,
    ))

    first.observe_after_batch()
    second.observe_after_batch()

    assert first.state == "recycle_requested"
    assert second.state == "active"
    assert second.automatic is False


def test_automatic_drain_reuses_lease_acquired_before_stop() -> None:
    lease = _Lease()
    lifecycle = _lifecycle(lease)
    lifecycle.observe_after_batch()
    assert lease.acquire_calls == 1

    snapshot = lifecycle.drain_until_safe(lambda _metadata: None)

    assert snapshot.blockers() == ()
    assert lease.acquire_calls == 1
    assert lease.renew_calls > 0
    assert lease.release_calls == 0


def test_worker_continues_next_batch_when_recycle_lease_is_busy(monkeypatch) -> None:
    lease = _Lease([False])
    lifecycle = _lifecycle(lease)
    batches: list[str] = []
    heartbeats: list[str] = []
    monkeypatch.setattr(worker, "_record_loop_heartbeat", lambda role, _limit: heartbeats.append(role))
    monkeypatch.setattr(worker, "_write_local_healthcheck_heartbeat", lambda: None)
    monkeypatch.setattr(worker, "_record_resource_sample", lambda *_args: None)
    monkeypatch.setattr(worker, "drain_once", lambda _limit, *, role: batches.append(role) or 1)

    assert worker._drain_worker_iteration("dispatcher", 1, lifecycle) is True
    assert worker._drain_worker_iteration("dispatcher", 1, lifecycle) is True

    assert batches == ["dispatcher", "dispatcher"]
    assert heartbeats == ["dispatcher", "dispatcher"]
    assert lifecycle.state == "active"
    assert lease.acquire_calls == 2


def test_sigterm_during_automatic_acquire_keeps_manual_drain(monkeypatch) -> None:
    lease = _Lease()
    lifecycle = _lifecycle(lease)

    def acquire_after_signal():
        lifecycle.request_stop("sigterm", automatic=False)
        return True

    monkeypatch.setattr(lease, "acquire", acquire_after_signal)
    lifecycle.observe_after_batch()

    assert lifecycle.trigger == "sigterm"
    assert lifecycle.automatic is False
    assert lease.release_calls == 1
    lifecycle.drain_until_safe(lambda _metadata: None)
    assert lease.renew_calls == 0


def test_unavailable_recycle_lease_does_not_prevent_manual_stop() -> None:
    lease = _Lease([False])
    lifecycle = _lifecycle(lease)
    lifecycle.observe_after_batch()
    assert lifecycle.state == "active"

    lifecycle.request_stop("sigterm", automatic=False)
    lifecycle.drain_until_safe(lambda _metadata: None)

    assert lifecycle.state == "safe_to_exit"
    assert lifecycle.automatic is False
    assert lease.acquire_calls == 1
    assert lease.renew_calls == 0


def test_lost_lease_blocks_drain_until_owner_can_be_reacquired(monkeypatch) -> None:
    lease = _Lease([True, False, True])
    lifecycle = _lifecycle(lease)
    lifecycle.observe_after_batch()
    renewals = iter([False, True])
    monkeypatch.setattr(lease, "renew", lambda: next(renewals))
    heartbeats: list[dict[str, object]] = []

    lifecycle.drain_until_safe(heartbeats.append)

    assert lease.acquire_calls == 3
    assert lifecycle.state == "safe_to_exit"
    assert any(
        row["lifecycle_state"] == "drain_blocked"
        and row["drain_blocker"] == "recycle_lease_unavailable"
        for row in heartbeats
    )
