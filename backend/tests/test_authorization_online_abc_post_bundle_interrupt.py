from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import func, select

from app.models import (
    AuditLog,
    AuthorizationDrRuntimeContract,
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationDrStageFact,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
)
from app.services._common import _now
from app.services.authorization_dr.artifact_reconcile import claim_artifact_reconcile
from app.services.authorization_dr.contracts import (
    AuthorizationDrError,
    CopyReceipt,
    RestoreProbeReceipt,
    WakeBundleReceipt,
)
from app.services.authorization_dr.online_abc_post_bundle_interrupt import (
    ACTION,
    BLOCKER,
    CLASSIFICATION,
    apply_post_bundle_interrupt,
    preview_post_bundle_interrupt,
    readback_post_bundle_interrupt,
)
from app.services.authorization_dr.online_abc_runner import resume_online_abc_batch
from app.services.authorization_dr.slot import commit_migration_slot
from app.services.authorization_dr.wake_bundle import (
    commit_wake_bundle_receipt,
    record_restore_probe,
)
from tests import test_authorization_online_abc as abc_tests
from tests import test_authorization_online_abc_c_precode_interrupt as interrupt_tests


pytestmark = pytest.mark.no_postgres
NEW_RELEASE_SHA = "d" * 40
KEY = "abc-post-bundle-interrupt:test:101:v1"
APPROVAL_REF = "user-approved-post-bundle-forward"
INTERRUPTION_REF = "my-log:restore-probe-502:operation-test"


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


def test_preview_is_read_only_and_freezes_central_bundle(db_session) -> None:
    batch_id, account_id, operation_id, _ = _post_bundle_interrupt(db_session)
    before = _snapshot(db_session, batch_id, account_id)

    preview = _preview(db_session, batch_id, account_id)

    assert preview["c_operation"][0] == operation_id
    assert preview["classification"] == CLASSIFICATION
    assert preview["artifact"]["bundle"][3:] == ["copies_verified", 2]
    assert len(preview["artifact"]["copies"]) == 2
    assert preview["primary"]["state"] == "qualified"
    assert _snapshot(db_session, batch_id, account_id) == before
    assert not db_session.new and not db_session.dirty and not db_session.deleted


def test_apply_approves_same_operation_and_stops_runtime(db_session) -> None:
    batch_id, account_id, operation_id, _ = _post_bundle_interrupt(db_session)
    before_a = interrupt_tests._a_snapshot(db_session, account_id)
    before_artifact = _artifact_snapshot(db_session, operation_id)
    preview = _preview(db_session, batch_id, account_id)

    result = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    item = interrupt_tests._item(db_session, batch_id, account_id)
    runtime = db_session.get(AuthorizationDrRuntimeContract, 1)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert result["already_applied"] is False
    assert result["batch_status"] == "stopped"
    assert (item.status, item.outcome, item.blocker_code) == (
        "stopped", "runner_blocked", BLOCKER,
    )
    assert (runtime.mode, runtime.claim_scope_operation_id) == ("off", "")
    assert operation.status == "bundle_copies_verified"
    assert operation.remote_call_state == "confirmed"
    assert operation.reconcile_status == "repair_approved"
    assert case and case.status == "repair_approved"
    assert case.classification == CLASSIFICATION
    assert case.persisted_artifact_state == "central_bundle"
    assert _artifact_snapshot(db_session, operation_id) == before_artifact
    assert interrupt_tests._a_snapshot(db_session, account_id) == before_a
    audit_row = db_session.scalar(select(AuditLog).where(AuditLog.action == ACTION))
    assert audit_row and INTERRUPTION_REF in audit_row.detail


def test_approved_bundle_reclaims_without_login_and_resumes_post_c(db_session) -> None:
    batch_id, account_id, operation_id, receipt = _post_bundle_interrupt(db_session)
    before_a = interrupt_tests._a_snapshot(db_session, account_id)
    preview = _preview(db_session, batch_id, account_id)
    _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])

    claim = claim_artifact_reconcile(db_session, operation_id, "my-node-1")
    owner = {
        "node_id": "my-node-1",
        "owner_epoch": claim["owner_epoch"],
        "lease_token": claim["lease_token"],
    }
    commit_wake_bundle_receipt(db_session, operation_id, receipt, **owner)
    record_restore_probe(db_session, operation_id, _probe_receipt(receipt.bundle_generation), **owner)
    commit_migration_slot(db_session, operation_id, **owner)
    resumed = resume_online_abc_batch(
        db_session,
        batch_id,
        requested_by="requester",
        approved_by="approver",
        approval_ref="ABC-FULL",
        runtime_release_sha=NEW_RELEASE_SHA,
        account_id=account_id,
    )

    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    case = db_session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    assert operation.status == "succeeded"
    assert operation.reconcile_status == "applied"
    assert case.status == "applied"
    assert resumed["current_item"]["status"] == "running"
    assert resumed["next_action"] == "verify_e4"
    assert interrupt_tests._a_snapshot(db_session, account_id) == before_a
    assert db_session.scalar(select(func.count()).select_from(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.operation_type == "provision_standby_2",
    )) == 1


