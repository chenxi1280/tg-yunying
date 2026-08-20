from __future__ import annotations

from sqlalchemy import func, select

from app.models import (
    TgAccount,
    TgAccountAuthorization,
    TgAuthorizationDrBatch,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationRestoreProbeFact,
    TgAuthorizationSlotDecision,
    TgAuthorizationWakeBundle,
    TgAuthorizationWakeBundleCopy,
    TgAuthorizationWakeInventoryEntry,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .wake_bundle import _operation_bundle, _owned_operation, valid_copy_kinds


def commit_migration_slot(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> TgAuthorizationSlotDecision:
    operation = _owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    if operation.status not in ("ready_for_slot_commit", "slot_commit_prepared"):
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Operation is not ready for slot commit")
    source, candidate, bundle = _slot_inputs(session, operation)
    _require_recovery_evidence(session, operation, bundle)
    account = session.scalar(select(TgAccount).where(TgAccount.id == operation.account_id).with_for_update())
    _verify_slot_cas(operation, source, candidate=candidate, account=account)
    decision = _find_or_prepare_decision(
        session,
        operation,
        source=source,
        candidate=candidate,
        bundle=bundle,
        account=account,
    )
    _apply_slot_decision(source, candidate, bundle=bundle, decision=decision)
    _pass_recovery_gate(
        session,
        operation,
        source=source,
        candidate=candidate,
        bundle=bundle,
        decision=decision,
        account=account,
    )
    _finish_operation(session, operation)
    session.commit()
    return decision


def rollback_migration_slot(
    session,
    operation_id: str,
    *,
    actor: str,
    reason: str,
) -> TgAuthorizationSlotDecision:
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    if not operation:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    source, candidate, bundle = _slot_inputs(session, operation)
    if source.rollback_window_closed_at or source.remote_authorization_state == "revoked":
        raise AuthorizationDrError("rollback_window_closed", "Old SV authorization was already revoked")
    if not candidate.is_slot_current or source.remote_authorization_state != "active":
        raise AuthorizationDrError("slot_commit_decision_conflict", "Rollback source is not remotely active")
    account = session.scalar(select(TgAccount).where(TgAccount.id == operation.account_id).with_for_update())
    generation = _next_decision_generation(session, operation.account_id)
    decision = _rollback_decision(
        operation,
        source,
        candidate=candidate,
        bundle=bundle,
        account=account,
        generation=generation,
    )
    session.add(decision)
    candidate.is_slot_current = False
    candidate.dr_state = "repair_required"
    source.is_slot_current = True
    source.dr_state = "retained_restored"
    decision.status = "observed"
    decision.recovery_gate_status = "rollback_forward_applied"
    decision.observed_at = _now()
    operation.status = "migration_rolled_back_forward"
    operation.blocker_code = reason[:100]
    operation.operation_version += 1
    audit(
        session,
        tenant_id=operation.tenant_id,
        actor=actor,
        action="前滚恢复旧 SV standby_2",
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=f"decision={decision.id}; reason={reason[:160]}",
    )
    session.commit()
    return decision


def _slot_inputs(session, operation):
    source = session.get(TgAccountAuthorization, operation.source_authorization_id)
    candidate = session.get(TgAccountAuthorization, operation.candidate_authorization_id)
    bundle = _operation_bundle(session, operation)
    if not source or not candidate or candidate.wake_bundle_id != bundle.id:
        raise AuthorizationDrError("wake_bundle_missing", "Migration slot inputs are incomplete")
    return source, candidate, bundle


def _require_recovery_evidence(session, operation, bundle) -> None:
    copies = list(session.scalars(select(TgAuthorizationWakeBundleCopy).where(
        TgAuthorizationWakeBundleCopy.bundle_id == bundle.id,
    )))
    probes = list(session.scalars(select(TgAuthorizationRestoreProbeFact).where(
        TgAuthorizationRestoreProbeFact.bundle_id == bundle.id,
        TgAuthorizationRestoreProbeFact.status == "passed",
    )))
    inventory = session.scalar(select(TgAuthorizationWakeInventoryEntry.id).where(
        TgAuthorizationWakeInventoryEntry.bundle_id == bundle.id,
        TgAuthorizationWakeInventoryEntry.operation_id == operation.id,
    ).limit(1))
    copy_kinds = {copy.copy_kind for copy in copies}
    if bundle.recoverable_copy_count != 2 or not valid_copy_kinds(copy_kinds):
        raise AuthorizationDrError("wake_bundle_copy_count_insufficient", "Bundle copy readback is incomplete")
    if bundle.kms_decrypt_status != "verified" or not probes or not inventory:
        raise AuthorizationDrError("wake_bundle_restore_probe_failed", "Recovery evidence is incomplete")


def _verify_slot_cas(operation, source, *, candidate, account) -> None:
    valid = (
        source.is_slot_current
        and source.slot_generation == operation.source_generation
        and candidate.slot_generation == operation.target_generation
        and not candidate.is_slot_current
        and account is not None
    )
    if not valid:
        raise AuthorizationDrError("slot_commit_decision_conflict", "Standby slot changed before commit")


def _find_or_prepare_decision(session, operation, *, source, candidate, bundle, account):
    existing = session.scalar(select(TgAuthorizationSlotDecision).where(
        TgAuthorizationSlotDecision.account_id == operation.account_id,
        TgAuthorizationSlotDecision.logical_slot == operation.logical_slot,
        TgAuthorizationSlotDecision.new_authorization_id == candidate.id,
    ).order_by(TgAuthorizationSlotDecision.decision_generation.desc()).limit(1))
    if existing:
        return existing
    inventory = session.scalar(select(TgAuthorizationWakeInventoryEntry).where(
        TgAuthorizationWakeInventoryEntry.bundle_id == bundle.id,
    ).order_by(TgAuthorizationWakeInventoryEntry.inventory_sequence.desc()).limit(1))
    decision = TgAuthorizationSlotDecision(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        logical_slot=operation.logical_slot,
        decision_generation=_next_decision_generation(session, operation.account_id),
        expected_old_authorization_id=source.id,
        new_authorization_id=candidate.id,
        expected_old_slot_generation=source.slot_generation,
        new_slot_generation=candidate.slot_generation,
        expected_account_version=account.authorization_contract_version,
        inventory_sequence=inventory.inventory_sequence,
        manifest_digest=inventory.manifest_digest,
    )
    session.add(decision)
    session.flush()
    operation.status = "slot_commit_prepared"
    operation.operation_version += 1
    return decision


def _apply_slot_decision(source, candidate, *, bundle, decision) -> None:
    source.is_slot_current = False
    source.status = "retained"
    source.dr_state = "retained_protected"
    source.protected_from_cleanup = True
    candidate.is_slot_current = True
    candidate.status = "standby"
    candidate.dr_state = "dormant_ready"
    bundle.is_active = True
    bundle.activated_at = _now()
    bundle.receipt_status = "active"
    decision.status = "observed"
    decision.observed_at = _now()


def _pass_recovery_gate(session, operation, *, source, candidate, bundle, decision, account) -> None:
    _require_recovery_evidence(session, operation, bundle)
    source.migration_recovery_gate_status = "passed"
    candidate.migration_recovery_gate_status = "passed"
    decision.recovery_gate_status = "passed"
    account.authorization_recovery_status = "dormant_ready"
    account.authorization_contract_version += 1


def _finish_operation(session, operation) -> None:
    operation.status = "succeeded"
    operation.blocker_code = ""
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.finished_at = _now()
    operation.operation_version += 1
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    item.status = "succeeded"
    item.outcome = "succeeded"
    item.finished_at = _now()
    item.version += 1
    _refresh_batch(session, item.batch_id)


def _refresh_batch(session, batch_id: str) -> None:
    batch = session.get(TgAuthorizationDrBatch, batch_id)
    statuses = list(session.scalars(select(TgAuthorizationDrBatchItem.status).where(
        TgAuthorizationDrBatchItem.batch_id == batch_id,
    )))
    if statuses and all(status == "succeeded" for status in statuses):
        batch.status = "succeeded"
        batch.finished_at = _now()
    elif any(status in ("running", "reconcile_unknown") for status in statuses):
        batch.status = "running"
    batch.version += 1


def _next_decision_generation(session, account_id: int) -> int:
    current = session.scalar(select(func.max(TgAuthorizationSlotDecision.decision_generation)).where(
        TgAuthorizationSlotDecision.account_id == account_id,
        TgAuthorizationSlotDecision.logical_slot == "standby_2",
    ))
    return int(current or 0) + 1


def _rollback_decision(operation, source, *, candidate, bundle, account, generation: int):
    return TgAuthorizationSlotDecision(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        logical_slot="standby_2",
        decision_generation=generation,
        expected_old_authorization_id=candidate.id,
        new_authorization_id=source.id,
        expected_old_slot_generation=candidate.slot_generation,
        new_slot_generation=source.slot_generation,
        expected_account_version=account.authorization_contract_version,
        inventory_sequence=bundle.bundle_generation,
        manifest_digest=bundle.ciphertext_digest,
    )


__all__ = ["commit_migration_slot", "rollback_migration_slot"]
