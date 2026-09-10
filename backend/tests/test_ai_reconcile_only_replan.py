from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    FulfillmentObligationProjection,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
)
from app.services.task_center.daily_coverage_planning import (
    ready_coverage_plan_batch,
)

NOW = datetime(2026, 9, 10, 14, 0)


def test_ready_plan_excludes_non_open_obligation_projections() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = _seed_task(session)
        _add_coverage(session, task, "no-projection", account_id=1)
        _add_coverage(session, task, "open-projection", account_id=2)
        _add_coverage(session, task, "reconcile-only", account_id=3)
        _add_coverage(session, task, "unknown-shortfall", account_id=4)
        _add_coverage(session, task, "terminal-shortfall", account_id=5)
        _add_projection(session, task, "open-projection", state="open")
        _add_projection(session, task, "reconcile-only", state="remote_reconcile_only")
        _add_projection(
            session, task, "unknown-shortfall", state="closed_with_unknown_shortfall"
        )
        _add_projection(session, task, "terminal-shortfall", state="terminal_shortfall")
        session.commit()

        batch = ready_coverage_plan_batch(session, task, now=NOW, limit=20)

        assert [row.id for row in batch.rows] == ["no-projection", "open-projection"]


def _seed_task(session: Session) -> Task:
    task = Task(
        id="reconcile-only-task", tenant_id=1, name="非open投影排除",
        type="group_ai_chat", status="running",
        fulfillment_contract_version="fact_first_v3",
    )
    session.add_all([Tenant(id=1, name="租户"), task])
    return task


def _add_coverage(
    session: Session,
    task: Task,
    coverage_id: str,
    *,
    account_id: int,
) -> None:
    session.add(TaskAccountDailyCoverage(
        id=coverage_id, tenant_id=1, task_id=task.id, group_id=1,
        account_id=account_id, coverage_date=NOW.date(),
        target_count=1, state="ready", targeted_at=NOW - timedelta(minutes=5),
    ))


def _add_projection(session: Session, task: Task, coverage_id: str, *, state: str) -> None:
    session.add(FulfillmentObligationProjection(
        id=f"projection-{coverage_id}", tenant_id=1, task_id=task.id,
        obligation_type="coverage", obligation_id=coverage_id,
        work_lane="ai_generation", state=state, opened_at=NOW - timedelta(hours=1),
    ))


pytestmark = pytest.mark.no_postgres
