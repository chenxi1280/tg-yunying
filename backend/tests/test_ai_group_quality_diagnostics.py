from __future__ import annotations

import importlib.util
from pathlib import Path
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = PROJECT_ROOT / ".github/scripts/ai_group_quality_diagnostics.py"
pytestmark = pytest.mark.no_postgres


def load_quality_diagnostics_module():
    spec = importlib.util.spec_from_file_location("ai_group_quality_diagnostics", SCRIPT_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def hard_hourly_stats(deficit: int, blockers: dict[str, int] | None = None) -> dict[str, object]:
    return {
        "hard_hourly_target_enabled": True,
        "hard_hourly_planning_deficit": deficit,
        "hard_hourly_last_blockers": blockers or {},
    }


def fake_hard_hourly_task_service(module, *, created: int, woken_ids: list[str]):
    def wake(_session, *, limit):
        assert limit == module.HARD_HOURLY_PLANNER_DRAIN_LIMIT
        return woken_ids

    return SimpleNamespace(
        _wake_hard_hourly_tasks=wake,
        hard_hourly_requires_planning=lambda _session, _task, _now: True,
        _check_stop_conditions=lambda _session, _task: False,
        _planning_backlog_blocked=lambda _session, _task: False,
        build_task_plan=lambda _session, _task: created,
        refresh_task_stats=lambda _session, task: setattr(task, "stats_refreshed", True),
        next_run_after_task=lambda _task: None,
    )


def test_ai_group_quality_diagnostics_blocks_stale_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 9,
                    "stale_count": 1,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                    "samples": [{"account_id": 7, "bucket": "stale"}],
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "task-ai",
            "name": "郑州楼凤",
            "status": "running",
            "desired_count": 10,
            "online_count": 9,
            "non_online_count": 1,
            "samples": [{"account_id": 7, "bucket": "stale"}],
            "stale_count": 1,
            "missing_state_count": 0,
            "blocked_count": 0,
            "relogin_required_count": 0,
            "offline_count": 0,
        }
    ]


def test_ai_group_quality_diagnostics_accepts_fully_online_state():
    module = load_quality_diagnostics_module()

    blockers = module.online_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "online_summary": {
                    "desired_count": 10,
                    "online_count": 10,
                    "stale_count": 0,
                    "missing_state_count": 0,
                    "blocked_count": 0,
                    "relogin_required_count": 0,
                    "offline_count": 0,
                },
            }
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_waits_for_full_active_probe_window():
    module = load_quality_diagnostics_module()

    assert module.ONLINE_SETTLE_SECONDS >= 15 * 60


def test_ai_group_quality_diagnostics_drains_hard_hourly_after_online_gate(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="天津", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)

    def commit():
        session.commits += 1

    class FakeTaskService:
        @staticmethod
        def _wake_hard_hourly_tasks(_session, *, limit):
            assert limit == module.HARD_HOURLY_PLANNER_DRAIN_LIMIT
            return ["hard-ai"]

        @staticmethod
        def hard_hourly_requires_planning(_session, _task, _now):
            return True

        @staticmethod
        def _check_stop_conditions(_session, _task):
            return False

        @staticmethod
        def _planning_backlog_blocked(_session, _task):
            return False

        @staticmethod
        def build_task_plan(_session, _task):
            return 10

        @staticmethod
        def refresh_task_stats(_session, _task):
            _task.stats_refreshed = True

        @staticmethod
        def next_run_after_task(_task):
            return None

    session.commit = commit
    monkeypatch.setattr(module, "task_service", FakeTaskService)
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])
    monkeypatch.setattr(
        module,
        "diagnostic_task_stats",
        lambda _session, _task: {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0},
    )

    result = module.drain_hard_hourly_planner(session)

    assert result == {
        "task_count": 1,
        "attempts": 1,
        "processed": 10,
        "remaining_task_count": 0,
        "remaining_task_ids": [],
        "tasks": [{"task_id": "hard-ai", "name": "天津", "created": 10, "status": "planned"}],
    }
    assert task.stats_refreshed is True
    assert session.commits == 2


