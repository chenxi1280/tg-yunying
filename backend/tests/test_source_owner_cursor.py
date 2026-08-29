from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.services.task_center.executors.group_ai_chat import (
    _replace_stale_released_owner_release,
)
from app.services.task_center.pacing_persistence import freeze_pacing_owner
from app.services.task_center.source_owner_cursor import attach_owner_history
from app.services.task_center.source_pacing import (
    SourcePacingSlot,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 12, 0)


def test_owner_history_is_read_across_planner_batches_and_identity_is_frozen() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, old_owner, new_owner = _seed_owners(session)
        slot = _source_slot(task, ledger, new_owner)
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_historical_owner(old_owner, slot, plan_hash)
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]
        point = schedule_source_pacing_points(
            [enriched],
            {},
            seed_id=f"ai:{task.id}",
            now_at=NOW,
        )[slot.slot_key]
        freeze_pacing_owner(
            new_owner,
            plan_hash=plan_hash,
            slot_ordinal=slot.slot_ordinal,
            plan_total=slot.plan_total,
            due_at=point.due_at,
            release_not_before_at=point.release_not_before_at,
            source_identity=slot.owner_identity,
        )

        assert enriched.historical_cursor_at == NOW + timedelta(minutes=5)
        assert point.release_not_before_at > enriched.historical_cursor_at
        assert new_owner.pacing_period_key == ledger.id
        assert new_owner.pacing_source_key_hash == "a" * 64


def test_new_owner_gets_next_source_ordinal_after_out_of_order_history() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, old_owner, new_owner = _seed_owners(session)
        slot = _source_slot(task, ledger, new_owner)
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_historical_owner(old_owner, slot, plan_hash)
        old_owner.pacing_slot_ordinal = 2
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]

        assert slot.slot_ordinal == 1
        assert enriched.slot_ordinal == 3
        assert enriched.historical_max_ordinal == 2


def test_new_owner_continues_cursor_across_plan_hash_versions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, old_owner, new_owner = _seed_owners(session)
        slot = _source_slot(task, ledger, new_owner)
        old_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_historical_owner(old_owner, slot, old_hash)
        old_owner.pacing_plan_hash = "b" * 64
        old_owner.pacing_slot_ordinal = 2
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]

        assert enriched.slot_ordinal == 3
        assert enriched.historical_max_ordinal == 2


def test_new_owner_fails_when_frozen_source_plan_has_no_ordinal_capacity() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, old_owner, new_owner = _seed_owners(session)
        slot = _source_slot(task, ledger, new_owner)
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_historical_owner(old_owner, slot, plan_hash)
        old_owner.pacing_slot_ordinal = slot.plan_total - 1
        session.flush()

        with pytest.raises(ValueError, match="pacing_source_plan_exhausted"):
            attach_owner_history(
                session,
                task,
                [slot],
                owner_model=TaskGroupDailyMessageSlot,
                config={},
                seed_id=f"ai:{task.id}",
            )


def test_released_owner_keeps_frozen_identity_and_recovers_after_now() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, owner, _ = _seed_owners(session)
        base = _source_slot(task, ledger, owner)
        frozen_due = NOW - timedelta(minutes=30)
        slot = replace(base, release_not_before_at=None, frozen_due_at=frozen_due)
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_released_owner(
            owner,
            slot=slot,
            plan_hash=plan_hash,
            frozen_due=frozen_due,
        )
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]
        point = schedule_source_pacing_points(
            [enriched],
            {},
            seed_id=f"ai:{task.id}",
            now_at=NOW,
        )[slot.slot_key]
        freeze_pacing_owner(
            owner,
            plan_hash=plan_hash,
            slot_ordinal=enriched.slot_ordinal,
            plan_total=enriched.plan_total,
            due_at=point.due_at,
            release_not_before_at=point.release_not_before_at,
            source_identity=enriched.owner_identity,
        )

        assert enriched.slot_ordinal == slot.slot_ordinal
        assert point.due_at == frozen_due
        assert point.release_not_before_at > NOW
        assert owner.task_lifecycle_epoch == task.task_lifecycle_epoch


def test_single_slot_released_owner_does_not_count_its_old_release_as_history() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, owner, _ = _seed_owners(session)
        base = _source_slot(task, ledger, owner)
        frozen_due = NOW - timedelta(minutes=30)
        slot = replace(
            base,
            slot_ordinal=0,
            plan_total=1,
            release_not_before_at=NOW - timedelta(minutes=20),
            frozen_due_at=frozen_due,
        )
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_released_owner(
            owner,
            slot=slot,
            plan_hash=plan_hash,
            frozen_due=frozen_due,
        )
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]
        point = schedule_source_pacing_points(
            [enriched],
            {},
            seed_id=f"ai:{task.id}",
            now_at=NOW,
        )[slot.slot_key]

        assert enriched.historical_cursor_at is None
        assert point.release_not_before_at > NOW
        assert point.release_not_before_at < slot.deadline_at


