from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import UNKNOWN_OPERATION_STATUSES
from app.services.authorization_dr.online_abc_manifest import ACTIVE_OPERATION_STATUSES
from app.services.authorization_dr.online_abc_release_interrupted import (
    STOPPED_BLOCKER,
    STOPPED_CLASSIFICATION,
    apply_release_interrupted_b,
    preview_release_interrupted_b,
    readback_release_interrupted_b,
)
from app.services.authorization_dr.online_abc_release_interrupted_state import (
    STOPPED_UNKNOWN_BOUNDARY,
    STOPPED_UNKNOWN_SOURCE_BLOCKER,
)
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_release_interrupted as release_tests


pytestmark = pytest.mark.no_postgres
INTERRUPTION_KEY = "abc-b-preflow-unknown:test:101:v1"
INTERRUPTION_REF = "ssh-runner:timeout:account101"
APPROVAL_REF = "user-approved-b-preflow-unknown"


@pytest.fixture
def db_session():
    fixture = abc_tests.session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_preview_accepts_exact_stopped_timeout_and_is_read_only(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_unknown_item(db_session)
    before = _snapshot(db_session, batch_id, account_id, operation_id=operation_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["boundary"] == STOPPED_UNKNOWN_BOUNDARY
    assert preview["classification"] == STOPPED_CLASSIFICATION
    assert preview["blocker_code"] == STOPPED_BLOCKER
    assert preview["operation_status"] == "reconcile_unknown"
    assert preview["remote_call_state"] == "unknown"
    assert preview["runtime_release_sha"] == abc_tests.RELEASE_SHA
    assert preview["interrupted_flow"] is None
    assert preview["primary"]["state"] == "legacy_frozen"
    assert _snapshot(db_session, batch_id, account_id, operation_id=operation_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_closes_remote_unproven_and_preserves_a(db_session) -> None:
    batch_id, account_id, operation_id = _stopped_unknown_item(db_session)
    before_a = _a_snapshot(db_session, account_id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    before_effect = operation.remote_effect_started_at
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    batch = db_session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = release_tests._item(db_session, batch_id, account_id)
    slots = release_tests._slots(db_session, item.id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert result["already_applied"] is False and result["boundary"] == STOPPED_UNKNOWN_BOUNDARY
    assert batch.status == "running" and batch.execution_release_sha == abc_tests.RELEASE_SHA
    assert item.status == item.outcome == "manual_required"
    assert slots["standby_1"].outcome == slots["standby_2"].outcome == "manual_required"
    assert slots["standby_1"].operation_id == operation.id and slots["standby_2"].operation_id is None
    assert (operation.status, operation.remote_call_state) == ("manual_required", "reconciled_hold")
    assert operation.blocker_code == STOPPED_BLOCKER and operation.remote_effect_started_at == before_effect
    assert case and case.classification == STOPPED_CLASSIFICATION and case.status == "applied"
    assert case.evidence_manifest["boundary"] == STOPPED_UNKNOWN_BOUNDARY
    assert _a_snapshot(db_session, account_id) == before_a
    assert _operation_count(db_session, UNKNOWN_OPERATION_STATUSES) == 0
    assert _operation_count(db_session, ACTIVE_OPERATION_STATUSES) == 0


def test_apply_is_idempotent_and_readback_keeps_same_fingerprint(db_session) -> None:
    batch_id, account_id, _ = _stopped_unknown_item(db_session)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    readback = readback_release_interrupted_b(
        db_session, batch_id, account_id, idempotency_key=INTERRUPTION_KEY,
    )

    assert first["already_applied"] is False and second["already_applied"] is True
    assert readback == second and readback["fingerprint"] == preview["fingerprint"]
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint="f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("blocker_code", "ConnectionError"),
        ("login_challenge_sent_at", _now()),
        ("login_code_message_id", "telegram-message"),
        ("candidate_authorization_id", 1),
        ("owner_node_id", "unexpected-owner"),
        ("finished_at", _now()),
    ],
)
def test_preview_rejects_any_non_frozen_operation_fact(db_session, field, value) -> None:
    batch_id, account_id, operation_id = _stopped_unknown_item(db_session)
    setattr(db_session.get(TgAuthorizationDrOperation, operation_id), field, value)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_release_interrupted_state_invalid"


def test_preview_rejects_a_drift_or_second_unknown(db_session) -> None:
    batch_id, account_id, _ = _stopped_unknown_item(db_session)
    db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_primary_drift"

    db_session.get(TgAccount, account_id).connection_generation -= 1
    abc_tests._add_operation(db_session, abc_tests.ACCOUNT_IDS[1], "second-unknown", "reconcile_unknown")
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_release_interrupted_runtime_active"


def _stopped_unknown_item(session) -> tuple[str, int, str]:
    batch_id, account_id, operation_id = release_tests._interrupted_item(session, include_flow=False)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = release_tests._item(session, batch_id, account_id)
    slots = release_tests._slots(session, item.id)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    batch.status = "stopped"
    item.status = "stopped"
    item.outcome = item.blocker_code = "reconcile_unknown"
    operation.status = "reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = STOPPED_UNKNOWN_SOURCE_BLOCKER
    operation.operation_version = 3
    slots["standby_1"].outcome = "reconcile_unknown"
    slots["standby_1"].blocker_code = STOPPED_UNKNOWN_SOURCE_BLOCKER
    slots["standby_1"].operation_id = operation.id
    session.commit()
    return batch_id, account_id, operation_id


def _preview(session, batch_id: str, account_id: int) -> dict:
    return preview_release_interrupted_b(
        session,
        batch_id,
        account_id,
        runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=INTERRUPTION_KEY,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return apply_release_interrupted_b(
        session,
        batch_id,
        account_id,
        runtime_release_sha=abc_tests.RELEASE_SHA,
        idempotency_key=INTERRUPTION_KEY,
        expected_fingerprint=fingerprint,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _snapshot(session, batch_id: str, account_id: int, *, operation_id: str) -> tuple:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = release_tests._item(session, batch_id, account_id)
    slots = release_tests._slots(session, item.id)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    return (
        batch.status, batch.version, batch.execution_release_sha,
        item.status, item.outcome, item.blocker_code, item.version,
        slots["standby_1"].outcome, slots["standby_1"].operation_id,
        slots["standby_2"].outcome, slots["standby_2"].operation_id,
        operation.status, operation.remote_call_state, operation.blocker_code,
        operation.operation_version, operation.reconcile_case_id,
        _a_snapshot(session, account_id),
    )


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id, account.session_ciphertext,
        account.authorization_generation, account.authorization_fact_generation,
        account.connection_generation, primary.session_ciphertext, primary.fact_version,
        primary.status, primary.health_status, primary.is_current, primary.is_slot_current,
    )


def _operation_count(session, statuses: set[str]) -> int:
    return int(session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(statuses),
    )) or 0)