def test_ai_group_quality_diagnostics_drains_hard_hourly_until_planning_deficit_clears(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="天津", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)
    stats_queue = [
        {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 2},
        {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 2},
        {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0},
    ]

    class FakeTaskService:
        @staticmethod
        def _wake_hard_hourly_tasks(_session, *, limit):
            assert limit == module.HARD_HOURLY_PLANNER_DRAIN_LIMIT
            return ["hard-ai"]

        @staticmethod
        def hard_hourly_requires_planning(_session, _task, _now):
            return True

        @staticmethod
        def _check_stop_conditions(_session, _task):
            return False

        @staticmethod
        def _planning_backlog_blocked(_session, _task):
            return False

        @staticmethod
        def build_task_plan(_session, _task):
            return 2

        @staticmethod
        def refresh_task_stats(_session, _task):
            _task.stats_refreshed = True

        @staticmethod
        def next_run_after_task(_task):
            return None

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", FakeTaskService)
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])

    def diagnostic_task_stats(_session, _task):
        if stats_queue:
            return stats_queue.pop(0)
        return {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0}

    monkeypatch.setattr(module, "diagnostic_task_stats", diagnostic_task_stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["attempts"] == 2
    assert result["processed"] == 4
    assert result["remaining_task_count"] == 0
    assert [row["created"] for row in result["tasks"]] == [2, 2]
    assert task.stats_refreshed is True


def test_ai_group_quality_diagnostics_drains_hard_hourly_backfill_deficit(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="天津", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)
    stats_queue = [
        {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 0,
            "hard_hourly_backfill_planning_deficit": 2,
        },
        {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 0,
            "hard_hourly_backfill_planning_deficit": 0,
        },
    ]

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", fake_hard_hourly_task_service(module, created=2, woken_ids=[]))
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])

    def diagnostic_task_stats(_session, _task):
        if stats_queue:
            return stats_queue.pop(0)
        return {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 0,
            "hard_hourly_backfill_planning_deficit": 0,
        }

    monkeypatch.setattr(module, "diagnostic_task_stats", diagnostic_task_stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["task_count"] == 0
    assert result["attempts"] == 1
    assert result["processed"] == 2
    assert result["remaining_task_count"] == 0
    assert result["tasks"] == [{"task_id": "hard-ai", "name": "天津", "created": 2, "status": "planned"}]
    assert task.stats_refreshed is True


def test_ai_group_quality_diagnostics_does_not_redrain_structural_hard_hourly_blocker(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="天津", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)
    stats = {
        "hard_hourly_target_enabled": True,
        "hard_hourly_planning_deficit": 2,
        "hard_hourly_last_blockers": {"ai_generation_unavailable": 2},
    }

    class FakeTaskService:
        @staticmethod
        def _wake_hard_hourly_tasks(_session, *, limit):
            assert limit == module.HARD_HOURLY_PLANNER_DRAIN_LIMIT
            return ["hard-ai"]

        @staticmethod
        def hard_hourly_requires_planning(_session, _task, _now):
            return True

        @staticmethod
        def _check_stop_conditions(_session, _task):
            return False

        @staticmethod
        def _planning_backlog_blocked(_session, _task):
            return False

        @staticmethod
        def build_task_plan(_session, _task):
            return 0

        @staticmethod
        def refresh_task_stats(_session, _task):
            _task.stats_refreshed = True

        @staticmethod
        def next_run_after_task(_task):
            return None

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", FakeTaskService)
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])
    monkeypatch.setattr(module, "diagnostic_task_stats", lambda _session, _task: stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["attempts"] == 1
    assert result["processed"] == 0
    assert result["remaining_task_count"] == 0
    assert task.stats_refreshed is True


def test_ai_group_quality_diagnostics_retries_partial_ai_generation_hard_hourly_blocker(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="天津", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)
    stats_queue = [
        {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 1,
            "hard_hourly_last_blockers": {"ai_generation_unavailable": 1},
            "hard_hourly_success_count": 6,
            "hard_hourly_open_count": 3,
            "hard_hourly_overdue_open_count": 0,
        },
        {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0},
    ]

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", fake_hard_hourly_task_service(module, created=1, woken_ids=[]))
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])

    def diagnostic_task_stats(_session, _task):
        if stats_queue:
            return stats_queue.pop(0)
        return {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0}

    monkeypatch.setattr(module, "diagnostic_task_stats", diagnostic_task_stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["task_count"] == 0
    assert result["attempts"] == 1
    assert result["processed"] == 1
    assert result["remaining_task_count"] == 0
    assert result["tasks"] == [{"task_id": "hard-ai", "name": "天津", "created": 1, "status": "planned"}]
    assert task.stats_refreshed is True


def test_ai_group_quality_diagnostics_retries_quality_hard_hourly_blocker(monkeypatch):
    module = load_quality_diagnostics_module()
    task = SimpleNamespace(id="hard-ai", name="石家庄", status="running", next_run_at=None)
    session = SimpleNamespace(commits=0, get=lambda _model, _task_id: task)
    created_queue = [0, 2]
    stats_queue = [
        {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 2,
            "hard_hourly_last_blockers": {"duplicate_message": 2},
        },
        {
            "hard_hourly_target_enabled": True,
            "hard_hourly_planning_deficit": 2,
            "hard_hourly_last_blockers": {"duplicate_message": 2},
        },
        {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0},
    ]

    class FakeTaskService:
        @staticmethod
        def _wake_hard_hourly_tasks(_session, *, limit):
            assert limit == module.HARD_HOURLY_PLANNER_DRAIN_LIMIT
            return ["hard-ai"]

        @staticmethod
        def hard_hourly_requires_planning(_session, _task, _now):
            return True

        @staticmethod
        def _check_stop_conditions(_session, _task):
            return False

        @staticmethod
        def _planning_backlog_blocked(_session, _task):
            return False

        @staticmethod
        def build_task_plan(_session, _task):
            return created_queue.pop(0)

        @staticmethod
        def refresh_task_stats(_session, _task):
            _task.stats_refreshed = True

        @staticmethod
        def next_run_after_task(_task):
            return None

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", FakeTaskService)
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [task])

    def diagnostic_task_stats(_session, _task):
        if stats_queue:
            return stats_queue.pop(0)
        return {"hard_hourly_target_enabled": True, "hard_hourly_planning_deficit": 0}

    monkeypatch.setattr(module, "diagnostic_task_stats", diagnostic_task_stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["attempts"] == 2
    assert result["processed"] == 2
    assert result["remaining_task_count"] == 0
    assert [row["created"] for row in result["tasks"]] == [0, 2]


def test_ai_group_quality_diagnostics_drains_active_retryable_task_when_wake_is_empty(monkeypatch):
    module = load_quality_diagnostics_module()
    active_task = SimpleNamespace(id="active-ai", name="青岛师范学院", status="running", next_run_at=None)
    paused_task = SimpleNamespace(id="paused-ai", name="历史暂停任务", status="paused", next_run_at=None)
    tasks_by_id = {active_task.id: active_task, paused_task.id: paused_task}
    session = SimpleNamespace(commits=0, get=lambda _model, task_id: tasks_by_id.get(task_id))
    stats_queue = [hard_hourly_stats(1, {"duplicate_message": 6}), hard_hourly_stats(0)]

    session.commit = lambda: setattr(session, "commits", session.commits + 1)
    monkeypatch.setattr(module, "task_service", fake_hard_hourly_task_service(module, created=1, woken_ids=[]))
    monkeypatch.setattr(module, "active_group_tasks", lambda _session: [active_task, paused_task])

    def diagnostic_task_stats(_session, task):
        if task.status == "paused":
            return hard_hourly_stats(10, {"duplicate_message": 10})
        if stats_queue:
            return stats_queue.pop(0)
        return hard_hourly_stats(0)

    monkeypatch.setattr(module, "diagnostic_task_stats", diagnostic_task_stats)

    result = module.drain_hard_hourly_planner(session)

    assert result["task_count"] == 0
    assert result["attempts"] == 1
    assert result["processed"] == 1
    assert result["remaining_task_count"] == 0
    assert result["tasks"] == [{"task_id": "active-ai", "name": "青岛师范学院", "created": 1, "status": "planned"}]
    assert active_task.stats_refreshed is True
    assert not hasattr(paused_task, "stats_refreshed")


def test_ai_group_quality_diagnostics_settles_dispatch_lag_after_drain(monkeypatch):
    module = load_quality_diagnostics_module()
    since = datetime(2026, 7, 1, 10, 0, 0)
    session = SimpleNamespace(expired=0)
    snapshots = [["clear"]]
    blocker = {
        "blockers": {"dispatcher_lag": 1},
        "name": "石家庄",
        "goal": 10,
        "success_count": 5,
        "future_open_count": 4,
        "overdue_open_count": 1,
    }

    def expire_all():
        session.expired += 1

    session.expire_all = expire_all
    monkeypatch.setattr(module, "now_local", lambda: since)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "task_snapshots", lambda _session, _since: snapshots.pop(0))
    monkeypatch.setattr(module, "hard_hourly_gate_blockers", lambda value: [blocker] if value == ["blocked"] else [])

    result = module.settle_hard_hourly_gate(session, since, ["blocked"])

    assert result == ["clear"]
    assert session.expired == 1


