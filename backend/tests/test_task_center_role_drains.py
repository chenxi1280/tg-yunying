from __future__ import annotations

from datetime import UTC, datetime, timedelta
from threading import Lock
from time import sleep
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy import create_engine
from sqlalchemy import event
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import AccountStatus, Action, RuntimeCleanupAudit, RuntimeMetricSnapshot, Task, TaskRuntimeSummary, Tenant, TgAccount, WorkerHeartbeat
from app.schemas.task_center import TaskSettingsUpdate
from app.services._common import _now
from app.services.task_center import dispatcher, metrics_runtime, service


@pytest.fixture(autouse=True)
def clear_dispatcher_runtime_state():
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()
    yield
    dispatcher._ACTION_RESERVATIONS.clear()
    dispatcher._IN_FLIGHT_ACCOUNTS.clear()


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


class _FakePostgresSession:
    bind = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def commit(self):
        return None


def _pending_generation_action(index: int, generation_id: str = "generation-batch") -> SimpleNamespace:
    return SimpleNamespace(
        id=f"action-ai-{index}",
        tenant_id=1,
        task_id="task-ai",
        action_type="send_message",
        payload={
            "message_text": "",
            "ai_generation_id": generation_id,
            "ai_generation_status": "pending",
            "ai_generation_claim_owner": "worker-a",
            "ai_generation_claim_token": "claim-token",
        },
    )


def _max_parallel_dispatches(monkeypatch, claimed: list[SimpleNamespace]) -> int:
    activity = {"active": 0, "maximum": 0}
    lock = Lock()

    def fake_dispatch(_session_factory, _action_id):
        with lock:
            activity["active"] += 1
            activity["maximum"] = max(activity["maximum"], activity["active"])
        sleep(0.05)
        with lock:
            activity["active"] -= 1
        return 1

    monkeypatch.setattr(service, "record_worker_heartbeat", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(service, "claim_actions", lambda *_args, **_kwargs: claimed)
    monkeypatch.setattr(service, "_dispatcher_concurrency", lambda: 2)
    monkeypatch.setattr(service, "_dispatch_claimed_action", fake_dispatch)
    assert service.drain_task_dispatcher(_FakePostgresSession, len(claimed)) == len(claimed)
    return activity["maximum"]


def test_role_drains_record_distinct_heartbeats(monkeypatch):
    SessionFactory = _session_factory()
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

    monkeypatch.setattr(service, "drain_listener_runtime", lambda *_args, **_kwargs: type("Result", (), {"source_count": 0, "processed_count": 0})())

    assert service.drain_task_listener(SessionFactory, 5) == 0
    assert service.drain_task_recovery(SessionFactory, 5) >= 0
    assert service.drain_task_planner(SessionFactory, 5) >= 0
    assert service.drain_task_metrics(SessionFactory, 5) >= 1

    with SessionFactory() as session:
        roles = set(session.scalars(select(WorkerHeartbeat.process_type)))
        metric_count = session.query(RuntimeMetricSnapshot).count()

    assert {"listener", "recovery", "planner", "metrics"}.issubset(roles)
    assert metric_count >= 1


def test_planner_refreshes_heartbeat_between_due_tasks(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    heartbeat_calls: list[str] = []
    original_record = service.record_worker_heartbeat

    def spy_record_worker_heartbeat(session, *, process_type: str = "task_center", metadata: dict | None = None):
        heartbeat_calls.append(process_type)
        return original_record(session, process_type=process_type, metadata=metadata)

    monkeypatch.setattr(service, "record_worker_heartbeat", spy_record_worker_heartbeat)
    monkeypatch.setattr(service, "build_task_plan", lambda *_args, **_kwargs: 0)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        for index in range(2):
            session.add(
                Task(
                    id=f"task-planner-heartbeat-{index}",
                    tenant_id=1,
                    name=f"心跳任务 {index}",
                    type="channel_view",
                    status="running",
                    next_run_at=now_value - timedelta(seconds=1),
                )
            )
        session.commit()

    service.drain_task_planner(SessionFactory, 5)

    assert heartbeat_calls.count("planner") >= 3


def test_target_admission_retry_planner_keeps_pending_membership_actions() -> None:
    SessionFactory = _session_factory()
    now_value = _now()
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-admission-retry-planner",
                tenant_id=1,
                name="准入重试",
                type="target_admission_retry",
                status="running",
                next_run_at=now_value - timedelta(seconds=1),
                stats={},
            )
        )
        session.add(
            Action(
                id="action-admission-retry-pending",
                tenant_id=1,
                task_id="task-admission-retry-planner",
                task_type="target_admission_retry",
                action_type="ensure_target_membership",
                status="pending",
                scheduled_at=now_value,
            )
        )
        session.commit()

    service.drain_task_planner(SessionFactory, 5)

    with SessionFactory() as session:
        task = session.get(Task, "task-admission-retry-planner")
        assert task.status == "running"
        assert "total_actions" not in task.stats

    service.drain_task_metrics(SessionFactory, 5)

    with SessionFactory() as session:
        task = session.get(Task, "task-admission-retry-planner")
        assert task.stats["total_actions"] == 1
        assert task.stats["pending_count"] == 1


