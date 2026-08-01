from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    DispatchClaimScope,
    DispatchRuntimeShardState,
    WorkerHeartbeat,
)
from app.services._common import _now

from .dispatch_claim_ledger import for_update, scope_for_update
from .dispatch_runtime_contract import (
    DispatchRuntimeContract,
    DispatchRuntimeContractError,
    build_dispatch_runtime_contract,
    require_active_scope_contract,
    stage_scope_candidate,
)


FENCED_WRITER_ROLES = frozenset(
    {"all", "task_center", "planner", "dispatcher", "recovery", "ai-generation"}
)


def stage_dispatch_runtime_contract(
    session: Session,
    settings,
) -> DispatchClaimScope:
    contract = build_dispatch_runtime_contract(settings)
    scope = scope_for_update(
        session,
        contract.dispatcher_scope,
        contract.scope_capacity,
    )
    stage_scope_candidate(scope, contract)
    return scope


def record_dispatcher_shard_heartbeat(
    session: Session,
    settings,
    *,
    worker_id: str,
    lease_id: str = "",
    now: datetime | None = None,
    state: str = "live",
) -> DispatchRuntimeShardState:
    contract = build_dispatch_runtime_contract(settings)
    shard_index = _dispatcher_shard_index(settings, contract)
    scope = _locked_scope(session, contract.dispatcher_scope)
    if scope is None or not _candidate_matches(scope, contract):
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch",
            "candidate scope contract has not been staged",
        )
    row = _locked_shard_state(session, contract.dispatcher_scope, shard_index)
    observed_at = now or _now()
    if row is None:
        row = _create_shard_state(
            session,
            contract,
            shard_index=shard_index,
            worker_id=worker_id,
            lease_id=lease_id,
            observed_at=observed_at,
            state=state,
        )
    else:
        _update_shard_state(
            row,
            contract,
            worker_id=worker_id,
            lease_id=lease_id,
            observed_at=observed_at,
            state=state,
        )
    return row


def dispatch_writer_allowed(
    session: Session,
    settings,
    *,
    role: str,
) -> bool:
    if role not in FENCED_WRITER_ROLES:
        return True
    if getattr(settings, "app_env", "") != "production":
        return True
    contract = build_dispatch_runtime_contract(settings)
    scope = _locked_scope(session, contract.dispatcher_scope, lock=False)
    if scope is None:
        return False
    try:
        require_active_scope_contract(scope, contract)
    except DispatchRuntimeContractError:
        return False
    return True


def activate_dispatch_runtime_contract(
    session: Session,
    settings,
    *,
    takeover_head_batch_id: str,
    now: datetime | None = None,
) -> DispatchClaimScope:
    contract = build_dispatch_runtime_contract(settings)
    scope = _locked_scope(session, contract.dispatcher_scope)
    if scope is None or not _candidate_matches(scope, contract):
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "candidate scope is missing",
        )
    observed_at = now or _now()
    _require_candidate_shards(session, contract, settings, observed_at)
    _require_no_old_writers(session, contract, settings, observed_at)
    from .ai_content_scope_takeover_apply import takeover_chain_is_complete

    if not takeover_head_batch_id or not takeover_chain_is_complete(
        session,
        takeover_head_batch_id,
    ):
        raise DispatchRuntimeContractError(
            "legacy_content_scope_takeover_pending",
            "AI content scope takeover chain is incomplete",
        )
    from .dispatch_activation_ledger import (
        validate_dispatch_ledgers_for_activation,
    )

    validate_dispatch_ledgers_for_activation(session, settings, now=now)
    scope.active_contract_version = scope.candidate_contract_version
    scope.contract_activation_state = "active"
    scope.version += 1
    return scope


def verify_dispatch_runtime_candidate(
    session: Session,
    settings,
    *,
    now: datetime | None = None,
) -> dict:
    contract = build_dispatch_runtime_contract(settings)
    scope = _locked_scope(session, contract.dispatcher_scope, lock=False)
    if scope is None or not _candidate_matches(scope, contract):
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "candidate scope is missing",
        )
    observed_at = now or _now()
    _require_candidate_shards(session, contract, settings, observed_at)
    _require_no_old_writers(session, contract, settings, observed_at)
    return dispatch_runtime_candidate_status(session, settings)


def verify_dispatch_runtime_active(
    session: Session,
    settings,
    *,
    now: datetime | None = None,
) -> dict:
    status = verify_dispatch_runtime_candidate(
        session,
        settings,
        now=now,
    )
    contract = build_dispatch_runtime_contract(settings)
    scope = _locked_scope(session, contract.dispatcher_scope)
    if scope is None:
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "active scope is missing",
        )
    require_active_scope_contract(scope, contract)
    from .dispatch_runtime_ledger_validation import (
        validate_dispatch_ledgers_for_runtime,
    )

    validate_dispatch_ledgers_for_runtime(session, settings, now=now)
    return {**status, "verification_state": "active_verified"}


