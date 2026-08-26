from __future__ import annotations

import re
from collections import Counter

from sqlalchemy import select

from app.models import (
    TgAuthorizationDrReconcileCase,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .online_abc_manual_outcome import (
    MANUAL_OUTCOME,
    UPSTREAM_MANUAL_BLOCKER,
    _approval,
    _fingerprint,
    _idempotency_key,
    _mark_slot_manual,
    _primary_snapshot,
    _release_sha,
)
from .online_abc_release_interrupted_flow import (
    close_empty_interrupted_intent,
    flow_snapshot,
)
from .online_abc_release_interrupted_state import (
    InterruptedContext,
    RELEASE_CHANGED_BOUNDARY,
    STOPPED_UNKNOWN_BOUNDARY,
    load_interrupted_context,
    lock_interrupted_context,
    require_interrupted_boundary,
)


ACTION = "结案发布中断的 ABC B pre-flow"
BLOCKER = "release_interrupted_pre_flow_unproven"
CLASSIFICATION = "release_interrupted_remote_unproven"
STOPPED_CLASSIFICATION = "b_pre_challenge_remote_unproven"
STOPPED_BLOCKER = "b_pre_challenge_remote_unproven"
REF_PATTERN = re.compile(r"[A-Za-z0-9:._/-]{1,160}")


def preview_release_interrupted_b(
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
    context = load_interrupted_context(session, batch_id, account_id)
    approval = _approval(
        context.batch,
        requested_by=requested_by,
        approved_by=approved_by,
        approval_ref=approval_ref,
    )
    release_sha = _release_sha(runtime_release_sha)
    key = _idempotency_key(idempotency_key)
    interruption = _interruption_ref(interruption_ref)
    counts, boundary = require_interrupted_boundary(session, context, release_sha)
    _primary_snapshot(session, context.item)
    payload = _payload(
        session,
        context,
        counts=counts,
        release_sha=release_sha,
        key=key,
        approval=approval,
        interruption_ref=interruption,
        boundary=boundary,
    )
    return {**payload, "fingerprint": _fingerprint(payload)}


def apply_release_interrupted_b(
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
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if existing:
        return _idempotent(existing, expected_fingerprint)
    lock_interrupted_context(session, batch_id, account_id)
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if existing:
        return _idempotent(existing, expected_fingerprint)
    preview = preview_release_interrupted_b(
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
        raise AuthorizationDrError("migration_fingerprint_conflict", "Interrupted B preview changed")
    _apply_transition(session, load_interrupted_context(session, batch_id, account_id), preview)
    session.commit()
    return {**_result(session, batch_id, account_id), "already_applied": False}


def readback_release_interrupted_b(
    session, batch_id: str, account_id: int, *, idempotency_key: str,
) -> dict:
    existing = _existing_result(
        session, batch_id, account_id=account_id, key=idempotency_key,
    )
    if not existing:
        raise AuthorizationDrError(
            "online_abc_release_interrupted_not_found", "Interrupted B audit is unavailable",
        )
    return {**existing, "already_applied": True}


def _payload(
    session,
    context: InterruptedContext,
    *,
    counts: Counter,
    release_sha: str,
    key: str,
    approval: tuple[str, str, str],
    interruption_ref: str,
    boundary: str,
) -> dict:
    operation = context.operation
    classification, blocker = _manual_contract(boundary)
    return {
        "batch_id": context.batch.id,
        "batch_version": context.batch.version,
        "previous_execution_release_sha": context.batch.execution_release_sha,
        "runtime_release_sha": release_sha,
        "account_id": context.item.account_id,
        "item_id": context.item.id,
        "item_version": context.item.version,
        "b_slot_id": context.slots["standby_1"].id,
        "b_slot_version": context.slots["standby_1"].version,
        "c_slot_id": context.slots["standby_2"].id,
        "c_slot_version": context.slots["standby_2"].version,
        "operation_id": operation.id,
        "operation_version": operation.operation_version,
        "operation_status": operation.status,
        "remote_call_state": operation.remote_call_state,
        "remote_effect_started_at": str(operation.remote_effect_started_at),
        "interrupted_flow": flow_snapshot(context.flows[0] if context.flows else None),
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


def _apply_transition(session, context: InterruptedContext, preview: dict) -> None:
    operation = context.operation
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
        expected_runtime_image_sha=preview["runtime_release_sha"],
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
    close_empty_interrupted_intent(
        operation,
        context.flows[0] if context.flows else None,
        blocker_code=preview["blocker_code"],
        interruption_ref=preview["interruption_ref"],
    )
    _close_operation(operation, case, blocker=preview["blocker_code"])
    _mark_slot_manual(context.slots["standby_1"], preview["blocker_code"])
    context.slots["standby_1"].operation_id = operation.id
    _mark_slot_manual(context.slots["standby_2"], UPSTREAM_MANUAL_BLOCKER)
    _close_item_and_batch(context, preview)
    _audit_transition(session, context, preview)


def _close_operation(operation, case, *, blocker: str) -> None:
    operation.status = MANUAL_OUTCOME
    operation.remote_call_state = "reconciled_hold"
    operation.blocker_code = blocker
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.operation_version += 1


def _close_item_and_batch(context: InterruptedContext, preview: dict) -> None:
    context.item.status = MANUAL_OUTCOME
    context.item.outcome = MANUAL_OUTCOME
    context.item.blocker_code = preview["blocker_code"]
    context.item.finished_at = _now()
    context.item.version += 1
    context.batch.execution_release_sha = preview["runtime_release_sha"]
    context.batch.status = "running" if preview["pending_count"] else "completed_with_manual"
    context.batch.version += 1


def _evidence_manifest(preview: dict) -> dict:
    return {key: preview[key] for key in (
        "boundary", "classification", "blocker_code", "interruption_ref", "previous_execution_release_sha",
        "runtime_release_sha", "operation_status", "remote_call_state", "remote_effect_started_at",
        "interrupted_flow",
    )}


def _audit_transition(session, context: InterruptedContext, preview: dict) -> None:
    audit(
        session,
        tenant_id=context.batch.tenant_id,
        actor=preview["approved_by"],
        action=ACTION,
        target_type="tg_authorization_dr_operation",
        target_id=context.operation.id,
        detail=(
            f"account_id={context.item.account_id}; approval_ref={preview['approval_ref']}; "
            f"idempotency_key={preview['idempotency_key']}; fingerprint={preview['fingerprint']}; "
            f"interruption_ref={preview['interruption_ref']}; blocker={preview['blocker_code']}; "
            f"execution_release={preview['previous_execution_release_sha']}->{preview['runtime_release_sha']}"
        ),
    )


def _existing_result(
    session, batch_id: str, *, account_id: int, key: str,
) -> dict | None:
    normalized = _idempotency_key(key)
    context = load_interrupted_context(session, batch_id, account_id)
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == context.operation.id,
    ))
    if not case or case.classification not in {CLASSIFICATION, STOPPED_CLASSIFICATION}:
        return None
    if case.apply_idempotency_key != normalized:
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted B key was already used")
    return _result(session, batch_id, account_id)


def _idempotent(existing: dict, fingerprint: str) -> dict:
    if existing["fingerprint"] != fingerprint:
        raise AuthorizationDrError("idempotency_key_conflict", "Interrupted B fingerprint changed")
    return {**existing, "already_applied": True}


def _result(session, batch_id: str, account_id: int) -> dict:
    context = load_interrupted_context(session, batch_id, account_id)
    case = session.get(TgAuthorizationDrReconcileCase, context.operation.reconcile_case_id)
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
        "operation_id": context.operation.id,
        "operation_status": context.operation.status,
        "operation_version": context.operation.operation_version,
        "remote_call_state": context.operation.remote_call_state,
        "remote_effect_started_at": str(context.operation.remote_effect_started_at),
        "blocker_code": context.operation.blocker_code,
        "reconcile_status": context.operation.reconcile_status,
        "reconcile_case_id": context.operation.reconcile_case_id,
        "classification": case.classification if case else "",
        "boundary": manifest.get("boundary", RELEASE_CHANGED_BOUNDARY),
        "interruption_ref": manifest.get("interruption_ref", ""),
        "interrupted_flow": flow_snapshot(context.flows[0] if context.flows else None),
        "primary": _primary_snapshot(session, context.item),
        "fingerprint": case.evidence_fingerprint if case else "",
    }


def _interruption_ref(value: str) -> str:
    normalized = value.strip()
    if not REF_PATTERN.fullmatch(normalized):
        raise AuthorizationDrError("interruption_ref_invalid", "Interruption reference is invalid")
    return normalized


def _manual_contract(boundary: str) -> tuple[str, str]:
    if boundary == RELEASE_CHANGED_BOUNDARY:
        return CLASSIFICATION, BLOCKER
    if boundary == STOPPED_UNKNOWN_BOUNDARY:
        return STOPPED_CLASSIFICATION, STOPPED_BLOCKER
    raise AuthorizationDrError("online_abc_release_interrupted_state_invalid", "Interrupted B boundary is invalid")


__all__ = [
    "apply_release_interrupted_b",
    "STOPPED_BLOCKER",
    "STOPPED_CLASSIFICATION",
    "preview_release_interrupted_b",
    "readback_release_interrupted_b",
]
