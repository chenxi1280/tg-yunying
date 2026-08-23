from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    ExecutionAttempt,
    FulfillmentObligationProjection,
    FulfillmentRemoteFact,
)
from app.services.task_center.dispatcher import (
    _finalize_fact_first_dispatch,
    _settle_pure_search_click_obligation,
)
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
from scripts.reconcile_search_click_pre_accept_absence import (
    _load_row,
    _rebase_closed_unknown,
    _record_pre_accept_receipt,
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


@pytest.mark.parametrize(
    ("error_code", "event_type", "page_phase"),
    (
        (
            "bot_human_verification_required",
            "page_classified",
            "verification_page",
        ),
        (
            "jisou_image_verification_failed",
            "image_verification_failed",
            "verification_image_page",
        ),
    ),
)
def test_typed_pre_accept_receipt_reopens_closed_unknown_obligation(
    session: Session,
    error_code: str,
    event_type: str,
    page_phase: str,
) -> None:
    obligation, assignment, action = _closed_pre_accept_rows(
        session,
        error_code=error_code,
        event_type=event_type,
        page_phase=page_phase,
    )
    row = _load_row(session, action.task_id, assignment)
    _rebase_closed_unknown(row)
    _record_pre_accept_receipt(row)
    _finalize_fact_first_dispatch(session, action)

    assert assignment.state == SAFE_NOT_EXECUTED_FACT
    assert obligation.status == "open"
    assert obligation.source_action_id is None
    assert action.result["pre_accept_rejection"]["remote_mutation_started"] is False


def _closed_pre_accept_rows(
    session: Session,
    *,
    error_code: str,
    event_type: str,
    page_phase: str,
):
    task, ledger, obligation, assignment, action, attempt = _failed_search_rows(session)
    action.result = {
        "success": False,
        "error_code": error_code,
        "protocol_event_type": event_type,
        "jisou_page_phase": page_phase,
    }
    attempt.result_snapshot = {}
    bind_gateway_request_identity(action, attempt)
    record_gateway_result_evidence(
        session,
        action,
        attempt,
        GatewayResultEvidence(failure_code=error_code),
    )
    action.status = "closed_unknown"
    action.obligation_type = "search_click"
    action.obligation_id = obligation.id
    assignment.state = "closed_unknown"
    obligation.status = "closed_unknown"
    _add_closed_fact_and_projection(
        session,
        error_code=error_code,
        task=task,
        ledger=ledger,
        obligation=obligation,
        action=action,
        attempt=attempt,
    )
    session.flush()
    return obligation, assignment, action


def _add_closed_fact_and_projection(
    session,
    *,
    error_code,
    task,
    ledger,
    obligation,
    action,
    attempt,
) -> None:
    session.add(FulfillmentObligationProjection(
        tenant_id=1,
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_type="search_click",
        obligation_id=obligation.id,
        work_lane="search",
        deadline_at=ledger.deadline_at,
        state="closed_with_unknown_shortfall",
        active_action_id=action.id,
    ))
    session.add(FulfillmentRemoteFact(
        fact_id=f"closed-{error_code}",
        tenant_id=1,
        task_type="search_click",
        task_id=task.id,
        task_day_ledger_id=ledger.id,
        obligation_type="search_click",
        obligation_id=obligation.id,
        action_id=action.id,
        attempt_id=attempt.id,
        mutation_kind="search_join",
        remote_mutation_key_hash=f"{error_code}:mutation",
        gateway_request_hash=f"{error_code}:request",
        fact_kind="unknown_deadline_closed",
        fact_identity_hash=f"{error_code}:closed",
        outcome={},
    ))
