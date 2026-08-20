from __future__ import annotations

from sqlalchemy import select

from app.models import (
    TgAccountAuthorization,
    TgAuthorizationDrOperation,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
    TgAuthorizationWakeInventoryEntry,
)
from app.services._common import _now
from app.timezone import as_beijing_aware

from .contracts import AuthorizationDrError, RestoreProbeReceipt, WakeBundleReceipt


SUPPORTED_COPY_SETS = frozenset({
    frozenset({"local_persistent", "remote_ssh_snapshot"}),
    frozenset({"local_persistent", "object_snapshot"}),
})
SUCCESS_PROBE_STATES = frozenset({"passed", "authorized", "matched"})


def commit_wake_bundle_receipt(
    session,
    operation_id: str,
    receipt: WakeBundleReceipt,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationWakeBundle:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    existing = session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
        TgAuthorizationWakeBundle.bundle_generation == receipt.bundle_generation,
    ))
    if existing:
        _verify_idempotent_bundle(session, existing, receipt)
        return existing
    _require_remote_login_started(operation)
    _validate_bundle_receipt(operation, receipt)
    _reject_auth_key_collision(session, receipt.auth_key_fingerprint_digest)
    candidate = _create_candidate_authorization(session, operation, receipt)
    bundle = _create_bundle(session, operation, candidate, receipt=receipt)
    _create_copies(session, bundle, receipt)
    _append_inventory(session, operation, candidate=candidate, bundle=bundle, receipt=receipt)
    candidate.wake_bundle_id = bundle.id
    operation.candidate_authorization_id = candidate.id
    operation.remote_call_state = "confirmed"
    operation.status = "bundle_copies_verified"
    operation.operation_version += 1
    session.commit()
    return bundle


def record_restore_probe(
    session,
    operation_id: str,
    receipt: RestoreProbeReceipt,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationRestoreProbeFact:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    bundle = _operation_bundle(session, operation)
    existing = session.scalar(select(TgAuthorizationRestoreProbeFact).where(
        TgAuthorizationRestoreProbeFact.bundle_id == bundle.id,
        TgAuthorizationRestoreProbeFact.probe_generation == receipt.probe_generation,
    ))
    if existing:
        _verify_idempotent_probe(existing, receipt)
        return existing
    _validate_probe(receipt)
    fact = TgAuthorizationRestoreProbeFact(
        bundle_id=bundle.id,
        operation_id=operation.id,
        probe_generation=receipt.probe_generation,
        source_copy_kind=receipt.source_copy_kind,
        status=receipt.status,
        session_parse_status=receipt.session_parse_status,
        authorization_status=receipt.authorization_status,
        identity_match_status=receipt.identity_match_status,
        auth_key_match_status=receipt.auth_key_match_status,
        source_client_disconnected=receipt.source_client_disconnected,
        probe_client_disconnected=receipt.probe_client_disconnected,
        zeroize_receipt_digest=receipt.zeroize_receipt_digest,
    )
    session.add(fact)
    bundle.receipt_status = "restore_probe_passed"
    candidate = session.get(TgAccountAuthorization, operation.candidate_authorization_id)
    candidate.dr_state = "candidate_qualified"
    candidate.health_status = "healthy"
    operation.status = "ready_for_slot_commit"
    operation.operation_version += 1
    session.commit()
    return fact


def _require_remote_login_started(operation) -> None:
    if operation.remote_call_state != "started" or operation.status != "login_remote_started":
        raise AuthorizationDrError("provision_reconcile_unknown", "Remote login start boundary is missing")


def _owned_operation(session, operation_id: str, *, node_id: str, owner_epoch: int, lease_token: str):
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    valid_owner = operation and operation.owner_node_id == node_id and operation.owner_epoch == owner_epoch
    if not valid_owner:
        raise AuthorizationDrError("execution_node_mismatch", "Operation owner changed")
    if operation.lease_token != lease_token or not operation.lease_expires_at or operation.lease_expires_at <= _now():
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "Operation lease is stale")
    return operation


