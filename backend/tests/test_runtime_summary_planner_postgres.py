from time import perf_counter

import pytest
from sqlalchemy import delete, select

from app.database import Base, SessionLocal, engine
from app.models import (
    AccountRuntimeSummary,
    Action,
    OperationIssue,
    OperationIssueAccount,
    OperationIssueSource,
    Task,
    TaskRuntimeSummary,
    Tenant,
    TgAccount,
)
from app.services import runtime_summary
from app.services._common import _now
from app.services.runtime_summary import refresh_task_summary
from app.services.runtime_summary import reconcile_stale_operation_issues
from app.services.runtime_summary_batches import refresh_account_runtime_summary_batch

TEST_TENANT_ID = 913_716
ACCOUNT_COUNT = 580
MAX_PLANNER_SUMMARY_SECONDS = 5.0
MAX_ACCOUNT_BATCH_SECONDS = 10.0
EXPECTED_BATCH_COUNTS = [100, 100, 100, 100, 100, 80]

pytestmark = pytest.mark.allow_missing_rule_binding


def test_postgres_planner_summary_is_bounded_for_580_configured_accounts(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    refreshed: list[int] = []
    monkeypatch.setattr(
        runtime_summary,
        "refresh_account_summary",
        lambda _session, _tenant_id, account_id: refreshed.append(account_id),
    )
    try:
        with SessionLocal() as session:
            _seed_planner_task(session)
            task = session.get(Task, "pg-runtime-summary-task")
            started_at = perf_counter()
            refresh_task_summary(session, task, include_configured_accounts=False)
            session.commit()
            elapsed = perf_counter() - started_at

        assert refreshed == [7]
        assert elapsed < MAX_PLANNER_SUMMARY_SECONDS
    finally:
        _cleanup()


def test_postgres_account_summary_batches_cover_580_accounts() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        with SessionLocal() as session:
            session.add(Tenant(id=TEST_TENANT_ID, name="runtime-summary-scale"))
            session.add_all(_account(account_id) for account_id in range(1, ACCOUNT_COUNT + 1))
            session.commit()

        batch_results = [_timed_batch(100) for _ in EXPECTED_BATCH_COUNTS]
        with SessionLocal() as session:
            summaries = list(
                session.scalars(
                    select(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == TEST_TENANT_ID)
                )
            )

        assert [count for count, _elapsed in batch_results] == EXPECTED_BATCH_COUNTS
        assert all(elapsed < MAX_ACCOUNT_BATCH_SECONDS for _count, elapsed in batch_results)
        assert len(summaries) == ACCOUNT_COUNT
        assert all(summary.updated_at is not None for summary in summaries)
    finally:
        _cleanup()


def test_postgres_issue_reconcile_scales_with_580_action_sources() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        with SessionLocal() as session:
            session.add(Tenant(id=TEST_TENANT_ID, name="runtime-summary-scale"))
            session.commit()
            task = Task(
                id="pg-runtime-issue-task",
                tenant_id=TEST_TENANT_ID,
                name="runtime issue task",
                type="group_ai_chat",
                status="running",
            )
            session.add(task)
            session.commit()
            issue = OperationIssue(
                id="pg-runtime-issue",
                tenant_id=TEST_TENANT_ID,
                issue_type="task_execution",
                failure_type="ACCOUNT_UNAVAILABLE",
                source_task_id=task.id,
                status="open",
            )
            session.add(issue)
            session.add_all(_failed_action(task, index) for index in range(ACCOUNT_COUNT))
            session.add_all(_action_source(issue, index) for index in range(ACCOUNT_COUNT))
            session.commit()

            started_at = perf_counter()
            resolved = reconcile_stale_operation_issues(session, TEST_TENANT_ID)
            session.commit()
            elapsed = perf_counter() - started_at

        assert resolved == 0
        assert elapsed < MAX_PLANNER_SUMMARY_SECONDS
    finally:
        _cleanup()


def _seed_planner_task(session) -> None:
    session.add(Tenant(id=TEST_TENANT_ID, name="runtime-summary-scale"))
    session.commit()
    session.add(_account(7))
    task = Task(
        id="pg-runtime-summary-task",
        tenant_id=TEST_TENANT_ID,
        name="580-account task",
        type="group_ai_chat",
        status="running",
        account_config={"account_ids": list(range(1, ACCOUNT_COUNT + 1))},
    )
    session.add(task)
    session.commit()
    session.add(
        Action(
            id="pg-runtime-summary-action",
            tenant_id=TEST_TENANT_ID,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            account_id=7,
            status="failed",
            scheduled_at=_now(),
            executed_at=_now(),
            result={"failure_type": "ACCOUNT_UNAVAILABLE"},
        )
    )
    session.commit()


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=TEST_TENANT_ID,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        status="正常",
        session_ciphertext="session",
    )


def _failed_action(task: Task, index: int) -> Action:
    return Action(
        id=f"pg-runtime-source-{index:03d}",
        tenant_id=TEST_TENANT_ID,
        task_id=task.id,
        task_type=task.type,
        action_type="send_message",
        status="failed",
        scheduled_at=_now(),
        executed_at=_now(),
        result={"failure_type": "ACCOUNT_UNAVAILABLE"},
    )


def _action_source(issue: OperationIssue, index: int) -> OperationIssueSource:
    return OperationIssueSource(
        tenant_id=TEST_TENANT_ID,
        issue_id=issue.id,
        source_type="action",
        source_id=f"pg-runtime-source-{index:03d}",
        failure_type="ACCOUNT_UNAVAILABLE",
    )


def _run_batch(limit: int) -> int:
    with SessionLocal() as session:
        count = refresh_account_runtime_summary_batch(session, limit=limit)
        session.commit()
        return count


def _timed_batch(limit: int) -> tuple[int, float]:
    started_at = perf_counter()
    count = _run_batch(limit)
    return count, perf_counter() - started_at


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(OperationIssueAccount).where(OperationIssueAccount.tenant_id == TEST_TENANT_ID))
        session.execute(delete(OperationIssueSource).where(OperationIssueSource.tenant_id == TEST_TENANT_ID))
        session.execute(delete(OperationIssue).where(OperationIssue.tenant_id == TEST_TENANT_ID))
        session.execute(delete(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == TEST_TENANT_ID))
        session.execute(delete(AccountRuntimeSummary).where(AccountRuntimeSummary.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Action).where(Action.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TEST_TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TEST_TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TEST_TENANT_ID))
        session.commit()
