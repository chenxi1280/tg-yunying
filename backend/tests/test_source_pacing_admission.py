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
    SourcePacingState,
    Task,
    TaskDayLedger,
    TaskGroupDailyMessageSlot,
    Tenant,
    TgAccount,
)
from app.services.task_center.pacing import PACING_CONTRACT_VERSION
from app.services.task_center.source_pacing_admission import (
    admit_source_paced_attempt,
    settle_source_pacing_admission,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 10, 0)


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="tenant"))
        current.add(TgAccount(
            id=1,
            tenant_id=1,
            display_name="account",
            phone_masked="***0001",
            status="在线",
        ))
        current.add(OperationTarget(
            id=10,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-1009001",
            title="group",
        ))
        current.commit()
        yield current


def _paced_action(
    session: Session,
    *,
    task_id: str,
    slot_id: str,
    action_id: str,
) -> tuple[Action, ExecutionAttempt]:
    task, ledger, slot = _paced_owner_entities(task_id=task_id, slot_id=slot_id)
    action = _paced_action_record(
        task_id=task_id,
        slot_id=slot_id,
        action_id=action_id,
    )
    session.add_all([task, ledger, slot, action])
    session.flush()
    attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="before_call",
        before_call_at=NOW,
    )
    session.add(attempt)
    session.flush()
    return action, attempt


def _paced_owner_entities(*, task_id: str, slot_id: str):
    task = Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="group_ai_chat",
        status="running",
    )
    ledger = TaskDayLedger(
        id=f"ledger-{task_id}",
        tenant_id=1,
        task_id=task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 17),
        period_start_at=NOW,
        deadline_at=NOW + timedelta(days=1),
        day_phase="active",
        planning_anchor_at=NOW,
    )
    slot = TaskGroupDailyMessageSlot(
        id=slot_id,
        tenant_id=1,
        task_id=task_id,
        task_day_ledger_id=ledger.id,
        target_operation_target_id=10,
        slot_kind="quantity",
        slot_ordinal=1,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_ordinal=1,
        pacing_plan_total=100,
        pacing_due_at=NOW,
        release_not_before_at=NOW,
    )
    return task, ledger, slot


def _paced_action_record(*, task_id: str, slot_id: str, action_id: str) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task_id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=1,
        status="claiming",
        primary_quantity_slot_id=slot_id,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="a" * 64,
        pacing_slot_key=f"slot:{slot_id}",
        pacing_due_at=NOW,
        release_not_before_at=NOW,
        effective_claim_at=NOW,
        payload={
            "group_id": 10,
            "message_text": "test",
            "target_reference_snapshot": {"tg_peer_id": "-1009001"},
        },
    )


def test_same_source_across_tasks_uses_one_cursor_and_defers_without_sleep(
    session: Session,
) -> None:
    first, first_attempt = _paced_action(
        session,
        task_id="task-a",
        slot_id="slot-a",
        action_id="action-a",
    )
    assert admit_source_paced_attempt(session, first, first_attempt, now_value=NOW)
    first_attempt.gateway_call_started_at = NOW
    session.commit()

    second, second_attempt = _paced_action(
        session,
        task_id="task-b",
        slot_id="slot-b",
        action_id="action-b",
    )
    allowed = admit_source_paced_attempt(
        session,
        second,
        second_attempt,
        now_value=NOW + timedelta(seconds=1),
    )

    assert allowed is False
    assert second.status == "pending"
    assert second_attempt.status == "skipped_before_gateway"
    assert second.scheduled_at == NOW + timedelta(seconds=864)
    second_owner = session.get(TaskGroupDailyMessageSlot, "slot-b")
    assert second_owner.release_not_before_at == NOW + timedelta(seconds=864)
    third, third_attempt = _paced_action(
        session,
        task_id="task-c",
        slot_id="slot-c",
        action_id="action-c",
    )
    assert admit_source_paced_attempt(
        session,
        third,
        third_attempt,
        now_value=NOW + timedelta(seconds=2),
    ) is False
    assert third.scheduled_at == NOW + timedelta(seconds=1728)
    assert session.scalar(select(func.count(SourcePacingState.id))) == 1
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 3


def test_missing_stable_owner_fails_closed_before_gateway(session: Session) -> None:
    task = Task(
        id="missing-owner-task",
        tenant_id=1,
        name="missing owner",
        type="group_ai_chat",
        status="running",
    )
    action = Action(
        id="missing-owner-action",
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=1,
        status="claiming",
        primary_quantity_slot_id=None,
        pacing_contract_version=PACING_CONTRACT_VERSION,
        pacing_plan_hash="b" * 64,
        pacing_slot_key="missing",
        release_not_before_at=NOW,
        payload={
            "group_id": 10,
            "message_text": "test",
            "target_reference_snapshot": {"tg_peer_id": "-1009001"},
        },
    )
    session.add_all([task, action])
    session.flush()
    attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=1,
        status="before_call",
    )
    session.add(attempt)
    session.flush()

    assert admit_source_paced_attempt(session, action, attempt, now_value=NOW) is False
    assert action.status == "pending"
    assert action.result["error_code"] == "pacing_source_owner_missing"
    assert attempt.gateway_call_started_at is None


def test_known_remote_failure_uses_a_new_admission_on_explicit_retry(
    session: Session,
) -> None:
    action, first_attempt = _paced_action(
        session,
        task_id="retry-task",
        slot_id="retry-slot",
        action_id="retry-action",
    )
    assert admit_source_paced_attempt(session, action, first_attempt, now_value=NOW)
    first_attempt.gateway_call_started_at = NOW
    action.status = "retryable_failed"
    settle_source_pacing_admission(action, first_attempt)
    second_attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=2,
        status="before_call",
        before_call_at=NOW + timedelta(seconds=864),
    )
    session.add(second_attempt)
    session.flush()

    assert admit_source_paced_attempt(
        session,
        action,
        second_attempt,
        now_value=NOW + timedelta(seconds=864),
    )
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 2


def test_gateway_started_without_settlement_is_not_admitted_again(
    session: Session,
) -> None:
    action, first_attempt = _paced_action(
        session,
        task_id="unknown-task",
        slot_id="unknown-slot",
        action_id="unknown-action",
    )
    assert admit_source_paced_attempt(session, action, first_attempt, now_value=NOW)
    first_attempt.gateway_call_started_at = NOW
    second_attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=2,
        status="before_call",
    )
    session.add(second_attempt)
    session.flush()

    allowed = admit_source_paced_attempt(
        session,
        action,
        second_attempt,
        now_value=NOW + timedelta(seconds=864),
    )

    assert allowed is False
    assert action.status == "unknown_after_send"
    assert second_attempt.gateway_call_started_at is None
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 1


def test_finished_pre_gateway_admission_can_retry_same_reservation(
    session: Session,
) -> None:
    action, first_attempt = _paced_action(
        session,
        task_id="pre-gateway-task",
        slot_id="pre-gateway-slot",
        action_id="pre-gateway-action",
    )
    assert admit_source_paced_attempt(session, action, first_attempt, now_value=NOW)
    action.status = "retryable_failed"
    settle_source_pacing_admission(action, first_attempt)
    second_attempt = ExecutionAttempt(
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=2,
        status="before_call",
    )
    session.add(second_attempt)
    session.flush()

    assert admit_source_paced_attempt(
        session,
        action,
        second_attempt,
        now_value=NOW + timedelta(seconds=864),
    )
    assert session.scalar(select(func.count(SourcePacingAdmission.id))) == 1
