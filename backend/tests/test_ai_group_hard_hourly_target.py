from __future__ import annotations

import importlib.util
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    GroupContextMessage,
    OperationTarget,
    RuleSet,
    RuleSetVersion,
    SchedulingSetting,
    Task,
    TaskAccountDailyCoverage,
    Tenant,
    TgAccount,
    TgAccountOnlineState,
    TgGroup,
    TgGroupAccount,
)
from app.schemas import GroupAIChatTaskCreate, TaskPrecheckRequest
from app.services.task_center.executors import prepare_open_actions_for_planning
from app.services.task_center.executors.group_ai_chat import (
    _next_cycle_index,
    _online_ready_accounts,
    _select_accounts_for_plan,
    build_plan as build_group_ai_chat_plan,
)
from app.services.task_center.hard_hourly import (
    current_progress as hard_hourly_current_progress,
    hard_schedule_times,
    requires_planning as hard_hourly_requires_planning,
)
from app.services.task_center.service import (
    _merge_planner_task_ids,
    _wake_hard_hourly_tasks,
    create_group_ai_chat_task,
    list_tasks,
    precheck_task_creation,
)
from app.services.task_center.stats import next_run_after_task, refresh_task_stats
from app.timezone import BEIJING_TZ
from tests.ai_group_voice_profile_fixtures import assume_default_ai_group_voice_profiles

FIRST_PROFILE_READY_ACCOUNT_ID = 11
PROFILE_REFILL_ACCOUNT_TOTAL = 20
PROFILE_REFILL_ONLINE_GAP_ACCOUNT_TOTAL = 30
PROFILE_REFILL_HOURLY_GOAL = 6


def _send_action(
    action_id: str,
    task: Task,
    status: str,
    *,
    account_id: int | None = None,
    scheduled_at: datetime | None = None,
    executed_at: datetime | None = None,
) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        status=status,
        scheduled_at=scheduled_at,
        executed_at=executed_at,
    )


def _online_state(account_id: int, now: datetime) -> TgAccountOnlineState:
    return TgAccountOnlineState(
        tenant_id=1,
        account_id=account_id,
        desired_online=True,
        online_status="online",
        stale_after_at=now + timedelta(minutes=5),
    )


@pytest.fixture(autouse=True)
def assume_group_ai_accounts_ready_for_hard_hourly_tests(monkeypatch):
    assume_default_ai_group_voice_profiles(monkeypatch)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.online_ready_account_ids_for_planning",
        lambda _session, *, tenant_id, accounts, now=None: {account.id for account in accounts},
    )


def _forbid_planner_external_work(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("planner phase must not call AI or collect remote context")

    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_messages", fail)
    monkeypatch.setattr("app.services.task_center.ai_generator.generate_group_reply_messages", fail)
    monkeypatch.setattr("app.services.group_listeners.collect_group_context", fail)


def _voice_profiles_after_first_ten_accounts(_session, *, tenant_id: int, account_ids: list[int]):
    return {
        int(account_id): {"version": 1, "summary": f"账号{int(account_id)}短句，偶尔追问"}
        for account_id in account_ids
        if int(account_id) >= FIRST_PROFILE_READY_ACCOUNT_ID
    }


def _add_hard_hourly_profile_refill_fixture(session: Session, *, account_total: int = PROFILE_REFILL_ACCOUNT_TOTAL) -> None:
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
    for account_id in range(1, account_total + 1):
        session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))


def _add_ai_group_rule_binding(session: Session) -> None:
    session.add(
        RuleSet(
            id=21,
            tenant_id=1,
            name="AI活群默认规则",
            task_types=["group_ai_chat"],
            active_version_id=31,
        )
    )
    session.add(RuleSetVersion(id=31, tenant_id=1, rule_set_id=21, version=1, status="published"))


def _add_ready_group_accounts(session: Session, *, group_id: int, account_ids: list[int]) -> None:
    for account_id in account_ids:
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"账号{account_id}",
                phone_masked=str(account_id),
                status="在线",
                session_ciphertext=f"session-{account_id}",
            )
        )
        session.add(TgGroupAccount(tenant_id=1, group_id=group_id, account_id=account_id, can_send=True))


def _hard_hourly_memory_rotation_task() -> Task:
    return Task(
        id="ai-hard-hourly-memory-rotation",
        tenant_id=1,
        name="硬目标记忆不压制轮转",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
        type_config={
            "target_group_id": 7,
            "participation_rate": 1,
            "participation_jitter": 0,
            "fact_anchor_required": False,
            "low_confidence_silence_enabled": False,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 2,
            "hard_hourly_strategy": "force_planning",
            "rule_set_version_id": 31,
        },
    )


def _hard_hourly_profile_refill_task(*, max_concurrent: int = PROFILE_REFILL_ACCOUNT_TOTAL) -> Task:
    return Task(
        id="ai-hard-hourly-profile-refill",
        tenant_id=1,
        name="硬目标面具扩池",
        type="group_ai_chat",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": max_concurrent, "cooldown_per_account_minutes": 0},
        type_config={
            "target_group_id": 7,
            "reply_min_per_round": 0,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": PROFILE_REFILL_HOURLY_GOAL,
            "hard_hourly_strategy": "force_planning",
            "fact_anchor_required": False,
            "low_confidence_silence_enabled": False,
        },
    )


