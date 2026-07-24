from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, SchedulingSetting, Task, Tenant
from app.services.task_center.hard_hourly import current_progress, hard_hourly_stats, normalize, requires_planning
from app.services.task_center.hard_hourly_ledger import credit_success_once, ensure_bucket

pytestmark = pytest.mark.no_postgres


def test_hard_hourly_normalizes_all_comparisons_to_an_aware_task_zone():
    task = Task(id="task-zone", tenant_id=1, name="zone", type="group_ai_chat", timezone="Asia/Shanghai")

    from_utc = normalize(task, datetime(2026, 7, 24, 7, 10, tzinfo=UTC))
    from_naive = normalize(task, datetime(2026, 7, 24, 15, 10))

    assert from_utc.tzinfo is not None
    assert from_naive.tzinfo is not None
    assert from_utc.hour == 15
    assert from_utc == from_naive


def test_stats_refresh_does_not_create_empty_current_hour_bucket():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 24, 15, 10, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-no-write",
            tenant_id=1,
            name="no write",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 1,
            },
        )
        session.add(task)
        session.commit()
        stats = hard_hourly_stats(session, task, now_value, {})
        session.commit()
        from app.models import TaskHardHourlyBucket
        from sqlalchemy import select, func

        count = session.scalar(select(func.count()).select_from(TaskHardHourlyBucket)) or 0
        assert count == 0
        assert stats["hard_hourly_success_count"] == 0
        assert stats["hard_hourly_goal"] == 10


def test_hard_hourly_stats_exposes_durable_debt_and_unknown_hold():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 24, 15, 10, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-c",
            tenant_id=1,
            name="c",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 2,
            },
            config_revision=3,
        )
        session.add(task)
        past = ensure_bucket(
            session,
            task=task,
            operation_target_id=9,
            target_reference_revision=2,
            bucket_start=datetime(2026, 7, 24, 14, 0, 0),
            goal=10,
        )
        past.success_count = 4
        session.add(
            Action(
                id="u1",
                tenant_id=1,
                task_id="task-c",
                task_type="group_ai_chat",
                action_type="send_message",
                status="unknown_after_send",
                scheduled_at=now_value,
                payload={
                    "hard_hourly_target": True,
                    "hard_hourly_bucket": "2026-07-24T15:00:00",
                    "target_operation_target_id": 9,
                    "target_reference_revision": 2,
                },
            )
        )
        session.commit()

        stats = hard_hourly_stats(session, task, now_value, {})
        assert stats["hard_hourly_target_enabled"] is True
        assert stats["hard_hourly_durable_debt"] == 6
        assert stats["hard_hourly_unknown_after_send_hold_count"] == 1
        assert stats["hard_hourly_target_reference_revision"] == 2
        assert stats["hard_hourly_task_config_revision"] == 3
        assert stats["hard_hourly_awaiting_confirmation"] is True
        assert stats["hard_hourly_status"] == "awaiting_confirmation"
        assert stats["hard_hourly_planning_rate"] >= 10


def test_current_progress_uses_ledger_debt_and_does_not_block_on_dispatcher_lag():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 24, 15, 10, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-ledger-planner",
            tenant_id=1,
            name="ledger planner",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            created_at=datetime(2026, 7, 24, 14, 0, 0),
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 9,
                "target_reference_revision": 2,
            },
        )
        session.add(task)
        past = ensure_bucket(
            session,
            task=task,
            operation_target_id=9,
            target_reference_revision=2,
            bucket_start=datetime(2026, 7, 24, 14, 0, 0),
            goal=10,
        )
        past.success_count = 4
        ensure_bucket(
            session,
            task=task,
            operation_target_id=9,
            target_reference_revision=2,
            bucket_start=datetime(2026, 7, 24, 15, 0, 0),
            goal=10,
        )
        session.add(
            Action(
                id="ledger-overdue-action",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                status="pending",
                scheduled_at=datetime(2026, 7, 24, 15, 0, 0),
                payload={
                    "hard_hourly_target": True,
                    "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                    "target_operation_target_id": 9,
                    "target_reference_revision": 2,
                },
            )
        )
        session.commit()

        progress = current_progress(session, task, now_value)
        stats = hard_hourly_stats(session, task, now_value, {})

    assert progress["backfill_debt"] == 6
    assert progress["planning_blocked"] is False
    assert requires_planning(session, task, now_value) is True
    assert stats["hard_hourly_backfill_debt"] == 6
    assert stats["hard_hourly_overdue_open_count"] == 0


def test_recent_buckets_keep_success_on_the_immutable_plan_bucket():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 7, 24, 15, 10, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        session.add(SchedulingSetting(tenant_id=1, ai_group_send_continuity_v1=True))
        task = Task(
            id="task-plan-bucket-display",
            tenant_id=1,
            name="plan bucket display",
            type="group_ai_chat",
            status="running",
            timezone="Asia/Shanghai",
            type_config={
                "hard_hourly_target_enabled": True,
                "hourly_min_messages": 10,
                "target_operation_target_id": 11,
                "target_reference_revision": 1,
            },
        )
        session.add(task)
        past = ensure_bucket(
            session,
            task=task,
            operation_target_id=11,
            target_reference_revision=1,
            bucket_start=datetime(2026, 7, 24, 14, 0, 0),
            goal=10,
        )
        past.success_count = 1
        session.add(
            Action(
                id="cross-hour-credit-audit",
                tenant_id=1,
                task_id=task.id,
                task_type="group_ai_chat",
                action_type="send_message",
                status="success",
                scheduled_at=datetime(2026, 7, 24, 14, 59, 0),
                executed_at=datetime(2026, 7, 24, 15, 2, 0),
                payload={
                    "hard_hourly_target": True,
                    "hard_hourly_bucket": "2026-07-24T14:00:00+08:00",
                    "target_operation_target_id": 11,
                    "target_reference_revision": 1,
                },
            )
        )
        session.commit()

        stats = hard_hourly_stats(session, task, now_value, {})

    buckets = {row["bucket"]: row for row in stats["hard_hourly_recent_buckets"]}
    assert buckets["2026-07-24T14:00:00+08:00"]["success_count"] == 1
    assert buckets["2026-07-24T15:00:00+08:00"]["success_count"] == 0