def test_released_owner_replaces_stale_non_null_release_after_now() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task, ledger, owner, _ = _seed_owners(session)
        frozen_due = NOW - timedelta(minutes=40)
        stale_release = NOW - timedelta(minutes=20)
        base = _source_slot(task, ledger, owner)
        slot = replace(
            base,
            release_not_before_at=stale_release,
            frozen_due_at=frozen_due,
        )
        plan_hash = source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}")
        _freeze_released_owner(
            owner,
            slot=slot,
            plan_hash=plan_hash,
            frozen_due=frozen_due,
        )
        owner.release_not_before_at = stale_release
        session.flush()

        enriched = attach_owner_history(
            session,
            task,
            [slot],
            owner_model=TaskGroupDailyMessageSlot,
            config={},
            seed_id=f"ai:{task.id}",
        )[0]
        point = schedule_source_pacing_points(
            [enriched],
            {},
            seed_id=f"ai:{task.id}",
            now_at=NOW,
        )[slot.slot_key]
        _replace_stale_released_owner_release(
            owner,
            point.release_not_before_at,
        )
        freeze_pacing_owner(
            owner,
            plan_hash=plan_hash,
            slot_ordinal=enriched.slot_ordinal,
            plan_total=enriched.plan_total,
            due_at=point.due_at,
            release_not_before_at=point.release_not_before_at,
            source_identity=enriched.owner_identity,
        )

        assert point.release_not_before_at > NOW
        assert owner.release_not_before_at == point.release_not_before_at


def test_released_owner_keeps_future_non_null_release() -> None:
    future_release = NOW + timedelta(minutes=20)
    slot = replace(
        _standalone_released_slot(),
        release_not_before_at=future_release,
        historical_cursor_at=future_release,
    )

    point = schedule_source_pacing_points(
        [slot],
        {},
        seed_id="future-released-owner",
        now_at=NOW,
    )[slot.slot_key]

    assert point.release_not_before_at == future_release


def test_past_historical_cursor_recovers_after_now() -> None:
    slots = [
        SourcePacingSlot(
            "message-1",
            f"batch:{index}",
            index,
            8,
            NOW - timedelta(hours=2),
            NOW + timedelta(hours=2),
            historical_cursor_at=NOW - timedelta(hours=1),
            historical_max_ordinal=index - 1,
        )
        for index in (3, 4)
    ]

    points = schedule_source_pacing_points(
        slots,
        {},
        seed_id="past-cursor",
        now_at=NOW,
    )
    releases = sorted(point.release_not_before_at for point in points.values())

    assert releases[0] > NOW
    assert releases[1] - releases[0] >= timedelta(minutes=30)


def _seed_owners(session: Session):
    session.add(Tenant(id=1, name="tenant"))
    session.add(OperationTarget(
        id=10,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-100123",
        title="group",
    ))
    task = Task(id="task", tenant_id=1, name="task", type="group_ai_chat")
    ledger = TaskDayLedger(
        id="ledger",
        tenant_id=1,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 17),
        period_start_at=NOW - timedelta(hours=2),
        deadline_at=NOW + timedelta(hours=2),
        day_phase="active",
        planning_anchor_at=NOW - timedelta(hours=2),
    )
    owners = [
        TaskGroupDailyMessageSlot(
            id=f"owner-{ordinal}",
            tenant_id=1,
            task_id=task.id,
            task_day_ledger_id=ledger.id,
            target_operation_target_id=10,
            slot_kind="quantity",
            slot_ordinal=ordinal,
        )
        for ordinal in (1, 2)
    ]
    session.add_all([task, ledger, *owners])
    session.flush()
    return task, ledger, owners[0], owners[1]


def _source_slot(task: Task, ledger: TaskDayLedger, owner) -> SourcePacingSlot:
    return SourcePacingSlot(
        source_key=ledger.id,
        slot_key=f"ai:{owner.id}",
        slot_ordinal=1,
        plan_total=4,
        period_start_at=ledger.period_start_at,
        deadline_at=ledger.deadline_at,
        owner_id=owner.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=ledger.id,
        pacing_source_key_hash="a" * 64,
    )


def _standalone_released_slot() -> SourcePacingSlot:
    return SourcePacingSlot(
        source_key="ledger",
        slot_key="ai:released-owner",
        slot_ordinal=1,
        plan_total=4,
        period_start_at=NOW - timedelta(hours=2),
        deadline_at=NOW + timedelta(hours=2),
        frozen_due_at=NOW - timedelta(minutes=40),
        owner_id="released-owner",
        task_lifecycle_epoch=2,
        pacing_period_key="ledger",
        pacing_source_key_hash="a" * 64,
    )


def _freeze_historical_owner(owner, slot: SourcePacingSlot, plan_hash: str) -> None:
    owner.pacing_plan_hash = plan_hash
    owner.pacing_slot_ordinal = 0
    owner.pacing_plan_total = slot.plan_total
    owner.pacing_due_at = NOW - timedelta(minutes=30)
    owner.release_not_before_at = NOW + timedelta(minutes=5)
    owner.task_lifecycle_epoch = slot.task_lifecycle_epoch
    owner.pacing_period_key = slot.pacing_period_key
    owner.pacing_source_key_hash = slot.pacing_source_key_hash


def _freeze_released_owner(
    owner,
    *,
    slot: SourcePacingSlot,
    plan_hash: str,
    frozen_due: datetime,
) -> None:
    owner.pacing_plan_hash = plan_hash
    owner.pacing_slot_ordinal = slot.slot_ordinal
    owner.pacing_plan_total = slot.plan_total
    owner.pacing_due_at = frozen_due
    owner.release_not_before_at = None
    owner.task_lifecycle_epoch = None
    owner.pacing_period_key = slot.pacing_period_key
    owner.pacing_source_key_hash = slot.pacing_source_key_hash