def _load_hard_target_migration():
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0057_ai_group_hard_target_300.py"
    spec = importlib.util.spec_from_file_location("migration_0057_ai_group_hard_target_300", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hard_target_60_migration():
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0059_ai_group_hard_target_60.py"
    spec = importlib.util.spec_from_file_location("migration_0059_ai_group_hard_target_60", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_hard_target_10_migration():
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0066_ai_group_hard_target_10.py"
    spec = importlib.util.spec_from_file_location("migration_0066_ai_group_hard_target_10", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_group_ai_chat_create_persists_hard_hourly_target_config():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(
                name="硬目标 AI 活跃群",
                target_group_id=7,
                hard_hourly_target_enabled=True,
                hourly_min_messages=360,
                hard_hourly_strategy="force_planning",
            ),
            actor="tester",
        )

    assert task.type_config["hard_hourly_target_enabled"] is True
    assert task.type_config["hourly_min_messages"] == 360
    assert task.type_config["hard_hourly_strategy"] == "force_planning"


def test_ai_group_hard_target_migration_repairs_target_and_stale_stats():
    migration = _load_hard_target_migration()
    config = migration._hard_hourly_config(
        {
            "target_operation_target_id": "9",
            "target_group_name": "旧群名",
            "hourly_min_messages": 500,
        },
        2,
        {(1, 9): "青岛师范学院", (2, 9): "天津音乐学院"},
    )
    stats = migration._hard_hourly_stats(
        {
            "hard_hourly_status": "disabled",
            "hard_hourly_goal": 20,
            "hard_hourly_deficit": 12,
            "hard_hourly_next_check_at": "2026-06-08T23:50:00",
        },
        config["hourly_min_messages"],
    )

    assert config["target_group_name"] == "天津音乐学院"
    assert config["hard_hourly_target_enabled"] is True
    assert config["hourly_min_messages"] == 500
    assert stats["hard_hourly_status"] == "catching_up"
    assert stats["hard_hourly_goal"] == 500
    assert "hard_hourly_deficit" not in stats
    assert "hard_hourly_next_check_at" not in stats


def test_ai_group_hard_target_60_migration_lowers_only_old_default():
    migration = _load_hard_target_60_migration()
    current_time = datetime(2026, 6, 14, 12, 0)

    values = migration._task_update_values(
        {"hard_hourly_target_enabled": True, "hourly_min_messages": 300},
        {
            "hard_hourly_goal": 300,
            "hard_hourly_deficit": 240,
            "hard_hourly_next_check_at": "2026-06-14T12:01:00",
            "hard_hourly_status": "catching_up",
        },
        current_time,
    )
    manual_values = migration._task_update_values(
        {"hard_hourly_target_enabled": True, "hourly_min_messages": 360},
        {"hard_hourly_goal": 360},
        current_time,
    )

    assert values["type_config"]["hourly_min_messages"] == 60
    assert values["stats"]["hard_hourly_goal"] == 60
    assert "hard_hourly_deficit" not in values["stats"]
    assert "hard_hourly_next_check_at" not in values["stats"]
    assert manual_values is None


@pytest.mark.no_postgres
def test_ai_group_hard_target_10_migration_lowers_only_old_default():
    migration = _load_hard_target_10_migration()
    current_time = datetime(2026, 6, 28, 9, 0)

    values = migration._task_update_values(
        {"hard_hourly_target_enabled": True, "hourly_min_messages": 60},
        {
            "hard_hourly_goal": 60,
            "hard_hourly_deficit": 50,
            "hard_hourly_next_check_at": "2026-06-28T09:01:00",
            "hard_hourly_status": "catching_up",
        },
        current_time,
    )
    manual_values = migration._task_update_values(
        {"hard_hourly_target_enabled": True, "hourly_min_messages": 100},
        {"hard_hourly_goal": 100},
        current_time,
    )

    assert values["type_config"]["hourly_min_messages"] == 10
    assert values["stats"]["hard_hourly_goal"] == 10
    assert "hard_hourly_deficit" not in values["stats"]
    assert "hard_hourly_next_check_at" not in values["stats"]
    assert manual_values is None


def test_ai_group_task_list_prefers_authoritative_target_title():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                OperationTarget(id=9, tenant_id=1, target_type="group", tg_peer_id="-1009", title="天津音乐学院"),
                Task(
                    id="task-stale-target-name",
                    tenant_id=1,
                    name="青岛师范学院",
                    type="group_ai_chat",
                    status="running",
                    type_config={
                        "target_operation_target_id": 9,
                        "target_group_name": "青岛师范学院",
                        "hard_hourly_target_enabled": True,
                        "hourly_min_messages": 300,
                    },
                ),
            ]
        )
        session.commit()

        [task] = list_tasks(session, 1, task_type="group_ai_chat")

    assert task["target_summary"] == "天津音乐学院"
    assert task["name"] == "青岛师范学院"


@pytest.mark.no_postgres
def test_group_ai_chat_create_rejects_disabled_or_low_hard_hourly_target():
    with pytest.raises(ValueError, match="必须启用每小时硬目标"):
        GroupAIChatTaskCreate(
            name="关闭硬目标",
            target_group_id=7,
            hard_hourly_target_enabled=False,
        )
    with pytest.raises(ValueError, match="不能低于 10"):
        GroupAIChatTaskCreate(
            name="低硬目标",
            target_group_id=7,
            hard_hourly_target_enabled=True,
            hourly_min_messages=9,
        )


@pytest.mark.no_postgres
def test_group_ai_chat_create_defaults_to_hard_hourly_target_10():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        task = create_group_ai_chat_task(
            session,
            1,
            GroupAIChatTaskCreate(name="默认硬目标 AI 活跃群", target_group_id=7),
            actor="tester",
        )

    assert task.type_config["hard_hourly_target_enabled"] is True
    assert task.type_config["hourly_min_messages"] == 10
    assert task.type_config["hard_hourly_strategy"] == "force_planning"