def _validate_bundle_receipt(operation, receipt: WakeBundleReceipt) -> None:
    if receipt.bundle_generation != operation.target_generation:
        raise AuthorizationDrError("wake_bundle_generation_conflict", "Bundle generation does not match operation")
    if not all((receipt.ciphertext_digest, receipt.wrapped_dek_ciphertext, receipt.kms_key_ref_digest)):
        raise AuthorizationDrError("my_kms_recovery_unproven", "Bundle KMS evidence is incomplete")
    if not receipt.kms_key_version or not receipt.auth_key_fingerprint_digest or not receipt.telegram_user_id_digest:
        raise AuthorizationDrError("my_kms_recovery_unproven", "Bundle identity evidence is incomplete")
    if not receipt.remote_authorization_hash_ciphertext:
        raise AuthorizationDrError("authorization_hash_missing_or_zero", "Remote authorization hash is missing")
    copy_kinds = {copy.copy_kind for copy in receipt.copies}
    if len(receipt.copies) != 2 or not valid_copy_kinds(copy_kinds):
        raise AuthorizationDrError("wake_bundle_copy_count_insufficient", "Two independent bundle copies are required")
    if any(copy.ciphertext_digest != receipt.ciphertext_digest for copy in receipt.copies):
        raise AuthorizationDrError("wake_bundle_immutable_conflict", "Bundle copy digest mismatch")
    if any(not _copy_receipt_complete(copy) for copy in receipt.copies):
        raise AuthorizationDrError("wake_bundle_copy_count_insufficient", "Bundle copy readback is incomplete")
    if receipt.inventory_sequence < 1 or not receipt.inventory_manifest_digest:
        raise AuthorizationDrError("wake_bundle_inventory_ahead_of_central", "MY inventory receipt is missing")


def _copy_receipt_complete(copy) -> bool:
    return bool(
        copy.object_ref_digest
        and copy.immutable_version
        and copy.write_receipt_digest
        and copy.readback_receipt_digest
        and copy.write_verified_at
        and copy.readback_verified_at
        and copy.decrypt_verified_at
    )


def _reject_auth_key_collision(session, digest: str) -> None:
    collision = session.scalar(select(TgAccountAuthorization.id).where(
        TgAccountAuthorization.auth_key_fingerprint_digest == digest,
    ).limit(1))
    if collision:
        raise AuthorizationDrError("authorization_key_duplicated", "AuthKey fingerprint already exists")


def _create_candidate_authorization(session, operation, receipt):
    candidate = TgAccountAuthorization(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        role="standby_2",
        logical_slot="standby_2",
        slot_generation=operation.target_generation,
        is_slot_current=False,
        provision_region_code="my",
        credential_storage_scope="malaysia_wake_bundle",
        developer_app_id=operation.developer_app_id,
        developer_app_api_id_snapshot=operation.developer_app_api_id_snapshot,
        session_ciphertext=None,
        status="candidate",
        health_status="unknown",
        dr_state="bundle_copies_verified",
        remote_authorization_state="active",
        protected_from_cleanup=True,
        telegram_login_at=as_beijing_aware(_now()),
        telegram_authorization_hash_ciphertext=receipt.remote_authorization_hash_ciphertext,
        auth_key_fingerprint_digest=receipt.auth_key_fingerprint_digest,
        telegram_user_id_digest=receipt.telegram_user_id_digest,
        fact_version=1,
        created_by="authorization-dr-my",
    )
    session.add(candidate)
    session.flush()
    return candidate


def _create_bundle(session, operation, candidate, *, receipt):
    bundle = TgAuthorizationWakeBundle(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        authorization_id=candidate.id,
        operation_id=operation.id,
        bundle_generation=receipt.bundle_generation,
        ciphertext_digest=receipt.ciphertext_digest,
        wrapped_dek_ciphertext=receipt.wrapped_dek_ciphertext,
        kms_key_ref_digest=receipt.kms_key_ref_digest,
        kms_key_version=receipt.kms_key_version,
        kms_decrypt_status="verified",
        auth_key_fingerprint_digest=receipt.auth_key_fingerprint_digest,
        telegram_user_id_digest=receipt.telegram_user_id_digest,
        recoverable_copy_count=2,
        receipt_status="copies_verified",
    )
    session.add(bundle)
    session.flush()
    return bundle


