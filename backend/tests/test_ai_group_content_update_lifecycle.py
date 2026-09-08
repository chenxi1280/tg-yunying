"""Both public content-update APIs preserve task lifecycle and pending work."""
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Tenant
from app.schemas import GroupAIChatTaskConfigUpdate, GroupAIChatTaskCreate, TaskSettingsUpdate
from app.services.task_center import service


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="content-update"))
        current.commit()
        yield current
    engine.dispose()


def _task_with_pending_work(session, status):
    task = service.create_group_ai_chat_task(session, 1, GroupAIChatTaskCreate(
        name="内容更新", target_group_id=7, topic_participation_rate=0.10), actor="tester")
    task.status = status
    task.next_run_at = service._now() + timedelta(minutes=30) if status in {"running", "pending"} else None
    task.scheduled_start = task.next_run_at if status == "pending" else None
    session.add(Action(id="content-update-pending", tenant_id=1, task_id=task.id,
        task_type=task.type, action_type="send_message", status="pending"))
    session.commit()
    return task


@pytest.mark.parametrize("api", ("settings", "type_config"))
@pytest.mark.parametrize("status", ("running", "paused", "stopped", "pending"))
@pytest.mark.parametrize("changed", (True, False))
def test_content_edit_never_restarts_or_reschedules_task(session, api, status, changed):
    task = _task_with_pending_work(session, status)
    original = (task.status, task.next_run_at, task.task_lifecycle_epoch, task.config_revision)
    topics = [{"title": "新话题", "weight": 1}] if changed else []
    if api == "settings":
        service.update_task_settings(session, 1, task.id,
            TaskSettingsUpdate(topic_directions=topics), actor="tester")
    else:
        service.update_group_ai_chat_config(session, 1, task.id,
            GroupAIChatTaskConfigUpdate(target_group_id=7, topic_directions=topics,
                topic_participation_rate=0.10), actor="tester")
    session.expire_all()
    assert (task.status, task.next_run_at, task.task_lifecycle_epoch, task.config_revision) == original
    action = session.get(Action, "content-update-pending")
    assert action is not None and action.status == "pending"