def test_hard_hourly_wake_includes_legacy_string_enabled(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-string-enabled",
            tenant_id=1,
            name="硬目标历史字符串配置",
            type="group_ai_chat",
            status="running",
            priority=3,
            next_run_at=now_value + timedelta(hours=1),
            type_config={
                "hard_hourly_target_enabled": "true",
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_next_check_at": (now_value - timedelta(minutes=1)).isoformat()},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=10)

    assert task_ids == ["task-hard-hourly-string-enabled"]


def test_hard_hourly_wake_returns_due_batch_when_worker_limit_is_low(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        tasks = [
            Task(
                id=f"task-hard-hourly-due-{index}",
                tenant_id=1,
                name=f"硬目标待唤醒{index}",
                type="group_ai_chat",
                status="running",
                priority=3,
                next_run_at=now_value + timedelta(hours=1),
                type_config={
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 300,
                    "hard_hourly_strategy": "force_planning",
                },
                stats={"hard_hourly_next_check_at": (now_value - timedelta(minutes=1)).isoformat()},
            )
            for index in range(3)
        ]
        session.add_all([Tenant(id=1, name="默认运营空间"), *tasks])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=1)

    assert task_ids == ["task-hard-hourly-due-0", "task-hard-hourly-due-1", "task-hard-hourly-due-2"]


def test_hard_hourly_wake_prioritizes_stale_due_tasks_over_fixed_order(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        recent_due_at = (now_value - timedelta(minutes=1)).isoformat()
        stale_due_at = (now_value - timedelta(minutes=30)).isoformat()
        filler_tasks = [
            Task(
                id=f"task-hard-hourly-filler-{index:03d}",
                tenant_id=1,
                name=f"硬目标固定顺序{index}",
                type="group_ai_chat",
                status="running",
                priority=1,
                next_run_at=now_value + timedelta(hours=1),
                type_config={
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 300,
                    "hard_hourly_strategy": "force_planning",
                },
                stats={"hard_hourly_next_check_at": recent_due_at},
            )
            for index in range(120)
        ]
        stale_task = Task(
            id="task-hard-hourly-stale-critical",
            tenant_id=1,
            name="硬目标过期关键任务",
            type="group_ai_chat",
            status="running",
            priority=9,
            next_run_at=now_value + timedelta(hours=1),
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_next_check_at": stale_due_at},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), *filler_tasks, stale_task])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=100)

    assert len(task_ids) == 100
    assert task_ids[0] == "task-hard-hourly-stale-critical"


def test_merge_planner_task_ids_preserves_hard_hourly_primary_over_limit():
    task_ids = _merge_planner_task_ids(["hard-1", "hard-2", "hard-3"], ["hard-2", "normal-1"], limit=1)

    assert task_ids == ["hard-1", "hard-2", "hard-3"]


@pytest.mark.no_postgres
def test_refresh_task_stats_calculates_hard_hourly_target_progress(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30, tzinfo=BEIJING_TZ)
    hour_start = datetime(2026, 6, 7, 20, 0, tzinfo=BEIJING_TZ)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-stats",
            tenant_id=1,
            name="硬目标统计",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 5,
                "hard_hourly_strategy": "force_planning",
            },
            stats={
                "hard_hourly_last_check_at": "2026-06-07T20:20:00",
                "hard_hourly_last_blockers": {"account_capacity": 1},
                "hard_hourly_last_planned_count": 2,
            },
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("success-aware", task, "success", executed_at=hour_start + timedelta(minutes=5)),
                _send_action("success-naive", task, "success", executed_at=datetime(2026, 6, 7, 20, 10)),
                _send_action("future-open", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 45)),
                _send_action("overdue-open", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 15)),
                _send_action("old-success", task, "success", executed_at=datetime(2026, 6, 7, 19, 15)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_target_enabled"] is True
    assert stats["hard_hourly_goal"] == 5
    assert stats["hard_hourly_success_count"] == 2
    assert stats["hard_hourly_open_count"] == 1
    assert stats["hard_hourly_overdue_open_count"] == 1
    assert stats["hard_hourly_deficit"] == 3
    assert stats["hard_hourly_planning_deficit"] == 2
    assert stats["hard_hourly_status"] == "blocked"
    assert stats["hard_hourly_last_blockers"] == {"dispatcher_lag": 1}
    assert stats["hard_hourly_pipeline"] == {
        "membership": "ready",
        "verification": "ready",
        "can_send": "ready",
        "ai_draft": "ready",
        "dispatcher": "blocked",
        "hourly_target": "blocked",
    }
    assert stats["hard_hourly_bucket"] == "2026-06-07T20:00:00+08:00"
    assert stats["hard_hourly_last_check_at"] == "2026-06-07T20:20:00"
    buckets = stats["hard_hourly_recent_buckets"]
    current_bucket = next(item for item in buckets if item["bucket"] == "2026-06-07T20:00:00+08:00")
    previous_bucket = next(item for item in buckets if item["bucket"] == "2026-06-07T19:00:00+08:00")
    assert current_bucket["future_open_count"] == 1
    assert current_bucket["overdue_open_count"] == 1
    assert previous_bucket["status"] == "missed"


@pytest.mark.no_postgres
def test_hard_hourly_stats_exclude_actions_from_a_different_tenant(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30, tzinfo=BEIJING_TZ)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-tenant",
            tenant_id=1,
            name="租户隔离",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 1},
        )
        foreign = _send_action("foreign-success", task, "success", executed_at=now_value)
        foreign.tenant_id = 2
        session.add_all([Tenant(id=1, name="默认运营空间"), task, foreign])
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_success_count"] == 0


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_target_creates_deficit_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_online_state._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in [101, 102, 103, 104, 105]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-plan",
            tenant_id=1,
            name="硬目标补量",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0, "max_actions_per_hour": 1},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 5,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at.asc())))

    assert created == 5
    assert len(actions) == 5
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["message_text"] == "" for action in actions)
    assert all(action.payload["hard_hourly_target"] is True for action in actions)
    assert all(action.payload["hard_hourly_bucket"] == "2026-06-07T20:00:00+08:00" for action in actions)
    assert all(action.payload["hard_hourly_deficit_at_plan"] == 5 for action in actions)
    assert max(action.scheduled_at for action in actions) < datetime(2026, 6, 7, 21, 0)


@pytest.mark.no_postgres
@pytest.mark.parametrize("wait_for_context", [False, True])
def test_group_ai_chat_all_accounts_daily_coverage_plans_uncovered_accounts_when_reply_targets_are_missing(
    monkeypatch,
    wait_for_context,
):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.daily_coverage._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_online_state._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._should_wait_for_human_context",
        lambda *_args, **_kwargs: wait_for_context,
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="全账号覆盖群", auth_status="已授权运营"))
        for account_id in [101, 102, 103, 104]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", health_score=95, session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
            session.add(_online_state(account_id, now_value))
        task = Task(
            id="ai-all-accounts-daily",
            tenant_id=1,
            name="全账号日覆盖",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 4, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0, "max_actions_per_hour": 24},
            type_config={
                "target_group_id": 7,
                "account_coverage_mode": "all_accounts_daily",
                "per_account_daily_min_messages": 1,
                "per_account_daily_max_messages": 2,
                "coverage_window_hours": 24,
                "messages_per_round_mode": "manual",
                "messages_per_round": 1,
                "participation_rate": 0.25,
                "participation_jitter": 0,
                "allow_account_repeat": False,
                "reply_min_per_round": 1,
                "idle_continuation_enabled": False,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": False,
            },
            stats={},
        )
        session.add(task)
        session.add_all([
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                group_id=7,
                    account_id=account_id,
                    coverage_date=now_value.date(),
                    state="ready",
                    targeted_at=now_value,
            )
            for account_id in [101, 102, 103, 104]
        ])
        session.commit()
        statements: list[str] = []
        event.listen(
            engine,
            "before_cursor_execute",
            lambda _conn, _cursor, statement, _parameters, _context, _executemany: statements.append(statement),
        )

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.account_id.asc())))

    assert created == 1, (task.last_error, task.stats)
    assert [action.account_id for action in actions] == [101]
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["message_text"] == "" for action in actions)
    assert all(action.payload["account_coverage_mode"] == "all_accounts_daily" for action in actions)
    assert all(action.payload["coverage_window_date"] == "2026-06-07" for action in actions)
    assert all(action.payload["coverage_target_per_account"] == 1 for action in actions)
    assert all(action.payload["coverage_account_completed_before_action"] == 0 for action in actions)
    assert all(action.payload["coverage_account_remaining_before_action"] == 1 for action in actions)
    assert all(action.payload["coverage_reason"] == "daily_account_coverage" for action in actions)
    assert all(not action.payload.get("reply_to_message_id") for action in actions)
    assert task.stats["coverage_reply_shortfall_cycle_count"] == 1
    assert task.stats["daily_coverage_next_check_at"] == "2026-06-07T20:12:00"
    assert not any("UPDATE tg_account_online_state" in statement for statement in statements)


