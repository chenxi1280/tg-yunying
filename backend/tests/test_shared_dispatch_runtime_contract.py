from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.models import DispatchClaimScope, DispatchRuntimeShardState
from app.timezone import BEIJING_TZ
from app.services.task_center.dispatch_runtime_contract import (
    DispatchRuntimeContractError,
    build_dispatch_runtime_contract,
    live_shard_available_capacity,
    require_active_scope_contract,
    stage_scope_candidate,
)
from app.services.task_center.dispatch_claim_allocation import (
    _allocate_demands_with_live_limits,
)
from app.services.task_center.dispatch_claim_types import DispatchClaimDemand


pytestmark = pytest.mark.no_postgres


def test_canonical_contract_calculates_two_live_shards_at_thirteen_each() -> None:
    contract = build_dispatch_runtime_contract(_settings())

    assert contract.runtime_shard_total == 2
    assert contract.scope_capacity == 26
    assert [row.effective_worker_capacity for row in contract.shards] == [13, 13]
    assert len(contract.topology_fingerprint) == 64
    assert len(contract.capacity_config_fingerprint) == 64


def test_contract_hash_does_not_depend_on_worker_identity_or_input_order() -> None:
    first = build_dispatch_runtime_contract(_settings(worker_id="worker-a", pid=1))
    second = build_dispatch_runtime_contract(_settings(worker_id="worker-b", pid=2))

    assert first.topology_fingerprint == second.topology_fingerprint
    assert first.capacity_config_fingerprint == second.capacity_config_fingerprint


def test_scope_capacity_mismatch_fails_closed() -> None:
    with pytest.raises(DispatchRuntimeContractError) as caught:
        build_dispatch_runtime_contract(_settings(dispatcher_scope_capacity=52))

    assert caught.value.code == "dispatcher_scope_capacity_mismatch"


def test_scope_must_be_atomically_staged_then_activated() -> None:
    contract = build_dispatch_runtime_contract(_settings())
    scope = DispatchClaimScope(
        dispatcher_scope="task_center_dispatch",
        claim_capacity=0,
        contract_activation_state="preparing",
    )

    stage_scope_candidate(scope, contract)
    with pytest.raises(DispatchRuntimeContractError) as caught:
        require_active_scope_contract(scope, contract)
    assert caught.value.code == "shared_dispatch_contract_preparing"

    scope.active_contract_version = scope.candidate_contract_version
    scope.contract_activation_state = "active"
    require_active_scope_contract(scope, contract)


def test_stale_shard_reduces_live_new_budget_without_cross_shard_takeover() -> None:
    contract = build_dispatch_runtime_contract(_settings())
    observed_at = datetime(2026, 8, 1, 12, tzinfo=timezone.utc)
    states = [
        _state(contract, 0, observed_at - timedelta(seconds=30)),
        _state(contract, 1, observed_at - timedelta(seconds=121)),
    ]

    budget = live_shard_available_capacity(
        states,
        contract,
        active_by_shard={0: 3, 1: 0},
        unclaimed_by_shard={0: 2, 1: 0},
        now=observed_at,
        stale_seconds=120,
    )

    assert budget == {0: 8, 1: 0}


def test_naive_beijing_clock_keeps_recent_aware_shards_live() -> None:
    contract = build_dispatch_runtime_contract(_settings())
    observed_at = datetime(2026, 8, 1, 18, 39)
    heartbeat_at = observed_at.replace(tzinfo=BEIJING_TZ) - timedelta(seconds=30)
    states = [_state(contract, index, heartbeat_at) for index in range(2)]

    budget = live_shard_available_capacity(
        states,
        contract,
        active_by_shard={0: 0, 1: 0},
        unclaimed_by_shard={0: 0, 1: 0},
        now=observed_at,
        stale_seconds=120,
    )

    assert budget == {0: 13, 1: 13}


def test_allocator_writes_zero_new_reservations_for_unavailable_shard() -> None:
    demands = [
        DispatchClaimDemand(
            tenant_id=1,
            task_id=f"task-{index}",
            claim_class="ordinary",
            shard_total=2,
            shard_index=index,
            action_ids=tuple(f"action-{index}-{item}" for item in range(20)),
            required_claims=20,
            urgency_score=100,
            is_strict=False,
        )
        for index in range(2)
    ]

    grants = _allocate_demands_with_live_limits(
        demands,
        available=13,
        epoch=1,
        live_shard_available={0: 13, 1: 0},
        runtime_shard_total=2,
    )

    assert grants[demands[0].key] == 13
    assert grants[demands[1].key] == 0


def _settings(**overrides):
    values = {
        "dispatcher_claim_scope": "task_center_dispatch",
        "dispatch_runtime_shard_total": 2,
        "dispatcher_scope_capacity": 26,
        "dispatcher_concurrency": 20,
        "db_pool_size": 5,
        "db_max_overflow": 10,
        "db_pool_control_reserve": 2,
        "dispatch_topology_fingerprint_schema_version": "dispatch_topology_v1",
        "dispatch_rebuild_contract_version": "dispatch-rebuild-v3",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _state(contract, shard_index: int, heartbeat_at: datetime):
    return DispatchRuntimeShardState(
        dispatcher_scope=contract.dispatcher_scope,
        shard_index=shard_index,
        expected_capacity=13,
        config_fingerprint=contract.capacity_config_fingerprint,
        heartbeat_at=heartbeat_at,
        liveness_state="live",
    )