def test_ai_group_quality_diagnostics_does_not_settle_insufficient_dispatch_lag(monkeypatch):
    module = load_quality_diagnostics_module()
    since = datetime(2026, 7, 1, 10, 0, 0)
    blocker = {
        "blockers": {"dispatcher_lag": 1},
        "name": "石家庄",
        "goal": 10,
        "success_count": 0,
        "future_open_count": 0,
        "overdue_open_count": 1,
    }
    session = SimpleNamespace(expire_all=lambda: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: pytest.fail("should not wait"))
    monkeypatch.setattr(module, "hard_hourly_gate_blockers", lambda _snapshots: [blocker])

    result = module.settle_hard_hourly_gate(session, since, ["blocked"])

    assert result == ["blocked"]


def test_ai_group_quality_diagnostics_settles_backfill_dispatch_lag(monkeypatch):
    module = load_quality_diagnostics_module()
    since = datetime(2026, 7, 1, 10, 0, 0)
    session = SimpleNamespace(expired=0)
    snapshots = [["clear"]]
    blocker = {
        "reason": "hard_hourly_history_missed",
        "name": "天津",
        "backfill_planning_deficit": 0,
        "backfill_delivery_deficit": 3,
    }

    def expire_all():
        session.expired += 1

    session.expire_all = expire_all
    monkeypatch.setattr(module, "now_local", lambda: since)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(module, "task_snapshots", lambda _session, _since: snapshots.pop(0))
    monkeypatch.setattr(module, "hard_hourly_gate_blockers", lambda value: [blocker] if value == ["blocked"] else [])

    result = module.settle_hard_hourly_gate(session, since, ["blocked"])

    assert result == ["clear"]
    assert session.expired == 1


