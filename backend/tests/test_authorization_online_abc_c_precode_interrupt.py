from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditLog,
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationDrStageFact,
    TgAuthorizationOnlineAbcBatch,
    TgAuthorizationOnlineAbcItem,
    TgAuthorizationOnlineAbcSlotResult,
)
from app.services._common import _now
from app.services.authorization_dr.contracts import AuthorizationDrError
from app.services.authorization_dr.online_abc import start_next_online_abc_item
from app.services.authorization_dr.online_abc_c_precode_interrupt import (
    ACTION,
    BLOCKER,
    CLASSIFICATION,
    apply_c_precode_interrupt,
    preview_c_precode_interrupt,
    readback_c_precode_interrupt,
)
from app.services.authorization_dr.online_abc_manifest import (
    apply_full_online_abc_batch,
    preview_full_online_abc_batch,
)
from tests import test_authorization_online_abc as abc_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "b" * 40
KEY = "abc-c-precode-interrupt:test:101:v1"
INTERRUPTION_REF = "my-log:lease-renew-502:operation-test"
APPROVAL_REF = "user-approved-c-precode-interrupt"


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


def test_preview_is_read_only_and_freezes_precode_c_boundary(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_c(db_session)
    before = _snapshot(db_session, batch_id, account_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["c_operation_id"] == operation_id
    assert preview["classification"] == CLASSIFICATION
    assert preview["previous_execution_release_sha"] == abc_tests.RELEASE_SHA
    assert preview["runtime_release_sha"] == NEW_RELEASE_SHA
    assert preview["primary"]["state"] == "qualified"
    assert preview["c_owner"] == ["my-node-1", 1]
    assert _snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_closes_only_c_as_manual_and_preserves_a_and_b(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_c(db_session)
    before_a = _a_snapshot(db_session, account_id)
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    item = _item(db_session, batch_id, account_id)
    slots = _slots(db_session, item.id)
    runtime = db_session.get(AuthorizationDrRuntimeContract, 1)
    migration_item = db_session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    migration_batch = db_session.get(TgAuthorizationDrBatch, migration_item.batch_id)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert result["already_applied"] is False
    assert result["batch_status"] == "running"
    assert item.status == item.outcome == "manual_required"
    assert item.primary_probe_outcome == "succeeded"
    assert slots["standby_1"].outcome == "succeeded"
    assert slots["standby_2"].outcome == "manual_required"
    assert slots["standby_2"].operation_id == operation.id
    assert (operation.status, operation.remote_call_state) == ("manual_required", "reconciled_hold")
    assert operation.blocker_code == BLOCKER and not operation.lease_token
    assert operation.lease_expires_at is None and operation.finished_at
    assert migration_item.status == "manual_required" and migration_batch.status == "manual_required"
    assert (runtime.mode, runtime.claim_scope_operation_id) == ("off", "")
    assert case and case.classification == CLASSIFICATION and case.status == "applied"
    assert _a_snapshot(db_session, account_id) == before_a
    audit_row = db_session.scalar(select(AuditLog).where(AuditLog.action == ACTION))
    assert audit_row and INTERRUPTION_REF in audit_row.detail


def test_apply_is_idempotent_and_readback_matches(db_session) -> None:
    batch_id, account_id, _ = _interrupted_c(db_session)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    readback = readback_c_precode_interrupt(
        db_session, batch_id, account_id, idempotency_key=KEY,
    )

    assert second == readback
    assert second["already_applied"] is True
    assert second["item_version"] == first["item_version"]
    assert second["interruption_ref"] == INTERRUPTION_REF
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint="f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("login_flow_id", 99),
        ("candidate_authorization_id", 1),
        ("reconcile_status", "previewed"),
        ("finished_at", _now()),
    ],
)
def test_preview_rejects_any_downstream_c_fact(db_session, field, value) -> None:
    batch_id, account_id, operation_id = _interrupted_c(db_session)
    setattr(db_session.get(TgAuthorizationDrOperation, operation_id), field, value)
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code == "online_abc_c_precode_interrupt_state_invalid"


def test_preview_rejects_live_lease_or_extra_stage(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_c(db_session)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    operation.lease_expires_at = _now() + timedelta(minutes=1)
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_state_invalid"

    operation.lease_expires_at = _now() - timedelta(minutes=1)
    db_session.add(TgAuthorizationDrStageFact(
        operation_id=operation.id,
        node_id="my-node-1",
        owner_epoch=1,
        stage="remote_login_confirmed",
        manifest_digest="c" * 64,
    ))
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_state_invalid"


def test_preview_rejects_a_drift_or_nonzero_my_client(db_session) -> None:
    batch_id, account_id, _ = _interrupted_c(db_session)
    db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_state_invalid"

    db_session.get(TgAccount, account_id).connection_generation -= 1
    db_session.get(AuthorizationDrExecutionNode, "my-node-1").active_client_count = 1
    db_session.commit()
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_runtime_active"


def test_preview_rejects_same_release_or_second_sensitive_operation(db_session) -> None:
    batch_id, account_id, _ = _interrupted_c(db_session)
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id, release_sha=abc_tests.RELEASE_SHA)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_batch_invalid"

    abc_tests._add_operation(db_session, abc_tests.ACCOUNT_IDS[1], "second-sensitive", "approved")
    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)
    assert exc_info.value.code == "online_abc_c_precode_interrupt_runtime_active"


