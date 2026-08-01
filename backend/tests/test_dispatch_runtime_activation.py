from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from sqlalchemy import create_engine, update
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import (
    Action,
    AiContentScopeTakeoverBatch,
    DispatchClaimScope,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    DispatchRuntimeShardState,
    Task,
    Tenant,
    WorkerHeartbeat,
)
from app.services.task_center.dispatch_runtime_contract import (
    DispatchRuntimeContractError,
)
from app.services.task_center.dispatch_runtime_control import (
    activate_dispatch_runtime_contract,
    dispatch_writer_allowed,
    record_dispatcher_shard_heartbeat,
    retire_stopped_dispatch_writers,
    stage_dispatch_runtime_contract,
    verify_dispatch_runtime_active,
    verify_dispatch_runtime_candidate,
)


pytestmark = pytest.mark.no_postgres


def test_activation_requires_takeover_and_both_candidate_shards() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        settings = _settings(account_shard_index=0)
        stage_dispatch_runtime_contract(session, settings)
        completed_batch_id = _takeover_batch(session, "completed")
        record_dispatcher_shard_heartbeat(
            session, settings, worker_id="dispatcher-0", now=observed_at,
        )

        with pytest.raises(DispatchRuntimeContractError) as missing_shard:
            activate_dispatch_runtime_contract(
                session,
                settings,
                takeover_head_batch_id=completed_batch_id,
                now=observed_at,
            )
        assert missing_shard.value.code == "dispatcher_shard_unavailable"

        record_dispatcher_shard_heartbeat(
            session,
            _settings(account_shard_index=1),
            worker_id="dispatcher-1",
            now=observed_at,
        )
        with pytest.raises(DispatchRuntimeContractError) as not_active:
            verify_dispatch_runtime_active(
                session, settings, now=observed_at,
            )
        assert not_active.value.code == "shared_dispatch_contract_preparing"
        with pytest.raises(DispatchRuntimeContractError) as takeover_pending:
            pending_batch_id = _takeover_batch(session, "previewed")
            activate_dispatch_runtime_contract(
                session,
                settings,
                takeover_head_batch_id=pending_batch_id,
                now=observed_at,
            )
        assert takeover_pending.value.code == "legacy_content_scope_takeover_pending"

        scope = activate_dispatch_runtime_contract(
            session,
            settings,
            takeover_head_batch_id=completed_batch_id,
            now=observed_at,
        )
        assert scope.contract_activation_state == "active"
        assert dispatch_writer_allowed(
            session, settings, role="dispatcher",
        ) is True
        verified = verify_dispatch_runtime_active(
            session, settings, now=observed_at,
        )
        assert verified["verification_state"] == "active_verified"
        assert verified["scope_capacity"] == 26


def test_verify_active_accepts_claim_executing_past_window_end() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        settings = _settings(account_shard_index=0)
        stage_dispatch_runtime_contract(session, settings)
        completed_batch_id = _takeover_batch(session, "completed")
        for index in range(2):
            record_dispatcher_shard_heartbeat(
                session,
                _settings(account_shard_index=index),
                worker_id=f"dispatcher-{index}",
                now=observed_at,
            )
        scope = activate_dispatch_runtime_contract(
            session,
            settings,
            takeover_head_batch_id=completed_batch_id,
            now=observed_at,
        )
        _seed_cross_window_active_claim(session, scope, observed_at)

        verified = verify_dispatch_runtime_active(
            session,
            settings,
            now=observed_at,
        )

        assert verified["verification_state"] == "active_verified"


def test_verify_active_refreshes_scope_cached_before_lock() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        settings = _settings(account_shard_index=0)
        stage_dispatch_runtime_contract(session, settings)
        completed_batch_id = _takeover_batch(session, "completed")
        for index in range(2):
            record_dispatcher_shard_heartbeat(
                session,
                _settings(account_shard_index=index),
                worker_id=f"dispatcher-{index}",
                now=observed_at,
            )
        scope = activate_dispatch_runtime_contract(
            session, settings,
            takeover_head_batch_id=completed_batch_id,
            now=observed_at,
        )
        _seed_cross_window_active_claim(
            session, scope, observed_at, update_scope=False,
        )
        session.execute(update(DispatchClaimScope).where(
            DispatchClaimScope.id == scope.id,
        ).values(active_claim_count=1).execution_options(
            synchronize_session=False,
        ))

        verified = verify_dispatch_runtime_active(
            session, settings, now=observed_at,
        )

        assert verified["verification_state"] == "active_verified"


def test_fresh_old_writer_blocks_activation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        settings = _settings(account_shard_index=0)
        stage_dispatch_runtime_contract(session, settings)
        completed_batch_id = _takeover_batch(session, "completed")
        for index in range(2):
            record_dispatcher_shard_heartbeat(
                session,
                _settings(account_shard_index=index),
                worker_id=f"dispatcher-{index}",
                now=observed_at,
            )
        session.add(WorkerHeartbeat(
            id="old-worker-row",
            worker_id="old-planner",
            process_type="planner",
            hostname="host",
            pid=7,
            status="active",
            heartbeat_metadata={},
            started_at=observed_at,
            last_seen_at=observed_at,
        ))
        session.flush()

        with pytest.raises(DispatchRuntimeContractError) as caught:
            activate_dispatch_runtime_contract(
                session,
                settings,
                takeover_head_batch_id=completed_batch_id,
                now=observed_at,
            )

        assert caught.value.code == "old_dispatch_writer_active"


