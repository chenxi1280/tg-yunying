from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import (
    Action,
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    ExecutionAttempt,
    RemoteReconcileCase,
    Task,
    Tenant,
)
from app.services._common import _now
from app.services.task_center.dispatch_activation_ledger import (
    reconcile_dispatch_ledgers_for_activation,
    recover_fenced_dispatch_actions,
)
from app.services.task_center.dispatch_runtime_contract import (
    DispatchRuntimeContractError,
)
from app.services.task_center.dispatch_runtime_ledger_validation import (
    validate_dispatch_ledgers_for_runtime,
)
from app.services.task_center.dispatch_runtime_control import (
    stage_dispatch_runtime_contract,
)


pytestmark = pytest.mark.no_postgres


def test_fenced_pre_gateway_claim_returns_to_same_pending_action() -> None:
    engine = _engine()
    with Session(engine) as session:
        action = _seed_active_claim(session, "pre-gateway", gateway_started=False)
        assert recover_fenced_dispatch_actions(
            session,
            actor="release-owner",
        ) == 1
        session.commit()

        scope = session.get(DispatchClaimScope, "scope")
        window = session.get(DispatchClaimWindow, "window")
        allocation = session.get(DispatchClaimShardAllocation, "allocation")
        assert action.status == "pending"
        assert action.result["dispatch_claim_active"] is False
        assert (scope.active_claim_count, window.active_claim_count) == (0, 0)
        assert allocation.active_claim_count == 0


def test_fenced_gateway_claim_becomes_one_remote_case() -> None:
    engine = _engine()
    with Session(engine) as session:
        action = _seed_active_claim(session, "gateway", gateway_started=True)
        assert recover_fenced_dispatch_actions(
            session,
            actor="release-owner",
        ) == 1
        session.commit()

        case = session.query(RemoteReconcileCase).one()
        assert action.status == "unknown_after_send"
        assert action.result["dispatch_claim_active"] is False
        assert case.action_id == action.id
        assert recover_fenced_dispatch_actions(
            session,
            actor="release-owner",
        ) == 0


def test_activation_reconcile_releases_stale_unclaimed_contract() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_stale_unclaimed(session)
        settings = _settings()
        stage_dispatch_runtime_contract(session, settings)

        result = reconcile_dispatch_ledgers_for_activation(session, settings)
        session.commit()

        reservation = session.get(DispatchClaimReservation, "reservation")
        window = session.get(DispatchClaimWindow, "window")
        action = session.get(Action, "due-action")
        assert result["released_unclaimed_count"] == 1
        assert reservation.reserved_claims == 0
        assert window.effective_unclaimed_count == 0
        assert window.claim_capacity == 26
        assert action.result["dispatch_binding_replan_required"] is True


def test_activation_reconcile_does_not_replay_closed_window_history() -> None:
    engine = _engine()
    with Session(engine, autoflush=False) as session:
        _seed_stale_unclaimed(session)
        closed_reservation = _seed_closed_active_drift(session)
        settings = _settings()
        stage_dispatch_runtime_contract(session, settings)
        session.flush()
        session.expire_all()

        result = reconcile_dispatch_ledgers_for_activation(session, settings)
        session.commit()

        closed_window = session.get(DispatchClaimWindow, "closed-window")
        closed_allocation = session.get(
            DispatchClaimShardAllocation,
            "closed-allocation",
        )
        assert result["window_count"] == 1
        assert result["closed_active_window_count"] == 1
        assert closed_window.active_claim_count == 0
        assert closed_allocation.active_claim_count == 0
        assert closed_reservation.reserved_claims == 7


def test_runtime_validation_treats_closed_unclaimed_as_historical() -> None:
    engine = _engine()
    with Session(engine) as session:
        _seed_active_claim(session, "cross-window", gateway_started=True)
        observed_at = _now()
        window = session.get(DispatchClaimWindow, "window")
        allocation = session.get(DispatchClaimShardAllocation, "allocation")
        reservation = session.get(DispatchClaimReservation, "reservation")
        window.bucket_end = observed_at - timedelta(seconds=1)
        window.unclaimed_allocated_count = 1
        window.effective_unclaimed_count = 1
        allocation.unclaimed_allocated_count = 1
        reservation.reserved_claims = 2
        session.flush()

        validate_dispatch_ledgers_for_runtime(
            session,
            _settings(),
            now=observed_at,
        )


