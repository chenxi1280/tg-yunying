from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import (
    AccountRuntimeSummary,
    Action,
    OperationIssue,
    OperationIssueAccount,
    OperationIssueSource,
    Task,
    Tenant,
    TgAccount,
)
from app.services import runtime_summary
from app.services._common import _now
from app.services.runtime_summary import reconcile_stale_operation_issues, refresh_task_summary, upsert_operation_issue
from app.services.runtime_summary_batches import refresh_account_runtime_summary_batch
from app.services.task_center import service

pytestmark = pytest.mark.no_postgres


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"account-{account_id}",
        phone_masked=str(account_id),
        status="正常",
        session_ciphertext="session",
    )


def test_planner_task_summary_skips_all_account_projection(monkeypatch) -> None:
    factory = _session_factory()
    refreshed: list[int] = []
    monkeypatch.setattr(runtime_summary, "refresh_account_summary", lambda _session, _tenant, account_id: refreshed.append(account_id))
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        task = Task(
            id="task-all-accounts",
            tenant_id=1,
            name="全账号活群",
            type="group_ai_chat",
            status="running",
            account_config={"account_ids": list(range(1, 581))},
        )
        session.add(task)
        session.commit()

        refresh_task_summary(session, task, include_configured_accounts=False)

    assert refreshed == []


def test_planner_drain_requests_lightweight_task_stats(monkeypatch) -> None:
    factory = _session_factory()
    calls: list[bool] = []
    monkeypatch.setattr(service, "retry_failed_actions", lambda *_args, **_kwargs: 0)
    monkeypatch.setattr(service, "_open_actions_state", lambda *_args: (False, False))
    monkeypatch.setattr(service, "_planning_backlog_blocked", lambda *_args: False)
    monkeypatch.setattr(service, "build_task_plan", lambda *_args: 0)
    monkeypatch.setattr(
        service,
        "refresh_task_stats",
        lambda _session, _task, *, include_configured_accounts=True: calls.append(include_configured_accounts),
    )
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(
            Task(
                id="task-planner-boundary",
                tenant_id=1,
                name="planner boundary",
                type="group_ai_chat",
                status="running",
                next_run_at=_now() - timedelta(seconds=1),
            )
        )
        session.commit()

    service._drain_task_planner(factory, limit=1, process_type=None)

    assert calls == []


def test_planner_task_summary_still_refreshes_latest_failure_account(monkeypatch) -> None:
    factory = _session_factory()
    refreshed: list[int] = []
    monkeypatch.setattr(runtime_summary, "refresh_account_summary", lambda _session, _tenant, account_id: refreshed.append(account_id))
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(_account(7))
        task = Task(
            id="task-latest-failure",
            tenant_id=1,
            name="全账号活群",
            type="group_ai_chat",
            status="running",
            account_config={"account_ids": list(range(1, 581))},
        )
        session.add(task)
        session.add(
            Action(
                id="action-latest-failure",
                tenant_id=1,
                task_id=task.id,
                task_type=task.type,
                action_type="send_message",
                account_id=7,
                status="failed",
                scheduled_at=_now(),
            )
        )
        session.commit()

        refresh_task_summary(session, task, include_configured_accounts=False)

    assert refreshed == [7]


def test_issue_upsert_only_touches_accounts_seen_in_current_failure(monkeypatch) -> None:
    factory = _session_factory()
    first_seen = _now() - timedelta(minutes=5)
    second_seen = _now()
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all([_account(1), _account(2)])
        monkeypatch.setattr(runtime_summary, "_now", lambda: first_seen)
        issue = _upsert_issue(session, [1])
        session.commit()
        first_row = session.scalar(select(OperationIssueAccount).where(OperationIssueAccount.account_id == 1))
        original_latest_seen = first_row.latest_seen_at

        monkeypatch.setattr(runtime_summary, "_now", lambda: second_seen)
        issue = _upsert_issue(session, [2])
        session.commit()
        session.refresh(first_row)
        second_row = session.scalar(select(OperationIssueAccount).where(OperationIssueAccount.account_id == 2))
        affected_account_ids = issue.affected_account_ids
        affected_account_count = issue.affected_account_count

    assert first_row.latest_seen_at == original_latest_seen
    assert second_row.latest_seen_at == second_seen
    assert affected_account_ids == [1, 2]
    assert affected_account_count == 2


def _upsert_issue(session: Session, account_ids: list[int]) -> OperationIssue:
    return upsert_operation_issue(
        session,
        tenant_id=1,
        target_id=None,
        issue_type="task_execution",
        failure_type="ACCOUNT_UNAVAILABLE",
        source_task_id="task-runtime",
        representative_action_id=f"action-{account_ids[0]}",
        affected_account_ids=account_ids,
        failure_reason="unavailable",
        suggested_action="inspect",
    )


