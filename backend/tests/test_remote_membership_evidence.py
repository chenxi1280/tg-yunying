from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import pytest

from app.database import Base
from app.integrations.telegram.contracts import OperationResult
from app.models import (
    Action,
    ExecutionAttempt,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
)
from app.services._common import _now
from app.services.task_center.remote_membership_evidence import (
    preview_membership_probe_evidence,
)
from app.services.task_center.remote_reconciliation import (
    ensure_remote_reconcile_case,
)


pytestmark = pytest.mark.no_postgres


def test_membership_probe_evidence_requires_live_send_capability() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, attempt = _unknown_membership_action(session)
        case = ensure_remote_reconcile_case(session, action, attempt)
        observed: list[tuple[str, bool]] = []

        class Gateway:
            def probe_target_capabilities(self, _account_id, peer_id, _type, *_args, require_send):
                observed.append((peer_id, require_send))
                return OperationResult(True, detail="membership observed")

        evidence = preview_membership_probe_evidence(
            session,
            case.id,
            gateway_client=Gateway(),
            credentials_resolver=lambda *_args: None,
        )

        assert evidence.result == "remote_confirmed"
        assert evidence.source == "membership_reprobe_read_only"
        assert evidence.remote_fact_id.startswith("membership_observed:")
        assert evidence.exact_match_count == 1
        assert observed == [("-10081", True)]


def _unknown_membership_action(session: Session) -> tuple[Action, ExecutionAttempt]:
    now = _now()
    session.add_all([
        Tenant(id=1, name="tenant"),
        Task(id="task-1", tenant_id=1, name="task", type="group_ai_chat", status="running"),
        TgAccount(
            id=81,
            tenant_id=1,
            display_name="account",
            phone_masked="81",
            session_ciphertext="session",
        ),
        OperationTarget(
            id=81,
            tenant_id=1,
            target_type="group",
            tg_peer_id="-10081",
            title="group",
        ),
    ])
    action = Action(
        id="membership-1",
        tenant_id=1,
        task_id="task-1",
        task_type="group_ai_chat",
        action_type="ensure_target_membership",
        account_id=81,
        scheduled_at=now,
        status="unknown_after_send",
        payload={
            "channel_id": "-10081",
            "channel_target_id": 81,
            "target_type": "group",
            "require_send": True,
        },
        result={"gateway_request_identity": "request-1"},
    )
    attempt = ExecutionAttempt(
        id="attempt-1",
        tenant_id=1,
        action_id=action.id,
        worker_id="worker",
        account_id=81,
        attempt_no=1,
        status="result_unknown",
        before_call_at=now,
        gateway_call_started_at=now,
        result_snapshot={"gateway_request_identity": "request-1"},
    )
    session.add_all([action, attempt])
    session.flush()
    return action, attempt