def dispatch_runtime_candidate_status(session: Session, settings) -> dict:
    contract = build_dispatch_runtime_contract(settings)
    scope = _locked_scope(session, contract.dispatcher_scope, lock=False)
    rows = list(session.scalars(select(DispatchRuntimeShardState).where(
        DispatchRuntimeShardState.dispatcher_scope == contract.dispatcher_scope,
    ).order_by(DispatchRuntimeShardState.shard_index.asc())))
    return {
        "dispatcher_scope": contract.dispatcher_scope,
        "contract_version": contract.rebuild_contract_version,
        "runtime_shard_total": contract.runtime_shard_total,
        "scope_capacity": contract.scope_capacity,
        "topology_fingerprint": contract.topology_fingerprint,
        "capacity_config_fingerprint": contract.capacity_config_fingerprint,
        "activation_state": (
            scope.contract_activation_state if scope is not None else "missing"
        ),
        "active_contract_version": (
            scope.active_contract_version if scope is not None else ""
        ),
        "candidate_contract_version": (
            scope.candidate_contract_version if scope is not None else ""
        ),
        "shards": [
            {
                "shard_index": row.shard_index,
                "expected_capacity": row.expected_capacity,
                "liveness_state": row.liveness_state,
                "heartbeat_at": row.heartbeat_at,
                "config_fingerprint": row.config_fingerprint,
            }
            for row in rows
        ],
    }


def retire_stopped_dispatch_writers(
    session: Session,
    *,
    stopped_before: datetime,
    actor: str,
) -> list[str]:
    rows = session.scalars(select(WorkerHeartbeat).where(
        WorkerHeartbeat.process_type.in_(FENCED_WRITER_ROLES - {"all"}),
        WorkerHeartbeat.status == "active",
        WorkerHeartbeat.last_seen_at <= stopped_before,
    ))
    retired_at = _now()
    retired_ids: list[str] = []
    for row in rows:
        row.status = "stopped"
        row.last_seen_at = retired_at
        row.heartbeat_metadata = {
            **dict(row.heartbeat_metadata or {}),
            "stop_reason": "release_compose_stop",
            "stopped_at": retired_at.isoformat(),
            "retired_by": actor,
        }
        retired_ids.append(row.worker_id)
    return sorted(retired_ids)


def mark_stale_dispatch_shards(
    session: Session,
    settings,
    *,
    now: datetime | None = None,
) -> int:
    contract = build_dispatch_runtime_contract(settings)
    observed_at = now or _now()
    cutoff = observed_at - timedelta(
        seconds=int(settings.dispatch_shard_stale_seconds),
    )
    rows = session.scalars(
        select(DispatchRuntimeShardState).where(
            DispatchRuntimeShardState.dispatcher_scope
            == contract.dispatcher_scope,
            DispatchRuntimeShardState.heartbeat_at < cutoff,
            DispatchRuntimeShardState.liveness_state != "stale",
        )
    )
    changed = 0
    for row in rows:
        row.liveness_state = "stale"
        row.liveness_version += 1
        changed += 1
    return changed


def _dispatcher_shard_index(
    settings,
    contract: DispatchRuntimeContract,
) -> int:
    account_total = int(getattr(settings, "account_shard_total", 0) or 0)
    shard_index = int(getattr(settings, "account_shard_index", -1))
    if account_total != contract.runtime_shard_total:
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch",
            "dispatcher account shard total differs from runtime total",
        )
    if shard_index < 0 or shard_index >= contract.runtime_shard_total:
        raise DispatchRuntimeContractError(
            "dispatcher_topology_mismatch", "dispatcher shard index is invalid",
        )
    return shard_index


def _locked_scope(
    session: Session,
    scope_name: str,
    *,
    lock: bool = True,
) -> DispatchClaimScope | None:
    statement = select(DispatchClaimScope).where(
        DispatchClaimScope.dispatcher_scope == scope_name,
    )
    return session.scalar(for_update(session, statement) if lock else statement)


def _locked_shard_state(
    session: Session,
    scope_name: str,
    shard_index: int,
) -> DispatchRuntimeShardState | None:
    statement = select(DispatchRuntimeShardState).where(
        DispatchRuntimeShardState.dispatcher_scope == scope_name,
        DispatchRuntimeShardState.shard_index == shard_index,
    )
    return session.scalar(for_update(session, statement))


def _candidate_matches(
    scope: DispatchClaimScope,
    contract: DispatchRuntimeContract,
) -> bool:
    return bool(
        scope.contract_activation_state in {"preparing", "active"}
        and scope.candidate_contract_version == contract.rebuild_contract_version
        and scope.runtime_shard_total == contract.runtime_shard_total
        and scope.topology_fingerprint == contract.topology_fingerprint
        and scope.capacity_config_fingerprint
        == contract.capacity_config_fingerprint
    )