def test_ai_group_quality_diagnostics_does_not_settle_unplanned_backfill():
    module = load_quality_diagnostics_module()
    blocker = {
        "reason": "hard_hourly_history_missed",
        "backfill_planning_deficit": 2,
        "backfill_delivery_deficit": 3,
    }

    assert module._is_dispatch_settle_blocker(blocker) is False


def test_ai_group_quality_diagnostics_does_not_settle_generation_blocker(monkeypatch):
    module = load_quality_diagnostics_module()
    since = datetime(2026, 7, 1, 10, 0, 0)
    blocker = {"blockers": {"ai_generation_unavailable": 6}, "name": "天津"}
    session = SimpleNamespace(expire_all=lambda: None)
    monkeypatch.setattr(module.time, "sleep", lambda _seconds: pytest.fail("should not wait"))
    monkeypatch.setattr(module, "hard_hourly_gate_blockers", lambda _snapshots: [blocker])

    result = module.settle_hard_hourly_gate(session, since, ["blocked"])

    assert result == ["blocked"]


def test_ai_group_quality_diagnostics_blocks_recent_missed_hard_hourly_bucket():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "hard-ai",
                "name": "天津",
                "status": "running",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_bucket": "2026-07-01T21:00:00+08:00",
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 10,
                    "hard_hourly_status": "met",
                    "hard_hourly_recent_buckets": [
                        {
                            "bucket": "2026-07-01T20:00:00+08:00",
                            "goal": 10,
                            "success_count": 6,
                            "future_open_count": 0,
                            "overdue_open_count": 0,
                            "deficit": 4,
                            "planning_deficit": 4,
                            "status": "missed",
                            "blockers": {},
                        },
                        {
                            "bucket": "2026-07-01T21:00:00+08:00",
                            "goal": 10,
                            "success_count": 10,
                            "future_open_count": 0,
                            "overdue_open_count": 0,
                            "deficit": 0,
                            "planning_deficit": 0,
                            "status": "met",
                            "blockers": {},
                        },
                    ],
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "hard-ai",
            "name": "天津",
            "status": "running",
            "missed_bucket_count": 1,
            "missed_deficit": 4,
            "buckets": [
                {
                    "bucket": "2026-07-01T20:00:00+08:00",
                    "goal": 10,
                    "success_count": 6,
                    "deficit": 4,
                    "status": "missed",
                }
            ],
            "reason": "hard_hourly_history_missed",
        }
    ]


