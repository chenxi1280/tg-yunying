from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    SourcePacingAdmission,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
)
from app.services.task_center.pacing import PACING_CONTRACT_VERSION
from app.services.task_center.source_pacing_admission import admit_source_paced_attempt


NOW = datetime(2026, 8, 18, 10, 0)
SOURCE_GAP_SECONDS = 864
TENANT_ID = 990_001
ACCOUNT_ID = 990_001
TARGET_ID = 990_010


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=TENANT_ID, name="tenant"))
        current.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.commit()
        yield current


@pytest.mark.no_postgres
def test_replacement_reuses_stable_owner_pre_gateway_reservation(
    session: Session,
) -> None:
    due_at = NOW + timedelta(seconds=SOURCE_GAP_SECONDS)
    task, ledger, slot = _owner_entities("owner-task", "owner-slot", due_at)
    original = _action("original-action", task.id, slot.id, due_at)
    session.add_all([task, ledger, slot, original])
    session.flush()
    original_attempt = _attempt(original, 1, NOW)
    session.add(original_attempt)
    session.flush()
    assert not admit_source_paced_attempt(session, original, original_attempt, now_value=NOW)
    admission = session.scalar(select(SourcePacingAdmission))
    original.status = "failed"

    replacement = _action("replacement-action", task.id, slot.id, due_at)
    session.add(replacement)
    session.flush()
    replacement_attempt = _attempt(replacement, 1, due_at)
    session.add(replacement_attempt)
    session.flush()

    assert admit_source_paced_attempt(
        session,
        replacement,
        replacement_attempt,
        now_value=due_at,
    )
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 1
    assert admission.action_id == replacement.id
    assert admission.attempt_id == replacement_attempt.id


@pytest.mark.no_postgres
def test_overdue_reservations_still_apply_actual_source_gap(
    session: Session,
) -> None:
    first = _add_paced_action(session, "gap-task-a", "gap-slot-a", "gap-action-a")
    second = _add_paced_action(session, "gap-task-b", "gap-slot-b", "gap-action-b")
    before_due = NOW - timedelta(seconds=1)
    assert not admit_source_paced_attempt(session, first[0], first[1], now_value=before_due)
    assert not admit_source_paced_attempt(session, second[0], second[1], now_value=before_due)
    overdue = NOW + timedelta(seconds=SOURCE_GAP_SECONDS * 2)

    first_retry = _attempt(first[0], 2, overdue)
    session.add(first_retry)
    session.flush()
    assert admit_source_paced_attempt(session, first[0], first_retry, now_value=overdue)

    second_retry = _attempt(second[0], 2, overdue)
    session.add(second_retry)
    session.flush()
    assert not admit_source_paced_attempt(session, second[0], second_retry, now_value=overdue)
    expected = overdue + timedelta(seconds=SOURCE_GAP_SECONDS)
    assert second[0].scheduled_at == expected
    second_admission = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == second[0].id,
    ))
    assert second_admission.call_not_before_at == expected


def test_postgres_owner_reuse_executes_with_targeted_row_lock() -> None:
    from app.database import SessionLocal

    with SessionLocal() as current:
        current.add(Tenant(id=TENANT_ID, name="owner-reuse-postgres"))
        current.flush()
        current.add(TgAccount(
            id=ACCOUNT_ID,
            tenant_id=TENANT_ID,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=TARGET_ID,
            tenant_id=TENANT_ID,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.flush()
        due_at = NOW + timedelta(seconds=SOURCE_GAP_SECONDS)
        task, ledger, slot = _owner_entities("pg-owner-task", "pg-owner-slot", due_at)
        original = _action("pg-original-action", task.id, slot.id, due_at)
        current.add(task)
        current.flush()
        current.add(ledger)
        current.flush()
        current.add(slot)
        current.flush()
        current.add(original)
        current.flush()
        original_attempt = _attempt(original, 1, NOW)
        current.add(original_attempt)
        current.flush()
        assert not admit_source_paced_attempt(current, original, original_attempt, now_value=NOW)
        original.status = "failed"
        replacement = _action("pg-replacement-action", task.id, slot.id, due_at)
        current.add(replacement)
        current.flush()
        replacement_attempt = _attempt(replacement, 1, due_at)
        current.add(replacement_attempt)
        current.flush()
        assert admit_source_paced_attempt(
            current,
            replacement,
            replacement_attempt,
            now_value=due_at,
        )
        assert current.scalar(select(func.count(SourcePacingAdmission.id))) == 1


def _add_paced_action(
    session: Session,
    task_id: str,
    slot_id: str,
    action_id: str,
) -> tuple[Action, ExecutionAttempt]:
    task, ledger, slot = _owner_entities(task_id, slot_id, NOW)
    action = _action(action_id, task_id, slot_id, NOW)
    session.add_all([task, ledger, slot, action])
    session.flush()
    attempt = _attempt(action, 1, NOW - timedelta(seconds=1))
    session.add(attempt)
    session.flush()
    return action, attempt


def _owner_entities(
    task_id: str,
    slot_id: str,
    due_at: datetime,
) -> tuple[Task, TaskDayLedger, TaskGroupDailyMessageSlot]:
    task = Task(
        id=task_id,
        tenant_id=TENANT_ID,
        name=task_id,
        type="group_ai_chat",
        status="running",
    )
    ledger = TaskDayLedger(
        id=f"ledger-{task_id}",
        tenant_id=TENANT_ID,
        task_id=task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 18),
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=NOW,
    )
    slot = TaskGroupDailyMessageSlot(
        id=slot_id,
        tenant_id=TENANT_ID,
        task_id=task_id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=TARGET_ID,
        slot_kind="quantity",
        slot_ordinal=1,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=1,
        pacing_plan_total=100,
        pacing_due_at=due_at,
        release_not_before_at=due_at,
    )
    return task, ledger, slot


def _action(action_id: str, task_id: str, slot_id: str, due_at: datetime) -> Action:
    return Action(
        id=action_id,
        tenant_id=TENANT_ID,
        task_id=task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=ACCOUNT_ID,
        status="claiming",
        primary_quantity_slot_id=slot_id,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key=f"slot:{slot_id}",
        pacing_due_at=due_at,
        release_not_before_at=due_at,
        effective_claim_at=due_at,
        payload={
            "group_id": TARGET_ID,
            "message_text": "test",
            "target_reference_snapshot": {"tg_peer_id": "-1009001"},
        },
    )


def _attempt(action: Action, attempt_no: int, timestamp: datetime) -> ExecutionAttempt:
    return ExecutionAttempt(
        tenant_id=TENANT_ID,
        action_id=action.id,
        account_id=ACCOUNT_ID,
        attempt_no=attempt_no,
        status="before_call",
        before_call_at=timestamp,
    )