def _create_copies(session, bundle, receipt) -> None:
    for copy in receipt.copies:
        session.add(TgAuthorizationWakeBundleCopy(
            bundle_id=bundle.id,
            copy_kind=copy.copy_kind,
            object_ref_digest=copy.object_ref_digest,
            ciphertext_digest=copy.ciphertext_digest,
            immutable_version=copy.immutable_version,
            write_receipt_digest=copy.write_receipt_digest,
            readback_receipt_digest=copy.readback_receipt_digest,
            write_verified_at=copy.write_verified_at,
            readback_verified_at=copy.readback_verified_at,
            decrypt_verified_at=copy.decrypt_verified_at,
        ))


def _append_inventory(session, operation, *, candidate, bundle, receipt) -> None:
    session.add(TgAuthorizationWakeInventoryEntry(
        node_id=operation.owner_node_id,
        inventory_sequence=receipt.inventory_sequence,
        operation_id=operation.id,
        account_id=operation.account_id,
        authorization_id=candidate.id,
        bundle_id=bundle.id,
        event_type="bundle_receipt_committed",
        manifest_digest=receipt.inventory_manifest_digest,
        decision_payload={
            "logical_slot": operation.logical_slot,
            "target_generation": operation.target_generation,
            "ciphertext_digest": receipt.ciphertext_digest,
        },
        observed_by_central_at=_now(),
    ))


def _validate_probe(receipt: RestoreProbeReceipt) -> None:
    states = (
        receipt.status,
        receipt.session_parse_status,
        receipt.authorization_status,
        receipt.identity_match_status,
        receipt.auth_key_match_status,
    )
    if receipt.source_copy_kind not in {"remote_ssh_snapshot", "object_snapshot"}:
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Snapshot restore source is unsupported")
    if any(state not in SUCCESS_PROBE_STATES for state in states):
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Snapshot restore probe did not pass")
    if not receipt.source_client_disconnected or not receipt.probe_client_disconnected:
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Telegram clients were not disconnected")
    if not receipt.zeroize_receipt_digest:
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Restore probe zeroize receipt is missing")


def _operation_bundle(session, operation):
    bundle = session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ).order_by(TgAuthorizationWakeBundle.bundle_generation.desc()).limit(1))
    if not bundle:
        raise AuthorizationDrError("wake_bundle_missing", "Wake bundle does not exist")
    return bundle


def valid_copy_kinds(copy_kinds: set[str]) -> bool:
    return frozenset(copy_kinds) in SUPPORTED_COPY_SETS


def _verify_idempotent_bundle(session, bundle, receipt: WakeBundleReceipt) -> None:
    expected = (
        receipt.ciphertext_digest,
        receipt.wrapped_dek_ciphertext,
        receipt.kms_key_ref_digest,
        receipt.kms_key_version,
        receipt.auth_key_fingerprint_digest,
        receipt.telegram_user_id_digest,
    )
    observed = (
        bundle.ciphertext_digest,
        bundle.wrapped_dek_ciphertext,
        bundle.kms_key_ref_digest,
        bundle.kms_key_version,
        bundle.auth_key_fingerprint_digest,
        bundle.telegram_user_id_digest,
    )
    copies = list(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
    )))
    copy_digests = {(item.copy_kind, item.ciphertext_digest, item.immutable_version) for item in copies}
    expected_copies = {(item.copy_kind, item.ciphertext_digest, item.immutable_version) for item in receipt.copies}
    inventory = session.scalar(select(TgAuthorizationWakeInventoryEntry).where(
        TgAuthorizationWakeInventoryEntry.bundle_id == bundle.id,
    ))
    inventory_matches = inventory and (
        inventory.inventory_sequence == receipt.inventory_sequence
        and inventory.manifest_digest == receipt.inventory_manifest_digest
    )
    if observed != expected or copy_digests != expected_copies or not inventory_matches:
        raise AuthorizationDrError("wake_bundle_immutable_conflict", "Bundle generation digest changed")


def _verify_idempotent_probe(fact, receipt: RestoreProbeReceipt) -> None:
    if fact.zeroize_receipt_digest != receipt.zeroize_receipt_digest or fact.status != receipt.status:
        raise AuthorizationDrError("wake_bundle_immutable_conflict", "Restore probe generation changed")


__all__ = ["commit_wake_bundle_receipt", "record_restore_probe"]
