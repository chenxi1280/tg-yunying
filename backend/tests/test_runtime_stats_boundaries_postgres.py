from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from threading import Barrier
from time import perf_counter

import pytest
from sqlalchemy import delete, event, func, inspect, select, text

from app.database import Base, SessionLocal, engine
from app.models import (
    Action,
    AiAccountVoiceProfile,
    OperationIssue,
    OperationIssueAccount,
    OperationIssueSource,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    SchedulingSetting,
    Task,
    TaskAccountDailyCoverage,
    TaskDailyCoveragePlanCursor,
    TaskMembershipAdmissionItem,
    TaskRuntimeSummary,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now
from app.services.task_center.daily_coverage import reserve_coverage_for_action
from app.services.task_center.daily_coverage_planning import (
    advance_coverage_plan_cursor,
    ready_coverage_plan_batch,
)
from app.services.task_center.service import _claim_stale_executing_action_ids, _stale_executing_action_ids
from app.services.task_center import service as task_service
from app.services.task_center.stats import _json_text_expression, refresh_task_stats
from app.services.runtime_action_queries import task_action_status_counts_statement


pytestmark = pytest.mark.allow_missing_rule_binding

TENANT_ID = 913_793
ACCOUNT_BASE = 913_793_000
TASK_ID = "pg-coverage-cursor-task"
GROUP_ID = 913_793
ACCOUNT_COUNT = 580
ACTION_HISTORY_COUNT = 40_741
OTHER_TENANT_ID = TENANT_ID + 1
OTHER_TASK_ID = f"{TASK_ID}-other"
OTHER_GROUP_ID = GROUP_ID + 1


def test_postgres_two_planners_cover_580_rows_in_atomic_bounded_batches() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        now = _now()
        _seed_coverage_rows(now)
        _seed_foreign_tenant_coverage(now)
        with SessionLocal() as session:
            _assert_ready_plan_explain(session, now)
        _assert_crash_rolls_back_cursor_and_reservations(now)
        with ThreadPoolExecutor(max_workers=2) as pool:
            worker_results = list(pool.map(lambda worker: _reserve_until_empty(worker, now), range(2)))

        batch_sizes = [size for results in worker_results for size, _elapsed in results]
        elapsed = [duration for results in worker_results for _size, duration in results]
        with SessionLocal() as session:
            reserved = session.scalar(select(func.count()).select_from(TaskAccountDailyCoverage).where(
                TaskAccountDailyCoverage.task_id == TASK_ID,
                TaskAccountDailyCoverage.state == "reserved",
            ))
            actions = session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID))
            foreign_state = session.scalar(select(TaskAccountDailyCoverage.state).where(
                TaskAccountDailyCoverage.task_id == OTHER_TASK_ID,
            ))

        assert reserved == ACCOUNT_COUNT
        assert actions == ACCOUNT_COUNT
        assert sum(batch_sizes) == ACCOUNT_COUNT
        assert all(1 <= size <= 20 for size in batch_sizes)
        assert elapsed and max(elapsed) < 5.0
        assert foreign_state == "ready"
        assert _required_indexes_present()
    finally:
        _cleanup()


def test_postgres_metrics_and_recovery_scans_are_bounded_at_production_history_scale() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_action_history()
        _vacuum_actions()
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            _assert_task_stats_explain(session)
            expected_counts = dict(session.execute(
                select(Action.status, func.count()).where(Action.task_id == TASK_ID).group_by(Action.status)
            ).all())
            started_at = perf_counter()
            stats = refresh_task_stats(session, task, include_configured_accounts=False)
            session.commit()
            metrics_elapsed = perf_counter() - started_at

        with SessionLocal() as session:
            _assert_recovery_explain(session)
            started_at = perf_counter()
            recovery_ids = _stale_executing_action_ids(session, now=_now(), timeout_minutes=30, limit=20)
            recovery_elapsed = perf_counter() - started_at

        assert stats["total_actions"] == ACTION_HISTORY_COUNT
        assert stats["success_count"] == expected_counts["success"]
        assert stats["failure_count"] == expected_counts["failed"]
        assert stats["pending_count"] == expected_counts["pending"]
        assert stats["executing_count"] == expected_counts["executing"]
        assert stats["unknown_after_send_count"] == expected_counts["unknown_after_send"]
        assert stats["skipped_count"] == expected_counts["skipped"]
        assert metrics_elapsed < 10.0
        assert len(recovery_ids) == 20
        assert recovery_elapsed < 5.0
    finally:
        _cleanup()


