from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from time import perf_counter

import pytest
from sqlalchemy import delete, func, inspect, select

from app.database import Base, SessionLocal, engine
from app.models import (
    Action,
    OperationIssue,
    OperationIssueAccount,
    OperationIssueSource,
    Task,
    TaskAccountDailyCoverage,
    TaskDailyCoveragePlanCursor,
    TaskRuntimeSummary,
    Tenant,
    TgAccount,
    TgGroup,
)
from app.services._common import _now
from app.services.task_center.daily_coverage import reserve_coverage_for_action
from app.services.task_center.daily_coverage_planning import (
    advance_coverage_plan_cursor,
    ready_coverage_plan_batch,
)
from app.services.task_center.service import _stale_executing_action_ids
from app.services.task_center.stats import refresh_task_stats


pytestmark = pytest.mark.allow_missing_rule_binding

TENANT_ID = 913_793
ACCOUNT_BASE = 913_793_000
TASK_ID = "pg-coverage-cursor-task"
GROUP_ID = 913_793
ACCOUNT_COUNT = 580
ACTION_HISTORY_COUNT = 40_741


def test_postgres_two_planners_cover_580_rows_in_atomic_bounded_batches() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_coverage_rows()
        _assert_crash_rolls_back_cursor_and_reservations()
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker_results = list(pool.map(_reserve_until_empty, range(2)))

        batch_sizes = [size for results in worker_results for size, _elapsed in results]
        elapsed = [duration for results in worker_results for _size, duration in results]
        with SessionLocal() as session:
            reserved = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == TASK_ID,
                TaskAccountDailyCoverage.state == "reserved",
            ))
            actions = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID))

        assert reserved == ACCOUNT_COUNT
        assert actions == ACCOUNT_COUNT
        assert sum(batch_sizes) == ACCOUNT_COUNT
        assert all(1 <= size <= 20 for size in batch_sizes)
        assert elapsed and max(elapsed) < 5.0
        assert _required_indexes_present()
    finally:
        _cleanup()


def test_postgres_metrics_and_recovery_scans_are_bounded_at_production_history_scale() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_action_history()
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            started_at = perf_counter()
            stats = refresh_task_stats(session, task, include_configured_accounts=False)
            session.commit()
            metrics_elapsed = perf_counter() - started_at

        with SessionLocal() as session:
            started_at = perf_counter()
            recovery_ids = _stale_executing_action_ids(session, now=_now(), timeout_minutes=30, limit=20)
            recovery_elapsed = perf_counter() - started_at

        assert stats["total_actions"] == ACTION_HISTORY_COUNT
        assert metrics_elapsed < 10.0
        assert len(recovery_ids) == 20
        assert recovery_elapsed < 5.0
    finally:
        _cleanup()


def _seed_coverage_rows() -> None:
    timestamp = _now() - timedelta(minutes=1)
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="runtime-boundary"))
        session.commit()
        session.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="580 coverage", type="group_ai_chat", status="running"))
        session.add(TgGroup(id=GROUP_ID, tenant_id=TENANT_ID, tg_peer_id="-100913793", title="coverage"))
        session.add_all(_account(index) for index in range(ACCOUNT_COUNT))
        session.flush()
        session.add_all(
            TaskAccountDailyCoverage(
                tenant_id=TENANT_ID,
                task_id=TASK_ID,
                group_id=GROUP_ID,
                account_id=ACCOUNT_BASE + index,
                coverage_date=timestamp.date(),
                state="ready",
                targeted_at=timestamp,
            )
            for index in range(ACCOUNT_COUNT)
        )
        session.commit()


def _assert_crash_rolls_back_cursor_and_reservations() -> None:
    with SessionLocal() as session:
        task = session.get(Task, TASK_ID)
        rows = ready_coverage_plan_batch(session, task, now=_now(), limit=20).rows
        _reserve_rows(session, task, rows)
        session.rollback()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID)) == 0
        assert session.scalar(select(func.count()).select_from(TaskDailyCoveragePlanCursor).where(
            TaskDailyCoveragePlanCursor.task_id == TASK_ID,
        )) == 0


def _reserve_until_empty(_worker: int) -> list[tuple[int, float]]:
    results: list[tuple[int, float]] = []
    while True:
        started_at = perf_counter()
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            rows = ready_coverage_plan_batch(session, task, now=_now(), limit=20).rows
            if rows:
                _reserve_rows(session, task, rows)
            session.commit()
        elapsed = perf_counter() - started_at
        if not rows:
            return results
        results.append((len(rows), elapsed))


def _reserve_rows(session, task: Task, rows: list[TaskAccountDailyCoverage]) -> None:
    for row in rows:
        action_id = f"a{row.id[1:]}"
        session.add(Action(
            id=action_id,
            tenant_id=TENANT_ID,
            task_id=TASK_ID,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=row.account_id,
            status="pending",
            scheduled_at=_now(),
        ))
        session.flush()
        assert reserve_coverage_for_action(session, row.id, action_id)
    advance_coverage_plan_cursor(session, task, rows[-1], now=_now())


def _seed_action_history() -> None:
    timestamp = _now() - timedelta(hours=2)
    statuses = ("success", "failed", "pending", "unknown_after_send", "skipped")
    with SessionLocal() as session:
        session.add(Tenant(id=TENANT_ID, name="runtime-history"))
        session.commit()
        session.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="history", type="target_admission_retry", status="running"))
        session.flush()
        session.bulk_insert_mappings(Action, [
            {
                "id": f"pg-history-{index:05d}",
                "tenant_id": TENANT_ID,
                "task_id": TASK_ID,
                "task_type": "target_admission_retry",
                "action_type": "ensure_target_membership",
                "status": "executing" if index < ACCOUNT_COUNT else statuses[index % len(statuses)],
                "scheduled_at": timestamp + timedelta(microseconds=index),
                "lease_expires_at": timestamp if index < ACCOUNT_COUNT else None,
                "payload": {},
                "result": {},
            }
            for index in range(ACTION_HISTORY_COUNT)
        ])
        session.commit()


def _account(index: int) -> TgAccount:
    account_id = ACCOUNT_BASE + index
    return TgAccount(
        id=account_id,
        tenant_id=TENANT_ID,
        display_name=f"account-{index}",
        phone_masked=str(account_id),
        status="在线",
        session_ciphertext="session",
    )


def _required_indexes_present() -> bool:
    action_indexes = {item["name"] for item in inspect(engine).get_indexes("actions")}
    coverage_indexes = {item["name"] for item in inspect(engine).get_indexes("task_account_daily_coverage")}
    return {
        "ix_actions_task_stats_reconcile",
        "ix_actions_executing_recovery",
    }.issubset(action_indexes) and "ix_task_daily_coverage_plan_ready" in coverage_indexes


def _cleanup() -> None:
    with SessionLocal() as session:
        session.execute(delete(OperationIssueAccount).where(OperationIssueAccount.tenant_id == TENANT_ID))
        session.execute(delete(OperationIssueSource).where(OperationIssueSource.tenant_id == TENANT_ID))
        session.execute(delete(OperationIssue).where(OperationIssue.tenant_id == TENANT_ID))
        session.execute(delete(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id == TENANT_ID))
        session.execute(delete(TaskDailyCoveragePlanCursor).where(TaskDailyCoveragePlanCursor.tenant_id == TENANT_ID))
        session.execute(delete(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id == TENANT_ID))
        session.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        session.execute(delete(Task).where(Task.tenant_id == TENANT_ID))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id == TENANT_ID))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id == TENANT_ID))
        session.execute(delete(Tenant).where(Tenant.id == TENANT_ID))
        session.commit()