@pytest.mark.no_postgres
def test_daily_coverage_scans_past_offline_leading_accounts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.online_ready_account_ids_for_planning",
        lambda *_args, **_kwargs: {105},
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        group = TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="覆盖群", auth_status="已授权运营")
        session.add(group)
        _add_ready_group_accounts(session, group_id=7, account_ids=[101, 102, 103, 104, 105])
        task = Task(
            id="daily-online-scan",
            tenant_id=1,
            name="在线扩池",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 1, "cooldown_per_account_minutes": 0},
            type_config={"target_group_id": 7, "account_coverage_mode": "all_accounts_daily"},
        )
        session.add(task)
        session.add_all([
            TaskAccountDailyCoverage(
                tenant_id=1, task_id=task.id, group_id=7, account_id=account_id,
                coverage_date=now_value.date(), state="ready", targeted_at=now_value,
            )
            for account_id in [101, 102, 103, 104, 105]
        ])
        session.commit()
        rows = list(session.scalars(select(TaskAccountDailyCoverage).where(TaskAccountDailyCoverage.task_id == task.id)))
        selected = _select_accounts_for_plan(session, task, group, {}, task.type_config, coverage_rows=rows)
        ready = _online_ready_accounts(session, task, selected, {})

    assert [account.id for account in selected] == [101, 102, 103, 104, 105]
    assert [account.id for account in ready] == [105]


@pytest.mark.no_postgres
def test_group_ai_chat_all_accounts_daily_coverage_keeps_uncovered_before_memory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 23, 0)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.daily_coverage._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_online_state._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="全账号覆盖群", auth_status="已授权运营"))
        for account_id in [101, 102, 103, 104]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", health_score=95, session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
            session.add(_online_state(account_id, now_value))
        task = Task(
            id="ai-coverage-memory-priority",
            tenant_id=1,
            name="覆盖优先不被记忆打断",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 4, "cooldown_per_account_minutes": 0},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0, "max_actions_per_hour": 24},
            type_config={
                "target_group_id": 7,
                "account_coverage_mode": "all_accounts_daily",
                "per_account_daily_min_messages": 1,
                "per_account_daily_max_messages": 2,
                "coverage_window_hours": 24,
                "messages_per_round_mode": "manual",
                "messages_per_round": 2,
                "participation_rate": 1,
                "participation_jitter": 0,
                "allow_account_repeat": False,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": False,
            },
            stats={"force_bootstrap_once": True},
        )
        session.add(task)
        session.add_all([
            TaskAccountDailyCoverage(
                tenant_id=1,
                task_id=task.id,
                    group_id=7,
                    account_id=account_id,
                    coverage_date=now_value.date(),
                    confirmed_count=1 if account_id in {102, 104} else 0,
                    state="confirmed" if account_id in {102, 104} else "ready",
                    targeted_at=now_value,
            )
            for account_id in [101, 102, 103, 104]
        ])
        for account_id in [102, 104]:
            session.add(
                Action(
                    id=f"covered-memory-{account_id}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="group_ai_chat",
                    action_type="send_message",
                    account_id=account_id,
                    status="success",
                    scheduled_at=now_value,
                    executed_at=now_value,
                    payload={"message_text": f"账号{account_id}历史发言"},
                )
            )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = [
            action
            for action in session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.created_at.asc(), Action.id.asc()))
            if action.status == "pending"
        ]

    assert created == 2, (
        task.last_error,
        task.stats,
        [(action.account_id, action.payload.get("coverage_ledger_id")) for action in actions],
    )
    assert [action.account_id for action in actions] == [101, 103]
    assert all(action.payload["coverage_account_remaining_before_action"] == 1 for action in actions)
    assert all(action.payload["coverage_reason"] == "daily_account_coverage" for action in actions)


def test_group_ai_chat_hard_hourly_target_plans_large_deficit_in_batches(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 111):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-large-deficit",
            tenant_id=1,
            name="硬目标大缺口",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 300
    assert len(actions) == 300
    assert task.stats["hard_hourly_last_planned_count"] == 300
    assert task.stats["hard_hourly_next_check_at"] == "2026-06-07T20:10:30"
    assert all(action.payload["hard_hourly_deficit_at_plan"] == 300 for action in actions)
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["message_text"] == "" for action in actions)


def test_group_ai_chat_hard_hourly_ignores_configured_round_size_for_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 161):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-configured-round-size",
            tenant_id=1,
            name="硬目标按配置批量",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 100, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 60,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 300
    assert len(actions) == 300
    assert task.stats["hard_hourly_last_planned_count"] == 300
    assert all(action.payload["hard_hourly_target"] is True for action in actions)
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)


def test_hard_hourly_schedule_uses_remaining_deficit_for_batch_spacing():
    now_value = datetime(2026, 6, 7, 20, 10)
    task = Task(
        id="task-hard-hourly-schedule-deficit",
        tenant_id=1,
        name="硬目标排期按缺口分配",
        type="group_ai_chat",
        status="running",
        timezone="Asia/Shanghai",
        type_config={
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 300,
            "hard_hourly_strategy": "force_planning",
        },
    )

    times = hard_schedule_times(10, task, now_value, target_total=300)

    assert len(times) == 10
    assert times[0] == now_value
    assert times[1] - times[0] <= timedelta(seconds=3)
    assert times[-1] <= now_value + timedelta(seconds=30)
    assert max(times) < datetime(2026, 6, 7, 21, 0)


def test_hard_hourly_schedule_frontloads_when_bucket_cannot_be_evenly_spaced():
    now_value = datetime(2026, 6, 7, 20, 59, 50)
    task = Task(
        id="task-hard-hourly-schedule-frontload",
        tenant_id=1,
        name="硬目标临近整点立即排期",
        type="group_ai_chat",
        status="running",
        timezone="Asia/Shanghai",
        type_config={
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 300,
            "hard_hourly_strategy": "force_planning",
        },
    )

    times = hard_schedule_times(30, task, now_value, target_total=300)

    assert times == [now_value for _ in range(30)]


