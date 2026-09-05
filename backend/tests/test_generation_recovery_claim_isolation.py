from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant
from app.services._common import _now
from app.services.task_center import service as task_service
from app.services.task_center.ai_generation_recovery import (
    GenerationRecoveryClaimLost,
)


pytestmark = pytest.mark.no_postgres


def _stale_action(action_id: str, task_id: str) -> Action:
    now_value = _now()
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task_id,
        task_type="group_relay",
        action_type="send_message",
        status="executing",
        scheduled_at=now_value - timedelta(hours=1),
        lease_owner="expired-worker",
        lease_expires_at=now_value - timedelta(minutes=1),
        payload={},
        result={},
    )


def _seed_scope(session: Session, task_id: str) -> None:
    session.add_all([
        Tenant(id=1, name="tenant"),
        Task(
            id=task_id,
            tenant_id=1,
            name="claim loss isolation",
            type="group_relay",
            status="running",
            stats={},
        ),
        _stale_action("a-claim-lost", task_id),
        _stale_action("b-recoverable", task_id),
    ])
    session.commit()


def _claim_losing_recovery(action: Action, *, session: Session) -> bool:
    del session
    if action.id != "a-claim-lost":
        return False
    action.result = {"uncommitted": True}
    raise GenerationRecoveryClaimLost("generation_recovery_job_claim_lost")


def test_generation_claim_loss_does_not_abort_other_stale_recovery(monkeypatch) -> None:
    task_id = "generation-claim-loss-isolation-task"
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        _seed_scope(session, task_id)
        monkeypatch.setattr(
            task_service,
            "recover_stale_pre_gateway_generation",
            _claim_losing_recovery,
        )

        recovered = task_service._recover_stale_executing_actions(
            session,
            timeout_minutes=30,
            limit=2,
        )
        first = session.get(Action, "a-claim-lost")
        second = session.get(Action, "b-recoverable")

        assert recovered == 1
        assert first.status == "executing"
        assert first.result == {}
        assert first.claim_owner == ""
        assert first.claim_token == ""
        assert second.status == "failed"
        assert second.claim_owner == ""
        assert second.claim_token == ""
