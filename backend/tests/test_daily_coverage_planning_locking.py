from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    FulfillmentObligationProjection,
    Task,
    TaskAccountDailyCoverage,
    TaskDailyCoveragePlanCursor,
    Tenant,
)
from app.services.task_center import daily_coverage_planning
from app.services.task_center.daily_fulfillment import summarize_daily_fulfillment


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


def test_terminal_shortfall_coverage_is_not_selected_again(session: Session) -> None:
    timestamp = datetime(2026, 8, 30, 12, 0)
    task = Task(id="terminal-plan", tenant_id=1, name="终态防重入", type="group_ai_chat", status="running")
    session.add_all([
        Tenant(id=1, name="租户"),
        task,
        _coverage("terminal-coverage", task.id, 1, timestamp=timestamp),
        _coverage("open-coverage", task.id, 2, timestamp=timestamp),
        FulfillmentObligationProjection(
            id="terminal-projection", tenant_id=1, task_id=task.id,
            obligation_type="coverage", obligation_id="terminal-coverage",
            work_lane="ai_generation", state="terminal_shortfall",
        ),
    ])
    session.commit()

    batch = daily_coverage_planning.ready_coverage_plan_batch(session, task, now=timestamp)

    assert [row.id for row in batch.rows] == ["open-coverage"]


def test_terminal_shortfall_coverage_is_reported_as_blocked(session: Session) -> None:
    timestamp = datetime(2026, 8, 30, 12, 0)
    task = Task(id="terminal-summary", tenant_id=1, name="终态读模型", type="group_ai_chat", status="running")
    session.add_all([
        Tenant(id=1, name="租户"),
        task,
        _coverage("terminal-summary-row", task.id, 1, timestamp=timestamp),
        _coverage("ready-summary-row", task.id, 2, timestamp=timestamp),
        FulfillmentObligationProjection(
            id="terminal-summary-projection", tenant_id=1, task_id=task.id,
            obligation_type="coverage", obligation_id="terminal-summary-row",
            work_lane="ai_generation", state="terminal_shortfall",
        ),
    ])
    session.commit()

    summary = summarize_daily_fulfillment(session, task, now=timestamp)

    assert summary.ready_to_plan_count == 1
    assert summary.sendable_capacity_count == 1
    assert summary.blocked_shortfall_count == 1
    assert summary.blocker_counts["terminal_shortfall"] == 1


def _coverage(
    coverage_id: str,
    task_id: str,
    account_id: int,
    *,
    timestamp: datetime,
) -> TaskAccountDailyCoverage:
    return TaskAccountDailyCoverage(
        id=coverage_id, tenant_id=1, task_id=task_id, group_id=1,
        account_id=account_id, coverage_date=timestamp.date(), target_count=1,
        state="ready", targeted_at=timestamp,
    )
