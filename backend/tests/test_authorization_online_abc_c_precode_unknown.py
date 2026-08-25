from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationOnlineAbcBatch,
)
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import UNKNOWN_OPERATION_STATUSES
from app.services.authorization_dr.online_abc_c_precode_interrupt_state import UNKNOWN_BOUNDARY
from app.services.authorization_dr.online_abc_manifest import ACTIVE_OPERATION_STATUSES
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_c_precode_interrupt as interrupt_tests


pytestmark = pytest.mark.no_postgres


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


def test_unknown_preview_is_read_only_and_accepts_same_release(db_session) -> None:
    batch_id, account_id, operation_id = _unknown_c(db_session)
    before = interrupt_tests._snapshot(db_session, batch_id, account_id)

    preview = interrupt_tests._preview(
        db_session, batch_id, account_id, release_sha=abc_tests.RELEASE_SHA,
    )

    assert preview["boundary"] == UNKNOWN_BOUNDARY
    assert preview["c_operation_id"] == operation_id
    assert preview["c_lease_expires_at"] == "None"
    assert preview["primary"]["state"] == "qualified"
    assert interrupt_tests._snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_unknown_apply_closes_same_c_and_preserves_a_and_b(db_session) -> None:
    batch_id, account_id, operation_id = _unknown_c(db_session)
    before_a = interrupt_tests._a_snapshot(db_session, account_id)
    preview = interrupt_tests._preview(db_session, batch_id, account_id)

    result = interrupt_tests._apply(
        db_session, batch_id, account_id, fingerprint=preview["fingerprint"],
    )

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    item = interrupt_tests._item(db_session, batch_id, account_id)
    slots = interrupt_tests._slots(db_session, item.id)
    migration_item = db_session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert result["boundary"] == UNKNOWN_BOUNDARY
    assert result["batch_status"] == "running"
    assert item.status == item.outcome == "manual_required"
    assert slots["standby_1"].outcome == "succeeded"
    assert slots["standby_2"].outcome == "manual_required"
    assert (operation.status, operation.remote_call_state) == (
        "manual_required", "reconciled_hold",
    )
    assert operation.reconcile_status == "applied" and operation.finished_at
    assert migration_item.status == "manual_required"
    assert case and case.evidence_manifest["boundary"] == UNKNOWN_BOUNDARY
    assert interrupt_tests._a_snapshot(db_session, account_id) == before_a
    assert _global_counts(db_session) == (0, 0, "off", "", 0)


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("candidate", "online_abc_c_precode_interrupt_state_invalid"),
        ("live_runtime", "online_abc_c_precode_interrupt_runtime_active"),
        ("extra_unknown", "online_abc_c_precode_interrupt_runtime_active"),
    ],
)
def test_unknown_preview_rejects_artifact_or_global_drift(
    db_session, mutation: str, expected_code: str,
) -> None:
    batch_id, account_id, operation_id = _unknown_c(db_session)
    if mutation == "candidate":
        db_session.get(TgAuthorizationDrOperation, operation_id).candidate_authorization_id = 99
    elif mutation == "live_runtime":
        runtime = db_session.get(AuthorizationDrRuntimeContract, 1)
        runtime.mode = "migrate"
        runtime.claim_scope_operation_id = operation_id
    else:
        abc_tests._add_operation(
            db_session, abc_tests.ACCOUNT_IDS[1], "extra-c-unknown", "provision_reconcile_unknown",
        ).remote_call_state = "unknown"
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        interrupt_tests._preview(db_session, batch_id, account_id)

    assert exc_info.value.code == expected_code


def test_unknown_apply_rejects_version_drift_without_writes(db_session) -> None:
    batch_id, account_id, operation_id = _unknown_c(db_session)
    preview = interrupt_tests._preview(db_session, batch_id, account_id)
    db_session.get(TgAuthorizationDrOperation, operation_id).operation_version += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        interrupt_tests._apply(
            db_session, batch_id, account_id, fingerprint=preview["fingerprint"],
        )

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert not db_session.scalar(select(func.count()).select_from(TgAuthorizationDrReconcileCase))


def _unknown_c(session) -> tuple[str, int, str]:
    batch_id, account_id, operation_id = interrupt_tests._interrupted_c(session)
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = interrupt_tests._item(session, batch_id, account_id)
    slots = interrupt_tests._slots(session, item.id)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    migration_item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    migration_batch = session.get(TgAuthorizationDrBatch, migration_item.batch_id)
    batch.status = "stopped"
    item.status = "stopped"
    item.outcome = item.blocker_code = "reconcile_unknown"
    item.primary_probe_outcome = "succeeded"
    item.version += 1
    slots["standby_2"].operation_id = operation.id
    slots["standby_2"].outcome = "reconcile_unknown"
    slots["standby_2"].blocker_code = "provision_reconcile_unknown"
    slots["standby_2"].version += 1
    operation.status = "provision_reconcile_unknown"
    operation.remote_call_state = "unknown"
    operation.blocker_code = "provision_reconcile_unknown"
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version += 1
    migration_item.status = "reconcile_unknown"
    migration_item.outcome = migration_item.blocker_code = "provision_reconcile_unknown"
    migration_batch.status = "reconcile_unknown"
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    runtime.mode = "off"
    runtime.claim_scope_operation_id = ""
    session.commit()
    return batch_id, account_id, operation_id


def _global_counts(session) -> tuple[int, int, str, str, int]:
    unknown = session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(UNKNOWN_OPERATION_STATUSES),
    ))
    sensitive = session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.status.in_(ACTIVE_OPERATION_STATUSES),
    ))
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    clients = session.get(AuthorizationDrExecutionNode, "my-node-1").active_client_count
    return unknown, sensitive, runtime.mode, runtime.claim_scope_operation_id, clients
