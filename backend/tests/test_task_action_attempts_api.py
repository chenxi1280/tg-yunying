import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, Task, Tenant
from app.services._common import _now
from app.services.task_center.service import list_action_attempts


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_task_action_attempts_are_scoped_by_tenant_task_and_action() -> None:
    now = _now()
    with _sqlite_session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(Task(id="task-attempt", tenant_id=1, name="任务", type="group_ai_chat", status="running"))
        session.add(
            Action(
                id="action-attempt",
                tenant_id=1,
                task_id="task-attempt",
                task_type="group_ai_chat",
                action_type="send_message",
                status="failed",
                scheduled_at=now,
            )
        )
        session.add_all(
            [
                ExecutionAttempt(
                    id="attempt-2",
                    tenant_id=1,
                    action_id="action-attempt",
                    attempt_no=2,
                    status="failed",
                    failure_type="FloodWait",
                    created_at=now,
                ),
                ExecutionAttempt(
                    id="attempt-1",
                    tenant_id=1,
                    action_id="action-attempt",
                    attempt_no=1,
                    status="after_call",
                    remote_message_id="100",
                    created_at=now,
                ),
            ]
        )
        session.commit()

        rows = list_action_attempts(session, 1, "task-attempt", "action-attempt")

        assert [row.id for row in rows] == ["attempt-1", "attempt-2"]
        assert rows[1].failure_type == "FloodWait"
        with pytest.raises(ValueError, match="action not found"):
            list_action_attempts(session, 1, "other-task", "action-attempt")
        with pytest.raises(ValueError, match="action not found"):
            list_action_attempts(session, 2, "task-attempt", "action-attempt")