def test_ai_group_quality_diagnostics_allows_compensated_hard_hourly_history():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "hard-ai",
                "name": "天津",
                "status": "running",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_bucket": "2026-07-01T21:00:00+08:00",
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 10,
                    "hard_hourly_status": "met",
                    "hard_hourly_backfill_debt": 0,
                    "hard_hourly_backfill_planning_deficit": 0,
                    "hard_hourly_recent_buckets": [
                        {
                            "bucket": "2026-07-01T20:00:00+08:00",
                            "goal": 10,
                            "success_count": 6,
                            "deficit": 4,
                            "status": "missed",
                        }
                    ],
                },
            }
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_formats_online_failure_row():
    module = load_quality_diagnostics_module()
    now = datetime(2026, 6, 30, 12, 0, 0)
    state = SimpleNamespace(
        account_id=42,
        online_status="offline",
        failure_type="account_unavailable",
        failure_detail="账号没有可用 session，需要重新登录",
        last_probe_at=now - timedelta(minutes=1),
        next_probe_at=now + timedelta(minutes=2),
        stale_after_at=now + timedelta(minutes=9),
    )
    account = SimpleNamespace(display_name="账号42", status="会话过期", health_score=0)

    row = module._online_failure_row(state, account, now)

    assert row == {
        "account_id": 42,
        "display_name": "账号42",
        "account_status": "会话过期",
        "health_score": 0,
        "bucket": "offline",
        "online_status": "offline",
        "failure_type": "account_unavailable",
        "failure_detail": "账号没有可用 session，需要重新登录",
        "last_probe_at": now - timedelta(minutes=1),
        "next_probe_at": now + timedelta(minutes=2),
        "stale_after_at": now + timedelta(minutes=9),
    }


