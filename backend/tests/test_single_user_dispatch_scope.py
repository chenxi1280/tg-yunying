from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    DispatchClaimReservation,
    DispatchClaimScope,
    DispatchClaimShardAllocation,
    DispatchClaimTaskAllocation,
    DispatchClaimWindow,
    Task,
    Tenant,
)
from app.services.task_center.dispatch_claim_allocation import (
    _allocate_demands,
    allocate_window,
    request_window_rebuild,
)
from app.services.task_center.dispatch_claim_types import DispatchClaimDemand


pytestmark = pytest.mark.no_postgres


def _demand(
    task_id: str,
    shard_index: int,
    *,
    required: int = 1,
    lane: str = "fulfillment",
) -> DispatchClaimDemand:
    return DispatchClaimDemand(
        tenant_id=1,
        task_id=task_id,
        claim_class="ordinary",
        shard_total=2,
        shard_index=shard_index,
        action_ids=(f"{task_id}-{lane}-{shard_index}",),
        required_claims=required,
        urgency_score=100,
        is_strict=False,
        allocation_business_task_id=task_id,
        lane_business_kind=lane,
    )


def test_parent_task_gets_only_one_minimum_share_across_shards() -> None:
    demands = [
        _demand("task-a", 0),
        _demand("task-a", 1),
        _demand("task-b", 0),
    ]

    grants = _allocate_demands(demands, available=2, epoch=1)

    assert sum(grants[item.key] for item in demands[:2]) == 1
    assert grants[demands[2].key] == 1


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="唯一业务用户"))
        current.add_all(
            [
                Task(
                    id="task-a",
                    tenant_id=1,
                    name="A",
                    type="group_ai_chat",
                    status="running",
                ),
                Task(
                    id="task-b",
                    tenant_id=1,
                    name="B",
                    type="channel_comment",
                    status="running",
                ),
            ]
        )
        current.commit()
        yield current


def _window_rows(session: Session):
    start = datetime(2026, 7, 29, 0, tzinfo=timezone.utc)
    scope = DispatchClaimScope(
        dispatcher_scope="single_user_task_center",
        claim_capacity=2,
    )
    window = DispatchClaimWindow(
        dispatcher_scope=scope.dispatcher_scope,
        bucket_start=start,
        bucket_end=start + timedelta(seconds=60),
        claim_capacity=2,
        allocation_state="rebuild_required",
    )
    session.add_all([scope, window])
    session.flush()
    return scope, window


def test_epoch_is_published_atomically_and_old_rows_are_immutable(
    session: Session,
) -> None:
    scope, window = _window_rows(session)
    demands = [_demand("task-a", 0), _demand("task-b", 0)]

    allocate_window(session, scope, window, [], demands)
    session.commit()

    first_task_rows = session.scalars(
        select(DispatchClaimTaskAllocation).where(
            DispatchClaimTaskAllocation.dispatch_claim_window_id == window.id
        )
    ).all()
    first_shard_rows = session.scalars(select(DispatchClaimShardAllocation)).all()
    first_reservations = session.scalars(select(DispatchClaimReservation)).all()
    assert window.allocation_state == "ready"
    assert window.allocation_epoch == 1
    assert scope.opportunity_cursor == 1
    assert window.rebuild_input_hash
    assert {row.dispatch_allocation_epoch for row in first_task_rows} == {1}
    assert {row.dispatch_allocation_epoch for row in first_shard_rows} == {1}
    assert {row.dispatch_allocation_epoch for row in first_reservations} == {1}

    assert request_window_rebuild(
        window,
        released_count=0,
        rebuild_input_hash="ignored-empty-release",
    ) is False
    assert window.allocation_state == "ready"
    assert window.allocation_epoch == 1

    assert request_window_rebuild(
        window,
        released_count=1,
        rebuild_input_hash="complete-input-v2",
    ) is True
    allocate_window(session, scope, window, first_shard_rows, demands)
    session.commit()

    all_task_rows = session.scalars(
        select(DispatchClaimTaskAllocation).order_by(
            DispatchClaimTaskAllocation.dispatch_allocation_epoch
        )
    ).all()
    assert window.allocation_state == "ready"
    assert window.allocation_epoch == 2
    assert scope.opportunity_cursor == 2
    assert window.rebuild_input_hash != "complete-input-v2"
    assert window.ready_rebuild_snapshot_hash == window.rebuild_input_hash
    assert all(
        row.dispatch_rebuild_snapshot_hash == window.rebuild_input_hash
        for row in session.scalars(
            select(DispatchClaimTaskAllocation).where(
                DispatchClaimTaskAllocation.dispatch_allocation_epoch == 2
            )
        )
    )
    assert {row.dispatch_allocation_epoch for row in all_task_rows} == {1, 2}
    assert {row.opportunity_cursor_snapshot for row in all_task_rows} == {1, 2}
    assert all(row.rebuild_input_hash for row in all_task_rows)
    assert all_task_rows[0].reserved_claims == 1
