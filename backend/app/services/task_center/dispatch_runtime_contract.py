from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from typing import Iterable, Mapping

from app.models import DispatchClaimScope, DispatchRuntimeShardState

from .dispatch_claim_contract import dispatcher_scope
from .datetime_compat import ensure_aware


class DispatchRuntimeContractError(RuntimeError):
    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}:{detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class DispatchShardCapacity:
    shard_index: int
    dispatcher_concurrency: int
    db_pool_size: int
    db_max_overflow: int
    db_pool_control_reserve: int
    effective_worker_capacity: int


@dataclass(frozen=True)
class DispatchRuntimeContract:
    dispatcher_scope: str
    runtime_shard_total: int
    scope_capacity: int
    fingerprint_schema_version: str
    rebuild_contract_version: str
    shards: tuple[DispatchShardCapacity, ...]
    topology_fingerprint: str
    capacity_config_fingerprint: str


def build_dispatch_runtime_contract(settings) -> DispatchRuntimeContract:
    scope_name = dispatcher_scope(settings)
    shard_total = int(getattr(settings, "dispatch_runtime_shard_total", 0) or 0)
    schema_version = str(
        getattr(settings, "dispatch_topology_fingerprint_schema_version", "")
        or ""
    )
    rebuild_version = str(
        getattr(settings, "dispatch_rebuild_contract_version", "") or ""
    )
    _validate_identity(shard_total, schema_version, rebuild_version)
    shard = _shard_capacity(settings, 0)
    shards = tuple(
        DispatchShardCapacity(shard_index=index, **_capacity_values(shard))
        for index in range(shard_total)
    )
    scope_capacity = int(getattr(settings, "dispatcher_scope_capacity", 0) or 0)
    expected_capacity = sum(row.effective_worker_capacity for row in shards)
    if scope_capacity != expected_capacity:
        raise DispatchRuntimeContractError(
            "dispatcher_scope_capacity_mismatch",
            f"configured={scope_capacity},expected={expected_capacity}",
        )
    topology_payload = _topology_payload(
        scope_name,
        shard_total,
        schema_version,
        rebuild_version,
    )
    topology_fingerprint = canonical_sha256(topology_payload)
    capacity_payload = _capacity_payload(
        topology_fingerprint,
        scope_capacity,
        shards,
    )
    return DispatchRuntimeContract(
        dispatcher_scope=scope_name,
        runtime_shard_total=shard_total,
        scope_capacity=scope_capacity,
        fingerprint_schema_version=schema_version,
        rebuild_contract_version=rebuild_version,
        shards=shards,
        topology_fingerprint=topology_fingerprint,
        capacity_config_fingerprint=canonical_sha256(capacity_payload),
    )


def canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def stage_scope_candidate(
    scope: DispatchClaimScope,
    contract: DispatchRuntimeContract,
) -> None:
    scope.runtime_shard_total = contract.runtime_shard_total
    scope.claim_capacity = contract.scope_capacity
    scope.topology_fingerprint = contract.topology_fingerprint
    scope.capacity_config_fingerprint = contract.capacity_config_fingerprint
    scope.fingerprint_schema_version = contract.fingerprint_schema_version
    scope.candidate_contract_version = contract.rebuild_contract_version
    scope.contract_activation_state = "preparing"
    scope.version = int(scope.version or 0) + 1


def require_active_scope_contract(
    scope: DispatchClaimScope,
    contract: DispatchRuntimeContract,
) -> None:
    if scope.contract_activation_state != "active":
        raise DispatchRuntimeContractError(
            "shared_dispatch_contract_preparing",
            scope.contract_activation_state,
        )
    if not _scope_matches_contract(scope, contract):
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch",
            "database scope differs from the local runtime contract",
        )


def live_shard_available_capacity(
    states: Iterable[DispatchRuntimeShardState],
    contract: DispatchRuntimeContract,
    *,
    active_by_shard: Mapping[int, int],
    unclaimed_by_shard: Mapping[int, int],
    now: datetime,
    stale_seconds: int,
) -> dict[int, int]:
    state_by_index = {state.shard_index: state for state in states}
    result: dict[int, int] = {}
    for shard in contract.shards:
        state = state_by_index.get(shard.shard_index)
        if not _state_is_live(state, contract, now, stale_seconds):
            result[shard.shard_index] = 0
            continue
        occupied = active_by_shard.get(shard.shard_index, 0)
        occupied += unclaimed_by_shard.get(shard.shard_index, 0)
        result[shard.shard_index] = max(
            0,
            shard.effective_worker_capacity - occupied,
        )
    return result