def test_group_ai_chat_hard_hourly_reuses_selected_accounts_when_front_accounts_are_full(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 201):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-scan-beyond-front-accounts",
            tenant_id=1,
            name="硬目标扫过前排满额账号",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 300,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.flush()
        session.add_all(
            _send_action(
                f"front-account-full-{account_id}",
                task,
                "pending",
                account_id=account_id,
                scheduled_at=now_value + timedelta(minutes=5),
            )
            for account_id in range(101, 181)
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.account_id.asc())))

    planned_account_ids = [int(action.account_id) for action in actions if (action.payload or {}).get("hard_hourly_target")]
    planned_counts = Counter(planned_account_ids)
    assert created == 220
    assert set(planned_account_ids) == set(range(101, 201))
    assert planned_counts[101] == 2
    assert planned_counts[180] == 2
    assert planned_counts[181] == 3
    assert planned_counts[200] == 3
    assert task.stats["hard_hourly_last_planned_count"] == 220
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_uses_current_slot_when_account_cools_down_later(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, jitter_min_seconds=0, jitter_max_seconds=0, default_account_cooldown_seconds=120))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线", session_ciphertext="session-101"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        task = Task(
            id="ai-hard-hourly-later-capacity",
            tenant_id=1,
            name="硬目标稍后可用",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.flush()
        recent_at = now_value - timedelta(seconds=60)
        session.add(_send_action("recent-success", task, "success", account_id=101, scheduled_at=recent_at, executed_at=recent_at))
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.scheduled_at.asc())))

    hard_actions = [action for action in actions if (action.payload or {}).get("hard_hourly_target")]
    assert created == 2
    assert [action.account_id for action in hard_actions] == [101, 101]
    assert hard_actions[0].scheduled_at == datetime(2026, 6, 7, 20, 10)
    assert max(action.scheduled_at for action in hard_actions) < datetime(2026, 6, 7, 21, 0)
    assert task.last_error == ""
    assert "hard_hourly_last_blockers" not in task.stats


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_deferred_ai_ignores_empty_text_voice_gate(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        lambda _session, *, tenant_id, account_ids: {
            int(account_id): {"version": 1, "summary": "夜场熟客，要求夜场主题锚点"}
            for account_id in account_ids
        },
    )

    _forbid_planner_external_work(monkeypatch)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        _add_ai_group_rule_binding(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        _add_ready_group_accounts(session, group_id=7, account_ids=list(range(101, 111)))
        task = Task(
            id="ai-hard-hourly-deferred-voice",
            tenant_id=1,
            name="硬目标延迟生成不被空文本面具拦截",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id, Action.action_type == "send_message").order_by(Action.created_at)))

    assert created == 10
    assert len(actions) == 10
    assert {action.payload["ai_generation_status"] for action in actions} == {"pending"}
    assert {action.payload["message_text"] for action in actions} == {""}
    assert "voice_profile_mismatch" not in (task.stats or {}).get("hard_hourly_last_blockers", {})


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_preserves_cycle_rotation_over_account_memory(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        _add_ai_group_rule_binding(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        _add_ready_group_accounts(session, group_id=7, account_ids=[101, 102, 103])
        task = _hard_hourly_memory_rotation_task()
        session.add(task)
        session.flush()
        previous = _send_action(
            "previous-cycle",
            task,
            "success",
            account_id=101,
            scheduled_at=now_value - timedelta(minutes=5),
            executed_at=now_value - timedelta(minutes=5),
        )
        previous.payload = {
            "cycle_id": f"{task.id}:cycle:1",
            "message_text": "上一轮账号101已经发过",
        }
        foreign = _send_action(
            "foreign-cycle",
            task,
            "success",
            account_id=102,
            scheduled_at=now_value - timedelta(minutes=4),
            executed_at=now_value - timedelta(minutes=4),
        )
        foreign.tenant_id = 2
        foreign.payload = {"cycle_id": f"{task.id}:cycle:99", "message_text": "其他租户动作"}
        session.add_all([previous, foreign])
        session.commit()

        assert _next_cycle_index(session, task) == 2
        session.delete(foreign)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        new_actions = list(session.scalars(
            select(Action)
                .where(Action.task_id == task.id, Action.id != previous.id)
            .order_by(Action.payload["turn_index"].as_integer())
        ))

    assert created == 3
    assert [action.account_id for action in new_actions] == [102, 103, 101]
    assert {action.payload["cycle_id"] for action in new_actions} == {f"{task.id}:cycle:2"}


def test_group_ai_chat_hard_hourly_reuses_accounts_in_same_round(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-prepared-capacity",
            tenant_id=1,
            name="硬目标同轮容量",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 5,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.account_id.asc())))

    assert created == 5
    assert [action.account_id for action in actions] == [101, 101, 102, 102, 103]
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 5
    assert "hard_hourly_last_blockers" not in task.stats


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_skips_skewed_open_actions_for_replan(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        _add_ai_group_rule_binding(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        _add_ready_group_accounts(session, group_id=7, account_ids=[101, 102, 103])
        task = _hard_hourly_memory_rotation_task()
        session.add(task)
        session.flush()
        for index in range(3):
            action = _send_action(
                f"skew-open-{index}",
                task,
                "pending",
                account_id=101,
                scheduled_at=now_value + timedelta(minutes=index + 1),
            )
            action.payload = {"hard_hourly_target": True, "account_voice_profile_version": 1, "account_mask_version": 1}
            session.add(action)
        session.commit()

        skipped = prepare_open_actions_for_planning(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.id.asc())))

    assert skipped == 3
    assert {action.status for action in actions} == {"skipped"}
    assert {action.result["error_code"] for action in actions} == {"hard_hourly_distribution_skew_replan"}


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_records_offline_account_samples(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.online_ready_account_ids_for_planning",
        lambda *_args, **_kwargs: set(),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        _add_ai_group_rule_binding(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        _add_ready_group_accounts(session, group_id=7, account_ids=[101, 102, 103])
        task = _hard_hourly_memory_rotation_task()
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert created == 0
    assert task.stats["account_online_selected_count"] == 3
    assert task.stats["account_online_ready_count"] == 0
    assert task.stats["account_offline_count"] == 3
    assert task.stats["account_offline_sample_account_ids"] == [101, 102, 103]
    assert task.stats["hard_hourly_last_blockers"] == {"account_offline": 2}


@pytest.mark.no_postgres
def test_group_ai_chat_hard_hourly_blocks_skewed_new_plan(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def skewed_generation_slots(turn, profile, **_kwargs):
        return [
            {
                "slot_id": f"{profile.cycle_id}:turn:{index + 1}",
                "account_id": 101,
                "act_type": "short_react",
            }
            for index in range(turn.turn_count)
        ]

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._immutable_generation_slots",
        skewed_generation_slots,
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        _add_ai_group_rule_binding(session)
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        _add_ready_group_accounts(session, group_id=7, account_ids=[101, 102, 103])
        task = _hard_hourly_memory_rotation_task()
        task.type_config = {**task.type_config, "hourly_min_messages": 3, "messages_per_round": 3}
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 0
    assert actions == []
    assert task.last_error == "账号分布偏斜，已阻断本轮硬目标规划"
    assert task.stats["hard_hourly_last_blockers"] == {"account_distribution_skew": 3}
    assert task.stats["hard_hourly_distribution_skew"] == {"max_consecutive_run": 3, "unique_account_count": 1}


def test_group_ai_chat_hard_hourly_records_account_blocker_without_accounts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        task = Task(
            id="ai-hard-hourly-no-account",
            tenant_id=1,
            name="硬目标无账号",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [999]},
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert created == 0
    assert task.stats["hard_hourly_last_planned_count"] == 0
    assert task.stats["hard_hourly_last_blockers"] == {"account_unavailable": 3}


def test_group_ai_chat_hard_hourly_plans_when_accounts_are_full(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 111):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-accounts-full",
            tenant_id=1,
            name="硬目标账号容量满",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 13,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.flush()
        session.add_all(
            _send_action(
                f"account-full-{account_id}",
                task,
                "pending",
                account_id=account_id,
                scheduled_at=now_value + timedelta(minutes=5),
            )
            for account_id in range(101, 111)
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert created == 3
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_skips_history_refresh_and_plans(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-history-permission",
            tenant_id=1,
            name="硬目标历史权限",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 3
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["context_message_ids"] == [] for action in actions)
    assert task.last_error == ""
    assert "history_fetch_degraded" not in task.stats
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_reuses_existing_context_without_refresh(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        session.add(
            GroupContextMessage(
                id=41,
                tenant_id=1,
                group_id=7,
                listener_account_id=101,
                sender_name="真人用户",
                content="已有真人上下文",
                remote_message_id="real-context",
                sent_at=now_value - timedelta(minutes=5),
            )
        )
        task = Task(
            id="ai-hard-hourly-existing-context",
            tenant_id=1,
            name="硬目标复用上下文",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 3
    assert all(action.payload["context_message_ids"] == [41] for action in actions)
    assert all("已有真人上下文" in action.payload["ai_generation_history"] for action in actions)
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_non_hard_planner_defers_history_collection(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="普通活群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线", session_ciphertext="session-101"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        task = Task(
            id="ai-normal-history-permission",
            tenant_id=1,
            name="普通历史权限",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={"target_group_id": 7},
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 3
    assert len(actions) == 3
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["context_message_ids"] == [] for action in actions)
    assert not task.stats.get("history_fetch_degraded")


def test_group_ai_chat_hard_hourly_defers_history_account_fallback(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
            session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=account_id, can_send=True))
        task = Task(
            id="ai-hard-hourly-history-fallback",
            tenant_id=1,
            name="硬目标历史账号补救",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "messages_per_round_mode": "manual",
                "messages_per_round": 3,
                "participation_rate": 1,
                "participation_jitter": 0,
                "fact_anchor_required": False,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 3
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_non_hard_planner_snapshots_stored_context(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="普通活群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线", session_ciphertext="session-101"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        session.add(GroupContextMessage(
            id=42,
            tenant_id=1,
            group_id=7,
            listener_account_id=101,
            sender_name="真人用户",
            content="监听器已落库的上下文",
            remote_message_id="stored-context",
            sent_at=now_value - timedelta(minutes=1),
        ))
        task = Task(
            id="ai-normal-history-non-permission",
            tenant_id=1,
            name="普通历史非权限错误",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={"target_group_id": 7},
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id)))

    assert created == 5
    assert len(actions) == 5
    assert all(action.payload["context_message_ids"] == [42] for action in actions)
    assert all("监听器已落库的上下文" in action.payload["ai_generation_history"] for action in actions)


def test_group_ai_chat_hard_hourly_membership_permission_blocker(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=21,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-10021",
                title="权限失败群",
                auth_status="已授权运营",
                can_send=True,
            )
        )
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线", session_ciphertext="session-101"))
        task = Task(
            id="ai-hard-hourly-membership-permission",
            tenant_id=1,
            name="硬目标准入权限失败",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_operation_target_id": 21,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.add(
            Action(
                id="membership-permission-denied",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                account_id=101,
                status="skipped",
                payload={"channel_target_id": 21},
                result={"error_code": "membership_permission_denied", "membership_status": "permission_denied"},
            )
        )
        session.commit()

        created = build_group_ai_chat_plan(session, task)

    assert created == 0
    assert task.stats["hard_hourly_last_planned_count"] == 0
    assert task.stats["hard_hourly_last_blockers"] == {"target_permission": 3}


def test_hard_hourly_future_pending_is_visible_but_does_not_cover_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-open-split",
            tenant_id=1,
            name="硬目标 pending 拆分",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("ok", task, "success", executed_at=datetime(2026, 6, 7, 20, 5)),
                _send_action("future", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 50)),
                _send_action("overdue", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 10)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)
        needs_more = hard_hourly_requires_planning(session, task, now_value)

    assert stats["hard_hourly_success_count"] == 1
    assert stats["hard_hourly_open_count"] == 1
    assert stats["hard_hourly_overdue_open_count"] == 1
    assert stats["hard_hourly_deficit"] == 2
    assert stats["hard_hourly_planning_deficit"] == 1
    assert stats["hard_hourly_status"] == "blocked"
    assert stats["hard_hourly_last_blockers"] == {"dispatcher_lag": 1}
    assert needs_more is True


def test_hard_hourly_future_pending_covers_planning_deficit_only(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-future-covered",
            tenant_id=1,
            name="硬目标 future 覆盖",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("ok", task, "success", executed_at=datetime(2026, 6, 7, 20, 5)),
                _send_action("future-1", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 40)),
                _send_action("future-2", task, "pending", account_id=102, scheduled_at=datetime(2026, 6, 7, 20, 50)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)
        needs_more = hard_hourly_requires_planning(session, task, now_value)

    assert stats["hard_hourly_open_count"] == 2
    assert stats["hard_hourly_overdue_open_count"] == 0
    assert stats["hard_hourly_deficit"] == 3
    assert stats["hard_hourly_planning_deficit"] == 1
    assert stats["hard_hourly_status"] == "catching_up"
    assert needs_more is True


@pytest.mark.no_postgres
def test_hard_hourly_planning_includes_recent_history_backfill_debt(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-backfill-debt",
            tenant_id=1,
            name="天津",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            created_at=datetime(2026, 6, 7, 19, 0),
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"started_at": "2026-06-07T19:00:00"},
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("last-hour-ok", task, "success", executed_at=datetime(2026, 6, 7, 19, 5)),
                _send_action("current-ok", task, "success", executed_at=datetime(2026, 6, 7, 20, 5)),
                _send_action("current-future-1", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 35)),
                _send_action("current-future-2", task, "pending", account_id=102, scheduled_at=datetime(2026, 6, 7, 20, 40)),
                _send_action("current-future-3", task, "pending", account_id=103, scheduled_at=datetime(2026, 6, 7, 20, 45)),
                _send_action("current-future-4", task, "pending", account_id=104, scheduled_at=datetime(2026, 6, 7, 20, 50)),
                _send_action("current-future-5", task, "pending", account_id=105, scheduled_at=datetime(2026, 6, 7, 20, 55)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)
        progress = hard_hourly_current_progress(session, task, now_value)
        needs_more = hard_hourly_requires_planning(session, task, now_value)

    assert stats["hard_hourly_planning_deficit"] == 0
    assert stats["hard_hourly_backfill_debt"] == 3
    assert stats["hard_hourly_backfill_planning_deficit"] == 1
    assert progress["deficit"] == 1
    assert needs_more is True


def test_hard_hourly_refresh_clears_stale_blockers_when_future_actions_are_on_time(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-stale-blocker",
            tenant_id=1,
            name="硬目标陈旧阻塞",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_last_blockers": {"dispatcher_lag": 1}},
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("ok", task, "success", executed_at=datetime(2026, 6, 7, 20, 5)),
                _send_action("future", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 40)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_overdue_open_count"] == 0
    assert stats["hard_hourly_status"] == "catching_up"
    assert "hard_hourly_last_blockers" not in stats


@pytest.mark.no_postgres
def test_hard_hourly_refresh_clears_ai_blocker_when_future_actions_cover_planning_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-covered-ai-blocker",
            tenant_id=1,
            name="硬目标 AI 阻塞已覆盖",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_last_blockers": {"ai_generation_unavailable": 1}},
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                task,
                _send_action("ok", task, "success", executed_at=datetime(2026, 6, 7, 20, 5)),
                _send_action("future-1", task, "pending", account_id=101, scheduled_at=datetime(2026, 6, 7, 20, 40)),
                _send_action("future-2", task, "pending", account_id=102, scheduled_at=datetime(2026, 6, 7, 20, 45)),
                _send_action("future-3", task, "pending", account_id=103, scheduled_at=datetime(2026, 6, 7, 20, 50)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_deficit"] == 3
    assert stats["hard_hourly_planning_deficit"] == 0
    assert stats["hard_hourly_status"] == "catching_up"
    assert stats["hard_hourly_pipeline"]["ai_draft"] == "ready"
    assert "hard_hourly_last_blockers" not in stats


@pytest.mark.no_postgres
def test_hard_hourly_refresh_preserves_plan_blockers_while_deficit_remains(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-plan-blocker",
            tenant_id=1,
            name="硬目标计划阻塞",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_last_blockers": {"rule_binding_missing": 4}},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_deficit"] == 4
    assert stats["hard_hourly_status"] == "blocked"
    assert stats["hard_hourly_last_blockers"] == {"rule_binding_missing": 4}


@pytest.mark.no_postgres
def test_hard_hourly_refresh_drops_transient_account_offline_blocker(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-transient-offline",
            tenant_id=1,
            name="硬目标账号在线恢复",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 4,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_last_blockers": {"account_offline": 4}},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        stats = refresh_task_stats(session, task)

    assert stats["hard_hourly_deficit"] == 4
    assert stats["hard_hourly_status"] == "catching_up"
    assert "hard_hourly_last_blockers" not in stats


def test_hard_hourly_future_open_over_account_capacity_stays_visible_without_covering_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-future-over-capacity",
            tenant_id=1,
            name="硬目标 future 容量透支",
            type="group_ai_chat",
            status="running",
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all(
            [
                Tenant(id=1, name="默认运营空间"),
                SchedulingSetting(tenant_id=1, default_account_hour_limit=1),
                task,
                _send_action(
                    "future-over-capacity-1",
                    task,
                    "pending",
                    account_id=101,
                    scheduled_at=datetime(2026, 6, 7, 20, 40),
                ),
                _send_action(
                    "future-over-capacity-2",
                    task,
                    "pending",
                    account_id=101,
                    scheduled_at=datetime(2026, 6, 7, 20, 45),
                ),
                _send_action(
                    "future-over-capacity-3",
                    task,
                    "pending",
                    account_id=101,
                    scheduled_at=datetime(2026, 6, 7, 20, 50),
                ),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)
        needs_more = hard_hourly_requires_planning(session, task, now_value)

    assert stats["hard_hourly_open_count"] == 3
    assert stats["hard_hourly_overdue_open_count"] == 0
    assert stats["hard_hourly_deficit"] == 3
    assert stats["hard_hourly_planning_deficit"] == 0
    assert stats["hard_hourly_status"] == "catching_up"
    assert "hard_hourly_last_blockers" not in stats
    assert needs_more is False


def test_hard_hourly_deficit_wakes_future_next_run(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-wake",
            tenant_id=1,
            name="硬目标唤醒",
            type="group_ai_chat",
            status="running",
            next_run_at=datetime(2026, 6, 7, 21, 0),
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 2,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=10)

    assert task.next_run_at == now_value
    assert task_ids == ["task-hard-hourly-wake"]


def test_hard_hourly_due_check_overrides_future_next_run(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        due_task = Task(
            id="task-hard-hourly-due-check",
            tenant_id=1,
            name="硬目标到期检查",
            type="group_ai_chat",
            status="running",
            next_run_at=datetime(2026, 6, 8, 4, 30),
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 2,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_next_check_at": "2026-06-07T20:29:30"},
        )
        future_task = Task(
            id="task-hard-hourly-future-check",
            tenant_id=1,
            name="硬目标未到检查",
            type="group_ai_chat",
            status="running",
            next_run_at=datetime(2026, 6, 8, 4, 30),
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 2,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_next_check_at": "2026-06-07T20:30:30"},
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), due_task, future_task])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=10)

    assert task_ids == ["task-hard-hourly-due-check"]
    assert due_task.next_run_at == now_value
    assert future_task.next_run_at == datetime(2026, 6, 8, 4, 30)


def test_hard_hourly_wake_keeps_already_due_next_run(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)
    due_at = now_value - timedelta(minutes=5)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        task = Task(
            id="task-hard-hourly-already-due",
            tenant_id=1,
            name="硬目标已到期",
            type="group_ai_chat",
            status="running",
            next_run_at=due_at,
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 2,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add_all([Tenant(id=1, name="默认运营空间"), task])
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=10)

    assert task_ids == ["task-hard-hourly-already-due"]
    assert task.next_run_at == due_at


def test_hard_hourly_wake_scans_past_non_hard_tasks(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 30)

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        for index in range(25):
            session.add(
                Task(
                    id=f"task-normal-{index}",
                    tenant_id=1,
                    name=f"普通 AI {index}",
                    type="group_ai_chat",
                    status="running",
                    priority=1,
                    next_run_at=now_value - timedelta(minutes=1),
                    created_at=now_value - timedelta(minutes=30 - index),
                    type_config={"target_group_id": 7},
                )
            )
        hard_task = Task(
            id="task-hard-hourly-after-window",
            tenant_id=1,
            name="硬目标排在后面",
            type="group_ai_chat",
            status="running",
            priority=2,
            next_run_at=datetime(2026, 6, 8, 4, 30),
            created_at=now_value,
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 2,
                "hard_hourly_strategy": "force_planning",
            },
            stats={"hard_hourly_next_check_at": "2026-06-07T20:29:30"},
        )
        session.add(hard_task)
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=1)

    assert task_ids == ["task-hard-hourly-after-window"]
    assert hard_task.next_run_at == now_value


def test_hard_hourly_wake_filters_non_hard_tasks_before_due_check(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 8, 4, 30)
    checked_task_ids: list[str] = []

    monkeypatch.setattr("app.services.task_center.service._now", lambda: now_value)

    def fake_progress(_session, task: Task, _now_value: datetime) -> dict[str, object]:
        checked_task_ids.append(task.id)
        return {"deficit": 2}

    monkeypatch.setattr("app.services.task_center.service.hard_hourly_current_progress", fake_progress)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        for index in range(25):
            session.add(
                Task(
                    id=f"task-normal-ai-{index}",
                    tenant_id=1,
                    name=f"普通 AI 活群 {index}",
                    type="group_ai_chat",
                    status="running",
                    priority=1,
                    next_run_at=now_value,
                    created_at=now_value,
                    type_config={"target_group_id": index + 1},
                )
            )
        session.add(
            Task(
                id="task-hard-hourly-only-candidate",
                tenant_id=1,
                name="硬目标候选",
                type="group_ai_chat",
                status="running",
                priority=2,
                next_run_at=now_value,
                created_at=now_value,
                type_config={
                    "target_group_id": 99,
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 2,
                    "hard_hourly_strategy": "force_planning",
                },
            )
        )
        session.commit()

        task_ids = _wake_hard_hourly_tasks(session, limit=1)

    assert task_ids == ["task-hard-hourly-only-candidate"]
    assert checked_task_ids == ["task-hard-hourly-only-candidate"]


def test_next_run_after_task_uses_hard_hourly_next_check(monkeypatch):
    now_value = datetime(2026, 6, 7, 20, 30)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    task = Task(
        id="task-hard-hourly-next-check",
        tenant_id=1,
        name="硬目标下次检查",
        type="group_ai_chat",
        status="running",
        type_config={
            "target_group_id": 7,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 2,
            "hard_hourly_strategy": "force_planning",
        },
        stats={"hard_hourly_next_check_at": "2026-06-07T20:31:00"},
    )

    assert next_run_after_task(task) == datetime(2026, 6, 7, 20, 31)


def test_next_run_after_task_clamps_stale_hard_hourly_next_check(monkeypatch):
    now_value = datetime(2026, 6, 7, 20, 35)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    task = Task(
        id="task-hard-hourly-stale-next-check",
        tenant_id=1,
        name="硬目标过期检查",
        type="group_ai_chat",
        status="running",
        type_config={
            "target_group_id": 7,
            "hard_hourly_target_enabled": True,
            "hourly_min_messages": 2,
            "hard_hourly_strategy": "force_planning",
        },
        stats={"hard_hourly_next_check_at": "2026-06-07T20:26:00"},
    )

    assert next_run_after_task(task) == now_value


@pytest.mark.no_postgres
def test_next_run_after_task_prefers_daily_coverage_debt_check(monkeypatch):
    now_value = datetime(2026, 6, 7, 20, 10)
    monkeypatch.setattr("app.services.task_center.stats._now", lambda: now_value)
    task = Task(
        id="task-daily-coverage-next-check",
        tenant_id=1,
        name="覆盖欠账检查",
        type="group_ai_chat",
        status="running",
        pacing_config={"operation_profile": {"hourly_activity_curve": [1] * 24}},
        type_config={"target_group_id": 7, "account_coverage_mode": "all_accounts_daily"},
        stats={"daily_coverage_next_check_at": "2026-06-07T20:12:00"},
    )

    assert next_run_after_task(task) == datetime(2026, 6, 7, 20, 12)


def test_group_ai_chat_hard_hourly_reply_shortfall_fills_with_normal_turns(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线", session_ciphertext="session-101"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        task = Task(
            id="ai-hard-hourly-reply-shortfall",
            tenant_id=1,
            name="硬目标引用不足",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "reply_min_per_round": 1,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
        )
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    assert created == 3
    assert len(actions) == 3
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(not action.payload["reply_to_message_id"] for action in actions)
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats
    assert task.stats["hard_hourly_next_check_at"] == "2026-06-07T20:10:30"


@pytest.mark.no_postgres
def test_hard_hourly_refills_accounts_after_missing_voice_profiles(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        _voice_profiles_after_first_ten_accounts,
    )

    with Session(engine) as session:
        _add_hard_hourly_profile_refill_fixture(session)
        task = _hard_hourly_profile_refill_task()
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.account_id)))
        session.refresh(task)

    assert created == 6
    assert [action.account_id for action in actions] == [11, 12, 13, 14, 15, 16]
    assert task.stats["voice_profile_missing_count"] == 10
    assert task.stats["voice_profile_refill_account_count"] == 10
    assert task.stats["hard_hourly_last_planned_count"] == 6
    assert "hard_hourly_last_blockers" not in task.stats