def test_release_retirement_only_stops_writers_at_or_before_cutoff() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    cutoff = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        rows = (
            WorkerHeartbeat(
                id="old", worker_id="old-planner", process_type="planner",
                hostname="host", pid=1, status="active", heartbeat_metadata={},
                started_at=cutoff, last_seen_at=cutoff,
            ),
            WorkerHeartbeat(
                id="fresh", worker_id="fresh-planner", process_type="planner",
                hostname="host", pid=2, status="active", heartbeat_metadata={},
                started_at=cutoff, last_seen_at=cutoff.replace(minute=1),
            ),
            WorkerHeartbeat(
                id="listener", worker_id="listener", process_type="listener",
                hostname="host", pid=3, status="active", heartbeat_metadata={},
                started_at=cutoff, last_seen_at=cutoff,
            ),
        )
        session.add_all(rows)
        session.flush()

        retired = retire_stopped_dispatch_writers(
            session,
            stopped_before=cutoff,
            actor="release-owner",
        )

        assert retired == ["old-planner"]
        assert rows[0].status == "stopped"
        assert rows[1].status == "active"
        assert rows[2].status == "active"
        assert rows[0].heartbeat_metadata["retired_by"] == "release-owner"


def test_stale_out_of_range_shard_history_does_not_break_readiness() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    observed_at = datetime(2026, 8, 1, 4, tzinfo=timezone.utc)
    with Session(engine) as session:
        settings = _settings(account_shard_index=0)
        stage_dispatch_runtime_contract(session, settings)
        session.add(DispatchRuntimeShardState(
            dispatcher_scope="task_center_dispatch",
            shard_index=3,
            expected_capacity=13,
            config_fingerprint="retired-topology",
            current_worker_id="retired-dispatcher",
            heartbeat_at=observed_at,
            liveness_state="stale",
        ))
        for index in range(2):
            record_dispatcher_shard_heartbeat(
                session,
                _settings(account_shard_index=index),
                worker_id=f"dispatcher-{index}",
                now=observed_at,
            )

        status = verify_dispatch_runtime_candidate(
            session, settings, now=observed_at,
        )

        assert status["runtime_shard_total"] == 2
        assert len(status["shards"]) == 3


def _settings(**overrides):
    values = {
        "app_env": "production",
        "worker_role": "dispatcher",
        "dispatcher_claim_scope": "task_center_dispatch",
        "dispatch_runtime_shard_total": 2,
        "account_shard_total": 2,
        "account_shard_index": 0,
        "dispatcher_scope_capacity": 26,
        "dispatcher_concurrency": 20,
        "db_pool_size": 5,
        "db_max_overflow": 10,
        "db_pool_control_reserve": 2,
        "dispatch_shard_stale_seconds": 120,
        "dispatch_topology_fingerprint_schema_version": "dispatch_topology_v1",
        "dispatch_rebuild_contract_version": "dispatch-rebuild-v3",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _takeover_batch(session: Session, status: str) -> str:
    batch = AiContentScopeTakeoverBatch(
        dispatcher_scope="task_center_dispatch",
        cutoff_at=datetime(2026, 8, 1, 3, tzinfo=timezone.utc),
        actor="release-owner",
        classification_hash="a" * 64,
        classification_counts={},
        status=status,
    )
    session.add(batch)
    session.flush()
    return batch.id


def _seed_cross_window_active_claim(
    session: Session,
    scope,
    observed_at: datetime,
    *,
    update_scope: bool = True,
) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="cross-window-task",
        tenant_id=1,
        name="cross-window",
        type="channel_view",
        status="running",
    ))
    ledger = _cross_window_ledger(scope.dispatcher_scope, observed_at)
    action = _cross_window_action(scope.dispatcher_scope, ledger)
    if update_scope:
        scope.active_claim_count = 1
    session.add_all([*ledger, action])
    session.flush()


def _cross_window_ledger(
    dispatcher_scope: str,
    observed_at: datetime,
) -> tuple[
    DispatchClaimWindow,
    DispatchClaimShardAllocation,
    DispatchClaimReservation,
]:
    window = DispatchClaimWindow(
        id="cross-window",
        dispatcher_scope=dispatcher_scope,
        bucket_start=observed_at - timedelta(seconds=61),
        bucket_end=observed_at - timedelta(seconds=1),
        claim_capacity=26,
        active_claim_count=1,
    )
    allocation = DispatchClaimShardAllocation(
        id="cross-window-allocation",
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=1,
        account_shard_total=2,
        account_shard_index=0,
        active_claim_count=1,
    )
    reservation = DispatchClaimReservation(
        id="cross-window-reservation",
        dispatch_claim_shard_allocation_id=allocation.id,
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="cross-window-task",
        claim_class="ordinary",
        bucket_start=window.bucket_start,
        required_claims=1,
        reserved_claims=1,
        claimed_count=1,
    )
    return window, allocation, reservation


def _cross_window_action(
    dispatcher_scope: str,
    ledger: tuple[
        DispatchClaimWindow,
        DispatchClaimShardAllocation,
        DispatchClaimReservation,
    ],
) -> Action:
    window, allocation, reservation = ledger
    return Action(
        id="cross-window-action",
        tenant_id=1,
        task_id="cross-window-task",
        task_type="channel_view",
        action_type="view_message",
        status="executing",
        scheduled_at=window.bucket_start,
        payload={},
        result={
            "dispatch_claim_active": True,
            "dispatch_claim_scope": dispatcher_scope,
            "dispatch_claim_window_id": window.id,
            "dispatch_claim_shard_allocation_id": allocation.id,
            "dispatch_reservation_id": reservation.id,
        },
    )
