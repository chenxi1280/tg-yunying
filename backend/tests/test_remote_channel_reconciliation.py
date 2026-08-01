from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ChannelMessage,
    ExecutionAttempt,
    OperationTarget,
    ReactionFulfillmentObligation,
    ReactionRemoteFact,
    Task,
    TaskDayLedger,
    Tenant,
    TgAccount,
    ViewFulfillmentObligation,
    ViewRemoteFact,
)
from app.services._common import _now
from app.services.task_center.channel_payloads import (
    LikeMessagePayload,
    ViewMessagePayload,
)
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence,
    bind_gateway_request_identity,
    record_gateway_result_evidence,
)
from app.services.task_center.remote_reconciliation import (
    apply_remote_reconcile_evidence,
    ensure_remote_reconcile_case,
    evidence_from_gateway_journal,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("kind", ["view", "reaction"])
def test_operation_journal_rebuilds_type_specific_remote_fact_once(kind: str) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, attempt, fact_model, obligation = _seed_operation_case(
            session, kind,
        )
        bind_gateway_request_identity(action, attempt)
        record_gateway_result_evidence(
            session,
            action,
            attempt,
            GatewayResultEvidence(
                remote_fact_id="700",
                remote_mutation_started=True,
            ),
        )
        case = ensure_remote_reconcile_case(session, action, attempt)
        evidence = evidence_from_gateway_journal(session, case.id)

        first = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-owner",
        )
        second = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-owner",
        )

        assert evidence.result == "remote_confirmed"
        assert evidence.remote_message_id == ""
        assert evidence.remote_fact_id == "700"
        assert first.changed is True
        assert second.changed is False
        assert action.status == "success"
        assert action.result["remote_fact_id"] == "700"
        assert obligation.status == "confirmed"
        assert session.scalar(select(func.count(fact_model.id))) == 1


def test_reaction_payload_drift_is_quarantined_before_fact_write() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        action, attempt, _, obligation = _seed_operation_case(
            session, "reaction",
        )
        bind_gateway_request_identity(action, attempt)
        record_gateway_result_evidence(
            session,
            action,
            attempt,
            GatewayResultEvidence(
                remote_fact_id="700",
                remote_mutation_started=True,
            ),
        )
        case = ensure_remote_reconcile_case(session, action, attempt)
        evidence = evidence_from_gateway_journal(session, case.id)
        action.payload = {**action.payload, "reaction_emoji": "👍"}

        outcome = apply_remote_reconcile_evidence(
            session, case.id, evidence, actor="release-owner",
        )

        assert outcome.state == "conflict"
        assert action.status == "unknown_after_send"
        assert obligation.status == "unknown"
        assert session.scalar(select(func.count(ReactionRemoteFact.id))) == 0


def _seed_operation_case(session: Session, kind: str):
    _seed_base_rows(session)
    task_id = f"task-{kind}"
    session.add(Task(
        id=task_id,
        tenant_id=1,
        name=kind,
        type="channel_view" if kind == "view" else "channel_like",
        status="running",
    ))
    ledger = _view_ledger(task_id) if kind == "view" else None
    if ledger is not None:
        session.add(ledger)
    session.flush()
    obligation = _operation_obligation(kind, task_id, ledger)
    action = _operation_action(kind, task_id, obligation.id)
    obligation.current_action_id = action.id
    obligation.status = "unknown"
    attempt = _unknown_attempt(action.id)
    session.add_all([obligation, action, attempt])
    session.flush()
    fact_model = ViewRemoteFact if kind == "view" else ReactionRemoteFact
    return action, attempt, fact_model, obligation


def _seed_base_rows(session: Session) -> None:
    session.add(Tenant(id=1, name="tenant"))
    session.add(TgAccount(
        id=11,
        tenant_id=1,
        display_name="account",
        phone_masked="***11",
        status="在线",
    ))
    session.add(OperationTarget(
        id=8,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-1008",
        title="channel",
    ))
    session.add(ChannelMessage(
        id=44,
        tenant_id=1,
        channel_target_id=8,
        message_id=700,
    ))


def _view_ledger(task_id: str) -> TaskDayLedger:
    start = datetime(2026, 8, 1, tzinfo=UTC)
    return TaskDayLedger(
        id="ledger-view",
        tenant_id=1,
        task_id=task_id,
        timezone_snapshot="Asia/Shanghai",
        timezone_revision=1,
        obligation_local_date=date(2026, 8, 1),
        period_start_at=start,
        deadline_at=start + timedelta(days=1),
        day_phase="full_day_committed",
        planning_anchor_at=start,
    )


def _operation_obligation(kind: str, task_id: str, ledger):
    if kind == "view":
        return ViewFulfillmentObligation(
            id="obligation-view",
            tenant_id=1,
            task_day_ledger_id=ledger.id,
            channel_message_id=44,
            account_id=11,
        )
    return ReactionFulfillmentObligation(
        id="obligation-reaction",
        tenant_id=1,
        task_id=task_id,
        channel_message_id=44,
        account_id=11,
        reaction_contract_version=1,
    )


def _operation_action(kind: str, task_id: str, obligation_id: str) -> Action:
    base = {
        "channel_id": "-1008",
        "channel_target_id": 8,
        "channel_message_id": 44,
        "message_id": 700,
    }
    if kind == "view":
        payload = ViewMessagePayload(
            **base,
            task_day_ledger_id="ledger-view",
            view_fulfillment_obligation_id=obligation_id,
        ).model_dump(mode="json")
    else:
        payload = LikeMessagePayload(
            **base,
            reaction_emoji="🔥",
            reaction_contract_version=1,
            reaction_fulfillment_obligation_id=obligation_id,
        ).model_dump(mode="json")
    return Action(
        id=f"action-{kind}",
        tenant_id=1,
        task_id=task_id,
        task_type="channel_view" if kind == "view" else "channel_like",
        action_type="view_message" if kind == "view" else "like_message",
        account_id=11,
        scheduled_at=_now(),
        status="unknown_after_send",
        payload=payload,
        result={"error_code": "unknown_after_send"},
    )


def _unknown_attempt(action_id: str) -> ExecutionAttempt:
    return ExecutionAttempt(
        id=f"attempt-{action_id}",
        tenant_id=1,
        action_id=action_id,
        account_id=11,
        attempt_no=1,
        status="result_unknown",
        before_call_at=_now() - timedelta(seconds=2),
        gateway_call_started_at=_now() - timedelta(seconds=1),
        after_call_at=_now(),
        failure_type="unknown_after_send",
        result_snapshot={},
    )