def live_shard_indexes(
    states: Iterable[DispatchRuntimeShardState],
    contract: DispatchRuntimeContract,
    *,
    now: datetime,
    stale_seconds: int,
) -> set[int]:
    state_by_index = {state.shard_index: state for state in states}
    return {
        shard.shard_index for shard in contract.shards
        if _state_is_live(
            state_by_index.get(shard.shard_index),
            contract,
            now,
            stale_seconds,
        )
    }


def _validate_identity(
    shard_total: int,
    schema_version: str,
    rebuild_version: str,
) -> None:
    if shard_total < 1:
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "runtime shard total must be positive",
        )
    if not schema_version or not rebuild_version:
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "contract versions are required",
        )


def _shard_capacity(settings, shard_index: int) -> DispatchShardCapacity:
    concurrency = int(getattr(settings, "dispatcher_concurrency", 0) or 0)
    pool_size = int(getattr(settings, "db_pool_size", 0) or 0)
    overflow = int(getattr(settings, "db_max_overflow", 0) or 0)
    reserve = int(getattr(settings, "db_pool_control_reserve", 0) or 0)
    effective = min(concurrency, pool_size + overflow - reserve)
    if concurrency < 1 or pool_size < 1 or overflow < 0 or reserve < 0 or effective < 1:
        raise DispatchRuntimeContractError(
            "dispatcher_scope_capacity_mismatch", "invalid worker capacity inputs",
        )
    return DispatchShardCapacity(
        shard_index=shard_index,
        dispatcher_concurrency=concurrency,
        db_pool_size=pool_size,
        db_max_overflow=overflow,
        db_pool_control_reserve=reserve,
        effective_worker_capacity=effective,
    )


def _capacity_values(shard: DispatchShardCapacity) -> dict[str, int]:
    return {
        "dispatcher_concurrency": shard.dispatcher_concurrency,
        "db_pool_size": shard.db_pool_size,
        "db_max_overflow": shard.db_max_overflow,
        "db_pool_control_reserve": shard.db_pool_control_reserve,
        "effective_worker_capacity": shard.effective_worker_capacity,
    }


def _topology_payload(
    scope_name: str,
    shard_total: int,
    schema_version: str,
    rebuild_version: str,
) -> dict[str, object]:
    return {
        "fingerprint_schema_version": schema_version,
        "dispatcher_scope": scope_name,
        "runtime_shard_total": shard_total,
        "expected_shard_indexes": list(range(shard_total)),
        "dispatch_rebuild_contract_version": rebuild_version,
    }


def _capacity_payload(
    topology_fingerprint: str,
    scope_capacity: int,
    shards: tuple[DispatchShardCapacity, ...],
) -> dict[str, object]:
    return {
        "topology_fingerprint": topology_fingerprint,
        "shards": [
            {"shard_index": shard.shard_index, **_capacity_values(shard)}
            for shard in sorted(shards, key=lambda row: row.shard_index)
        ],
        "dispatcher_scope_capacity": scope_capacity,
    }


def _scope_matches_contract(
    scope: DispatchClaimScope,
    contract: DispatchRuntimeContract,
) -> bool:
    return bool(
        scope.runtime_shard_total == contract.runtime_shard_total
        and scope.claim_capacity == contract.scope_capacity
        and scope.topology_fingerprint == contract.topology_fingerprint
        and scope.capacity_config_fingerprint
        == contract.capacity_config_fingerprint
        and scope.fingerprint_schema_version
        == contract.fingerprint_schema_version
        and scope.active_contract_version == contract.rebuild_contract_version
    )


def _state_is_live(
    state: DispatchRuntimeShardState | None,
    contract: DispatchRuntimeContract,
    now: datetime,
    stale_seconds: int,
) -> bool:
    if state is None or state.liveness_state != "live" or state.heartbeat_at is None:
        return False
    if state.config_fingerprint != contract.capacity_config_fingerprint:
        return False
    observed_at = ensure_aware(now)
    heartbeat = ensure_aware(state.heartbeat_at)
    return (observed_at.astimezone(timezone.utc) - heartbeat.astimezone(timezone.utc)).total_seconds() <= stale_seconds


__all__ = [
    "DispatchRuntimeContract",
    "DispatchRuntimeContractError",
    "DispatchShardCapacity",
    "build_dispatch_runtime_contract",
    "canonical_sha256",
    "live_shard_available_capacity",
    "live_shard_indexes",
    "require_active_scope_contract",
    "stage_scope_candidate",
]