def test_ai_group_quality_diagnostics_blocks_recent_effective_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a2", status="pending", payload={"message_text": "嫩是真嫩 就是不知道稳不稳"}),
        SimpleNamespace(id="a3", status="failed", payload={"message_text": "没发送成功不用阻断"}),
        SimpleNamespace(id="a4", status="skipped", payload={"message_text": "没发送成功不用阻断"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["duplicate_blockers"] == [
        {
            "text": "嫩是真嫩 就是不知道稳不稳",
            "effective_count": 2,
            "status_counts": {"pending": 1, "success": 1},
            "action_ids": ["a1", "a2"],
        }
    ]


def test_ai_group_quality_diagnostics_blocks_missing_human_quality_payload():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(
            id="a1",
            status="pending",
            account_id=11,
            payload={
                "message_text": "花花老师这个接话还行",
                "account_voice_profile_version": 0,
                "ai_message_memory_id": "",
                "human_quality_decision": "",
                "generation_source": "",
                "act_type": "",
            },
        ),
        SimpleNamespace(
            id="a2",
            status="success",
            account_id=12,
            payload={
                "message_text": "我先看看反馈",
                "account_voice_profile_version": 2,
                "ai_message_memory_id": "memory-2",
                "human_quality_decision": "accepted",
                "generation_source": "ai",
                "act_type": "short_react",
            },
        ),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["quality_payload_blockers"] == [
        {
            "action_id": "a1",
            "account_id": 11,
            "status": "pending",
            "missing_fields": [
                "account_voice_profile_version",
                "ai_message_memory_id",
                "human_quality_decision",
                "generation_source",
                "act_type",
            ],
            "text": "花花老师这个接话还行",
        }
    ]


def test_ai_group_quality_diagnostics_reports_material_trace_samples():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(
            id="a1",
            status="success",
            account_id=11,
            scheduled_at=None,
            executed_at=None,
            payload={
                "message_text": "这个表情包挺合适",
                "rule_trace": {
                    "material_intent": "表情包:围观",
                    "material_matched_tags": ["围观", "吃瓜"],
                    "material_candidate_count": 3,
                    "material_ok": True,
                    "material_id": 88,
                    "material_failure_reason": "",
                },
            },
        ),
        SimpleNamespace(
            id="a2",
            status="success",
            account_id=12,
            scheduled_at=None,
            executed_at=None,
            payload={
                "message_text": "想配图但是没找到",
                "rule_trace": {
                    "material_intent": "表情包:疑问",
                    "material_matched_tags": [],
                    "material_candidate_count": 0,
                    "material_ok": False,
                    "material_id": None,
                    "material_failure_reason": "没有匹配可用素材",
                },
            },
        ),
        SimpleNamespace(
            id="a3",
            status="success",
            account_id=13,
            scheduled_at=None,
            executed_at=None,
            payload={"message_text": "普通文本"},
        ),
    ]

    samples = module.material_trace_samples(actions)
    action_samples = module.action_samples(actions)

    assert samples == [
        {
            "action_id": "a1",
            "status": "success",
            "account_id": 11,
            "material_intent": "表情包:围观",
            "material_matched_tags": ["围观", "吃瓜"],
            "material_candidate_count": 3,
            "material_ok": True,
            "material_id": 88,
            "material_failure_reason": "",
            "text": "这个表情包挺合适",
        },
        {
            "action_id": "a2",
            "status": "success",
            "account_id": 12,
            "material_intent": "表情包:疑问",
            "material_matched_tags": [],
            "material_candidate_count": 0,
            "material_ok": False,
            "material_id": None,
            "material_failure_reason": "没有匹配可用素材",
            "text": "想配图但是没找到",
        }
    ]
    assert action_samples[0]["material_intent"] == "表情包:围观"
    assert action_samples[0]["material_matched_tags"] == ["围观", "吃瓜"]
    assert action_samples[0]["material_candidate_count"] == 3
    assert action_samples[0]["material_id"] == 88
    assert action_samples[0]["material_failure_reason"] == ""
    assert action_samples[1]["material_id"] is None
    assert action_samples[1]["material_failure_reason"] == "没有匹配可用素材"
    assert action_samples[2]["material_intent"] == ""


def test_ai_group_quality_diagnostics_reports_success_only_duplicates_without_blocking():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="success", payload={"message_text": "已发历史重复"}),
        SimpleNamespace(id="a2", status="success", payload={"message_text": "已发历史重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["sent_duplicate_observations"] == [
        {
            "text": "已发历史重复",
            "sent_count": 2,
            "status_counts": {"success": 2},
            "action_ids": ["a1", "a2"],
        }
    ]
    assert snapshot["duplicate_blockers"] == []


def test_ai_group_quality_diagnostics_ignores_failed_only_duplicate_text():
    module = load_quality_diagnostics_module()
    actions = [
        SimpleNamespace(id="a1", status="failed", payload={"message_text": "失败文本重复"}),
        SimpleNamespace(id="a2", status="skipped", payload={"message_text": "失败文本重复"}),
    ]

    snapshot = module.recent_action_duplicate_summary(actions)

    assert snapshot["repeated_texts"] == [{"text": "失败文本重复", "count": 2}]
    assert snapshot["duplicate_blockers"] == []


def test_ai_group_quality_diagnostics_blocks_unmet_hard_hourly_target():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "task-ai",
                "name": "郑州楼凤",
                "status": "running",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 7,
                    "hard_hourly_open_count": 2,
                    "hard_hourly_overdue_open_count": 1,
                    "hard_hourly_deficit": 3,
                    "hard_hourly_status": "blocked",
                    "hard_hourly_bucket": "2026-07-01T15:00:00+08:00",
                    "hard_hourly_last_blockers": {"dispatcher_lag": 1},
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "task-ai",
            "name": "郑州楼凤",
            "status": "running",
            "bucket": "2026-07-01T15:00:00+08:00",
            "goal": 10,
            "success_count": 7,
            "future_open_count": 2,
            "overdue_open_count": 1,
            "deficit": 3,
            "planning_deficit": 0,
            "hard_hourly_status": "blocked",
            "blockers": {"dispatcher_lag": 1},
            "reason": "hard_hourly_not_met",
        }
    ]


