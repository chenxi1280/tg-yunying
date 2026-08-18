from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql
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
    SourceAdmissionSpec,
    admit_source_paced_attempt,
    align_source_gateway_call_started,
    settle_source_pacing_admission,
)
from app.services.task_center.source_pacing_reservation import lock_or_create_admission


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


def test_gateway_marker_advances_next_source_gate_without_subsecond_leak(
    session: Session,
) -> None:
    first, first_attempt = _paced_action(
        session,
        task_id="marker-task-a",
        slot_id="marker-slot-a",
        action_id="marker-action-a",
    )
    assert admit_source_paced_attempt(session, first, first_attempt, now_value=NOW)
    marker_at = NOW + timedelta(milliseconds=250)
    first_attempt.gateway_call_started_at = marker_at
    align_source_gateway_call_started(session, first_attempt)
    session.commit()

    second, second_attempt = _paced_action(
        session,
        task_id="marker-task-b",
        slot_id="marker-slot-b",
        action_id="marker-action-b",
    )
    allowed = admit_source_paced_attempt(
        session,
        second,
        second_attempt,
        now_value=NOW + timedelta(seconds=864),
    )

    assert allowed is False
    assert second.scheduled_at == marker_at + timedelta(seconds=864)
    state = session.scalar(select(SourcePacingState))
    assert state.last_call_started_at == marker_at
    assert state.next_call_not_before_at == marker_at + timedelta(seconds=1728)


def test_existing_reservation_keeps_its_slot_after_later_reservations(
    session: Session,
) -> None:
    first, first_attempt = _paced_action(
        session,
        task_id="reserved-task-a",
        slot_id="reserved-slot-a",
        action_id="reserved-action-a",
    )
    first_due = NOW + timedelta(seconds=864)
    first.release_not_before_at = first_due
    first.effective_claim_at = first_due
    session.get(TaskGroupDailyMessageSlot, "reserved-slot-a").release_not_before_at = first_due
    assert not admit_source_paced_attempt(session, first, first_attempt, now_value=NOW)

    second, second_attempt = _paced_action(
        session,
        task_id="reserved-task-b",
        slot_id="reserved-slot-b",
        action_id="reserved-action-b",
    )
    assert not admit_source_paced_attempt(session, second, second_attempt, now_value=NOW)
    first_admission = session.scalar(select(SourcePacingAdmission).where(
        SourcePacingAdmission.action_id == first.id,
    ))
    assert first_admission.call_not_before_at == first_due

    retry = ExecutionAttempt(
        tenant_id=1,
        action_id=first.id,
        account_id=1,
        attempt_no=2,
        status="before_call",
    )
    session.add(retry)
    session.flush()
    assert admit_source_paced_attempt(session, first, retry, now_value=first_due)
    assert first_admission.call_not_before_at == first_due


def test_postgres_conflict_detection_uses_returning_not_rowcount() -> None:
    action = _paced_action_record(
        task_id="conflict-task",
        slot_id="conflict-slot",
        action_id="conflict-action",
    )
    attempt = ExecutionAttempt(id="conflict-attempt", action_id=action.id)
    state = SourcePacingState(id="conflict-state", tenant_id=1)
    admission = SourcePacingAdmission(
        id="existing-admission",
        admission_key="existing-key",
        tenant_id=1,
        task_id=action.task_id,
        source_pacing_state_id=state.id,
        owner_type="task_group_daily_message_slots",
        owner_id="conflict-slot",
        pacing_period_key="period",
        pacing_plan_hash="a" * 64,
        planned_release_at=NOW,
        call_not_before_at=NOW,
        source_gap_seconds=864,
        state="reserved",
    )
    session = _ConflictSession(admission)
    spec = SourceAdmissionSpec(
        pacing_domain="ai_send",
        source_key_hash="b" * 64,
        owner_type="task_group_daily_message_slots",
        owner_id="conflict-slot",
        lifecycle_epoch=1,
        period_key="period",
        plan_hash="a" * 64,
        release_at=NOW,
        source_gap_seconds=864,
    )

    returned, created = lock_or_create_admission(
        session,
        action,
        attempt=attempt,
        state=state,
        spec=spec,
    )

    assert returned is admission
    assert created is False
    assert session.saw_owner_lock
    assert session.saw_returning


class _ConflictSession:
    def __init__(self, admission: SourcePacingAdmission) -> None:
        self.admission = admission
        self.saw_owner_lock = False
        self.saw_returning = False

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def scalar(self, statement):
        sql = str(statement.compile(dialect=postgresql.dialect()))
        if "count(" in sql.lower():
            return 0
        if "join actions as" in sql.lower():
            self.saw_owner_lock = "FOR UPDATE OF source_pacing_admissions" in sql
            return None
        if "insert into source_pacing_admissions" in sql.lower():
            self.saw_returning = "RETURNING source_pacing_admissions.id" in sql
            return None
        return self.admission


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