@pytest.mark.no_postgres
def test_metrics_drain_reconciles_missing_task_runtime_summary(monkeypatch) -> None:
    SessionFactory = _session_factory()
    now_value = _now()
    refresh_calls: list[bool] = []
    real_refresh = metrics_runtime.refresh_task_stats

    def record_refresh(session: Session, task: Task, *, include_configured_accounts: bool = True):
        refresh_calls.append(include_configured_accounts)
        return real_refresh(session, task, include_configured_accounts=include_configured_accounts)

    monkeypatch.setattr(metrics_runtime, "refresh_task_stats", record_refresh)
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-unrelated-summary", tenant_id=1, name="待 metrics 汇总", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-unrelated-summary",
                tenant_id=1,
                task_id="task-unrelated-summary",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=now_value,
                executed_at=now_value,
                result={"failure_type": "send_failed"},
            )
        )
        session.commit()

    assert service.drain_task_metrics(SessionFactory, 5) >= 1

    with SessionFactory() as session:
        summary = session.scalar(select(TaskRuntimeSummary).where(TaskRuntimeSummary.task_id == "task-unrelated-summary"))
        assert summary is not None
        assert summary.failed_count == 1
    assert refresh_calls == [False]


@pytest.mark.no_postgres
def test_dispatcher_finalize_does_not_refresh_full_task_stats(monkeypatch) -> None:
    SessionFactory = _session_factory()
    now_value = _now()

    def fake_dispatch(_session: Session, action: Action) -> bool:
        action.status = "success"
        action.executed_at = now_value
        return True

    monkeypatch.setattr(service, "dispatch_action", fake_dispatch)
    monkeypatch.setattr(
        service,
        "refresh_task_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("dispatcher full stats refresh")),
    )
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="A", phone_masked="***", status=AccountStatus.ACTIVE.value, session_ciphertext="mock"))
        session.add(Task(id="task-no-finalize-stats", tenant_id=1, name="dispatcher", type="group_relay", status="running"))
        session.add(Action(
            id="action-no-finalize-stats", tenant_id=1, task_id="task-no-finalize-stats",
            task_type="group_relay", action_type="send_message", account_id=101,
            status="pending", scheduled_at=now_value - timedelta(seconds=1),
            payload={"chat_id": "-1001", "message_text": "hello"},
        ))
        session.commit()

    assert service.drain_task_dispatcher(SessionFactory, 1) == 1


@pytest.mark.no_postgres
def test_all_account_planner_preserves_round_size_across_short_batches(monkeypatch) -> None:
    SessionFactory = _session_factory()
    now_value = _now()
    calls: list[str] = []

    def fake_build(_session: Session, task: Task) -> int:
        calls.append(task.id)
        return min(20, int(_session.info["daily_coverage_plan_limit"]))

    monkeypatch.setattr(service, "build_task_plan", fake_build)
    monkeypatch.setattr(
        service,
        "refresh_task_stats",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("planner full stats refresh")),
    )
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(
            id="task-coverage-short-batches", tenant_id=1, name="覆盖任务",
            type="group_ai_chat", status="running", next_run_at=now_value - timedelta(seconds=1),
            type_config={
                "account_coverage_mode": "all_accounts_daily",
                "messages_per_round_mode": "manual",
                "messages_per_round": 60,
            },
        ))
        session.commit()

    assert service.drain_task_planner(SessionFactory, 1) == 60
    assert calls == ["task-coverage-short-batches"] * 3


