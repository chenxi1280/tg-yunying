from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, TaskAccountDailyCoverage, TaskDailyCoveragePlanCursor, Tenant
from app.services.task_center import daily_coverage_planning


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def test_existing_daily_cursor_does_not_lock_task_row(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    timestamp = datetime(2026, 7, 26, 20, 0)
    task = Task(id="coverage-locking", tenant_id=1, name="覆盖锁", type="group_ai_chat", status="running")
    session.add_all([
        Tenant(id=1, name="租户"),
        task,
        TaskAccountDailyCoverage(
            id="coverage-locking-row", tenant_id=1, task_id=task.id, group_id=1, account_id=1,
            coverage_date=timestamp.date(), target_count=1, state="ready", targeted_at=timestamp,
        ),
        TaskDailyCoveragePlanCursor(
            tenant_id=1, task_id=task.id, coverage_date=timestamp.date(),
        ),
    ])
    session.commit()

    def unexpected_task_lock(*_args: object) -> None:
        raise AssertionError("existing cursor must not lock the task row")

    monkeypatch.setattr(daily_coverage_planning, "_lock_task", unexpected_task_lock, raising=False)

    batch = daily_coverage_planning.ready_coverage_plan_batch(session, task, now=timestamp)

    assert [row.id for row in batch.rows] == ["coverage-locking-row"]


def test_missing_daily_cursor_is_created_without_task_lock(monkeypatch: pytest.MonkeyPatch, session: Session) -> None:
    timestamp = datetime(2026, 7, 26, 20, 0)
    task = Task(id="coverage-create", tenant_id=1, name="新游标", type="group_ai_chat", status="running")
    session.add_all([
        Tenant(id=1, name="租户"),
        task,
        TaskAccountDailyCoverage(
            id="coverage-create-row", tenant_id=1, task_id=task.id, group_id=1, account_id=1,
            coverage_date=timestamp.date(), target_count=1, state="ready", targeted_at=timestamp,
        ),
    ])
    session.commit()

    def unexpected_task_lock(*_args: object) -> None:
        raise AssertionError("cursor creation must not lock the task row")

    monkeypatch.setattr(daily_coverage_planning, "_lock_task", unexpected_task_lock, raising=False)

    batch = daily_coverage_planning.ready_coverage_plan_batch(session, task, now=timestamp)

    assert [row.id for row in batch.rows] == ["coverage-create-row"]
    assert session.query(TaskDailyCoveragePlanCursor).one().task_id == task.id