def test_apply_rechecks_operation_version_under_lock(db_session) -> None:
    batch_id, account_id, operation_id = _interrupted_c(db_session)
    preview = _preview(db_session, batch_id, account_id)
    db_session.get(TgAuthorizationDrOperation, operation_id).operation_version += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    assert exc_info.value.code == "migration_fingerprint_conflict"
    assert not db_session.scalar(select(func.count()).select_from(TgAuthorizationDrReconcileCase))


def _interrupted_c(session) -> tuple[str, int, str]:
    batch_id = _new_full_batch(session)
    command = start_next_online_abc_item(session, batch_id, actor="approver", approval_ref="ABC-FULL")
    account_id = command["account_id"]
    abc_tests._qualify_primary(session, account_id)
    primary = session.get(TgAccountAuthorization, session.get(TgAccount, account_id).current_authorization_id)
    b_operation = abc_tests._add_operation(session, account_id, command["b_idempotency_key"], "succeeded")
    candidate = _add_b_candidate(session, account_id, primary)
    b_operation.candidate_authorization_id = candidate.id
    b_operation.source_authorization_id = primary.id
    b_operation.code_source_authorization_id = primary.id
    c_result = abc_tests._add_c_operation(
        session, account_id, command["c_idempotency_key"], "login_remote_started",
    )
    operation = session.get(TgAuthorizationDrOperation, c_result["operation_id"])
    migration_item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.operation_id == operation.id,
    ))
    operation.batch_item_id = migration_item.id
    operation.operation_type = "provision_standby_2"
    operation.logical_slot = "standby_2"
    operation.code_source_authorization_id = primary.id
    operation.expected_current_authorization_id = primary.id
    operation.expected_authorization_generation = session.get(TgAccount, account_id).authorization_generation
    operation.expected_authorization_fact_generation = session.get(TgAccount, account_id).authorization_fact_generation
    operation.expected_connection_generation = session.get(TgAccount, account_id).connection_generation
    operation.expected_code_source_fact_version = primary.fact_version
    operation.expected_code_source_user_id_digest = primary.telegram_user_id_digest
    operation.expected_code_source_auth_key_digest = primary.auth_key_fingerprint_digest
    operation.remote_call_state = "started"
    operation.remote_effect_started_at = _now()
    operation.login_challenge_sent_at = _now()
    operation.owner_node_id = "my-node-1"
    operation.owner_epoch = 1
    operation.lease_token = "expired-lease"
    operation.lease_expires_at = _now() - timedelta(minutes=1)
    operation.operation_version = 3
    migration_item.status = migration_item.outcome = "running"
    session.get(TgAuthorizationDrBatch, migration_item.batch_id).status = "running"
    _arm_runtime_and_node(session, operation.id)
    session.add(TgAuthorizationDrStageFact(
        operation_id=operation.id,
        node_id="my-node-1",
        owner_epoch=1,
        stage="remote_login_started",
        manifest_digest="a" * 64,
    ))
    session.commit()
    return batch_id, account_id, operation.id