def test_issue_reconcile_does_not_materialize_all_action_source_ids(monkeypatch) -> None:
    factory = _session_factory()
    now_value = _now()
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add(Task(id="task-runtime", tenant_id=1, name="task", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-failed",
                tenant_id=1,
                task_id="task-runtime",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=now_value,
            )
        )
        issue = OperationIssue(
            id="issue-runtime",
            tenant_id=1,
            issue_type="task_execution",
            failure_type="ACCOUNT_UNAVAILABLE",
            source_task_id="task-runtime",
            representative_action_id="action-failed",
            status="open",
        )
        session.add(issue)
        session.add(
            OperationIssueSource(
                tenant_id=1,
                issue_id=issue.id,
                source_type="action",
                source_id="action-failed",
            )
        )
        session.commit()
        monkeypatch.setattr(runtime_summary, "_issue_source_ids", lambda *_args: (_ for _ in ()).throw(AssertionError("unbounded source list")))

        assert reconcile_stale_operation_issues(session, 1) == 0
        assert issue.status == "open"


def test_issue_action_source_join_is_tenant_isolated() -> None:
    factory = _session_factory()
    with factory() as session:
        session.add_all([Tenant(id=1, name="tenant-1"), Tenant(id=2, name="tenant-2")])
        session.add(Task(id="foreign-task", tenant_id=2, name="task", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="foreign-action",
                tenant_id=2,
                task_id="foreign-task",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=_now(),
            )
        )
        issue = OperationIssue(
            id="tenant-isolated-issue",
            tenant_id=1,
            issue_type="task_execution",
            failure_type="ACCOUNT_UNAVAILABLE",
            status="open",
        )
        session.add(issue)
        session.add(
            OperationIssueSource(
                tenant_id=1,
                issue_id=issue.id,
                source_type="action",
                source_id="foreign-action",
            )
        )
        session.commit()

        assert reconcile_stale_operation_issues(session, 1) == 1
        assert issue.status == "resolved"


def test_issue_action_source_join_has_supporting_unique_index_contract() -> None:
    constraint = next(
        item
        for item in OperationIssueSource.__table__.constraints
        if item.name == "uq_operation_issue_sources_source"
    )

    assert [column.name for column in constraint.columns] == [
        "tenant_id",
        "issue_id",
        "source_type",
        "source_id",
    ]


def test_account_summary_batches_cover_all_accounts_without_early_repeats(monkeypatch) -> None:
    factory = _session_factory()
    refreshed: list[int] = []
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all(_account(account_id) for account_id in range(1, 206))
        session.commit()

    def fake_refresh(session: Session, tenant_id: int, account_id: int) -> None:
        refreshed.append(account_id)
        session.add(AccountRuntimeSummary(tenant_id=tenant_id, account_id=account_id))

    monkeypatch.setattr("app.services.runtime_summary_batches.refresh_account_summary", fake_refresh)
    assert _run_batch(factory, 500) == 100
    assert _run_batch(factory, 500) == 100
    assert _run_batch(factory, 500) == 5
    assert len(refreshed) == len(set(refreshed)) == 205


def test_account_summary_batch_retries_failed_page_without_losing_accounts(monkeypatch) -> None:
    factory = _session_factory()
    fail_on_account = {150}
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all(_account(account_id) for account_id in range(1, 206))
        session.commit()

    def flaky_refresh(session: Session, tenant_id: int, account_id: int) -> None:
        if account_id in fail_on_account:
            raise RuntimeError("account summary refresh failed")
        session.add(AccountRuntimeSummary(tenant_id=tenant_id, account_id=account_id))

    monkeypatch.setattr("app.services.runtime_summary_batches.refresh_account_summary", flaky_refresh)
    assert _run_batch(factory, 100) == 100
    with pytest.raises(RuntimeError, match="account summary refresh failed"):
        _run_batch(factory, 100)

    fail_on_account.clear()
    assert _run_batch(factory, 100) == 100
    assert _run_batch(factory, 100) == 5
    with factory() as session:
        assert session.query(AccountRuntimeSummary).count() == 205


def test_account_summary_batches_rotate_oldest_updated_rows(monkeypatch) -> None:
    factory = _session_factory()
    initial_time = _now() - timedelta(days=3)
    refreshed: list[int] = []
    with factory() as session:
        session.add(Tenant(id=1, name="tenant"))
        session.add_all(_account(account_id) for account_id in range(1, 4))
        session.flush()
        session.add_all(
            AccountRuntimeSummary(
                tenant_id=1,
                account_id=account_id,
                updated_at=initial_time + timedelta(days=account_id - 1),
            )
            for account_id in range(1, 4)
        )
        session.commit()

    def fake_refresh(session: Session, tenant_id: int, account_id: int) -> None:
        refreshed.append(account_id)
        summary = session.scalar(
            select(AccountRuntimeSummary).where(
                AccountRuntimeSummary.tenant_id == tenant_id,
                AccountRuntimeSummary.account_id == account_id,
            )
        )
        summary.updated_at = _now() + timedelta(seconds=len(refreshed))

    monkeypatch.setattr("app.services.runtime_summary_batches.refresh_account_summary", fake_refresh)
    assert _run_batch(factory, 2) == 2
    assert _run_batch(factory, 2) == 2
    assert refreshed == [1, 2, 3, 1]


def _run_batch(factory, limit: int) -> int:
    with factory() as session:
        count = refresh_account_runtime_summary_batch(session, limit=limit)
        session.commit()
        return count