@pytest.mark.no_postgres
def test_all_account_planner_does_not_round_thirty_up_to_forty(monkeypatch) -> None:
    SessionFactory = _session_factory()
    planned_batches: list[int] = []

    def fake_build(session: Session, _task: Task) -> int:
        planned = min(20, int(session.info["daily_coverage_plan_limit"]))
        planned_batches.append(planned)
        return planned

    monkeypatch.setattr(service, "build_task_plan", fake_build)
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="default"))
        session.add(Task(
            id="task-coverage-thirty", tenant_id=1, name="coverage",
            type="group_ai_chat", status="running", next_run_at=_now() - timedelta(seconds=1),
            type_config={
                "account_coverage_mode": "all_accounts_daily",
                "messages_per_round_mode": "manual",
                "messages_per_round": 30,
            },
        ))
        session.commit()

    assert service.drain_task_planner(SessionFactory, 1) == 30
    assert planned_batches == [20, 10]


def test_metrics_drain_uses_index_friendly_counts() -> None:
    SessionFactory = _session_factory()
    statements: list[str] = []

    event.listen(
        SessionFactory.kw["bind"],
        "before_cursor_execute",
        lambda _conn, _cursor, statement, _parameters, _context, _executemany: statements.append(statement),
    )
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Action(
                id="action-metrics-count",
                tenant_id=1,
                task_id="task-metrics-count",
                task_type="channel_view",
                action_type="view_post",
                status="success",
                scheduled_at=_now(),
                executed_at=_now(),
            )
        )
        session.commit()

    assert service.drain_task_metrics(SessionFactory, 5) >= 1

    metric_count_statements = [statement.lower() for statement in statements if "from actions" in statement.lower()]
    assert metric_count_statements
    assert all("count(actions.id)" not in statement for statement in metric_count_statements)


def test_runtime_metric_retention_deletes_old_snapshots_in_batches() -> None:
    from app.services.task_center.runtime_retention import cleanup_runtime_metric_snapshots

    SessionFactory = _session_factory()
    old_at = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    fresh_at = datetime(2026, 6, 24, 9, 0, tzinfo=UTC)
    with SessionFactory() as session:
        session.add_all(
            [
                RuntimeMetricSnapshot(id="metric-old-1", captured_at=old_at, metric_name="worker.active.count", metric_value=1),
                RuntimeMetricSnapshot(id="metric-old-2", captured_at=old_at + timedelta(seconds=1), metric_name="worker.stale.count", metric_value=2),
                RuntimeMetricSnapshot(id="metric-fresh", captured_at=fresh_at, metric_name="worker.active.count", metric_value=3),
            ]
        )
        session.commit()

        deleted = cleanup_runtime_metric_snapshots(session, retention_days=7, today=fresh_at.date(), batch_size=1)
        session.commit()

        remaining_ids = set(session.scalars(select(RuntimeMetricSnapshot.id)))

    assert deleted == 1
    assert remaining_ids == {"metric-old-2", "metric-fresh"}


def test_runtime_metric_retention_runs_only_when_due() -> None:
    from app.services.task_center.runtime_retention import cleanup_runtime_metric_snapshots_if_due

    SessionFactory = _session_factory()
    old_at = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)
    now_value = datetime(2026, 6, 24, 9, 0, tzinfo=UTC)
    with SessionFactory() as session:
        session.add_all(
            [
                RuntimeMetricSnapshot(id="metric-old-due", captured_at=old_at, metric_name="worker.active.count", metric_value=1),
                RuntimeMetricSnapshot(id="metric-old-skip", captured_at=old_at + timedelta(seconds=1), metric_name="worker.stale.count", metric_value=2),
            ]
        )
        session.commit()

        first_deleted = cleanup_runtime_metric_snapshots_if_due(
            session,
            retention_days=7,
            batch_size=1,
            interval_seconds=300,
            now_value=now_value,
        )
        second_deleted = cleanup_runtime_metric_snapshots_if_due(
            session,
            retention_days=7,
            batch_size=1,
            interval_seconds=300,
            now_value=now_value + timedelta(seconds=60),
        )
        session.commit()

        remaining_ids = set(session.scalars(select(RuntimeMetricSnapshot.id)))
        audit_count = session.query(RuntimeCleanupAudit).count()

    assert first_deleted == 1
    assert second_deleted == 0
    assert remaining_ids == {"metric-old-skip"}
    assert audit_count == 1