@pytest.mark.no_postgres
def test_hard_hourly_refill_rechecks_online_ready_accounts(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    _forbid_planner_external_work(monkeypatch)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.voice_profile_prompt_details",
        _voice_profiles_after_first_ten_accounts,
    )
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat.online_ready_account_ids_for_planning",
        lambda _session, *, tenant_id, accounts, now=None: {
            account.id for account in accounts if int(account.id) <= 10 or int(account.id) >= 21
        },
    )

    with Session(engine) as session:
        _add_hard_hourly_profile_refill_fixture(session, account_total=PROFILE_REFILL_ONLINE_GAP_ACCOUNT_TOTAL)
        task = _hard_hourly_profile_refill_task(max_concurrent=PROFILE_REFILL_ONLINE_GAP_ACCOUNT_TOTAL)
        session.add(task)
        session.commit()

        created = build_group_ai_chat_plan(session, task)
        actions = list(session.scalars(select(Action).where(Action.task_id == task.id).order_by(Action.account_id)))
        session.refresh(task)

    assert created == 6
    assert [action.account_id for action in actions] == [21, 22, 23, 24, 25, 26]
    assert task.stats["voice_profile_missing_count"] == 10
    assert task.stats["voice_profile_refill_account_count"] == 10
    assert task.stats["account_offline_count"] == 10


