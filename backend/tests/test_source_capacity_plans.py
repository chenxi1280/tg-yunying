from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import SourcePacingCapacityPlan, SourcePacingCapacityPolicyVersion, Task, Tenant
from app.services.task_center.source_capacity_plans import (
    CapacityDemand,
    CapacityBaseline,
    CapacityPolicy,
    CapacityScope,
    SourceCapacityConflict,
    apply_source_capacity_plan,
    freeze_source_capacity_plan,
    plan_source_capacity,
)
from app.services.task_center.source_capacity_repository import lock_source_capacity
from app.services.task_center.source_pacing import SourcePacingPoint, SourcePacingSlot


pytestmark = pytest.mark.no_postgres


def _curve(*enabled_hours: int) -> tuple[float, ...]:
    return tuple(1.0 if hour in enabled_hours else 0.0 for hour in range(24))


def _scope(start: datetime) -> CapacityScope:
    return CapacityScope(
        tenant_id=1,
        pacing_domain="group:7",
        source_key_hash="s" * 64,
        window_start_at=start,
        window_end_at=start + timedelta(hours=2),
        policy_version_id="policy-1",
        curve_hash="c" * 64,
    )


def _policy(*hours: int) -> CapacityPolicy:
    return CapacityPolicy(
        hourly_curve=_curve(*hours),
        minimum_gap_seconds=300,
        hourly_ceiling=8,
        headroom_floor=0.5,
        provider_retry_slots=1,
    )


def test_capacity_curve_keeps_zero_hours_empty_and_reserves_headroom() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    demands = (
        CapacityDemand("owner-1", start, start + timedelta(hours=1)),
        CapacityDemand("owner-2", start, start + timedelta(hours=1)),
    )

    result = plan_source_capacity(
        _scope(start),
        _policy(10),
        occupied_at=(),
        demands=demands,
    )

    assert result.admitted is True
    assert result.incoming_count == 2
    assert result.replacement_headroom == 1
    assert result.available_count == 3
    assert len(result.owner_slot_ordinals) == 2
    assert all(item.hour == 10 for item in result.capacity_slots)
    gaps = [
        (right - left).total_seconds()
        for left, right in zip(result.capacity_slots, result.capacity_slots[1:])
    ]
    assert min(gaps) >= 300
    assert len(set(gaps)) > 1


def test_capacity_places_a_slot_inside_late_tail_demand_window() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    demand = CapacityDemand(
        "owner-1",
        start + timedelta(minutes=50),
        start + timedelta(hours=1),
    )

    result = plan_source_capacity(
        _scope(start),
        replace(_policy(10), headroom_floor=0, provider_retry_slots=0),
        occupied_at=(),
        demands=(demand,),
    )

    assert result.admitted is True
    assigned = result.capacity_slots[result.owner_slot_ordinals["owner-1"] - 1]
    assert demand.earliest_at <= assigned < demand.latest_at


def test_capacity_keeps_feasible_late_tail_demands_gap_safe() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    scope = replace(
        _scope(start),
        source_key_hash=f"{1:064x}",
        window_end_at=start + timedelta(hours=1),
    )
    demands = tuple(
        CapacityDemand(
            f"owner-{index}",
            start + timedelta(minutes=50),
            start + timedelta(hours=1),
        )
        for index in (1, 2)
    )

    result = plan_source_capacity(
        scope,
        replace(_policy(10), headroom_floor=0, provider_retry_slots=0),
        occupied_at=(),
        demands=demands,
    )

    assert result.admitted is True
    assigned = sorted(
        result.capacity_slots[ordinal - 1]
        for ordinal in result.owner_slot_ordinals.values()
    )
    assert len(assigned) == 2
    assert (assigned[1] - assigned[0]).total_seconds() >= 300


def test_capacity_revision_preserves_prior_slots_and_reuses_headroom() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    scope = _scope(start)
    policy = _policy(10)
    first = plan_source_capacity(
        scope,
        policy,
        occupied_at=(),
        demands=(CapacityDemand("owner-1", start, start + timedelta(hours=1)),),
    )
    second = plan_source_capacity(
        scope,
        policy,
        occupied_at=(),
        demands=(CapacityDemand("owner-2", start, start + timedelta(hours=1)),),
        baseline=CapacityBaseline(
            capacity_slots=first.capacity_slots,
            incoming_count=first.incoming_count,
            replacement_headroom=first.replacement_headroom,
        ),
    )

    assert second.admitted is True
    assert second.incoming_count == 2
    assert second.replacement_headroom == 1
    assert set(first.capacity_slots) < set(second.capacity_slots)
    assert min(
        (right - left).total_seconds()
        for left, right in zip(second.capacity_slots, second.capacity_slots[1:])
    ) >= policy.minimum_gap_seconds


