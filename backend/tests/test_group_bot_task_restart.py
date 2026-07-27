from __future__ import annotations

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupBotRequiredChannelFollow, Task, Tenant
from app.services.task_center.group_bot_admission import ensure_admission_after_join, ingest_trusted_bot_prompt
from app.services.task_center.service import _mark_task_started

pytestmark = pytest.mark.no_postgres


def test_start_rebuilds_stopped_follow_and_confirmation_once() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="t"))
        task = Task(
            id="task-ai-1",
            tenant_id=1,
            name="ai",
            type="group_ai_chat",
            status="stopped",
            type_config={"target_group_id": 7, "hourly_min_messages": 30},
        )
        session.add(task)
        session.flush()
        admission = ensure_admission_after_join(
            session,
            tenant_id=1,
            group_id=7,
            account_id=11,
            membership_action_id="join-1",
            join_start_cursor="100",
        )
        ingest_trusted_bot_prompt(
            session,
            admission=admission,
            message_id="bot-1",
            text="请关注 @school_news 后确认",
            bot_peer_id="900",
            is_admin_bot=True,
            control_buttons=[
                {"row": 0, "col": 0, "text": "关注频道", "url": "https://t.me/school_news", "action_type": "url"},
                {"row": 1, "col": 0, "text": "完成验证", "action_type": "callback"},
            ],
            bound_task_id=task.id,
        )
        original_actions = list(session.scalars(select(Action).order_by(Action.id)))
        assert len(original_actions) == 2
        for action in original_actions:
            action.status = "skipped"
            action.result = {"success": False, "error_code": "task_stopped"}
        session.flush()

        _mark_task_started(session, task)
        _mark_task_started(session, task)

        actions = list(session.scalars(select(Action).order_by(Action.created_at, Action.id)))
        pending = [action for action in actions if action.status == "pending"]
        assert len(actions) == 4
        assert len(pending) == 2
        assert {action.action_type for action in pending} == {
            "group_bot_channel_follow",
            "group_bot_confirmation_button",
        }
        follow = session.scalar(select(GroupBotRequiredChannelFollow))
        assert follow is not None
        assert follow.action_id in {action.id for action in pending}
        assert task.status == "running"
        assert all(action.status == "skipped" for action in original_actions)