def _create_shard_state(
    session: Session,
    contract: DispatchRuntimeContract,
    *,
    shard_index: int,
    worker_id: str,
    lease_id: str,
    observed_at: datetime,
    state: str,
) -> DispatchRuntimeShardState:
    row = DispatchRuntimeShardState(
        dispatcher_scope=contract.dispatcher_scope,
        shard_index=shard_index,
        expected_capacity=contract.shards[shard_index].effective_worker_capacity,
        config_fingerprint=contract.capacity_config_fingerprint,
        current_worker_id=worker_id,
        current_lease_id=lease_id,
        heartbeat_at=observed_at,
        liveness_state=state,
        liveness_version=1,
    )
    try:
        with session.begin_nested():
            session.add(row)
            session.flush()
        return row
    except IntegrityError:
        existing = _locked_shard_state(
            session, contract.dispatcher_scope, shard_index,
        )
        if existing is None:
            raise
        _update_shard_state(
            existing,
            contract,
            worker_id=worker_id,
            lease_id=lease_id,
            observed_at=observed_at,
            state=state,
        )
        return existing


def _update_shard_state(
    row: DispatchRuntimeShardState,
    contract: DispatchRuntimeContract,
    *,
    worker_id: str,
    lease_id: str,
    observed_at: datetime,
    state: str,
) -> None:
    recovered = row.liveness_state != "live" and state == "live"
    owner_changed = row.current_worker_id != worker_id
    row.expected_capacity = contract.shards[row.shard_index].effective_worker_capacity
    row.config_fingerprint = contract.capacity_config_fingerprint
    row.current_worker_id = worker_id
    row.current_lease_id = lease_id
    row.heartbeat_at = observed_at
    row.liveness_state = state
    if recovered or owner_changed:
        row.liveness_version += 1


def _require_candidate_shards(
    session: Session,
    contract: DispatchRuntimeContract,
    settings,
    observed_at: datetime,
) -> None:
    cutoff = observed_at - timedelta(
        seconds=int(settings.dispatch_shard_stale_seconds),
    )
    rows = list(session.scalars(
        select(DispatchRuntimeShardState).where(
            DispatchRuntimeShardState.dispatcher_scope
            == contract.dispatcher_scope,
        )
    ))
    live = {
        row.shard_index for row in rows
        if 0 <= row.shard_index < contract.runtime_shard_total
        and row.liveness_state == "live"
        and row.heartbeat_at is not None
        and _is_at_or_after(row.heartbeat_at, cutoff)
        and row.config_fingerprint == contract.capacity_config_fingerprint
        and row.expected_capacity
        == contract.shards[row.shard_index].effective_worker_capacity
    }
    expected = set(range(contract.runtime_shard_total))
    if live != expected:
        raise DispatchRuntimeContractError(
            "dispatcher_shard_unavailable",
            f"live={sorted(live)},expected={sorted(expected)}",
        )


def _require_no_old_writers(
    session: Session,
    contract: DispatchRuntimeContract,
    settings,
    observed_at: datetime,
) -> None:
    cutoff = observed_at - timedelta(
        seconds=int(settings.dispatch_shard_stale_seconds),
    )
    heartbeats = session.scalars(select(WorkerHeartbeat).where(
        WorkerHeartbeat.process_type.in_(FENCED_WRITER_ROLES - {"all"}),
        WorkerHeartbeat.status == "active",
        WorkerHeartbeat.last_seen_at >= cutoff,
    ))
    stale_writers = [
        row.worker_id for row in heartbeats
        if str((row.heartbeat_metadata or {}).get("dispatch_contract_version") or "")
        != contract.rebuild_contract_version
    ]
    if stale_writers:
        raise DispatchRuntimeContractError(
            "old_dispatch_writer_active",
            f"workers={sorted(stale_writers)}",
        )


def _is_at_or_after(value: datetime, cutoff: datetime) -> bool:
    observed = value
    boundary = cutoff
    if observed.tzinfo is None and boundary.tzinfo is not None:
        observed = observed.replace(tzinfo=boundary.tzinfo)
    if boundary.tzinfo is None and observed.tzinfo is not None:
        boundary = boundary.replace(tzinfo=observed.tzinfo)
    return observed >= boundary


__all__ = [
    "FENCED_WRITER_ROLES",
    "activate_dispatch_runtime_contract",
    "dispatch_runtime_candidate_status",
    "dispatch_writer_allowed",
    "mark_stale_dispatch_shards",
    "record_dispatcher_shard_heartbeat",
    "retire_stopped_dispatch_writers",
    "stage_dispatch_runtime_contract",
    "verify_dispatch_runtime_active",
    "verify_dispatch_runtime_candidate",
]
