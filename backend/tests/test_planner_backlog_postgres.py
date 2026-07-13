from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import Base, SessionLocal, engine
from app.models import Action, Task, Tenant
from app.services.task_center import planner_backlog
from app.timezone import BEIJING_TZ


TEST_TENANT_ID = 913_713


def _action(
    action_id: str,
    task: Task,
    *,
    scheduled_at: datetime,
    payload: dict,
    action_type: str = "send_message",
) -> Action:
    return Action(
        id=action_id,
        tenant_id=task.tenant_id,
        task_id=task.id,
        task_type=task.type,
        action_type=action_type,
        status="pending",
        scheduled_at=scheduled_at,
        payload=payload,
    )


def test_planner_backlog_postgres_json_legacy_timezone_and_partial_membership(monkeypatch) -> None:
    Base.metadata.create_all(engine)
    now = datetime(2026, 7, 13, 12, 30)
    current_bucket = now.replace(minute=0, second=0, microsecond=0, tzinfo=BEIJING_TZ)
    expired_bucket = current_bucket - timedelta(hours=2)
    task = Task(id="pg-backlog", tenant_id=TEST_TENANT_ID, name="pg", type="group_ai_chat", status="running")
    partial = Task(
        id="pg-partial", tenant_id=TEST_TENANT_ID, name="partial", type="group_ai_chat", status="running",
        type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 10},
        stats={"membership_joined_count": 1, "hard_hourly_last_blockers": {"target_membership_pending": 1}},
    )
    with SessionLocal() as session:
        session.add(Tenant(id=TEST_TENANT_ID, name="planner backlog test"))
        session.commit()
        session.add_all([task, partial])
        session.commit()
        session.add_all([
            _action("normal", task, scheduled_at=now - timedelta(minutes=20), payload={}),
            _action("hard-current", task, scheduled_at=now - timedelta(minutes=10), payload={"hard_hourly_target": True, "hard_hourly_bucket": current_bucket.isoformat()}),
            _action("hard-expired-aware", task, scheduled_at=now - timedelta(hours=2), payload={"hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.isoformat()}),
            _action("hard-expired-naive", task, scheduled_at=now - timedelta(hours=2), payload={"hard_hourly_target": True, "hard_hourly_bucket": expired_bucket.replace(tzinfo=None).isoformat()}),
            _action("hard-legacy", task, scheduled_at=now - timedelta(minutes=5), payload={"hard_hourly_target": True}),
            _action("hard-false", task, scheduled_at=now - timedelta(minutes=4), payload={"hard_hourly_target": False}),
            _action("partial-membership", partial, scheduled_at=now - timedelta(minutes=3), payload={}, action_type="ensure_target_membership"),
            _action("partial-send", partial, scheduled_at=now - timedelta(minutes=2), payload={}),
        ])
        session.commit()
        monkeypatch.setattr(planner_backlog, "_now", lambda: now)

        task_snapshot = planner_backlog.planner_backlog_snapshot(session, task)
        partial_snapshot = planner_backlog.planner_backlog_snapshot(session, partial)
        loaded_actions = list(session.scalars(select(Action)))

    assert len(loaded_actions) == 8
    assert task_snapshot["global_pending"] == 6
    assert task_snapshot["task_pending"] == 4
    assert task_snapshot["oldest_age_seconds"] == 20 * 60
    assert partial_snapshot["global_pending"] == 6
    assert partial_snapshot["task_pending"] == 1