def test_deadline_in_zero_curve_hour_fails_entire_admission() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    demand = CapacityDemand(
        "owner-1",
        start + timedelta(hours=1),
        start + timedelta(hours=2),
    )

    result = plan_source_capacity(
        _scope(start),
        _policy(10),
        occupied_at=(),
        demands=(demand,),
    )

    assert result.admitted is False
    assert result.deficit_count >= 1
    assert result.owner_slot_ordinals == {}


def test_occupied_source_timeline_can_make_headroom_insufficient() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    policy = replace(_policy(10), minimum_gap_seconds=1800, hourly_ceiling=3)
    demand = CapacityDemand("owner-1", start, start + timedelta(hours=1))

    result = plan_source_capacity(
        _scope(start),
        policy,
        occupied_at=(start + timedelta(minutes=20),),
        demands=(demand,),
    )

    assert result.admitted is False
    assert result.owner_slot_ordinals == {}


def test_frozen_capacity_scope_is_idempotent_and_revisable() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    scope = _scope(start)
    result = plan_source_capacity(scope, _policy(10), occupied_at=(), demands=())
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant-1"))
        session.add(SourcePacingCapacityPolicyVersion(
            id="policy-1",
            tenant_id=1,
            pacing_domain="group:7",
            revision=1,
            hourly_curve=list(_curve(10)),
            minimum_gap_seconds=300,
            hourly_ceiling=8,
            telemetry_window={},
            headroom_floor=0.5,
            provider_retry_slots=1,
            status="active",
            content_hash="p" * 64,
        ))
        first = freeze_source_capacity_plan(session, scope, result)
        second = freeze_source_capacity_plan(session, scope, result)
        assert first.id == second.id
        first_hash = first.plan_hash

        changed = replace(result, deficit_count=1)
        revised = freeze_source_capacity_plan(session, scope, changed)

        assert revised.id != first.id
        assert revised.revision == 2
        plans = session.scalars(
            select(SourcePacingCapacityPlan).order_by(SourcePacingCapacityPlan.revision)
        ).all()
        assert [plan.revision for plan in plans] == [1, 2]
        assert plans[0].plan_hash == first_hash


def test_postgres_capacity_freeze_uses_stable_source_transaction_lock() -> None:
    session = Mock()
    session.get_bind.return_value.dialect.name = "postgresql"
    scope = _scope(datetime(2026, 8, 19, 10, 0))

    lock_source_capacity(session, scope)
    first_params = session.execute.call_args.args[1]
    session.execute.reset_mock()
    lock_source_capacity(session, replace(scope, window_end_at=scope.window_end_at + timedelta(hours=1)))
    second_statement, second_params = session.execute.call_args.args

    assert "pg_advisory_xact_lock" in str(second_statement)
    assert first_params == second_params


def test_enabled_planner_capacity_preserves_due_and_freezes_release_binding() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = Task(
            id="task-1",
            tenant_id=1,
            name="group-ai",
            type="group_ai_chat",
            pacing_config={
                "source_capacity_v2_enabled": True,
                "source_capacity_policy_version_id": "policy-ai",
            },
        )
        session.add_all((
            Tenant(id=1, name="tenant-1"),
            task,
            SourcePacingCapacityPolicyVersion(
                id="policy-ai",
                tenant_id=1,
                pacing_domain="ai_send",
                revision=1,
                hourly_curve=list(_curve(10)),
                minimum_gap_seconds=300,
                hourly_ceiling=8,
                telemetry_window={},
                headroom_floor=0.5,
                provider_retry_slots=1,
                status="active",
                content_hash="p" * 64,
            ),
        ))
        session.flush()
        slot = SourcePacingSlot(
            source_key="group-7",
            slot_key="ai:owner-1",
            slot_ordinal=1,
            plan_total=1,
            period_start_at=start,
            deadline_at=start + timedelta(hours=1),
            owner_id="owner-1",
            pacing_period_key="2026-08-19",
            pacing_source_key_hash="s" * 64,
        )
        due = start + timedelta(minutes=5)

        points, enriched = apply_source_capacity_plan(
            session,
            task,
            [slot],
            points={slot.slot_key: SourcePacingPoint(due, due)},
            pacing_domain="ai_send",
        )

        assert points[slot.slot_key].due_at == due
        assert points[slot.slot_key].release_not_before_at > due
        assert enriched[0].source_capacity_plan_hash
        assert enriched[0].source_capacity_slot_ordinal == 1


