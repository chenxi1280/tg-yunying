from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    OperationTarget,
    Task,
    TaskAccountDailyCoverage,
    TaskMembershipAdmissionItem,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services.task_center.executors.group_ai_extra_candidates import (
    DAILY_GROUP_EXTRA_CANDIDATE_LIMIT,
    DailyGroupExtraCandidateSpec,
    daily_group_extra_candidate_ids,
)


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _seed_candidates(session: Session, count: int) -> DailyGroupExtraCandidateSpec:
    task = Task(id="task", tenant_id=1, name="task", type="group_ai_chat", status="running")
    group = TgGroup(id=1, tenant_id=1, tg_peer_id="-1001", title="group")
    target = OperationTarget(
        id=1, tenant_id=1, target_type="group", tg_peer_id="-1001", title="group",
    )
    ledger_id = "ledger"
    coverage_date = datetime(2026, 8, 18).date()
    session.add_all([Tenant(id=1, name="tenant"), task, group, target])
    session.flush()
    for account_id in range(1, count + 1):
        _add_candidate(
            session,
            task=task,
            target=target,
            ledger_id=ledger_id,
            coverage_date=coverage_date,
            group=group,
            account_id=account_id,
        )
    session.flush()
    return DailyGroupExtraCandidateSpec(
        tenant_id=1,
        task_id=task.id,
        group_id=group.id,
        task_day_ledger_id=ledger_id,
        coverage_date=coverage_date,
    )


def _add_candidate(
    session,
    *,
    task,
    target,
    ledger_id,
    coverage_date,
    group,
    account_id: int,
) -> None:
    account = TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=str(account_id),
        phone_masked=f"***{account_id:04d}",
    )
    item = TaskMembershipAdmissionItem(
        id=account_id, tenant_id=1, task_id=task.id,
        account_id=account_id, target_id=target.id, phase="active",
    )
    coverage = TaskAccountDailyCoverage(
        tenant_id=1, task_id=task.id, task_day_ledger_id=ledger_id,
        group_id=group.id, account_id=account_id, membership_item_id=account_id,
        coverage_date=coverage_date, target_count=1, confirmed_count=1,
        state="confirmed",
    )
    session.add_all([account, item, coverage])


def test_extra_candidate_projection_is_bounded_and_rotates(session: Session) -> None:
    total = DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 5
    spec = _seed_candidates(session, total)
    timestamp = datetime(2026, 8, 18, 10)
    for account_id in range(DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 1, total + 1):
        session.add(
            Action(
                id=f"success-{account_id}",
                tenant_id=1,
                task_id=spec.task_id,
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=account_id,
                status="success",
                scheduled_at=timestamp,
                executed_at=timestamp,
                payload={},
            )
        )
    session.flush()

    first = daily_group_extra_candidate_ids(session, spec, now=timestamp)
    second = daily_group_extra_candidate_ids(session, spec, now=timestamp)

    assert len(first) == DAILY_GROUP_EXTRA_CANDIDATE_LIMIT
    assert first == list(range(1, DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 1))
    assert second[:5] == list(range(DAILY_GROUP_EXTRA_CANDIDATE_LIMIT + 1, total + 1))