def test_dispatcher_role_claims_and_dispatches_without_listener(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    listener_called = False

    def fake_listener(*_args, **_kwargs):
        nonlocal listener_called
        listener_called = True
        return type("Result", (), {"source_count": 0, "processed_count": 0})()

    def fake_dispatch(session: Session, action: Action) -> bool:
        action.status = "success"
        action.executed_at = _now()
        action.result = {"success": True, "remote_message_id": "mock-role-drain"}
        return True

    monkeypatch.setattr(service, "drain_listener_runtime", fake_listener)
    monkeypatch.setattr(service, "dispatch_action", fake_dispatch)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="A", phone_masked="***", status=AccountStatus.ACTIVE.value, session_ciphertext="mock"))
        session.add(Task(id="task-dispatcher-role", tenant_id=1, name="dispatcher", type="group_relay", status="running"))
        session.add(
            Action(
                id="action-dispatcher-role",
                tenant_id=1,
                task_id="task-dispatcher-role",
                task_type="group_relay",
                action_type="send_message",
                account_id=11,
                status="pending",
                scheduled_at=now_value - timedelta(seconds=1),
                payload={"chat_id": "-1001", "message_text": "hello"},
            )
        )
        session.commit()

    assert service.drain_task_dispatcher(SessionFactory, 5) == 1
    assert listener_called is False

    with SessionFactory() as session:
        action = session.get(Action, "action-dispatcher-role")
        heartbeat = session.scalar(select(WorkerHeartbeat).where(WorkerHeartbeat.process_type == "dispatcher"))

    assert action.status == "success"
    assert action.result["remote_message_id"] == "mock-role-drain"
    assert heartbeat is not None


@pytest.mark.no_postgres
def test_dispatcher_role_limits_claim_batch_to_effective_concurrency(monkeypatch):
    SessionFactory = _session_factory()
    claimed_limits: list[int] = []

    def fake_claim_actions(_session, *, limit: int, exclude_task_ids=None):
        claimed_limits.append(limit)
        return []

    monkeypatch.setattr(service, "claim_actions", fake_claim_actions)
    monkeypatch.setattr(service, "_dispatcher_concurrency", lambda: 13)

    assert service.drain_task_dispatcher(SessionFactory, 100) == 0
    assert claimed_limits == [13]


@pytest.mark.no_postgres
def test_dispatcher_serializes_claim_batch_with_pending_ai_generation(monkeypatch):
    claimed = [_pending_generation_action(1), _pending_generation_action(2)]

    assert _max_parallel_dispatches(monkeypatch, claimed) == 1


@pytest.mark.no_postgres
def test_dispatcher_keeps_non_shared_generation_actions_parallel(monkeypatch):
    claimed = [
        _pending_generation_action(1, "generation-a"),
        _pending_generation_action(2, "generation-b"),
    ]

    assert _max_parallel_dispatches(monkeypatch, claimed) == 2


@pytest.mark.no_postgres
def test_shared_generation_detection_requires_two_matching_actions():
    one_generation = _pending_generation_action(1)
    ordinary = SimpleNamespace(
        id="action-ordinary",
        tenant_id=1,
        task_id="task-ordinary",
        action_type="send_message",
        payload={"message_text": "ready"},
    )

    assert service._has_shared_ai_generation_batch([one_generation]) is False
    assert service._has_shared_ai_generation_batch([one_generation, ordinary]) is False


