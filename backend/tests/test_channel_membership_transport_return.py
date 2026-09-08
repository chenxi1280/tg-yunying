"""Returned unknown memberships must not indefinitely occupy unrelated targets."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ExecutionAttempt, OperationTarget, Task, Tenant, TgAccount
from app.services.task_center.channel_membership_runtime import membership_runtime_wait
from app.services.task_center.gateway_evidence_journal import (
    GatewayResultEvidence, bind_gateway_request_identity, record_gateway_result_evidence,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 9, 8, 8)


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="test"))
        current.flush()
        for number in (1, 2, 3):
            current.add(TgAccount(id=number, tenant_id=1, display_name=str(number), phone_masked="***"))
        current.add(Task(id="task", tenant_id=1, name="test", type="channel_like", status="running"))
        current.add(OperationTarget(id=1, tenant_id=1, target_type="channel", tg_peer_id="-1001", title="one"))
        current.add(OperationTarget(id=2, tenant_id=1, target_type="channel", tg_peer_id="-1002", title="two"))
        current.commit()
        yield current
    engine.dispose()


def previous_attempt(session, *, account_id=1, journal=True):
    action = Action(tenant_id=1, task_id="task", task_type="channel_like", account_id=account_id,
        action_type="ensure_target_membership", status="closed_unknown", task_lifecycle_epoch=1,
        payload={"channel_target_id": 1, "channel_id": "-1001"})
    session.add(action)
    session.flush()
    attempt = ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=account_id,
        task_lifecycle_epoch=1, status="result_unknown", gateway_call_started_at=NOW,
        after_call_at=NOW + timedelta(seconds=1))
    session.add(attempt)
    session.flush()
    bind_gateway_request_identity(action, attempt)
    proof = record_gateway_result_evidence(session, action, attempt,
        GatewayResultEvidence(failure_code="unknown", remote_mutation_started=None)) if journal else None
    if proof:
        proof.observed_at = NOW + timedelta(seconds=1)
    session.flush()
    return action, attempt, proof


def wait_for_target(session, *, target_id=2, account_id=1):
    action = Action(tenant_id=1, task_id="task", account_id=account_id,
        task_type="channel_like", action_type="ensure_target_membership", status="executing")
    return membership_runtime_wait(session, action,
        target=session.get(OperationTarget, target_id), now=NOW + timedelta(minutes=2))


def test_recorded_unknown_return_releases_other_target_without_changing_business(session):
    action, attempt, _ = previous_attempt(session)
    original = dict(attempt.result_snapshot)
    assert wait_for_target(session) is None
    assert action.status == "closed_unknown"
    assert attempt.status == "result_unknown"
    assert attempt.result_snapshot == original


@pytest.mark.parametrize("proof", ["journal", "ack"])
def test_terminated_unknown_still_blocks_same_account_same_target(session, proof):
    _, attempt, _ = previous_attempt(session, journal=proof == "journal")
    if proof == "ack":
        attempt.result_snapshot = {"transport_termination_state": "acknowledged"}
    session.flush()
    assert wait_for_target(session, target_id=1).code == "account_membership_inflight_wait"


def test_acknowledged_unknown_releases_other_target_without_journal(session):
    _, attempt, _ = previous_attempt(session, journal=False)
    attempt.result_snapshot = {"transport_termination_state": "acknowledged"}
    session.flush()
    assert wait_for_target(session) is None


@pytest.mark.parametrize("corruption", ["missing", "conflict", "hash", "evidence_hash", "request",
    "target", "observed", "account", "action", "epoch", "cancel", "unproven", "missing_target"])
def test_unproven_return_remains_blocking(session, corruption):
    action, attempt, journal = previous_attempt(session, journal=corruption != "missing")
    changes = {
        "conflict": (journal, "state", "conflict"),
        "hash": (journal, "result_fingerprint", "incorrect"),
        "evidence_hash": (journal, "evidence_hash", "incorrect"),
        "request": (journal, "gateway_request_identity", "incorrect"),
        "target": (journal, "target_fingerprint", "incorrect"),
        "observed": (journal, "observed_at", NOW - timedelta(seconds=1)),
        "account": (journal, "account_id", 2),
        "action": (journal, "action_id", "other"),
        "epoch": (action, "task_lifecycle_epoch", 2),
        "missing_target": (action, "payload", {}),
    }
    if corruption in changes:
        obj, field, value = changes[corruption]
        setattr(obj, field, value)
    elif corruption in {"cancel", "unproven"}:
        attempt.result_snapshot = {**attempt.result_snapshot,
            "transport_termination_state": "cancellation_unconfirmed" if corruption == "cancel" else "unproven"}
    session.flush()
    assert wait_for_target(session).code == "account_membership_inflight_wait"


def test_finished_unknowns_free_channel_slots_for_other_accounts(session):
    previous_attempt(session, account_id=1)
    previous_attempt(session, account_id=2)
    assert wait_for_target(session, target_id=1, account_id=3) is None


@pytest.mark.parametrize("kind", ["failed", "timeout", "connection_error", "release"])
def test_membership_reprobe_preserves_original_transport_and_identity(kind):
    from app.services.task_center import service
    from app.integrations.telegram.contracts import OperationResult

    original = {"transport_termination_state": "acknowledged", "gateway_request_identity": "original"}
    attempt = SimpleNamespace(result_snapshot=dict(original))
    action = SimpleNamespace(result={"error_message": "probe result"})
    task = SimpleNamespace(last_error="")
    kwargs = dict(action=action, task=task, latest_attempt=attempt, now=NOW)
    if kind == "release":
        service._release_unknown_membership_reprobe_result(**kwargs)
    elif kind == "failed":
        service._mark_unknown_membership_reprobe_failed(**kwargs, result=OperationResult(False))
    elif kind == "timeout":
        service._mark_unknown_membership_reprobe_timeout(**kwargs, exc=TimeoutError("timeout"))
    else:
        service._mark_unknown_membership_reprobe_connection_error(**kwargs, exc=ConnectionError("closed"))
    assert all(attempt.result_snapshot[key] == value for key, value in original.items())
