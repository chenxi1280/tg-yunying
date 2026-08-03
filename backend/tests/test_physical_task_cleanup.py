from __future__ import annotations

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.models import Action, ExecutionAttempt, FulfillmentRemoteFact, Task, Tenant
from app.services._common import _now
from app.services.task_center.physical_task_cleanup import delete_task_runtime_rows


pytestmark = pytest.mark.no_postgres


def test_cleanup_deletes_task_runtime_in_dependency_order() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=991338, name="cleanup-test"))
        session.add(Task(
            id="cleanup-task",
            tenant_id=991338,
            name="cleanup",
            type="channel_comment",
            status="deleting",
        ))
        session.flush()
        action = Action(
            id="cleanup-action",
            tenant_id=991338,
            task_id="cleanup-task",
            task_type="channel_comment",
            action_type="send_message",
            status="success",
        )
        attempt = ExecutionAttempt(
            id="cleanup-attempt",
            tenant_id=991338,
            action_id=action.id,
            status="success",
            gateway_call_started_at=_now(),
        )
        session.add_all([action, attempt, FulfillmentRemoteFact(
            fact_id="cleanup-fact",
            tenant_id=991338,
            task_type="channel_comment",
            task_id="cleanup-task",
            obligation_type="comment",
            obligation_id="cleanup-obligation",
            action_id=action.id,
            attempt_id=attempt.id,
            mutation_kind="send_message",
            remote_mutation_key_hash="a" * 64,
            gateway_request_hash="b" * 64,
            fact_kind="remote_message_observed",
            fact_identity_hash="c" * 64,
        )])
        session.flush()

        delete_task_runtime_rows(session, "cleanup-task")
        session.flush()

        assert session.scalar(select(func.count(Action.id))) == 0
        assert session.scalar(select(func.count(ExecutionAttempt.id))) == 0
        assert session.scalar(select(func.count(FulfillmentRemoteFact.fact_id))) == 0
