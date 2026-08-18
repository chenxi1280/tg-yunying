from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta
from threading import Barrier, Event

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.database import SessionLocal
from app.models import (
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskPlannerWakeState,
    Tenant,
)
from app.services.task_center.pacing_persistence import freeze_pacing_owner
from app.services.task_center.planner_wake import (
    mark_task_planner_started,
    wake_task_planner,
)
from app.services.task_center.service import _prepare_task_planning_transaction
from app.services.task_center.source_owner_cursor import attach_owner_history
from app.services.task_center.source_pacing import (
    SourcePacingSlot,
    schedule_source_pacing_points,
    source_pacing_plan_hash,
)


NOW = datetime(2026, 8, 17, 12, 0)


def test_concurrent_first_wakes_merge_into_one_revisioned_row() -> None:
    _seed_task("wake-concurrent")
    barrier = Barrier(2)

    def wake(reason: str) -> None:
        with SessionLocal() as session:
            task = session.get(Task, "wake-concurrent")
            barrier.wait()
            wake_task_planner(session, task, reason_code=reason, not_before_at=NOW)
            session.commit()

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(wake, ("listener", "task_update")))

    with SessionLocal() as session:
        rows = list(session.scalars(select(TaskPlannerWakeState).where(
            TaskPlannerWakeState.task_id == "wake-concurrent",
        )))
        assert len(rows) == 1
        assert rows[0].wake_revision == 2


def test_group_ai_planner_reacquires_wake_lock_after_commit_boundary() -> None:
    task_id = "listener-planner-lock-order"
    _seed_task(task_id, task_type="group_ai_chat")
    with SessionLocal() as session:
        task = session.get(Task, task_id)
        wake_task_planner(session, task, reason_code="seed", not_before_at=NOW)
        session.commit()
        task = session.get(Task, task_id)
        mark_task_planner_started(session, task)
        task, _, _, _ = _prepare_task_planning_transaction(session, task)
        assert task is not None
        with SessionLocal() as contender:
            with pytest.raises(OperationalError):
                contender.scalar(
                    select(TaskPlannerWakeState)
                    .where(TaskPlannerWakeState.task_id == task_id)
                    .with_for_update(nowait=True)
                )


def test_source_advisory_lock_makes_second_batch_continue_first_release() -> None:
    _seed_pacing_owners()
    first_locked = Event()
    release_first = Event()
    second_started = Event()

    def plan(owner_id: str, *, ordinal: int, hold: bool) -> datetime:
        with SessionLocal() as session:
            task = session.get(Task, "cursor-concurrent")
            ledger = session.get(TaskDayLedger, "cursor-ledger")
            owner = session.get(TaskGroupDailyMessageSlot, owner_id)
            slot = _source_slot(task, ledger, owner, ordinal=ordinal)
            if not hold:
                second_started.set()
            enriched = attach_owner_history(
                session,
                task,
                [slot],
                owner_model=TaskGroupDailyMessageSlot,
                config={},
                seed_id=f"ai:{task.id}",
            )[0]
            point = schedule_source_pacing_points(
                [enriched], {}, seed_id=f"ai:{task.id}", now_at=NOW,
            )[slot.slot_key]
            freeze_pacing_owner(
                owner,
                plan_hash=source_pacing_plan_hash(slot, {}, seed_id=f"ai:{task.id}"),
                slot_ordinal=ordinal,
                plan_total=20,
                due_at=point.due_at,
                release_not_before_at=point.release_not_before_at,
                source_identity=slot.owner_identity,
            )
            if hold:
                first_locked.set()
                assert release_first.wait(timeout=10)
            session.commit()
            return point.release_not_before_at

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(plan, "cursor-owner-1", ordinal=0, hold=True)
        assert first_locked.wait(timeout=10)
        second = pool.submit(plan, "cursor-owner-2", ordinal=1, hold=False)
        assert second_started.wait(timeout=10)
        release_first.set()
        first_release = first.result(timeout=10)
        second_release = second.result(timeout=10)

    assert second_release > first_release


def _seed_task(task_id: str, *, task_type: str = "channel_view") -> None:
    with SessionLocal() as session:
        if session.get(Tenant, 1) is None:
            session.add(Tenant(id=1, name="tenant"))
            session.flush()
        session.add(Task(
            id=task_id,
            tenant_id=1,
            name=task_id,
            type=task_type,
            status="running",
        ))
        session.commit()


def _seed_pacing_owners() -> None:
    _seed_task("cursor-concurrent")
    with SessionLocal() as session:
        session.add(OperationTarget(
            id=991001,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-100991001",
            title="cursor group",
        ))
        session.flush()
        ledger = TaskDayLedger(
            id="cursor-ledger",
            tenant_id=1,
            task_id="cursor-concurrent",
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 17),
            period_start_at=NOW - timedelta(hours=10),
            deadline_at=NOW + timedelta(hours=14),
            day_phase="active",
            planning_anchor_at=NOW - timedelta(hours=10),
        )
        session.add(ledger)
        session.flush()
        owners = [
            TaskGroupDailyMessageSlot(
                id=f"cursor-owner-{index}",
                tenant_id=1,
                task_id="cursor-concurrent",
                task_day_ledger_id=ledger.id,
                target_operation_target_id=991001,
                slot_kind="quantity",
                slot_ordinal=index,
            )
            for index in (1, 2)
        ]
        session.add_all(owners)
        session.commit()


def _source_slot(
    task: Task,
    ledger: TaskDayLedger,
    owner,
    *,
    ordinal: int,
) -> SourcePacingSlot:
    return SourcePacingSlot(
        source_key=ledger.id,
        slot_key=f"ai:{owner.id}",
        slot_ordinal=ordinal,
        plan_total=20,
        period_start_at=ledger.period_start_at,
        deadline_at=ledger.deadline_at,
        owner_id=owner.id,
        task_lifecycle_epoch=int(task.task_lifecycle_epoch or 1),
        pacing_period_key=ledger.id,
        pacing_source_key_hash="c" * 64,
    )