@pytest.mark.no_postgres
def test_dispatcher_db_error_does_not_stop_other_claimed_actions(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    calls: list[str] = []

    def fake_dispatch(session: Session, action: Action) -> bool:
        calls.append(action.id)
        if action.id == "action-db-deadlock":
            raise SQLAlchemyError("deadlock detected")
        action.status = "success"
        action.executed_at = _now()
        action.result = {"success": True, "remote_message_id": "mock-after-db-error"}
        return True

    monkeypatch.setattr(service, "dispatch_action", fake_dispatch)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgAccount(id=11, tenant_id=1, display_name="A", phone_masked="***0011", status=AccountStatus.ACTIVE.value, session_ciphertext="mock"))
        session.add(TgAccount(id=12, tenant_id=1, display_name="B", phone_masked="***0012", status=AccountStatus.ACTIVE.value, session_ciphertext="mock"))
        session.add(Task(id="task-dispatcher-db-error", tenant_id=1, name="dispatcher", type="group_relay", status="running"))
        for action_id, account_id, offset in (("action-db-deadlock", 11, 2), ("action-after-db-error", 12, 1)):
            session.add(
                Action(
                    id=action_id,
                    tenant_id=1,
                    task_id="task-dispatcher-db-error",
                    task_type="group_relay",
                    action_type="send_message",
                    account_id=account_id,
                    status="pending",
                    scheduled_at=now_value - timedelta(seconds=offset),
                    payload={"chat_id": "-1001", "message_text": action_id},
                )
            )
        session.commit()

    assert service.drain_task_dispatcher(SessionFactory, 2) == 1
    assert calls == ["action-db-deadlock", "action-after-db-error"]

    with SessionFactory() as session:
        deadlocked = session.get(Action, "action-db-deadlock")
        succeeded = session.get(Action, "action-after-db-error")

    assert deadlocked.status == "pending"
    assert deadlocked.retry_count == 1
    assert deadlocked.claim_owner == ""
    assert deadlocked.lease_owner == ""
    assert deadlocked.result["error_code"] == "dispatcher_db_error"
    assert deadlocked.result["validation_stage"] == "dispatcher_db"
    assert deadlocked.scheduled_at > now_value
    assert succeeded.status == "success"
    assert succeeded.result["remote_message_id"] == "mock-after-db-error"


@pytest.mark.no_postgres
def test_dispatcher_db_error_resets_pre_gateway_generation_for_retry():
    SessionFactory = _session_factory()
    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-generation-db-error", tenant_id=1, name="AI群", type="group_ai_chat"))
        session.add(Action(
            id="action-generation-db-error",
            tenant_id=1,
            task_id="task-generation-db-error",
            task_type="group_ai_chat",
            action_type="send_message",
            status="executing",
            payload={
                "group_id": 7,
                "ai_generation_status": "generating",
                "ai_generation_attempt_id": "attempt-db-error",
                "ai_generation_request_id": "request-db-error",
                "ai_generation_claim_owner": "worker-a",
                "ai_generation_claim_token": "claim-a",
            },
            result={"generation_stage": "generation_claimed"},
        ))
        session.commit()

        assert dispatcher.mark_dispatcher_db_error(session, "action-generation-db-error", "connection timeout")
        session.commit()
        action = session.get(Action, "action-generation-db-error")

    assert action.status == "pending"
    assert action.payload["ai_generation_status"] == "pending"
    assert action.payload["ai_generation_attempt_id"] == ""
    assert action.payload["ai_generation_claim_token"] == ""
    assert action.result["error_code"] == "dispatcher_db_error"


def test_planner_does_not_exclude_due_ai_open_actions_with_beijing_clock(monkeypatch):
    SessionFactory = _session_factory()
    beijing_now = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=8)
    due_at = beijing_now - timedelta(seconds=1)

    def unexpected_build_task_plan(*_args, **_kwargs):
        raise AssertionError("open actions should block planning a new AI round")

    monkeypatch.setattr(service, "_now", lambda: beijing_now)
    monkeypatch.setattr(service, "build_task_plan", unexpected_build_task_plan)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-ai-due-open",
                tenant_id=1,
                name="天津",
                type="group_ai_chat",
                status="running",
                next_run_at=due_at,
                account_config={},
                pacing_config={},
                failure_policy={},
                type_config={},
                stats={},
            )
        )
        session.add(
            Action(
                id="action-ai-due-open",
                tenant_id=1,
                task_id="task-ai-due-open",
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=due_at,
                payload={"chat_id": "-1001", "message_text": "hello"},
                result={},
            )
        )
        session.commit()

    _processed, future_open_action_task_ids = service._drain_task_planner(SessionFactory, limit=5, process_type=None)

    assert future_open_action_task_ids == set()
    with SessionFactory() as session:
        task = session.get(Task, "task-ai-due-open")
        assert task.next_run_at == due_at


