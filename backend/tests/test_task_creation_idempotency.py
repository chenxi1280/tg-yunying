from __future__ import annotations

from datetime import date, datetime, timezone

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AppUser, Task, TaskDayLedger, TaskStartOperation, Tenant
from app.services.task_center.creation_operations import (
    CreateTaskCommand,
    IdempotencyKeyReused,
    StartExecutionResult,
    StartTaskCommand,
    create_task_once,
    start_task_once,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.add(
            AppUser(
                id=7,
                tenant_id=1,
                name="运营",
                role="admin",
                email="operator@example.com",
            )
        )
        current.commit()
        yield current


def _builder(name: str):
    def build() -> Task:
        return Task(
            tenant_id=1,
            name=name,
            type="group_ai_chat",
            status="draft",
        )

    return build


def _create_command(payload: dict) -> CreateTaskCommand:
    return CreateTaskCommand(
        created_by_user_id=7,
        task_type="group_ai_chat",
        client_request_id="create-request-1",
        normalized_request=payload,
        start_requested=True,
    )


def test_same_create_key_and_fingerprint_returns_original_task(session: Session) -> None:
    first = create_task_once(
        session,
        _create_command({"name": " AI 活群 ", "target_ids": [2, 1]}),
        _builder("AI 活群"),
    )
    replay = create_task_once(
        session,
        _create_command({"target_ids": [2, 1], "name": "AI 活群"}),
        _builder("不会创建"),
    )

    assert first.create_status == "created"
    assert replay.create_status == "existing_idempotent"
    assert replay.task.id == first.task.id
    assert session.scalar(select(func.count(Task.id))) == 1


def test_same_create_key_with_different_payload_is_409_contract(session: Session) -> None:
    create_task_once(
        session,
        _create_command({"name": "AI 活群", "daily_message_target": 10}),
        _builder("AI 活群"),
    )

    with pytest.raises(IdempotencyKeyReused) as captured:
        create_task_once(
            session,
            _create_command({"name": "AI 活群", "daily_message_target": 11}),
            _builder("AI 活群"),
        )

    assert captured.value.code == "idempotency_key_reused"
    assert captured.value.conflict_fields == ("daily_message_target",)


def _starter(
    session: Session,
    task: Task,
    *,
    waiting: bool = True,
) -> StartExecutionResult:
    ledger = TaskDayLedger(
        tenant_id=task.tenant_id,
        task_id=task.id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 7, 29),
        period_start_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
        deadline_at=datetime(2026, 7, 29, 16, tzinfo=timezone.utc),
        day_phase="full_day_committed",
        planning_anchor_at=datetime(2026, 7, 28, 16, tzinfo=timezone.utc),
    )
    session.add(ledger)
    session.flush()
    task.status = "running"
    return StartExecutionResult(
        task_day_ledger_id=ledger.id,
        runtime_state="waiting" if waiting else "runnable",
        runtime_blocker_codes=("waiting_transport",) if waiting else (),
    )


def test_started_operation_does_not_repeat_when_runtime_is_waiting(
    session: Session,
) -> None:
    created = create_task_once(
        session,
        _create_command({"name": "AI 活群"}),
        _builder("AI 活群"),
    )
    command = StartTaskCommand(
        start_operation_id="start-request-1",
        requested_by_user_id=7,
        source="create_and_start",
    )

    first = start_task_once(session, created.task.id, command, _starter)
    replay = start_task_once(session, created.task.id, command, _starter)

    assert first.start_status == "started"
    assert first.runtime_state == "waiting"
    assert replay.start_operation_id == first.start_operation_id
    assert replay.start_operation_version == 1
    assert session.scalar(select(func.count(TaskDayLedger.id))) == 1


def test_failed_start_keeps_task_and_same_key_retry_advances_version(
    session: Session,
) -> None:
    created = create_task_once(
        session,
        _create_command({"name": "AI 活群"}),
        _builder("AI 活群"),
    )
    command = StartTaskCommand(
        start_operation_id="start-request-2",
        requested_by_user_id=7,
        source="create_and_start",
    )

    def fail_start(_session: Session, _task: Task) -> StartExecutionResult:
        raise RuntimeError("ledger_write_failed")

    failed = start_task_once(session, created.task.id, command, fail_start)
    assert failed.start_status == "start_failed"
    assert failed.start_failure_code == "ledger_write_failed"
    assert session.get(Task, created.task.id).status == "draft"

    retried = start_task_once(
        session,
        created.task.id,
        command,
        lambda current, task: _starter(current, task, waiting=False),
    )
    operation = session.get(TaskStartOperation, created.task.id)
    assert retried.start_status == "started"
    assert operation.operation_version == 2
    assert operation.status == "started"