def test_postgres_ai_generation_json_key_is_literal_for_expression_index() -> None:
    with SessionLocal() as session:
        expression = _json_text_expression(
            session,
            column=Action.payload,
            key="ai_generation_status",
        )
        statement = select(expression, func.count()).select_from(Action).group_by(expression)
        compiled = str(statement.compile(engine))

    assert "payload ->> 'ai_generation_status'" in compiled
    assert "ai_generation_status_1" not in compiled


def test_postgres_recovery_workers_claim_disjoint_stable_batches() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_stale_recovery_actions(40)
        barrier = Barrier(2)
        with ThreadPoolExecutor(max_workers=2) as pool:
            batches = list(pool.map(lambda worker: _claim_recovery_batch(barrier, worker), range(2)))

        assert [len(batch) for batch in batches] == [20, 20]
        assert set(batches[0]).isdisjoint(batches[1])
        assert len(set().union(*map(set, batches))) == 40
    finally:
        _cleanup()


def test_postgres_recovery_cooldown_batch_does_not_block_next_keyset() -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    try:
        _seed_stale_recovery_actions(40)
        first = _claim_recovery_ids()
        with SessionLocal() as session:
            for action in session.scalars(select(Action).where(Action.id.in_(first))):
                action.status = "unknown_after_send"
                action.claim_owner = ""
                action.claim_token = ""
                action.claim_expires_at = None
                action.result = {
                    "unknown_membership_reprobe_status": "connection_error",
                    "unknown_membership_reprobe_next_at": (_now() + timedelta(minutes=5)).isoformat(),
                }
            session.commit()
        second = _claim_recovery_ids()

        assert len(first) == len(second) == 20
        assert set(first).isdisjoint(second)
    finally:
        _cleanup()


def test_postgres_four_dispatch_finalizers_are_bounded_without_history_grouping(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    _cleanup()
    statements: list[str] = []

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany) -> None:
        statements.append(statement.lower())

    def finish_dispatch(_session, action: Action) -> bool:
        action.status = "success"
        action.executed_at = _now()
        return True

    event.listen(engine, "before_cursor_execute", record_statement)
    monkeypatch.setattr(task_service, "dispatch_action", finish_dispatch)
    try:
        _seed_stale_recovery_actions(4)
        action_ids = [f"pg-recovery-{index:03d}" for index in range(4)]
        barrier = Barrier(4)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda action_id: _timed_dispatch(barrier, action_id), action_ids))
        with SessionLocal() as session:
            lock_waiters = session.scalar(text(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database() AND pid <> pg_backend_pid() "
                "AND wait_event_type = 'Lock'"
            ))

        assert [processed for processed, _elapsed in results] == [1, 1, 1, 1]
        assert all(elapsed < 2.0 for _processed, elapsed in results)
        assert not any("group by" in statement and "actions" in statement for statement in statements)
        assert lock_waiters == 0
    finally:
        event.remove(engine, "before_cursor_execute", record_statement)
        _cleanup()


def _seed_coverage_rows(now: datetime) -> None:
    timestamp = now
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


def _seed_foreign_tenant_coverage(now: datetime) -> None:
    timestamp = now
    with SessionLocal() as session:
        session.add(Tenant(id=OTHER_TENANT_ID, name="runtime-boundary-other"))
        session.commit()
        session.add(Task(id=OTHER_TASK_ID, tenant_id=OTHER_TENANT_ID, name="other", type="group_ai_chat", status="running"))
        session.add(TgGroup(id=OTHER_GROUP_ID, tenant_id=OTHER_TENANT_ID, tg_peer_id="-100913794", title="other"))
        session.add(TgAccount(
            id=ACCOUNT_BASE + ACCOUNT_COUNT,
            tenant_id=OTHER_TENANT_ID,
            display_name="other-account",
            phone_masked="other",
            status="在线",
            session_ciphertext="session",
        ))
        session.flush()
        session.add(TaskAccountDailyCoverage(
            tenant_id=OTHER_TENANT_ID,
            task_id=OTHER_TASK_ID,
            group_id=OTHER_GROUP_ID,
            account_id=ACCOUNT_BASE + ACCOUNT_COUNT,
            coverage_date=timestamp.date(),
            state="ready",
            targeted_at=timestamp,
        ))
        session.commit()


