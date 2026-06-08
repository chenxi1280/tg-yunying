from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.schemas import GroupAIChatTaskCreate, TaskPrecheckRequest
from app.services.task_center.executors.group_ai_chat import build_plan as build_group_ai_chat_plan
from app.services.task_center.hard_hourly import requires_planning as hard_hourly_requires_planning
from app.services.task_center.service import _wake_hard_hourly_tasks, create_group_ai_chat_task, precheck_task_creation
from app.services.task_center.stats import next_run_after_task, refresh_task_stats
from app.timezone import BEIJING_TZ


def _send_action(action_id: str, task: Task, status: str, *, scheduled_at: datetime | None = None, executed_at: datetime | None = None) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task.id,
        task_type="group_ai_chat",
        action_type="send_message",
        status=status,
        scheduled_at=scheduled_at,
        executed_at=executed_at,
    )


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
                hourly_min_messages=60,
                hard_hourly_strategy="force_planning",
            ),
            actor="tester",
        )

    assert task.type_config["hard_hourly_target_enabled"] is True
    assert task.type_config["hourly_min_messages"] == 60
    assert task.type_config["hard_hourly_strategy"] == "force_planning"


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
                _send_action("future-open", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 45)),
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
    captured: dict[str, object] = {}

    def fake_generate_group_messages(_session, _tenant_id, _config, *, count, target_label, history):
        captured["count"] = count
        samples = [
            "今晚活动几点开始",
            "报名入口谁再发下",
            "新来的先看置顶哈",
            "这个群公告挺清楚",
            "有问题直接群里问",
            "我刚看到老师通知",
            "名单确认完了吗",
            "后面还有补充安排吗",
            "先按公告来就行",
            "等会儿有人统一说",
        ]
        return samples[:count], 0

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.generate_group_messages", fake_generate_group_messages)

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
    assert captured["count"] == 10
    assert len(actions) == 10
    assert task.stats["hard_hourly_last_planned_count"] == 10
    assert task.stats["hard_hourly_next_check_at"] == "2026-06-07T20:10:30"
    assert all(action.payload["hard_hourly_deficit_at_plan"] == 300 for action in actions)


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


def test_group_ai_chat_hard_hourly_records_history_permission_blocker(monkeypatch):
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
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        task = Task(
            id="ai-hard-hourly-history-permission",
            tenant_id=1,
            name="硬目标历史权限",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
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
    assert task.stats["hard_hourly_last_blockers"] == {"target_permission": 3}
    assert "监听账号无法读取目标群历史" in task.last_error


def test_group_ai_chat_hard_hourly_tries_next_history_account(monkeypatch):
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
    assert attempted == [101, 102]
    assert task.last_error == ""
    assert task.stats["hard_hourly_last_planned_count"] == 3
    assert "hard_hourly_last_blockers" not in task.stats


def test_group_ai_chat_history_collect_exposes_non_permission_errors(monkeypatch):
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
        session.add(TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="硬目标群", auth_status="已授权运营", listener_interval_seconds=1))
        session.add(TgAccount(id=101, tenant_id=1, display_name="账号101", phone_masked="101", status="在线"))
        session.add(TgGroupAccount(tenant_id=1, group_id=7, account_id=101, can_send=True))
        task = Task(
            id="ai-hard-hourly-history-non-permission",
            tenant_id=1,
            name="硬目标历史非权限错误",
            type="group_ai_chat",
            status="running",
            account_config={"selection_mode": "all", "max_concurrent": 20, "cooldown_per_account_minutes": 0},
            type_config={
                "target_group_id": 7,
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 3,
                "hard_hourly_strategy": "force_planning",
            },
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
                _send_action("future", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 50)),
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
                _send_action("future-1", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 40)),
                _send_action("future-2", task, "pending", scheduled_at=datetime(2026, 6, 7, 20, 50)),
            ]
        )
        session.commit()

        stats = refresh_task_stats(session, task)
        needs_more = hard_hourly_requires_planning(session, task, now_value)

    assert stats["hard_hourly_open_count"] == 2
    assert stats["hard_hourly_overdue_open_count"] == 0
    assert stats["hard_hourly_deficit"] == 0
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


def test_group_ai_chat_hard_hourly_reply_shortfall_records_blocker(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 6, 7, 20, 10)

    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat._now", lambda: now_value)
    monkeypatch.setattr("app.services.task_center.executors.group_ai_chat.should_collect_listener", lambda *_args, **_kwargs: False)

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

    assert created == 0
    assert task.stats["hard_hourly_last_planned_count"] == 0
    assert task.stats["hard_hourly_last_blockers"] == {"reply_target_shortfall": 3}
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
                    "hourly_min_messages": 5,
                    "hard_hourly_strategy": "force_planning",
                },
            ),
        )

    assert result["decision"] == "warn"
    assert result["estimated_hourly_capacity"] == 3
    assert result["hard_hourly_target"] == {
        "enabled": True,
        "hourly_min_messages": 5,
        "estimated_hourly_capacity": 3,
        "capacity_gap": 2,
        "hard_target_over_capacity": True,
        "warnings": ["硬目标高于当前账号容量，可能持续未达标"],
    }
    assert "硬目标高于当前账号容量，可能持续未达标" in result["warnings"]