def test_ai_group_quality_diagnostics_ignores_paused_or_disabled_hard_hourly_target():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "paused-ai",
                "name": "暂停任务",
                "status": "paused",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 0,
                    "hard_hourly_deficit": 10,
                    "hard_hourly_status": "missed",
                },
            },
            {
                "task_id": "normal-ai",
                "name": "未开启硬目标",
                "status": "running",
                "stats": {"hard_hourly_target_enabled": False},
            },
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_allows_queued_current_hour_catchup():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "queued-ai",
                "name": "郑州楼凤",
                "status": "running",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 5,
                    "hard_hourly_open_count": 5,
                    "hard_hourly_overdue_open_count": 0,
                    "hard_hourly_deficit": 5,
                    "hard_hourly_status": "catching_up",
                },
            }
        ]
    )

    assert blockers == []


def test_ai_group_quality_diagnostics_blocks_unqueued_current_hour_catchup():
    module = load_quality_diagnostics_module()

    blockers = module.hard_hourly_gate_blockers(
        [
            {
                "task_id": "unqueued-ai",
                "name": "天津",
                "status": "running",
                "stats": {
                    "hard_hourly_target_enabled": True,
                    "hard_hourly_goal": 10,
                    "hard_hourly_success_count": 0,
                    "hard_hourly_open_count": 0,
                    "hard_hourly_overdue_open_count": 0,
                    "hard_hourly_deficit": 10,
                    "hard_hourly_planning_deficit": 10,
                    "hard_hourly_status": "catching_up",
                },
            }
        ]
    )

    assert blockers == [
        {
            "task_id": "unqueued-ai",
            "name": "天津",
            "status": "running",
            "bucket": "",
            "goal": 10,
            "success_count": 0,
            "future_open_count": 0,
            "overdue_open_count": 0,
            "deficit": 10,
            "planning_deficit": 10,
            "hard_hourly_status": "catching_up",
            "blockers": {},
            "reason": "hard_hourly_not_met",
        }
    ]
