from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import (
    AuthorizationDrRuntimeContract,
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationOnlineAbcBatch,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc_c_precode_interrupt import (
    POST_CODE_BLOCKER,
    POST_CODE_CLASSIFICATION,
)
from app.services.authorization_dr.online_abc_c_precode_interrupt_state import (
    POST_CODE_UNKNOWN_BOUNDARY,
)
from app.services.authorization_dr.online_abc_operations import online_abc_item_operations
from tests import test_authorization_online_abc_c_precode_interrupt as interrupt_tests


pytestmark = pytest.mark.no_postgres


@pytest.fixture
def db_session():
    fixture = interrupt_tests.db_session.__wrapped__()
    session = next(fixture)
    try:
        yield session
    finally:
        try:
            next(fixture)
        except StopIteration:
            pass


def test_preview_freezes_stopped_postcode_unknown_without_writes(db_session) -> None:
    batch_id, account_id, operation_id = _post_code_unknown(db_session)
    before = interrupt_tests._snapshot(db_session, batch_id, account_id)

    preview = interrupt_tests._preview(db_session, batch_id, account_id)

    assert preview["c_operation_id"] == operation_id
    assert preview["boundary"] == POST_CODE_UNKNOWN_BOUNDARY
    assert preview["classification"] == POST_CODE_CLASSIFICATION
    assert preview["blocker_code"] == POST_CODE_BLOCKER
    assert preview["c_code_message_id"] == "303"
    assert preview["c_code_received_at"] != "None"
    assert interrupt_tests._snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_holds_only_c_and_preserves_a_b_and_remote_facts(db_session) -> None:
    batch_id, account_id, operation_id = _post_code_unknown(db_session)
    before_a = interrupt_tests._a_snapshot(db_session, account_id)
    before_b = _b_snapshot(db_session, batch_id, account_id)
    preview = interrupt_tests._preview(db_session, batch_id, account_id)

    result = interrupt_tests._apply(
        db_session, batch_id, account_id, fingerprint=preview["fingerprint"],
    )

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    item = interrupt_tests._item(db_session, batch_id, account_id)
    slots = interrupt_tests._slots(db_session, item.id)
    runtime = db_session.get(AuthorizationDrRuntimeContract, 1)
    assert result["classification"] == POST_CODE_CLASSIFICATION
    assert result["boundary"] == POST_CODE_UNKNOWN_BOUNDARY
    assert item.status == item.outcome == "manual_required"
    assert item.blocker_code == POST_CODE_BLOCKER
    assert slots["standby_1"].outcome == "succeeded"
    assert slots["standby_2"].outcome == "manual_required"
    assert operation.status == "manual_required"
    assert operation.remote_call_state == "reconciled_hold"
    assert operation.blocker_code == POST_CODE_BLOCKER
    assert operation.login_code_message_id == "303"
    assert operation.login_code_received_at is not None
    assert operation.candidate_authorization_id is None
    assert (runtime.mode, runtime.claim_scope_operation_id) == ("off", "")
    assert interrupt_tests._a_snapshot(db_session, account_id) == before_a
    assert _b_snapshot(db_session, batch_id, account_id) == before_b


@pytest.mark.parametrize(
    ("message_id", "received_at"),
    [("303", None), ("", _now())],
)
def test_preview_rejects_partial_postcode_facts(db_session, message_id, received_at) -> None:
    batch_id, account_id, operation_id = _post_code_unknown(db_session)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    operation.login_code_message_id = message_id
    operation.login_code_received_at = received_at
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        interrupt_tests._preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_c_precode_interrupt_state_invalid"


def test_apply_rejects_postcode_fact_drift(db_session) -> None:
    batch_id, account_id, operation_id = _post_code_unknown(db_session)
    preview = interrupt_tests._preview(db_session, batch_id, account_id)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    operation.login_code_received_at += timedelta(seconds=1)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        interrupt_tests._apply(
            db_session, batch_id, account_id, fingerprint=preview["fingerprint"],
        )

    assert exc_info.value.code == "migration_fingerprint_conflict"


def _post_code_unknown(session) -> tuple[str, int, str]:
    batch_id, account_id, operation_id = interrupt_tests._interrupted_c(session)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    item = interrupt_tests._item(session, batch_id, account_id)
    slots = interrupt_tests._slots(session, item.id)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    operation.status = "provision_reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = "provision_reconcile_unknown"
    operation.login_code_message_id = "303"
    operation.login_code_received_at = _now()
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version = 6
    item.status = "stopped"
    item.outcome = "reconcile_unknown"
    item.primary_probe_outcome = "succeeded"
    item.blocker_code = "reconcile_unknown"
    slots["standby_2"].outcome = "reconcile_unknown"
    slots["standby_2"].operation_id = operation.id
    slots["standby_2"].blocker_code = "provision_reconcile_unknown"
    batch.status = "stopped"
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    runtime.mode = "off"
    runtime.claim_scope_operation_id = ""
    session.commit()
    return batch_id, account_id, operation_id


def _b_snapshot(session, batch_id: str, account_id: int) -> tuple:
    item = interrupt_tests._item(session, batch_id, account_id)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    operation = online_abc_item_operations(session, batch, item)["b"]
    candidate = session.get(TgAccountAuthorization, operation.candidate_authorization_id)
    return (
        operation.status,
        operation.remote_call_state,
        operation.operation_version,
        candidate.id,
        candidate.session_ciphertext,
        candidate.status,
        candidate.health_status,
    )
