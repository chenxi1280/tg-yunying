from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    ContentMixCycle,
    ContentMixCycleSlot,
    FulfillmentObligationProjection,
    Task,
    TaskAccountDailyCoverage,
    TaskGroupDailyMessageSlot,
    Tenant,
)
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def test_bound_replan_excludes_terminal_shortfall_coverage() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        task = _seed_replan_rows(session)

        statement = group_ai_chat._base_replan_coverage_statement(
            task,
            "ledger-1",
            fact_first=False,
        )

        assert [row.id for row in session.scalars(statement)] == ["open-coverage"]


def _seed_replan_rows(session: Session) -> Task:
    task = Task(
        id="terminal-replan-task", tenant_id=1, name="终态重排防重入",
        type="group_ai_chat", status="running",
    )
    session.add_all([Tenant(id=1, name="租户"), task])
    _add_bound_coverage(
        session, task, coverage_id="terminal-coverage", account_id=1,
    )
    _add_bound_coverage(
        session, task, coverage_id="open-coverage", account_id=2,
    )
    session.add(FulfillmentObligationProjection(
        id="terminal-replan-projection", tenant_id=1, task_id=task.id,
        obligation_type="coverage", obligation_id="terminal-coverage",
        work_lane="ai_generation", state="terminal_shortfall",
    ))
    session.commit()
    return task


def _add_bound_coverage(
    session: Session,
    task: Task,
    *,
    coverage_id: str,
    account_id: int,
) -> None:
    quantity_id = f"quantity-{account_id}"
    cycle_id = f"cycle-{account_id}"
    session.add_all([
        TaskAccountDailyCoverage(
            id=coverage_id, tenant_id=1, task_id=task.id, group_id=1,
            account_id=account_id, coverage_date=datetime(2026, 8, 30).date(),
            target_count=1, state="ready", targeted_at=datetime(2026, 8, 30),
        ),
        TaskGroupDailyMessageSlot(
            id=quantity_id, tenant_id=1, task_id=task.id,
            task_day_ledger_id="ledger-1", target_operation_target_id=1,
            task_account_daily_coverage_id=coverage_id, slot_kind="coverage",
            slot_ordinal=account_id, state="open",
        ),
        ContentMixCycle(
            id=cycle_id, tenant_id=1, task_id=task.id,
            target_operation_target_id=1, task_day_ledger_id="ledger-1",
            cycle_seq=account_id, config_revision=1, scope_total_slots=1,
            allocation_seed="seed", allocation_closed_at=datetime(2026, 8, 30),
        ),
        ContentMixCycleSlot(
            id=f"slot-{account_id}", tenant_id=1, cycle_id=cycle_id,
            slot_index=0, primary_quantity_slot_id=quantity_id,
            relation_kind="direct", slot_state="replan_required",
        ),
    ])