def test_precheck_reports_hard_hourly_capacity_without_blocking_on_max_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    risk_result = {
        "decision": "allow",
        "decision_reasons": [],
        "available_accounts": [101, 102, 103],
        "limited_accounts": [],
        "blocked_accounts": [],
        "target_warnings": [],
        "content_warnings": [],
        "proxy_warnings": [],
        "suggested_actions": [],
        "trace_id": "risk-ok",
    }

    monkeypatch.setattr("app.services.task_center.precheck.risk_preflight", lambda *_args, **_kwargs: risk_result)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            OperationTarget(
                id=21,
                tenant_id=1,
                target_type="group",
                tg_peer_id="-1007",
                title="硬目标群",
                can_send=True,
                auth_status="已授权运营",
            )
        )
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线", session_ciphertext=f"session-{account_id}"))
        session.commit()

        result = precheck_task_creation(
            session,
            1,
            TaskPrecheckRequest(
                task_type="group_ai_chat",
                payload={
                    "name": "硬目标预检",
                    "target_operation_target_id": 21,
                    "account_config": {"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
                    "pacing_config": {
                        "mode": "template",
                        "max_actions_per_hour": 2,
                        "operation_profile": {
                            "hourly_activity_curve": [1] * 24,
                            "quiet_threshold": 2,
                            "peak_threshold": 8,
                        },
                    },
                    "messages_per_round_mode": "manual",
                    "messages_per_round": 3,
                    "hard_hourly_target_enabled": True,
                    "hourly_min_messages": 300,
                    "hard_hourly_strategy": "force_planning",
                },
            ),
        )

    assert result["decision"] == "warn"
    assert result["estimated_hourly_capacity"] == 3
    assert result["hard_hourly_target"] == {
        "enabled": True,
        "hourly_min_messages": 300,
        "estimated_hourly_capacity": 3,
        "capacity_gap": 297,
        "hard_target_over_capacity": True,
        "warnings": ["硬目标高于当前账号容量，可能持续未达标"],
    }
    assert "硬目标高于当前账号容量，可能持续未达标" in result["warnings"]