def test_apply_is_idempotent_and_readback_matches(db_session) -> None:
    batch_id, account_id, _, _ = _post_bundle_interrupt(db_session)
    preview = _preview(db_session, batch_id, account_id)

    first = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    second = _apply(db_session, batch_id, account_id, fingerprint=preview["fingerprint"])
    readback = readback_post_bundle_interrupt(
        db_session, batch_id, account_id, idempotency_key=KEY,
    )

    assert second == readback
    assert second["already_applied"] is True
    assert second["item_version"] == first["item_version"]
    with pytest.raises(AuthorizationDrError) as exc_info:
        _apply(db_session, batch_id, account_id, fingerprint="f" * 64)
    assert exc_info.value.code == "idempotency_key_conflict"


@pytest.mark.parametrize("mutation", ["live_lease", "probe", "copy", "a_drift"])
def test_preview_rejects_downstream_or_frozen_fact_drift(db_session, mutation: str) -> None:
    batch_id, account_id, operation_id, receipt = _post_bundle_interrupt(db_session)
    operation = db_session.get(TgAuthorizationDrOperation, operation_id)
    bundle = db_session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation_id,
    ))
    if mutation == "live_lease":
        operation.lease_expires_at = _now() + timedelta(minutes=1)
    elif mutation == "probe":
        db_session.add(_probe_fact(operation_id, bundle.id, receipt.bundle_generation))
    elif mutation == "copy":
        copy = db_session.scalar(select(TgAuthorizationWakeBundleCopy).where(
            TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
        ))
        copy.ciphertext_digest = "0" * 64
    else:
        db_session.get(TgAccount, account_id).connection_generation += 1
    db_session.commit()

    with pytest.raises(AuthorizationDrError) as exc_info:
        _preview(db_session, batch_id, account_id)

    assert exc_info.value.code in {
        "online_abc_post_bundle_interrupt_state_invalid",
        "online_abc_post_bundle_interrupt_artifact_invalid",
    }


def _post_bundle_interrupt(session) -> tuple[str, int, str, WakeBundleReceipt]:
    batch_id, account_id, operation_id = interrupt_tests._interrupted_c(session)
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    item = interrupt_tests._item(session, batch_id, account_id)
    migration_item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    primary = session.get(TgAccountAuthorization, session.get(TgAccount, account_id).current_authorization_id)
    old_c = session.get(TgAccountAuthorization, 2000 + account_id)
    old_c.is_slot_current = False
    old_c.status = "retained"
    old_c.protected_from_cleanup = True
    item.standby_2_plan = "provision"
    item.source_c_authorization_id = None
    item.source_c_fact_version = 0
    item.source_c_slot_generation = 0
    migration_item.expected_source_authorization_id = None
    migration_item.expected_source_fact_version = 0
    migration_item.expected_source_generation = 0
    migration_item.status = "running"
    migration_item.outcome = "pending"
    session.get(TgAuthorizationDrBatch, migration_item.batch_id).status = "approved"
    for stage in (
        "remote_login_confirmed", "local_copy_verified", "snapshot_copy_verified", "inventory_persisted",
    ):
        session.add(TgAuthorizationDrStageFact(
            operation_id=operation.id,
            node_id=operation.owner_node_id,
            owner_epoch=operation.owner_epoch,
            stage=stage,
            manifest_digest="a" * 64,
        ))
    operation.lease_expires_at = _now() + timedelta(minutes=1)
    session.commit()
    receipt = _bundle_receipt(operation, primary)
    commit_wake_bundle_receipt(
        session,
        operation.id,
        receipt,
        node_id=operation.owner_node_id,
        owner_epoch=operation.owner_epoch,
        lease_token=operation.lease_token,
    )
    operation.lease_expires_at = _now() - timedelta(minutes=1)
    session.commit()
    return batch_id, account_id, operation.id, receipt