def _add_b_candidate(session, account_id: int, primary) -> TgAccountAuthorization:
    candidate = TgAccountAuthorization(
        id=3000 + account_id,
        tenant_id=1,
        account_id=account_id,
        role="standby_1",
        logical_slot="standby_1",
        provision_region_code="sv",
        developer_app_id=1,
        proxy_id=primary.proxy_id,
        session_ciphertext=f"b-{account_id}",
        status="standby",
        health_status="healthy",
        is_current=False,
        is_slot_current=True,
        protected_from_cleanup=True,
        telegram_user_id_digest=primary.telegram_user_id_digest,
        auth_key_fingerprint_digest="3" * 64,
    )
    session.add(candidate)
    session.flush()
    return candidate


def _arm_runtime_and_node(session, operation_id: str) -> None:
    node = AuthorizationDrExecutionNode(
        id="my-node-1",
        region_code="my",
        purpose="standby_session_dr",
        capability_version="2.21-abc-a-source",
        runtime_image_sha="c" * 40,
        standby_egress_id="my-egress-1",
        status="ready",
        active_client_count=0,
        last_heartbeat_at=_now(),
    )
    session.add(node)
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    runtime.mode = "migrate"
    runtime.claim_scope_operation_id = operation_id
    runtime.required_node_capability_version = node.capability_version
    runtime.required_node_runtime_image_sha = node.runtime_image_sha


def _new_full_batch(session) -> str:
    abc_tests._seed_accepted_canary(session)
    preview = preview_full_online_abc_batch(
        session, 1, idempotency_key="full-c-precode-interrupt", deployed_release_sha=abc_tests.RELEASE_SHA,
    )
    return apply_full_online_abc_batch(
        session,
        1,
        idempotency_key="full-c-precode-interrupt",
        deployed_release_sha=abc_tests.RELEASE_SHA,
        expected_fingerprint=preview["fingerprint"],
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-FULL",
    )["batch_id"]


def _preview(session, batch_id: str, account_id: int, *, release_sha: str = NEW_RELEASE_SHA) -> dict:
    return preview_c_precode_interrupt(
        session,
        batch_id,
        account_id,
        runtime_release_sha=release_sha,
        idempotency_key=KEY,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return apply_c_precode_interrupt(
        session,
        batch_id,
        account_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=KEY,
        expected_fingerprint=fingerprint,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _item(session, batch_id: str, account_id: int):
    return session.scalar(select(TgAuthorizationOnlineAbcItem).where(
        TgAuthorizationOnlineAbcItem.batch_id == batch_id,
        TgAuthorizationOnlineAbcItem.account_id == account_id,
    ))


def _slots(session, item_id: str) -> dict:
    rows = session.scalars(select(TgAuthorizationOnlineAbcSlotResult).where(
        TgAuthorizationOnlineAbcSlotResult.item_id == item_id,
    ))
    return {row.logical_slot: row for row in rows}


def _a_snapshot(session, account_id: int) -> tuple:
    account = session.get(TgAccount, account_id)
    primary = session.get(TgAccountAuthorization, account.current_authorization_id)
    return (
        account.current_authorization_id,
        account.session_ciphertext,
        account.authorization_generation,
        account.authorization_fact_generation,
        account.connection_generation,
        primary.session_ciphertext,
        primary.fact_version,
        primary.status,
        primary.health_status,
    )


def _snapshot(session, batch_id: str, account_id: int) -> tuple:
    batch = session.get(TgAuthorizationOnlineAbcBatch, batch_id)
    item = _item(session, batch_id, account_id)
    slots = _slots(session, item.id)
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.operation_type == "provision_standby_2",
    ))
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    return (
        batch.status,
        batch.version,
        batch.execution_release_sha,
        item.status,
        item.outcome,
        item.version,
        slots["standby_1"].outcome,
        slots["standby_1"].version,
        slots["standby_2"].outcome,
        slots["standby_2"].version,
        operation.status,
        operation.operation_version,
        operation.lease_token,
        operation.lease_expires_at,
        runtime.mode,
        runtime.claim_scope_operation_id,
        _a_snapshot(session, account_id),
    )
