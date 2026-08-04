from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt
from app.services.task_center.dispatcher import _settle_pure_search_click_obligation
from app.services.task_center.executors.search_click_direct import _open_units
from app.services.task_center.search_click_safe_settlement import (
    SAFE_NOT_EXECUTED_FACT,
    settle_search_click_assignment_from_remote_fact,
)
from app.services.task_center.fulfillment_remote_facts import persist_remote_fact
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence,
    bind_gateway_request_identity,
    record_gateway_result_evidence,
)

from test_unknown_deadline_search_progress import _runtime, _unknown_assignment


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        yield current


def _failed_search_rows(session: Session):
    task, ledger, obligations = _runtime(session)
    obligation = obligations[0]
    assignment = _unknown_assignment(session, task, ledger, obligation)
    action = Action(
        id="safe-action",
        tenant_id=1,
        task_id=task.id,
        task_type="search_click",
        action_type="search_join",
        status="failed",
        payload={
            "search_click_assignment_id": assignment.id,
            "search_click_obligation_id": obligation.id,
        },
        result={
            "success": False,
            "error_code": "search_transport_unavailable",
            "remote_mutation_started": False,
        },
    )
    attempt = ExecutionAttempt(
        id="safe-attempt",
        tenant_id=1,
        action_id=action.id,
        account_id=1,
        attempt_no=0,
        status="failed",
        gateway_call_started_at=datetime(2026, 8, 4, 8, 1),
        result_snapshot={"remote_mutation_started": False},
    )
    assignment.action_id = action.id
    obligation.source_action_id = action.id
    session.add_all([action, attempt])
    session.flush()
    return task, ledger, obligation, assignment, action, attempt


def test_gateway_started_false_evidence_releases_direct_assignment(session: Session) -> None:
    task, _, obligation, assignment, action, attempt = _failed_search_rows(session)

    _settle_pure_search_click_obligation(session, action, attempt)

    assert assignment.state == SAFE_NOT_EXECUTED_FACT
    assert obligation.status == "open"
    assert obligation.source_action_id is None
    units = _open_units(session, datetime(2026, 8, 4, 8, 5), 1)
    assert [unit.obligation_id for unit in units] == [obligation.id]


def test_safe_fact_projection_is_idempotent(session: Session) -> None:
    _, _, obligation, assignment, action, _ = _failed_search_rows(session)
    first_version = assignment.version

    assert settle_search_click_assignment_from_remote_fact(
        session,
        action,
        SAFE_NOT_EXECUTED_FACT,
    )
    assert not settle_search_click_assignment_from_remote_fact(
        session,
        action,
        SAFE_NOT_EXECUTED_FACT,
    )

    assert assignment.state == SAFE_NOT_EXECUTED_FACT
    assert assignment.version == first_version + 1
    assert obligation.status == "open"
    assert obligation.source_action_id is None


def test_unknown_action_with_false_evidence_persists_safe_fact(session: Session) -> None:
    _, _, _, _, action, attempt = _failed_search_rows(session)
    action.status = "unknown_after_send"
    attempt.status = "result_unknown"

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == SAFE_NOT_EXECUTED_FACT


def test_typed_pre_accept_evidence_can_complete_legacy_unknown_journal(
    session: Session,
) -> None:
    _, _, _, _, action, attempt = _failed_search_rows(session)
    attempt.result_snapshot.pop("remote_mutation_started", None)
    bind_gateway_request_identity(action, attempt)
    record_gateway_result_evidence(
        session,
        action,
        attempt,
        GatewayResultEvidence(
            failure_code="jisou_image_verification_required",
        ),
    )
    attempt.result_snapshot["remote_mutation_started"] = False

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == SAFE_NOT_EXECUTED_FACT


def test_callback_unknown_cannot_be_downgraded_by_stale_false_snapshot(
    session: Session,
) -> None:
    _, _, _, _, action, attempt = _failed_search_rows(session)
    attempt.result_snapshot["remote_mutation_started"] = False
    action.result["callback_mutation_started"] = True
    bind_gateway_request_identity(action, attempt)
    record_gateway_result_evidence(
        session,
        action,
        attempt,
        GatewayResultEvidence(
            failure_code="verification_callback_result_unknown",
        ),
    )
    action.status = "unknown_after_send"
    attempt.status = "result_unknown"

    fact = persist_remote_fact(session, action)

    assert fact is not None
    assert fact.fact_kind == "remote_outcome_unknown"
