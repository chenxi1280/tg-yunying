from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select

from app.models import (
    TgAuthorizationDrReconcileCase,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .migration_results import refresh_migration_batch
from .online_abc_manual_outcome import (
    MANUAL_OUTCOME,
    _approval,
    _fingerprint,
    _idempotency_key,
    _primary_snapshot,
    _release_sha,
)
from .online_abc_c_precode_interrupt_state import (
    ACTIVE_BOUNDARY,
    InterruptContext,
    POST_CODE_UNKNOWN_BOUNDARY,
    UNKNOWN_BOUNDARY,
    load_interrupt_context,
    lock_interrupt_context,
    require_interrupt_boundary,
)
from .runtime_scope import disarm_scoped_runtime


ACTION = "结案 ABC C 控制面中断"
BLOCKER = "c_control_plane_interrupted_pre_code"
CLASSIFICATION = "c_pre_code_control_plane_interrupted"
POST_CODE_BLOCKER = "c_control_plane_interrupted_post_code_unproven"
POST_CODE_CLASSIFICATION = "c_post_code_remote_unproven"
INTERRUPTION_PATTERN = re.compile(r"[A-Za-z0-9:._/-]{1,160}")


def preview_c_precode_interrupt(
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
    context = load_interrupt_context(session, batch_id, account_id)
    approval = _approval(
        context.batch,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    counts, boundary = require_interrupt_boundary(session, context, release_sha)
    payload = _payload(
        session,
        context,
        counts=counts,
        release_sha=release_sha,
        key=_idempotency_key(idempotency_key),
        approval=approval,
        interruption_ref=_interruption_ref(interruption_ref),
        boundary=boundary,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_c_precode_interrupt(
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
    lock_interrupt_context(session, batch_id, account_id)
    existing = _existing_result(session, batch_id, account_id, key=idempotency_key)
    if existing:
        return _idempotent(existing, expected_fingerprint)
    preview = preview_c_precode_interrupt(
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
        raise AuthorizationDrError("migration_fingerprint_conflict", "Interrupted C preview changed")
    _apply_transition(session, load_interrupt_context(session, batch_id, account_id), preview)
    session.commit()
    return {**_result(session, batch_id, account_id), "already_applied": False}


def readback_c_precode_interrupt(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    existing = _existing_result(session, batch_id, account_id, key=idempotency_key)
    if not existing:
        raise AuthorizationDrError(
            "online_abc_c_precode_interrupt_not_found", "Interrupted C audit is unavailable",
        )
    return {**existing, "already_applied": True}


def _payload(
    session,
    context: InterruptContext,
    *,
    counts: Counter,
    release_sha: str,
    key: str,
    approval: tuple[str, str, str],
    interruption_ref: str,
    boundary: str,
) -> dict:
    operation = context.c_operation
    classification, blocker = _manual_contract(boundary)
    return {
        "batch_id": context.batch.id,
        "batch_version": context.batch.version,
        "previous_execution_release_sha": context.batch.execution_release_sha,
        "runtime_release_sha": release_sha,
        "account_id": context.item.account_id,
        "item_id": context.item.id,
        "item_version": context.item.version,
        "b_slot_version": context.slots["standby_1"].version,
        "c_slot_version": context.slots["standby_2"].version,
        "b_operation_id": context.b_operation.id if context.b_operation else None,
        "c_operation_id": operation.id,
        "c_operation_version": operation.operation_version,
        "c_owner": [operation.owner_node_id, operation.owner_epoch],
        "c_lease_expires_at": str(operation.lease_expires_at),
        "c_remote_effect_started_at": str(operation.remote_effect_started_at),
        "c_challenge_sent_at": str(operation.login_challenge_sent_at),
        "c_code_message_id": operation.login_code_message_id,
        "c_code_received_at": str(operation.login_code_received_at),
        "migration_item": [context.migration_item.id, context.migration_item.version],
        "migration_batch": [context.migration_batch.id, context.migration_batch.version],
        "node": [context.node.id, context.node.capability_version, context.node.runtime_image_sha],
        "primary": _primary_snapshot(session, context.item),
        "pending_count": counts["pending"],
        "manual_count": counts[MANUAL_OUTCOME],
        "boundary": boundary,
        "classification": classification,
        "blocker_code": blocker,
        "interruption_ref": interruption_ref,
        "idempotency_key": key,
        "requested_by": approval[0],
        "approved_by": approval[1],
        "approval_ref": approval[2],
    }


def _apply_transition(session, context: InterruptContext, preview: dict) -> None:
    operation = context.c_operation
    case = TgAuthorizationDrReconcileCase(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        operation_id=operation.id,
        status="applied",
        classification=preview["classification"],
        recommended_transition=MANUAL_OUTCOME,
        blocker_code=preview["blocker_code"],
        expected_operation_version=operation.operation_version,
        expected_item_version=context.item.version,
        expected_source_fact_version=context.primary.fact_version,
        expected_owner_epoch=operation.owner_epoch,
        expected_node_id=operation.owner_node_id,
        expected_runtime_image_sha=context.node.runtime_image_sha,
        evidence_fingerprint=preview["fingerprint"],
        evidence_manifest=_evidence_manifest(preview),
        persisted_artifact_state="none",
        requested_by=preview["requested_by"],
        applied_by=preview["approved_by"],
        approval_ref=preview["approval_ref"],
        apply_idempotency_key=preview["idempotency_key"],
        applied_at=_now(),
    )
    session.add(case)
    session.flush()
    _close_operation(operation, case)
    _close_migration(session, context, blocker=preview["blocker_code"])
    _close_online_item(context, preview)
    disarm_scoped_runtime(session, operation, actor="online-abc-c-precode-interrupt")
    _audit_transition(session, context, preview)


def _close_operation(operation, case) -> None:
    operation.status = MANUAL_OUTCOME
    operation.remote_call_state = "reconciled_hold"
    operation.blocker_code = case.blocker_code
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version += 1


def _close_migration(session, context: InterruptContext, *, blocker: str) -> None:
    context.migration_item.status = MANUAL_OUTCOME
    context.migration_item.outcome = blocker
    context.migration_item.blocker_code = blocker
    context.migration_item.finished_at = _now()
    context.migration_item.version += 1
    refresh_migration_batch(session, context.migration_item.id)


def _close_online_item(context: InterruptContext, preview: dict) -> None:
    b_slot = context.slots["standby_1"]
    c_slot = context.slots["standby_2"]
    b_slot.outcome = "already_qualified" if context.b_operation is None else "succeeded"
    b_slot.operation_id = context.b_operation.id if context.b_operation else None
    b_slot.blocker_code = ""
    b_slot.version += 1
    c_slot.outcome = MANUAL_OUTCOME
    c_slot.operation_id = context.c_operation.id
    c_slot.blocker_code = preview["blocker_code"]
    c_slot.version += 1
    context.item.status = MANUAL_OUTCOME
    context.item.outcome = MANUAL_OUTCOME
    context.item.primary_probe_outcome = "succeeded"
    context.item.blocker_code = preview["blocker_code"]
    context.item.finished_at = _now()
    context.item.version += 1
    context.batch.execution_release_sha = preview["runtime_release_sha"]
    context.batch.status = "running" if preview["pending_count"] else "completed_with_manual"
    context.batch.version += 1


def _evidence_manifest(preview: dict) -> dict:
    return {key: preview[key] for key in (
        "boundary", "classification", "blocker_code", "interruption_ref", "previous_execution_release_sha",
        "runtime_release_sha", "c_owner", "c_lease_expires_at", "c_remote_effect_started_at",
        "c_challenge_sent_at", "c_code_message_id", "c_code_received_at", "node",
    )}


def _audit_transition(session, context: InterruptContext, preview: dict) -> None:
    audit(
        session,
        tenant_id=context.batch.tenant_id,
        actor=preview["approved_by"],
        action=ACTION,
        target_type="tg_authorization_dr_operation",
        target_id=context.c_operation.id,
        detail=(
            f"account_id={context.item.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"interruption_ref={preview['interruption_ref']}; blocker={preview['blocker_code']}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing_result(session, batch_id: str, account_id: int, *, key: str) -> dict | None:
    context = load_interrupt_context(session, batch_id, account_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == context.c_operation.id,
    ))
    classifications = {CLASSIFICATION, POST_CODE_CLASSIFICATION}
    if not case or case.classification not in classifications:
        return None
    if case.apply_idempotency_key != _idempotency_key(key):
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted C key was already used")
    return _result(session, batch_id, account_id)


def _idempotent(existing: dict, fingerprint: str) -> dict:
    if existing["fingerprint"] != fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted C fingerprint changed")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int) -> dict:
    context = load_interrupt_context(session, batch_id, account_id)
    case = session.get(TgAuthorizationDrReconcileCase, context.c_operation.reconcile_case_id)
    manifest = case.evidence_manifest if case else {}
    return {
        "batch_id": context.batch.id,
        "batch_status": context.batch.status,
        "batch_version": context.batch.version,
        "execution_release_sha": context.batch.execution_release_sha,
        "account_id": context.item.account_id,
        "item_status": context.item.status,
        "item_outcome": context.item.outcome,
        "item_version": context.item.version,
        "b_outcome": context.slots["standby_1"].outcome,
        "c_outcome": context.slots["standby_2"].outcome,
        "operation_id": context.c_operation.id,
        "operation_status": context.c_operation.status,
        "operation_version": context.c_operation.operation_version,
        "remote_call_state": context.c_operation.remote_call_state,
        "blocker_code": context.c_operation.blocker_code,
        "reconcile_status": context.c_operation.reconcile_status,
        "reconcile_case_id": context.c_operation.reconcile_case_id,
        "classification": case.classification if case else "",
        "boundary": manifest.get("boundary", ACTIVE_BOUNDARY),
        "interruption_ref": manifest.get("interruption_ref", ""),
        "primary": _primary_snapshot(session, context.item),
        "fingerprint": case.evidence_fingerprint if case else "",
    }


def _interruption_ref(value: str) -> str:
    normalized = value.strip()
    if not INTERRUPTION_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("interruption_ref_invalid", "Interruption reference is invalid")
    return normalized


def _manual_contract(boundary: str) -> tuple[str, str]:
    if boundary == POST_CODE_UNKNOWN_BOUNDARY:
        return POST_CODE_CLASSIFICATION, POST_CODE_BLOCKER
    if boundary in {ACTIVE_BOUNDARY, UNKNOWN_BOUNDARY}:
        return CLASSIFICATION, BLOCKER
    raise AuthorizationDrError("online_abc_c_precode_interrupt_state_invalid", "Interrupted C boundary is invalid")


__all__ = [
    "apply_c_precode_interrupt",
    "POST_CODE_BLOCKER",
    "POST_CODE_CLASSIFICATION",
    "preview_c_precode_interrupt",
    "readback_c_precode_interrupt",
]
