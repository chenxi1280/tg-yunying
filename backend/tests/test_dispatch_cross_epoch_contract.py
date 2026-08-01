from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import (
    Action,
    DispatchAllocationExclusion,
    DispatchClaimReservation,
    DispatchClaimShardAllocation,
    DispatchClaimWindow,
    Task,
    Tenant,
)
from app.services._common import _now
from app.services.task_center.dispatch_claim_ledger import (
    claimable_window_reservations,
)
from app.services.task_center.dispatch_claim_reconciliation import (
    reconcile_window_unclaimed,
)


pytestmark = pytest.mark.no_postgres
CURRENT_CONTRACT = "dispatch-rebuild-v3"


def test_current_contract_prior_epoch_remains_claimable() -> None:
    engine = _engine()
    with Session(engine) as session:
        rows = _seed_ordinary(session, contract_version=CURRENT_CONTRACT)

        released = _reconcile(session, rows)
        claimable = claimable_window_reservations(session, rows["window"].id)

        assert released == 0
        assert rows["reservation"].reserved_claims == 1
        assert rows["window"].effective_unclaimed_count == 1
        assert claimable[(1, "task-1", "channel_comment", 2, 1)].id == "reservation-1"


def test_stale_contract_releases_binding_and_reenters_same_action_demand() -> None:
    engine = _engine()
    with Session(engine) as session:
        rows = _seed_ordinary(session, contract_version="dispatch-rebuild-v2")

        released = _reconcile(session, rows)

        action = rows["action"]
        assert released == 1
        assert rows["reservation"].reserved_claims == 0
        assert rows["reservation"].reason == "dispatch_binding_replan_required"
        assert rows["window"].effective_unclaimed_count == 0
        assert action.status == "pending"
        assert action.result["dispatch_binding_replan_required"] is True
        assert action.result["error_code"] == "dispatch_binding_replan_required"
        assert "dispatch_reservation_id" not in action.result
        assert session.scalar(select(func.count(DispatchAllocationExclusion.id))) == 0


def test_no_longer_due_releases_without_search_exclusion() -> None:
    engine = _engine()
    with Session(engine) as session:
        rows = _seed_ordinary(session, contract_version=CURRENT_CONTRACT)
        rows["action"].status = "success"

        released = _reconcile(session, rows)

        assert released == 1
        assert rows["reservation"].reserved_claims == 0
        assert rows["reservation"].reason == "unclaimed_action_no_longer_due"
        assert session.scalar(select(func.count(DispatchAllocationExclusion.id))) == 0


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_ordinary(session: Session, *, contract_version: str) -> dict:
    now_value = _now()
    session.add(Tenant(id=1, name="tenant"))
    session.add(Task(
        id="task-1",
        tenant_id=1,
        name="comment",
        type="channel_comment",
        status="running",
    ))
    action = Action(
        id="action-1",
        tenant_id=1,
        task_id="task-1",
        task_type="channel_comment",
        action_type="post_comment",
        account_id=3,
        scheduled_at=now_value - timedelta(minutes=1),
        status="pending",
        payload={},
        result={
            "dispatch_reservation_id": "reservation-1",
            "dispatch_claim_window_id": "window-1",
            "dispatch_claim_shard_allocation_id": "allocation-1",
        },
    )
    window = DispatchClaimWindow(
        id="window-1",
        dispatcher_scope="task_center_dispatch",
        bucket_start=now_value - timedelta(seconds=30),
        bucket_end=now_value + timedelta(seconds=30),
        claim_capacity=26,
        unclaimed_allocated_count=1,
        effective_unclaimed_count=1,
        allocation_epoch=2,
        allocation_state="rebuild_required",
    )
    allocation = DispatchClaimShardAllocation(
        id="allocation-1",
        dispatch_claim_window_id=window.id,
        dispatch_allocation_epoch=1,
        dispatch_contract_version=contract_version,
        account_shard_total=2,
        account_shard_index=1,
        unclaimed_allocated_count=1,
    )
    reservation = DispatchClaimReservation(
        id="reservation-1",
        dispatch_claim_shard_allocation_id=allocation.id,
        dispatch_allocation_epoch=1,
        tenant_id=1,
        task_id="task-1",
        claim_class="channel_comment",
        bucket_start=window.bucket_start,
        required_claims=1,
        reserved_claims=1,
    )
    session.add_all([action, window, allocation, reservation])
    session.flush()
    return {
        "action": action,
        "window": window,
        "allocation": allocation,
        "reservation": reservation,
        "now": now_value,
    }


def _reconcile(session: Session, rows: dict) -> int:
    return reconcile_window_unclaimed(
        session,
        rows["window"],
        allocations=[rows["allocation"]],
        reservations={
            (1, "task-1", "channel_comment", 2, 1): rows["reservation"],
        },
        now=rows["now"],
        current_contract_version=CURRENT_CONTRACT,
        runtime_shard_total=2,
    )
