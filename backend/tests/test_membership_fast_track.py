from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant
from app.services._common import _now
from app.services.task_center.membership_fast_track import fast_track_pending_hard_hourly_memberships


def test_fast_tracks_future_hard_hourly_membership_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all(
            [
                Task(
                    id="task-ai",
                    tenant_id=1,
                    name="AI 活跃群",
                    type="group_ai_chat",
                    status="running",
                    type_config={"hard_hourly_target_enabled": True, "hourly_min_messages": 60},
                ),
                Task(
                    id="task-normal",
                    tenant_id=1,
                    name="普通群",
                    type="group_ai_chat",
                    status="running",
                    type_config={"hard_hourly_target_enabled": False, "hourly_min_messages": 60},
                ),
                Action(
                    id="future-ai-1",
                    tenant_id=1,
                    task_id="task-ai",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=11,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=3),
                ),
                Action(
                    id="future-ai-2",
                    tenant_id=1,
                    task_id="task-ai",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=12,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=4),
                ),
                Action(
                    id="future-normal",
                    tenant_id=1,
                    task_id="task-normal",
                    task_type="group_ai_chat",
                    action_type="ensure_target_membership",
                    account_id=13,
                    status="pending",
                    scheduled_at=now_value + timedelta(hours=3),
                ),
            ]
        )
        session.commit()

        moved = fast_track_pending_hard_hourly_memberships(session, limit=10)
        rows = {action.id: action for action in session.query(Action).all()}
        task = session.get(Task, "task-ai")
        task_stats = dict(task.stats or {}) if task else {}

        assert moved == 2
        assert rows["future-ai-1"].scheduled_at <= now_value + timedelta(seconds=5)
        assert rows["future-ai-2"].scheduled_at <= now_value + timedelta(seconds=10)
        assert rows["future-ai-1"].result["fast_tracked_reason"] == "recovery_hard_hourly_membership"
        assert rows["future-normal"].scheduled_at == now_value + timedelta(hours=3)
        assert task_stats["membership_recovery_fast_tracked_actions"] == 2