def test_runtime_validation_rejects_unbacked_closed_active_projection() -> None:
    engine = _engine()
    with Session(engine) as session:
        action = _seed_active_claim(session, "drift", gateway_started=False)
        observed_at = _now()
        window = session.get(DispatchClaimWindow, "window")
        window.bucket_end = observed_at - timedelta(seconds=1)
        action.status = "success"
        session.flush()

        with pytest.raises(DispatchRuntimeContractError) as caught:
            validate_dispatch_ledgers_for_runtime(
                session,
                _settings(),
                now=observed_at,
            )

        assert caught.value.code == "dispatch_ledger_invariant_failed"
        assert str(caught.value).endswith(":scope_active_projection")


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_active_claim(
    session: Session,
    suffix: str,
    *,
    gateway_started: bool,
) -> Action:
    now = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task",
        tenant_id=1,
        name="dispatch",
        type="channel_view",
        status="running",
    ))
    scope = DispatchClaimScope(
        id="scope",
        dispatcher_scope="task_center_dispatch",
        claim_capacity=26,
        active_claim_count=1,
    )
    window = DispatchClaimWindow(
        id="window",
        dispatcher_scope=scope.dispatcher_scope,
        bucket_start=now - timedelta(seconds=10),
        bucket_end=now + timedelta(seconds=50),
        claim_capacity=26,
        active_claim_count=1,
        effective_unclaimed_count=0,
    )
    allocation = DispatchClaimShardAllocation(
        id="allocation",
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=1,
        dispatch_contract_version="dispatch-rebuild-v3",
        account_shard_total=2,
        account_shard_index=0,
        active_claim_count=1,
    )
    reservation = DispatchClaimReservation(
        id="reservation",
        dispatch_claim_shard_allocation_id=allocation.id,
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task",
        claim_class="ordinary",
        bucket_start=window.bucket_start,
        required_claims=1,
        reserved_claims=1,
        claimed_count=1,
    )
    action = Action(
        id=f"action-{suffix}",
        tenant_id=1,
        task_id="task",
        task_type="channel_view",
        action_type="view_message",
        status="executing",
        scheduled_at=now - timedelta(minutes=1),
        payload={},
        result={
            "dispatch_claim_active": True,
            "dispatch_claim_scope": scope.dispatcher_scope,
            "dispatch_claim_window_id": window.id,
            "dispatch_claim_shard_allocation_id": allocation.id,
            "dispatch_reservation_id": reservation.id,
            "gateway_request_identity": f"request-{suffix}",
        },
    )
    session.add_all([scope, window, allocation, reservation, action])
    if gateway_started:
        session.add(ExecutionAttempt(
            id=f"attempt-{suffix}",
            tenant_id=1,
            action_id=action.id,
            attempt_no=1,
            status="gateway_call_started",
            before_call_at=now - timedelta(seconds=2),
            gateway_call_started_at=now - timedelta(seconds=1),
            result_snapshot={
                "gateway_request_identity": f"request-{suffix}",
            },
        ))
    session.flush()
    return action


def _seed_stale_unclaimed(session: Session) -> None:
    now = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task",
        tenant_id=1,
        name="dispatch",
        type="channel_view",
        status="running",
    ))
    session.add(DispatchClaimScope(
        id="scope",
        dispatcher_scope="task_center_dispatch",
        claim_capacity=52,
    ))
    session.add(DispatchClaimWindow(
        id="window",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now - timedelta(seconds=10),
        bucket_end=now + timedelta(seconds=50),
        claim_capacity=52,
        unclaimed_allocated_count=1,
        effective_unclaimed_count=1,
    ))
    session.add(DispatchClaimShardAllocation(
        id="allocation",
        dispatch_claim_window_id="window",
        dispatch_allocation_epoch=1,
        dispatch_contract_version="",
        account_shard_total=1,
        account_shard_index=0,
        required_claims=1,
        unclaimed_allocated_count=1,
    ))
    session.add(DispatchClaimReservation(
        id="reservation",
        dispatch_claim_shard_allocation_id="allocation",
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task",
        claim_class="ordinary",
        bucket_start=now - timedelta(seconds=10),
        required_claims=1,
        reserved_claims=1,
    ))
    session.add(Action(
        id="due-action",
        tenant_id=1,
        task_id="task",
        task_type="channel_view",
        action_type="view_message",
        status="pending",
        scheduled_at=now - timedelta(minutes=1),
        payload={},
        result={},
    ))
    session.flush()


def _seed_closed_active_drift(session: Session) -> DispatchClaimReservation:
    now = _now()
    window = DispatchClaimWindow(
        id="closed-window",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now - timedelta(hours=2),
        bucket_end=now - timedelta(hours=1),
        claim_capacity=52,
        active_claim_count=9,
        unclaimed_allocated_count=7,
        effective_unclaimed_count=7,
    )
    allocation = DispatchClaimShardAllocation(
        id="closed-allocation",
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=1,
        account_shard_total=1,
        account_shard_index=0,
        active_claim_count=9,
        unclaimed_allocated_count=7,
    )
    reservation = DispatchClaimReservation(
        id="closed-reservation",
        dispatch_claim_shard_allocation_id=allocation.id,
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task",
        claim_class="ordinary",
        bucket_start=window.bucket_start,
        required_claims=7,
        reserved_claims=7,
    )
    session.add_all([window, allocation, reservation])
    session.flush()
    return reservation


def _settings():
    return SimpleNamespace(
        app_env="production",
        worker_role="dispatcher",
        dispatcher_claim_scope="task_center_dispatch",
        dispatch_runtime_shard_total=2,
        account_shard_total=2,
        account_shard_index=0,
        dispatcher_scope_capacity=26,
        dispatcher_concurrency=20,
        db_pool_size=5,
        db_max_overflow=10,
        db_pool_control_reserve=2,
        dispatch_shard_stale_seconds=120,
        dispatch_topology_fingerprint_schema_version="dispatch_topology_v1",
        dispatch_rebuild_contract_version="dispatch-rebuild-v3",
    )