def _assert_ready_plan_explain(session, now: datetime) -> None:
    session.execute(text("ANALYZE task_account_daily_coverage"))
    cursor_targeted = session.scalar(select(TaskAccountDailyCoverage.targeted_at).where(
        TaskAccountDailyCoverage.task_id == TASK_ID,
    ).limit(1))
    _assert_explain_uses_index(
        session,
        """
        SELECT id FROM task_account_daily_coverage
        WHERE task_id = :task_id AND coverage_date = :coverage_date AND state = 'ready'
          AND confirmed_count < target_count AND targeted_at <= :now
          AND (targeted_at, account_id, id) > (:cursor_targeted, :cursor_account_id, :cursor_id)
        ORDER BY targeted_at, account_id, id LIMIT 20
        """,
        {
            "task_id": TASK_ID,
            "coverage_date": now.date(),
            "now": now,
            "cursor_targeted": cursor_targeted,
            "cursor_account_id": ACCOUNT_BASE + 100,
            "cursor_id": "",
        },
        "ix_task_daily_coverage_plan_ready",
    )


def _assert_recovery_explain(session) -> None:
    session.execute(text("ANALYZE actions"))
    _assert_explain_uses_index(
        session,
        """
        SELECT id FROM actions
        WHERE status = 'executing' AND lease_expires_at <= :now
        ORDER BY scheduled_at, id LIMIT 20
        """,
        {"now": _now()},
        "ix_actions_executing_recovery",
    )


def _assert_task_stats_explain(session) -> None:
    task = session.get(Task, TASK_ID)
    statement = task_action_status_counts_statement(task)
    _assert_explain_uses_index(
        session,
        str(statement.compile(engine, compile_kwargs={"literal_binds": True})),
        {},
        "ix_actions_task_stats_reconcile",
    )


def _vacuum_actions() -> None:
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text("VACUUM ANALYZE actions"))


def _assert_explain_uses_index(session, query: str, parameters: dict, index_name: str) -> None:
    payload = session.execute(
        text(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) {query}"),
        parameters,
    ).scalar_one()
    root = payload[0]["Plan"]
    nodes = list(_plan_nodes(root))
    assert index_name in {str(node.get("Index Name") or "") for node in nodes}
    assert "Seq Scan" not in {str(node.get("Node Type") or "") for node in nodes}


def _plan_nodes(node: dict):
    yield node
    for child in node.get("Plans") or []:
        yield from _plan_nodes(child)


def _assert_crash_rolls_back_cursor_and_reservations(now: datetime) -> None:
    with SessionLocal() as session:
        task = session.get(Task, TASK_ID)
        rows = ready_coverage_plan_batch(session, task, now=now, limit=20).rows
        _reserve_rows(session, task, rows, now)
        session.rollback()
    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(Action).where(Action.task_id == TASK_ID)) == 0
        assert session.scalar(select(func.count()).select_from(TaskDailyCoveragePlanCursor).where(
            TaskDailyCoveragePlanCursor.task_id == TASK_ID,
        )) == 0


def _reserve_until_empty(_worker: int, now: datetime) -> list[tuple[int, float]]:
    results: list[tuple[int, float]] = []
    while True:
        started_at = perf_counter()
        with SessionLocal() as session:
            task = session.get(Task, TASK_ID)
            rows = ready_coverage_plan_batch(session, task, now=now, limit=20).rows
            if rows:
                _reserve_rows(session, task, rows, now)
            session.commit()
        elapsed = perf_counter() - started_at
        if not rows:
            return results
        results.append((len(rows), elapsed))