@pytest.mark.no_postgres
def test_planner_allows_daily_coverage_debt_with_existing_open_action(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    built_task_ids: list[str] = []

    monkeypatch.setattr(service, "requires_planning_with_open_actions", lambda *_args: True)
    monkeypatch.setattr(service, "build_task_plan", lambda _session, task: built_task_ids.append(task.id) or 1)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="task-ai-daily-debt-open",
            tenant_id=1,
            name="每日覆盖债务",
            type="group_ai_chat",
            status="running",
            next_run_at=now_value - timedelta(seconds=1),
            type_config={"account_coverage_mode": "all_accounts_daily"},
        )
        session.add(task)
        session.add(Action(
            id="action-ai-daily-debt-open",
            tenant_id=1,
            task_id=task.id,
            task_type=task.type,
            action_type="send_message",
            status="pending",
            scheduled_at=now_value + timedelta(minutes=1),
            payload={"message_text": "existing"},
        ))
        session.commit()

    service._drain_task_planner(SessionFactory, limit=5, process_type=None)

    assert built_task_ids == ["task-ai-daily-debt-open"]


@pytest.mark.no_postgres
def test_planner_prepares_open_ai_actions_before_skip(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    built_task_ids: list[str] = []

    def fake_prepare_open_actions(session, task):
        action = session.get(Action, "action-ai-profileless-open")
        action.status = "skipped"
        return 1

    def fake_build_task_plan(_session, task):
        built_task_ids.append(task.id)
        return 2

    monkeypatch.setattr(service, "prepare_open_actions_for_planning", fake_prepare_open_actions)
    monkeypatch.setattr(service, "build_task_plan", fake_build_task_plan)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-ai-profileless-open",
                tenant_id=1,
                name="天津",
                type="group_ai_chat",
                status="running",
                next_run_at=now_value - timedelta(seconds=1),
                account_config={},
                pacing_config={},
                failure_policy={},
                type_config={},
                stats={},
            )
        )
        session.add(
            Action(
                id="action-ai-profileless-open",
                tenant_id=1,
                task_id="task-ai-profileless-open",
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=now_value + timedelta(minutes=1),
                payload={"chat_id": "-1001", "message_text": "old"},
                result={},
            )
        )
        session.commit()

    processed, future_open_action_task_ids = service._drain_task_planner(SessionFactory, limit=5, process_type=None)

    assert processed == 3
    assert built_task_ids == ["task-ai-profileless-open"]
    assert future_open_action_task_ids == set()
    with SessionFactory() as session:
        action = session.get(Action, "action-ai-profileless-open")
        assert action.status == "skipped"


@pytest.mark.no_postgres
def test_planner_skips_open_action_preparation_when_task_has_no_open_actions(monkeypatch):
    SessionFactory = _session_factory()
    now_value = _now()
    built_task_ids: list[str] = []

    def fail_prepare_open_actions(*_args, **_kwargs):
        raise AssertionError("no open action should skip preparation")

    def fake_build_task_plan(_session, task):
        built_task_ids.append(task.id)
        return 1

    monkeypatch.setattr(service, "prepare_open_actions_for_planning", fail_prepare_open_actions)
    monkeypatch.setattr(service, "build_task_plan", fake_build_task_plan)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="default"))
        session.add(
            Task(
                id="task-ai-no-open-actions",
                tenant_id=1,
                name="daily coverage debt",
                type="group_ai_chat",
                status="running",
                next_run_at=now_value - timedelta(seconds=1),
                type_config={"account_coverage_mode": "all_accounts_daily"},
            )
        )
        session.commit()

    processed, _ = service._drain_task_planner(SessionFactory, limit=5, process_type=None)

    assert processed == 1
    assert built_task_ids == ["task-ai-no-open-actions"]


def test_update_task_settings_restarts_running_task_with_business_clock(monkeypatch):
    SessionFactory = _session_factory()
    beijing_now = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=8)

    monkeypatch.setattr(service, "_now", lambda: beijing_now)

    with SessionFactory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="task-ai-settings-clock",
                tenant_id=1,
                name="天津",
                type="group_ai_chat",
                status="running",
                next_run_at=beijing_now - timedelta(minutes=5),
                account_config={},
                pacing_config={},
                failure_policy={},
                type_config={},
                stats={},
            )
        )
        session.commit()

    with SessionFactory() as session:
        updated = service.update_task_settings(
            session,
            1,
            "task-ai-settings-clock",
            TaskSettingsUpdate(name="天津"),
            "pytest",
        )

        assert updated.status == "running"
        assert updated.next_run_at == beijing_now
