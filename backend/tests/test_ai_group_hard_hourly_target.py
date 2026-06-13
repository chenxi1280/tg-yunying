from __future__ import annotations

import importlib.util
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupContextMessage, OperationTarget, SchedulingSetting, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas import GroupAIChatTaskCreate, TaskPrecheckRequest
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_chat_plan
from app.services.task_center.hard_hourly import hard_schedule_times, requires_planning as hard_hourly_requires_planning
from app.services.task_center.service import (
    _merge_planner_task_ids,
    _wake_hard_hourly_tasks,
    create_group_ai_chat_task,
    list_tasks,
    precheck_task_creation,
)
from app.services.task_center.stats import next_run_after_task, refresh_task_stats
from app.timezone import BEIJING_TZ


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


def _load_hard_target_migration():
    migration_path = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0057_ai_group_hard_target_300.py"
    spec = importlib.util.spec_from_file_location("migration_0057_ai_group_hard_target_300", migration_path)
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


def test_group_ai_chat_create_rejects_disabled_or_low_hard_hourly_target():
    with pytest.raises(ValueError, match="必须启用每小时硬目标"):
        GroupAIChatTaskCreate(
            name="关闭硬目标",
            target_group_id=7,
            hard_hourly_target_enabled=False,
        )
    with pytest.raises(ValueError, match="不能低于 300"):
        GroupAIChatTaskCreate(
            name="低硬目标",
            target_group_id=7,
            hard_hourly_target_enabled=True,
            hourly_min_messages=299,
        )


def test_group_ai_chat_create_defaults_to_hard_hourly_target_300():
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
    assert task.type_config["hourly_min_messages"] == 300
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
    assert stats["hard_hourly_deficit"] == 2
    assert stats["hard_hourly_status"] == "blocked"
    assert stats["hard_hourly_last_blockers"] == {"dispatcher_lag": 1}
    assert stats["hard_hourly_bucket"] == "2026-06-07T20:00:00+08:00"
    assert stats["hard_hourly_last_check_at"] == "2026-06-07T20:20:00"
    buckets = stats["hard_hourly_recent_buckets"]
    current_bucket = next(item for item in buckets if item["bucket"] == "2026-06-07T20:00:00+08:00")
    previous_bucket = next(item for item in buckets if item["bucket"] == "2026-06-07T19:00:00+08:00")
    assert current_bucket["future_open_count"] == 1
    assert current_bucket["overdue_open_count"] == 1
    assert previous_bucket["status"] == "missed"


