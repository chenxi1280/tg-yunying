from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from threading import Event
from types import SimpleNamespace

from sqlalchemy import delete

from app.database import Base, SessionLocal, engine
from app.models import (
    ContentMixCycle,
    ContentMixCycleSlot,
    OperationTarget,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    TaskGroupDailyTarget,
    Tenant,
    TgGroup,
)
from app.services.task_center.executors import group_ai_chat


TENANT_ID = 980
GROUP_ID = 980
TARGET_ID = 980
TASK_ID = "pg-ai-quantity-slot-alignment"
LEDGER_ID = "pg-ai-quantity-ledger"
DAILY_TARGET_ID = "pg-ai-quantity-daily-target"
QUANTITY_SLOT_ID = "pg-ai-quantity-slot"


def test_postgres_target_lock_reloads_bound_quantity_slot() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    _seed()
    first_locked = Event()
    release_first = Event()
    second_started = Event()
    second_locked = Event()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(_bind_quantity_slot, first_locked, release_first)
            assert first_locked.wait(timeout=5)
            second = pool.submit(_read_after_target_lock, second_started, second_locked)
            assert second_started.wait(timeout=5)
            assert not second_locked.wait(timeout=0.3)
            release_first.set()
            first.result(timeout=5)
            result = second.result(timeout=5)
        assert second_locked.is_set()
        assert result.code == "extra_volume_slot_unavailable"
        assert result.aligned_count == 0
        assert result.missing_extra_count == 1
    finally:
        release_first.set()
        _cleanup()


def _bind_quantity_slot(locked: Event, release: Event) -> None:
    with SessionLocal() as session:
        task = session.get(Task, TASK_ID)
        group_ai_chat._locked_content_mix_daily_target(session, DAILY_TARGET_ID)
        result = group_ai_chat._quantity_slot_alignment_for_content_mix(
            session,
            task,
            _blueprint(),
            LEDGER_ID,
        )
        assert result.code == "aligned"
        cycle = ContentMixCycle(
            id="pg-ai-quantity-cycle",
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            target_operation_target_id=TARGET_ID,
            task_day_ledger_id=LEDGER_ID,
            cycle_seq=1,
            config_revision=1,
            scope_total_slots=1,
            allocation_seed="pg-ai-quantity-seed",
            allocation_closed_at=datetime.now(timezone.utc),
        )
        session.add(cycle)
        session.flush()
        session.add(ContentMixCycleSlot(
            id="pg-ai-quantity-cycle-slot",
            tenant_id=TENANT_ID,
            cycle_id=cycle.id,
            slot_index=1,
            primary_quantity_slot_id=QUANTITY_SLOT_ID,
            relation_kind="direct",
        ))
        session.flush()
        locked.set()
        assert release.wait(timeout=5)
        session.commit()


def _read_after_target_lock(started: Event, locked: Event):
    with SessionLocal() as session:
        task = session.get(Task, TASK_ID)
        started.set()
        group_ai_chat._locked_content_mix_daily_target(session, DAILY_TARGET_ID)
        locked.set()
        return group_ai_chat._quantity_slot_alignment_for_content_mix(
            session,
            task,
            _blueprint(),
            LEDGER_ID,
        )


def _blueprint():
    return SimpleNamespace(
        profile=SimpleNamespace(coverage_rows={}),
        generation=SimpleNamespace(quality_items=[{"slot_account_id": 1}]),
    )


def _seed() -> None:
    timestamp = datetime(2026, 8, 2, 16, tzinfo=timezone.utc)
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="PostgreSQL AI 数量槽"))
        session.flush()
        session.add(TgGroup(
            id=GROUP_ID,
            tenant_id=TENANT_ID,
            tg_peer_id="-100980",
            title="数量槽锁测试群",
        ))
        session.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-100980",
            title="数量槽锁测试群",
            auth_status="已授权运营",
            can_send=True,
        ))
        session.flush()
        task = Task(
            id=TASK_ID,
            tenant_id=TENANT_ID,
            name="PostgreSQL AI 数量槽锁",
            type="group_ai_chat",
            status="running",
            type_config={"rule_set_version_id": 1},
        )
        ledger = TaskDayLedger(
            id=LEDGER_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            timezone_snapshot="Asia/Shanghai",
            timezone_revision=1,
            obligation_local_date=date(2026, 8, 3),
            period_start_at=timestamp,
            deadline_at=datetime(2026, 8, 3, 16, tzinfo=timezone.utc),
            day_phase="full_day_committed",
            planning_anchor_at=timestamp,
        )
        session.add(task)
        session.flush()
        session.add(ledger)
        session.flush()
        session.add(TaskGroupDailyTarget(
            id=DAILY_TARGET_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            task_day_ledger_id=LEDGER_ID,
            group_id=GROUP_ID,
            target_date=date(2026, 8, 3),
            configured_message_target=1,
            frozen_account_count=0,
            effective_message_target=1,
            daily_fulfillment_phase="full_day_committed",
            scope_frozen_at=timestamp,
            full_day_committed_at=timestamp,
        ))
        session.add(TaskGroupDailyMessageSlot(
            id=QUANTITY_SLOT_ID,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            task_day_ledger_id=LEDGER_ID,
            target_operation_target_id=TARGET_ID,
            slot_kind="extra_volume",
            slot_ordinal=1,
        ))
        session.commit()


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(Task).where(Task.id == TASK_ID))
        session.execute(delete(OperationTarget).where(OperationTarget.id == TARGET_ID))
        session.execute(delete(TgGroup).where(TgGroup.id == GROUP_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
