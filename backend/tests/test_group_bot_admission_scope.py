from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, GroupBotAdmission, Task, Tenant
from app.services.task_center.dispatcher import _group_bot_admission_window_busy


pytestmark = pytest.mark.no_postgres


def test_other_account_admission_does_not_block_same_group() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="单用户"))
        session.add(
            Task(
                id="ai-task",
                tenant_id=1,
                name="AI 活群",
                type="group_ai_chat",
                status="running",
            )
        )
        action = Action(
            id="membership-account-11",
            tenant_id=1,
            task_id="ai-task",
            task_type="group_ai_chat",
            action_type="ensure_channel_membership",
            account_id=11,
            status="pending",
        )
        session.add_all(
            [
                action,
                GroupBotAdmission(
                    tenant_id=1,
                    group_id=7,
                    account_id=22,
                    state="awaiting_group_bot_rule",
                ),
            ]
        )
        session.flush()

        assert _group_bot_admission_window_busy(session, action, 7) is False