def test_enabled_planner_aggregates_same_scope_across_tasks() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="tenant-1"))
        session.add(SourcePacingCapacityPolicyVersion(
            id="policy-ai",
            tenant_id=1,
            pacing_domain="ai_send",
            revision=1,
            hourly_curve=list(_curve(10)),
            minimum_gap_seconds=300,
            hourly_ceiling=8,
            telemetry_window={},
            headroom_floor=0.5,
            provider_retry_slots=1,
            status="active",
            content_hash="p" * 64,
        ))
        tasks = [
            Task(
                id=f"task-{index}",
                tenant_id=1,
                name=f"group-ai-{index}",
                type="group_ai_chat",
                pacing_config={
                    "source_capacity_v2_enabled": True,
                    "source_capacity_policy_version_id": "policy-ai",
                },
            )
            for index in (1, 2)
        ]
        session.add_all(tasks)
        session.flush()

        releases = []
        for index, task in enumerate(tasks, 1):
            slot = _source_slot(start, index)
            points, _enriched = apply_source_capacity_plan(
                session,
                task,
                [slot],
                points={slot.slot_key: SourcePacingPoint(start, start)},
                pacing_domain="ai_send",
            )
            releases.append(points[slot.slot_key].release_not_before_at)

        plans = session.scalars(
            select(SourcePacingCapacityPlan).order_by(SourcePacingCapacityPlan.revision)
        ).all()
        assert [plan.incoming_count for plan in plans] == [1, 2]
        assert [plan.revision for plan in plans] == [1, 2]
        assert abs((releases[1] - releases[0]).total_seconds()) >= 300


def test_enabled_planner_aggregates_unequal_overlapping_windows() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tasks = _seed_capacity_tasks(session)
        windows = (
            (start, start + timedelta(hours=1)),
            (start + timedelta(minutes=30), start + timedelta(hours=1, minutes=30)),
        )
        releases = []
        for index, (task, window) in enumerate(zip(tasks, windows, strict=True), 1):
            slot = replace(
                _source_slot(start, index),
                period_start_at=window[0],
                deadline_at=window[1],
            )
            points, _enriched = apply_source_capacity_plan(
                session,
                task,
                [slot],
                points={slot.slot_key: SourcePacingPoint(window[0], window[0])},
                pacing_domain="ai_send",
            )
            releases.append(points[slot.slot_key].release_not_before_at)

        plans = session.scalars(
            select(SourcePacingCapacityPlan).order_by(SourcePacingCapacityPlan.created_at)
        ).all()
        latest = plans[-1]
        assert latest.window_start_at == start
        assert latest.window_end_at == start + timedelta(hours=1, minutes=30)
        assert latest.incoming_count == 2
        assert abs((releases[1] - releases[0]).total_seconds()) >= 300


def test_bridging_window_preserves_disconnected_capacity_timelines() -> None:
    start = datetime(2026, 8, 19, 10, 0)
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tasks = _seed_capacity_tasks(session)
        third = Task(
            id="task-overlap-3",
            tenant_id=1,
            name="group-ai-overlap-3",
            type="group_ai_chat",
            pacing_config={
                "source_capacity_v2_enabled": True,
                "source_capacity_policy_version_id": "policy-ai",
            },
        )
        session.add(third)
        session.flush()
        windows = (
            (start, start + timedelta(hours=1)),
            (start + timedelta(hours=2), start + timedelta(hours=3)),
            (start + timedelta(minutes=30), start + timedelta(hours=2, minutes=30)),
        )
        for index, (task, window) in enumerate(zip((*tasks, third), windows, strict=True), 1):
            slot = replace(
                _source_slot(window[0], index),
                deadline_at=window[1],
            )
            apply_source_capacity_plan(
                session,
                task,
                [slot],
                points={slot.slot_key: SourcePacingPoint(window[0], window[0])},
                pacing_domain="ai_send",
            )

        plans = session.scalars(
            select(SourcePacingCapacityPlan).order_by(SourcePacingCapacityPlan.created_at)
        ).all()
        latest = plans[-1]
        assert latest.window_start_at == start
        assert latest.window_end_at == start + timedelta(hours=3)
        assert latest.incoming_count == 3


def _seed_capacity_tasks(session: Session) -> list[Task]:
    session.add(Tenant(id=1, name="tenant-1"))
    session.add(SourcePacingCapacityPolicyVersion(
        id="policy-ai",
        tenant_id=1,
        pacing_domain="ai_send",
        revision=1,
        hourly_curve=list(_curve(10, 11, 12)),
        minimum_gap_seconds=300,
        hourly_ceiling=8,
        telemetry_window={},
        headroom_floor=0.5,
        provider_retry_slots=1,
        status="active",
        content_hash="p" * 64,
    ))
    tasks = [
        Task(
            id=f"task-overlap-{index}",
            tenant_id=1,
            name=f"group-ai-overlap-{index}",
            type="group_ai_chat",
            pacing_config={
                "source_capacity_v2_enabled": True,
                "source_capacity_policy_version_id": "policy-ai",
            },
        )
        for index in (1, 2)
    ]
    session.add_all(tasks)
    session.flush()
    return tasks


def _source_slot(start: datetime, index: int) -> SourcePacingSlot:
    return SourcePacingSlot(
        source_key="group-7",
        slot_key=f"ai:owner-{index}",
        slot_ordinal=index,
        plan_total=2,
        period_start_at=start,
        deadline_at=start + timedelta(hours=1),
        owner_id=f"owner-{index}",
        pacing_period_key="2026-08-19",
        pacing_source_key_hash="s" * 64,
    )