def _reserve_rows(session, task: Task, rows: list[TaskAccountDailyCoverage], now: datetime) -> None:
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
                scheduled_at=now,
        ))
        session.flush()
        assert reserve_coverage_for_action(session, row.id, action_id)
    advance_coverage_plan_cursor(session, task, rows[-1], now=now)


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


def _seed_stale_recovery_actions(
    count: int,
    *,
    task_id: str = TASK_ID,
    action_prefix: str = "pg-recovery",
) -> None:
    timestamp = _now() - timedelta(hours=2)
    with SessionLocal() as session:
        if session.get(Tenant, TENANT_ID) is None:
            session.add(Tenant(id=TENANT_ID, name="recovery-claims"))
            session.commit()
        session.add(Task(id=task_id, tenant_id=TENANT_ID, name="recovery", type="target_admission_retry", status="running"))
        session.flush()
        session.add_all(
            Action(
                id=f"{action_prefix}-{index:03d}",
                tenant_id=TENANT_ID,
                task_id=task_id,
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                status="executing",
                scheduled_at=timestamp + timedelta(microseconds=index),
                lease_expires_at=timestamp,
            )
            for index in range(count)
        )
        session.commit()


def _claim_recovery_batch(barrier: Barrier, _worker: int) -> list[str]:
    barrier.wait()
    return _claim_recovery_ids()


def _claim_recovery_ids() -> list[str]:
    with SessionLocal() as session:
        claims, _stale_workers = _claim_stale_executing_action_ids(
            session,
            now=_now(),
            timeout_minutes=30,
            limit=20,
        )
        return [claim.action_id for claim in claims]


def _timed_dispatch(barrier: Barrier, action_id: str) -> tuple[int, float]:
    barrier.wait()
    started_at = perf_counter()
    processed = task_service._dispatch_claimed_action_once(SessionLocal, action_id)
    return processed, perf_counter() - started_at


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
        tenant_ids = (TENANT_ID, OTHER_TENANT_ID)
        session.execute(delete(OperationIssueAccount).where(OperationIssueAccount.tenant_id.in_(tenant_ids)))
        session.execute(delete(OperationIssueSource).where(OperationIssueSource.tenant_id.in_(tenant_ids)))
        session.execute(delete(OperationIssue).where(OperationIssue.tenant_id.in_(tenant_ids)))
        session.execute(delete(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.tenant_id.in_(tenant_ids)))
        session.execute(delete(TaskDailyCoveragePlanCursor).where(TaskDailyCoveragePlanCursor.tenant_id.in_(tenant_ids)))
        session.execute(delete(TaskMembershipAdmissionItem).where(TaskMembershipAdmissionItem.tenant_id.in_(tenant_ids)))
        session.execute(delete(OperationTarget).where(OperationTarget.tenant_id.in_(tenant_ids)))
        session.execute(delete(TaskRuntimeSummary).where(TaskRuntimeSummary.tenant_id.in_(tenant_ids)))
        session.execute(delete(Action).where(Action.tenant_id.in_(tenant_ids)))
        session.execute(delete(AiAccountVoiceProfile).where(AiAccountVoiceProfile.tenant_id.in_(tenant_ids)))
        session.execute(delete(TgAccountOnlineState).where(TgAccountOnlineState.tenant_id.in_(tenant_ids)))
        session.execute(delete(TgGroupAccount).where(TgGroupAccount.tenant_id.in_(tenant_ids)))
        session.execute(delete(Task).where(Task.tenant_id.in_(tenant_ids)))
        session.execute(delete(RuleSetVersion).where(RuleSetVersion.tenant_id.in_(tenant_ids)))
        session.execute(delete(RuleSet).where(RuleSet.tenant_id.in_(tenant_ids)))
        session.execute(delete(SchedulingSetting).where(SchedulingSetting.tenant_id.in_(tenant_ids)))
        session.execute(delete(TgGroup).where(TgGroup.tenant_id.in_(tenant_ids)))
        session.execute(delete(TgAccount).where(TgAccount.tenant_id.in_(tenant_ids)))
        session.execute(delete(Tenant).where(Tenant.id.in_(tenant_ids)))
        session.commit()