def test_group_ai_chat_hard_hourly_target_creates_deficit_actions(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    captured: dict[str, object] = {}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["count"] = count
        samples = [
            "今晚活动几点开始",
            "报名入口有人发下吗",
            "我看群公告写得挺清楚",
            "新来的可以先看置顶",
            "有问题直接在群里问",
        ]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._drop_repeated_planned_items", lambda items, _previous: items)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in [101, 102, 103, 104, 105]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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
    assert captured["count"] == 5
    assert len(actions) == 5
    assert all(action.payload["hard_hourly_target"] is True for action in actions)
    assert all(action.payload["hard_hourly_bucket"] == "2026-06-07T20:00:00+08:00" for action in actions)
    assert all(action.payload["hard_hourly_deficit_at_plan"] == 5 for action in actions)
    assert max(action.scheduled_at for action in actions) < datetime(2026, 6, 7, 21, 0)


def test_group_ai_chat_hard_hourly_target_plans_large_deficit_in_batches(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    captured: dict[str, object] = {"counts": []}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["counts"].append(count)
        batch_index = len(captured["counts"])
        return [f"硬目标缺口补量{batch_index}-{index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._drop_repeated_planned_items", lambda items, _previous: items)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 111):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 10
    assert captured["counts"] == [10]
    assert len(actions) == 10
    assert task.stats["hard_hourly_last_planned_count"] == 10
    assert task.stats["hard_hourly_next_check_at"] == "2026-06-07T20:10:30"
    assert all(action.payload["hard_hourly_deficit_at_plan"] == 300 for action in actions)


def test_group_ai_chat_hard_hourly_ignores_configured_round_size_for_deficit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    captured: dict[str, object] = {"counts": []}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["counts"].append(count)
        batch_index = len(captured["counts"])
        return [f"硬目标补量消息{batch_index}-{index}" for index in range(count)], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._drop_repeated_planned_items", lambda items, _previous: items)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 161):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 10
    assert captured["counts"] == [10]
    assert len(actions) == 10
    assert task.stats["hard_hourly_last_planned_count"] == 10
    assert all(action.payload["hard_hourly_target"] is True for action in actions)


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
    assert times[1] - times[0] <= timedelta(seconds=11)
    assert times[-1] <= now_value + timedelta(seconds=100)
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


def test_group_ai_chat_hard_hourly_scans_goal_sized_pool_when_front_accounts_are_full(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        samples = [
            "今晚安排我看群公告确认了",
            "报名入口刚才有人发过吗",
            "新来的同学先看下置顶",
            "后续通知应该还会补充",
            "这个时间大家都在线吗",
            "我这边看到流程挺清楚",
            "还有人需要报名链接吗",
            "先按老师通知来就行",
            "名单确认完再同步一下",
            "等会儿有变化群里说",
        ]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 201):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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
    assert created == 10
    assert planned_account_ids == list(range(181, 191))
    assert task.stats["hard_hourly_last_planned_count"] == 10
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_uses_accounts_available_later_in_hour(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return ["今晚安排先看群公告", "报名入口有人再发下吗"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, jitter_min_seconds=0, jitter_max_seconds=0, default_account_cooldown_seconds=120))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
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
    assert hard_actions[0].scheduled_at == datetime(2026, 6, 7, 20, 11)
    assert max(action.scheduled_at for action in hard_actions) < datetime(2026, 6, 7, 21, 0)
    assert task.last_error == ""
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_counts_prepared_actions_against_account_hour_limit(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        samples = [
            "今晚安排先看群公告",
            "报名入口有人再发下吗",
            "后面变化等群里通知",
            "新同学先看置顶",
            "有问题群里直接问",
        ]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 3
    assert [action.account_id for action in actions] == [101, 102, 103]
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert task.stats["hard_hourly_last_blockers"] == {"account_capacity": 2}


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


def test_group_ai_chat_hard_hourly_records_capacity_blocker_when_accounts_are_full(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return ["今晚安排先看群公告", "报名入口有人再发下吗", "后面变化等群里通知"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.account_capacity._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.account_pool._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(SchedulingSetting(tenant_id=1, default_account_hour_limit=1))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        for account_id in range(101, 111):
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 0
    assert task.last_error == "账号容量已排满，等待账号额度恢复后继续执行"
    assert task.stats["hard_hourly_last_planned_count"] == 0
    assert task.stats["hard_hourly_last_blockers"] == {"account_capacity": 3}


def test_group_ai_chat_hard_hourly_skips_history_refresh_and_plans(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def denied_history(*_args, **_kwargs):
        raise AssertionError("hard hourly should not synchronously fetch history")

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return ["先按群公告来就行", "报名入口有人再发下吗", "后面等通知"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.collect_group_context", denied_history)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 3
    assert task.last_error == ""
    assert "history_fetch_degraded" not in task.stats
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_hard_hourly_reuses_existing_context_without_refresh(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def broken_history(*_args, **_kwargs):
        raise AssertionError("hard hourly should reuse stored context")

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        assert "已有真人上下文" in history
        return ["今晚活动几点开始", "报名入口有人发下吗", "我看群公告写得挺清楚"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.collect_group_context", broken_history)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._drop_repeated_planned_items", lambda items, _previous: items)
    monkeypatch.setattr(
        "app.services.task_center.executors.group_ai_chat._quality_filter_ai_messages",
        lambda contents, _previous, **_kwargs: ([{"content": content} for content in contents], {}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 3
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_non_hard_history_permission_still_blocks(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def denied_history(*_args, **_kwargs):
        raise RuntimeError("ChannelPrivateError lack permission caused by GetHistoryRequest")

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.collect_group_context", denied_history)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="普通活群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
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

    assert created == 0
    assert actions == []
    assert "监听账号无法读取目标群历史" in task.last_error
    assert not task.stats.get("history_fetch_degraded")


def test_group_ai_chat_hard_hourly_defers_history_account_fallback(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)
    attempted: list[int] = []

    def history_with_first_account_denied(_session, _group, account_ids, **_kwargs):
        account_id = int(account_ids[0])
        attempted.append(account_id)
        if account_id == 101:
            raise RuntimeError("ChannelPrivateError lack permission caused by GetHistoryRequest")
        return 0

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        return ["先按群公告来就行", "报名入口有人再发下吗", "后面等通知"][:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.collect_group_context", history_with_first_account_denied)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        for account_id in [101, 102, 103]:
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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

    assert created == 3
    assert attempted == []
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_non_hard_history_collect_exposes_non_permission_errors(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def broken_history(*_args, **_kwargs):
        raise RuntimeError("telegram gateway unavailable")

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: True)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.collect_group_context", broken_history)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="普通活群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
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

        with pytest.raises(RuntimeError, match="telegram gateway unavailable"):
            build_group_ai_chat_plan(session, task)


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
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        task = Task(
            id="ai-hard-hourly-membership-permission",
            tenant_id=1,
            name="硬目标准入权限失败",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
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


def test_hard_hourly_future_pending_covers_deficit_but_overdue_does_not(monkeypatch):
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
    assert stats["hard_hourly_deficit"] == 1
    assert stats["hard_hourly_status"] == "blocked"
    assert stats["hard_hourly_last_blockers"] == {"dispatcher_lag": 1}
    assert needs_more is True


def test_hard_hourly_future_pending_can_fully_cover_deficit(monkeypatch):
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
                "hourly_min_messages": 3,
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
    assert stats["hard_hourly_deficit"] == 0
    assert needs_more is False


def test_hard_hourly_future_open_over_account_capacity_covers_deficit(monkeypatch):
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
    assert stats["hard_hourly_deficit"] == 0
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


def test_group_ai_chat_hard_hourly_reply_shortfall_fills_with_normal_turns(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        samples = [
            "今晚活动几点开始",
            "报名入口谁再发一下",
            "新来的可以先看群公告",
        ]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营"))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
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
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats
    assert task.stats["hard_hourly_next_check_at"] == "2026-06-07T20:10:30"


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
            session.add(TgAccount(id=account_id, tenant_id=1, display_name=f"账号{account_id}", phone_masked=str(account_id), status="在线"))
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
