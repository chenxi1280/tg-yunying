from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select

from app.models import TgAuthorizationDrReconcileCase
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc_manual_outcome import (
    MANUAL_OUTCOME,
    _approval,
    _fingerprint,
    _idempotency_key,
    _primary_snapshot,
    _release_sha,
)
from .online_abc_post_bundle_interrupt_state import (
    BOUNDARY,
    PostBundleContext,
    load_post_bundle_context,
    lock_post_bundle_context,
    require_post_bundle_boundary,
)
from .runtime_scope import disarm_scoped_runtime


ACTION = "批准 ABC C post-bundle 无登录前滚"
BLOCKER = "malaysia_wake_unavailable"
CLASSIFICATION = "central_bundle_restore_forward"
INTERRUPTION_PATTERN = re.compile(r"[A-Za-z0-9:._/-]{1,160}")


def preview_post_bundle_interrupt(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    interruption_ref: str,
) -> dict:
    context = load_post_bundle_context(session, batch_id, account_id)
    approval = _approval(
        context.interrupt.batch,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    counts = require_post_bundle_boundary(session, context, release_sha)
    payload = _payload(
        session,
        context,
        counts=counts,
        release_sha=release_sha,
        key=_idempotency_key(idempotency_key),
        approval=approval,
        interruption_ref=_interruption_ref(interruption_ref),
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_post_bundle_interrupt(
    session,
    batch_id: str,
    account_id: int,
    *,
    runtime_release_sha: str,
    idempotency_key: str,
    expected_fingerprint: str,
    requested_by: str,
    approved_by: str,
    approval_ref: str,
    interruption_ref: str,
) -> dict:
    existing = _existing_result(session, batch_id, account_id, key=idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    lock_post_bundle_context(session, batch_id, account_id)
    existing = _existing_result(session, batch_id, account_id, key=idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    preview = preview_post_bundle_interrupt(
        session,
        batch_id,
        account_id,
        runtime_release_sha=runtime_release_sha,
        idempotency_key=idempotency_key,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
        interruption_ref=interruption_ref,
    )
    if preview["fingerprint"] != expected_fingerprint:
        raise AuthorizationDrError("migration_fingerprint_conflict", "Post-bundle preview changed")
    _apply_transition(session, load_post_bundle_context(session, batch_id, account_id), preview)
    session.commit()
    return {**_result(session, batch_id, account_id), "already_applied": False}


def readback_post_bundle_interrupt(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    existing = _existing_result(session, batch_id, account_id, key=idempotency_key)
    if not existing:
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_not_found",
            "Post-bundle interruption audit is unavailable",
        )
    return {**existing, "already_applied": True}


def _payload(
    session,
    context: PostBundleContext,
    *,
    counts: Counter,
    release_sha: str,
    key: str,
    approval: tuple[str, str, str],
    interruption_ref: str,
) -> dict:
    base = context.interrupt
    operation = base.c_operation
    return {
        "batch_id": base.batch.id,
        "batch_version": base.batch.version,
        "previous_execution_release_sha": base.batch.execution_release_sha,
        "runtime_release_sha": release_sha,
        "account_id": base.item.account_id,
        "item_id": base.item.id,
        "item_version": base.item.version,
        "b_slot_version": base.slots["standby_1"].version,
        "c_slot_version": base.slots["standby_2"].version,
        "b_operation_id": base.b_operation.id if base.b_operation else None,
        "c_operation": [operation.id, operation.operation_version, operation.owner_node_id,
                        operation.owner_epoch, str(operation.lease_expires_at)],
        "migration_item": [base.migration_item.id, base.migration_item.version],
        "migration_batch": [base.migration_batch.id, base.migration_batch.version],
        "node": [base.node.id, base.node.capability_version, base.node.runtime_image_sha],
        "primary": _primary_snapshot(session, base.item),
        "artifact": _artifact_snapshot(context),
        "pending_count": counts["pending"],
        "manual_count": counts[MANUAL_OUTCOME],
        "boundary": BOUNDARY,
        "classification": CLASSIFICATION,
        "blocker_code": BLOCKER,
        "interruption_ref": interruption_ref,
        "idempotency_key": key,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _artifact_snapshot(context: PostBundleContext) -> dict:
    return {
        "candidate": [
            context.candidate.id,
            context.candidate.fact_version,
            context.candidate.slot_generation,
            context.candidate.wake_bundle_id,
        ],
        "bundle": [
            context.bundle.id,
            context.bundle.bundle_generation,
            context.bundle.ciphertext_digest,
            context.bundle.receipt_status,
            context.bundle.recoverable_copy_count,
        ],
        "copies": [
            [row.id, row.copy_kind, row.ciphertext_digest, row.immutable_version]
            for row in context.copies
        ],
        "inventory": [
            context.inventory.id,
            context.inventory.inventory_sequence,
            context.inventory.manifest_digest,
        ],
    }


def _apply_transition(session, context: PostBundleContext, preview: dict) -> None:
    case = _repair_case(context, preview)
    session.add(case)
    session.flush()
    operation = context.interrupt.c_operation
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "repair_approved"
    operation.blocker_code = CLASSIFICATION
    operation.operation_version += 1
    _stop_online_item(context, preview)
    if not disarm_scoped_runtime(session, operation, actor="online-abc-post-bundle-interrupt"):
        raise AuthorizationDrError(
            "online_abc_post_bundle_interrupt_runtime_active", "Post-bundle runtime scope changed",
        )
    _audit_transition(session, context, preview)


def _repair_case(context: PostBundleContext, preview: dict) -> TgAuthorizationDrReconcileCase:
    base = context.interrupt
    operation = base.c_operation
    return TgAuthorizationDrReconcileCase(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        operation_id=operation.id,
        status="repair_approved",
        classification=CLASSIFICATION,
        recommended_transition="reconcile_artifact_ready",
        blocker_code=CLASSIFICATION,
        expected_operation_version=operation.operation_version,
        expected_item_version=base.migration_item.version,
        expected_source_fact_version=base.primary.fact_version,
        expected_owner_epoch=operation.owner_epoch,
        expected_node_id=operation.owner_node_id,
        expected_runtime_image_sha=base.node.runtime_image_sha,
        evidence_fingerprint=preview["fingerprint"],
        evidence_manifest=_evidence_manifest(preview),
        persisted_artifact_state="central_bundle",
        requested_by=preview["requested_by"],
        applied_by=preview["approved_by"],
        approval_ref=preview["approval_ref"],
        apply_idempotency_key=preview["idempotency_key"],
    )


def _stop_online_item(context: PostBundleContext, preview: dict) -> None:
    base = context.interrupt
    base.item.status = "stopped"
    base.item.outcome = "runner_blocked"
    base.item.blocker_code = BLOCKER
    base.item.finished_at = _now()
    base.item.version += 1
    base.batch.execution_release_sha = preview["runtime_release_sha"]
    base.batch.status = "stopped"
    base.batch.version += 1


def _evidence_manifest(preview: dict) -> dict:
    manifest = {key: preview[key] for key in (
        "boundary",
        "classification",
        "blocker_code",
        "interruption_ref",
        "previous_execution_release_sha",
        "runtime_release_sha",
        "c_operation",
        "node",
        "artifact",
    )}
    manifest["bundle_generation"] = preview["artifact"]["bundle"][1]
    manifest["ciphertext_digest"] = preview["artifact"]["bundle"][2]
    manifest["inventory_sequence"] = preview["artifact"]["inventory"][1]
    return manifest


def _audit_transition(session, context: PostBundleContext, preview: dict) -> None:
    operation = context.interrupt.c_operation
    audit(
        session,
        tenant_id=operation.tenant_id,
        actor=preview["approved_by"],
        action=ACTION,
        target_type="tg_authorization_dr_operation",
        target_id=operation.id,
        detail=(
            f"account_id={operation.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"interruption_ref={preview['interruption_ref']}; classification={CLASSIFICATION}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing_result(session, batch_id: str, account_id: int, *, key: str) -> dict | None:
    context = load_post_bundle_context(session, batch_id, account_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == context.interrupt.c_operation.id,
    ))
    if not case or case.classification != CLASSIFICATION:
        return None
    if case.apply_idempotency_key != _idempotency_key(key):
        raise AuthorizationDrError("idempotency_key_conflict", "Post-bundle key was already used")
    return _result(session, batch_id, account_id)


def _idempotent(existing: dict, fingerprint: str) -> dict:
    if existing["fingerprint"] != fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Post-bundle fingerprint changed")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int) -> dict:
    context = load_post_bundle_context(session, batch_id, account_id)
    base = context.interrupt
    operation = base.c_operation
    case = session.get(TgAuthorizationDrReconcileCase, operation.reconcile_case_id)
    manifest = dict(case.evidence_manifest or {}) if case else {}
    return {
        "batch_id": base.batch.id,
        "batch_status": base.batch.status,
        "batch_version": base.batch.version,
        "execution_release_sha": base.batch.execution_release_sha,
        "account_id": base.item.account_id,
        "item_status": base.item.status,
        "item_outcome": base.item.outcome,
        "item_version": base.item.version,
        "operation_id": operation.id,
        "operation_status": operation.status,
        "operation_version": operation.operation_version,
        "remote_call_state": operation.remote_call_state,
        "reconcile_status": operation.reconcile_status,
        "reconcile_case_id": operation.reconcile_case_id,
        "classification": case.classification if case else "",
        "boundary": manifest.get("boundary", BOUNDARY),
        "interruption_ref": manifest.get("interruption_ref", ""),
        "primary": _primary_snapshot(session, base.item),
        "fingerprint": case.evidence_fingerprint if case else "",
    }


def _interruption_ref(value: str) -> str:
    normalized = value.strip()
    if not INTERRUPTION_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("interruption_ref_invalid", "Interruption reference is invalid")
    return normalized


__all__ = [
    "ACTION",
    "BLOCKER",
    "CLASSIFICATION",
    "apply_post_bundle_interrupt",
    "preview_post_bundle_interrupt",
    "readback_post_bundle_interrupt",
]