def _bundle_receipt(operation, primary) -> WakeBundleReceipt:
    digest = "e" * 64
    return WakeBundleReceipt(
        bundle_generation=operation.target_generation,
        ciphertext_digest=digest,
        wrapped_dek_ciphertext="kms-wrapped-dek-ciphertext",
        kms_key_ref_digest="f" * 64,
        kms_key_version="kms-key-v3",
        auth_key_fingerprint_digest="8" * 64,
        telegram_user_id_digest=primary.telegram_user_id_digest,
        authorization_fingerprint_digest="2" * 64,
        remote_authorization_hash_ciphertext="encrypted-hash-post-bundle",
        inventory_sequence=101,
        inventory_manifest_digest="1" * 64,
        copies=tuple(_copy(kind, digest) for kind in ("local_persistent", "remote_ssh_snapshot")),
    )


def _copy(kind: str, digest: str) -> CopyReceipt:
    now = _now()
    return CopyReceipt(
        copy_kind=kind,
        object_ref_digest=("a" if kind == "local_persistent" else "b") * 64,
        ciphertext_digest=digest,
        immutable_version=f"immutable-{kind}-v1",
        write_receipt_digest="c" * 64,
        readback_receipt_digest="d" * 64,
        write_verified_at=now,
        readback_verified_at=now,
        decrypt_verified_at=now,
    )


def _probe_receipt(generation: int) -> RestoreProbeReceipt:
    return RestoreProbeReceipt(
        probe_generation=generation,
        source_copy_kind="remote_ssh_snapshot",
        status="passed",
        session_parse_status="passed",
        authorization_status="authorized",
        identity_match_status="matched",
        auth_key_match_status="matched",
        source_client_disconnected=True,
        probe_client_disconnected=True,
        zeroize_receipt_digest="9" * 64,
    )


def _probe_fact(operation_id: str, bundle_id: int, generation: int):
    return TgAuthorizationRestoreProbeFact(
        bundle_id=bundle_id,
        operation_id=operation_id,
        probe_generation=generation,
        source_copy_kind="remote_ssh_snapshot",
        status="passed",
        session_parse_status="passed",
        authorization_status="authorized",
        identity_match_status="matched",
        auth_key_match_status="matched",
        source_client_disconnected=True,
        probe_client_disconnected=True,
        zeroize_receipt_digest="9" * 64,
    )


def _preview(session, batch_id: str, account_id: int) -> dict:
    return preview_post_bundle_interrupt(
        session,
        batch_id,
        account_id,
        runtime_release_sha=NEW_RELEASE_SHA,
        idempotency_key=KEY,
        requested_by="requester",
        approved_by="approver",
        approval_ref=APPROVAL_REF,
        interruption_ref=INTERRUPTION_REF,
    )


def _apply(session, batch_id: str, account_id: int, *, fingerprint: str) -> dict:
    return apply_post_bundle_interrupt(
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


def _artifact_snapshot(session, operation_id: str) -> tuple:
    bundle = session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation_id,
    ))
    copies = tuple(session.execute(select(
        TgAuthorizationWakeBundleCopy.copy_kind,
        TgAuthorizationWakeBundleCopy.ciphertext_digest,
        TgAuthorizationWakeBundleCopy.immutable_version,
    ).where(TgAuthorizationWakeBundleCopy.bundle_id == bundle.id).order_by(
        TgAuthorizationWakeBundleCopy.copy_kind,
    )).all())
    return bundle.id, bundle.authorization_id, bundle.ciphertext_digest, bundle.receipt_status, copies


def _snapshot(session, batch_id: str, account_id: int) -> tuple:
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.account_id == account_id,
        TgAuthorizationDrOperation.operation_type == "provision_standby_2",
    ))
    item = interrupt_tests._item(session, batch_id, account_id)
    runtime = session.get(AuthorizationDrRuntimeContract, 1)
    return (
        item.status,
        item.outcome,
        item.version,
        operation.status,
        operation.operation_version,
        operation.reconcile_case_id,
        runtime.mode,
        runtime.claim_scope_operation_id,
        _artifact_snapshot(session, operation.id),
        interrupt_tests._a_snapshot(session, account_id),
    )
