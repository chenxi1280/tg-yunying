from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.database import Base
from app.models import Action, Task, Tenant
from app.services._common import _now
from app.services.task_center import service


def _session_factory():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, future=True)


def test_group_ai_chat_planner_ignores_future_membership_actions(monkeypatch) -> None:
    session_factory = _session_factory()
    now_value = _now()
    planned: list[str] = []

    def fake_build_task_plan(_session: Session, task: Task) -> int:
        planned.append(task.id)
        return 1

    monkeypatch.setattr(service, "build_task_plan", fake_build_task_plan)

    with session_factory() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            Task(
                id="ai-membership-open",
                tenant_id=1,
                name="AI 活跃群",
                type="group_ai_chat",
                status="running",
                next_run_at=now_value - timedelta(seconds=1),
                account_config={},
                pacing_config={},
                type_config={},
                stats={},
            )
        )
        session.add(
            Action(
                id="membership-future",
                tenant_id=1,
                task_id="ai-membership-open",
                task_type="group_ai_chat",
                action_type="ensure_target_membership",
                status="pending",
                scheduled_at=now_value + timedelta(hours=1),
                payload={"channel_target_id": 26},
            )
        )
        session.commit()

    assert service.drain_task_planner(session_factory, 10) == 1
    assert planned == ["ai-membership-open"]
