from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import OperationTarget, Task, TaskDayLedger, Tenant, TgGroup
from app.schemas import GroupAIChatTaskConfigUpdate, GroupAIChatTaskCreate
from app.services.task_center import service
from app.services.task_center.task_creation_contract import (
    execute_task_creation_contract,
)
from app.services.task_center.hard_hourly import enabled as hard_hourly_enabled


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="唯一业务用户"))
        current.add(
            TgGroup(
                id=7,
                tenant_id=1,
                tg_peer_id="-1007",
                title="目标群",
                auth_status="已授权运营",
                can_send=True,
            )
        )
        current.add(
            OperationTarget(
                id=17,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="目标群",
                auth_status="已授权运营",
                can_send=True,
            )
        )
        current.commit()
        yield current


def _payload(name: str) -> GroupAIChatTaskCreate:
    return GroupAIChatTaskCreate(
        name=name,
        target_group_id=7,
        daily_message_target=10,
    )


def test_create_succeeds_without_runtime_capacity_precheck(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_assert_precheck_allows_start",
        lambda *_args, **_kwargs: pytest.fail("创建阶段不得运行容量预检"),
    )

    task = service.create_group_ai_chat_task(
        session,
        1,
        _payload("直接创建"),
        "运营",
    )

    assert task.status == "draft"
    assert session.get(Task, task.id) is not None


def test_create_and_start_commits_task_then_reports_runtime_waiting(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        service,
        "_assert_precheck_allows_start",
        lambda *_args, **_kwargs: pytest.fail("启动不得被容量预检阻断"),
    )

    task = service.create_and_start_group_ai_chat_task(
        session,
        1,
        _payload("创建并启动"),
        "运营",
    )

    assert task.status == "running"
    assert task.stats["runtime_state"] == "waiting"
    assert "no_frozen_accounts" in task.stats["runtime_blocker_codes"]
    assert session.query(TaskDayLedger).filter_by(task_id=task.id).count() == 1


def test_explicit_start_does_not_call_capacity_precheck(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = Task(
        id="draft-ai",
        tenant_id=1,
        name="待启动",
        type="group_ai_chat",
        status="draft",
        type_config={
            "target_group_id": 7,
            "target_operation_target_id": 17,
            "daily_message_target": 10,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 99,
        },
        scheduled_start=datetime(2026, 7, 29, 0, 0),
    )
    session.add(task)
    session.commit()
    monkeypatch.setattr(service, "_now", lambda: datetime(2026, 7, 29, 1, 0))
    monkeypatch.setattr(
        service,
        "_assert_precheck_allows_start",
        lambda *_args, **_kwargs: pytest.fail("启动不得运行旧门禁预检"),
    )

    started = service.start_task(session, 1, task.id, "运营")

    assert started.status == "running"
    assert started.stats["runtime_state"] == "waiting"
    assert "hard_hourly" not in started.stats
    assert hard_hourly_enabled(started) is False


def test_hard_hourly_fields_are_accepted_but_removed_from_update_contract() -> None:
    payload = GroupAIChatTaskConfigUpdate(
        target_group_id=7,
        hard_hourly_target_enabled=True,
        hourly_min_messages=20,
        hard_hourly_strategy="force_planning",
    )

    data = payload.model_dump(mode="json")
    assert "hard_hourly_target_enabled" not in data
    assert "hourly_min_messages" not in data
    assert "hard_hourly_strategy" not in data


def test_create_and_start_contract_replays_same_task_and_start(
    session: Session,
) -> None:
    payload = _payload("幂等创建").model_copy(
        update={"client_request_id": "request-stable-001"}
    )

    first = execute_task_creation_contract(
        session,
        tenant_id=1,
        user_id=7,
        actor="运营",
        task_type="group_ai_chat",
        payload=payload,
        start_requested=True,
    )
    replay = execute_task_creation_contract(
        session,
        tenant_id=1,
        user_id=7,
        actor="运营",
        task_type="group_ai_chat",
        payload=payload,
        start_requested=True,
    )

    assert first.task.id == replay.task.id
    assert first.create.create_status == "created"
    assert replay.create.create_status == "existing_idempotent"
    assert replay.start is not None
    assert replay.start.start_status == "started"
    assert session.query(TaskDayLedger).filter_by(task_id=first.task.id).count() == 1
