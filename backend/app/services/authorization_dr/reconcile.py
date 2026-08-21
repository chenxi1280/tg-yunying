from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TgAccountAuthorization,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationWakeBundle,
)
from app.services._common import _now, audit

from .contracts import AuthorizationDrError
from .migration_results import LOGIN_FAILURE_STATUSES, refresh_migration_batch


UNKNOWN_STATUS = "provision_reconcile_unknown"
EVIDENCE_KIND = "historical_typed_login_failure"


def preview_operation_reconcile(
    session,
    operation_id: str,
    *,
    tenant_id: int,
    expected_operation_version: int,
    evidence: dict,
    actor: str,
) -> TgAuthorizationDrReconcileCase:
    operation, item, source = _lock_inputs(session, operation_id, tenant_id=tenant_id)
    _require_stopped_runtime(session, operation.owner_node_id)
    _require_unknown(operation, expected_operation_version=expected_operation_version)
    manifest = _validated_evidence(operation, evidence)
    artifact_state = _artifact_state(session, operation.id)
    fingerprint = _evidence_fingerprint(operation, item, source, manifest, artifact_state)
    existing = _case_for_operation(session, operation.id, lock=True)
    if existing:
        return _matching_existing(existing, fingerprint)
    case = _new_case(operation, item, source, manifest, artifact_state, fingerprint, actor)
    session.add(case)
    session.flush()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="预览授权 DR unknown 对账",
        target_type="tg_authorization_dr_reconcile_case",
        target_id=case.id,
        detail=f"operation={operation.id}; fingerprint={fingerprint}; classification={case.classification}",
    )
    session.commit()
    return case


def apply_operation_reconcile(
    session,
    operation_id: str,
    *,
    tenant_id: int,
    expected_operation_version: int,
    evidence_fingerprint: str,
    approval_ref: str,
    idempotency_key: str,
    actor: str,
) -> TgAuthorizationDrReconcileCase:
    operation, item, source = _lock_inputs(session, operation_id, tenant_id=tenant_id)
    case = _case_for_operation(session, operation.id, lock=True)
    if not case:
        raise AuthorizationDrError("reconcile_case_not_found", "Reconcile preview does not exist")
    if case.status == "applied":
        return _idempotent_applied(case, idempotency_key)
    _require_apply_contract(case, actor=actor, approval_ref=approval_ref, idempotency_key=idempotency_key)
    _require_stopped_runtime(session, operation.owner_node_id)
    _require_unknown(operation, expected_operation_version=expected_operation_version)
    _require_frozen_facts(case, operation=operation, item=item, source=source)
    if _artifact_state(session, operation.id) != case.persisted_artifact_state:
        raise AuthorizationDrError("reconcile_artifact_conflict", "Persisted artifact state changed")
    if evidence_fingerprint != case.evidence_fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Reconcile evidence fingerprint changed")
    _require_apply_key_available(session, case, idempotency_key)
    _apply_confirmed_no_effect(operation, item, case)
    session.flush()
    refresh_migration_batch(session, item.id)
    _finish_case(case, actor=actor, approval_ref=approval_ref, idempotency_key=idempotency_key)
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="应用授权 DR unknown 对账",
        target_type="tg_authorization_dr_reconcile_case",
        target_id=case.id,
        detail=f"operation={operation.id}; transition={case.recommended_transition}; approval_ref={approval_ref}",
    )
    session.commit()
    return case


def reconcile_case_out(session, operation_id: str, tenant_id: int) -> dict:
    operation = session.get(TgAuthorizationDrOperation, operation_id)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    case = _case_for_operation(session, operation.id)
    if not case:
        raise AuthorizationDrError("reconcile_case_not_found", "Reconcile preview does not exist")
    fields = (
        "id", "tenant_id", "account_id", "operation_id", "reconcile_generation", "status",
        "classification", "recommended_transition", "blocker_code", "expected_operation_version",
        "expected_item_version", "expected_source_fact_version", "expected_owner_epoch", "expected_node_id",
        "expected_runtime_image_sha", "evidence_fingerprint", "persisted_artifact_state", "requested_by",
        "applied_by", "approval_ref", "created_at", "applied_at",
    )
    result = {field: getattr(case, field) for field in fields}
    result["evidence_manifest"] = dict(case.evidence_manifest or {})
    return result


def _lock_inputs(session, operation_id: str, *, tenant_id: int):
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
        TgAuthorizationDrOperation.tenant_id == tenant_id,
    ).with_for_update())
    if not operation:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    item = session.scalar(select(TgAuthorizationDrBatchItem).where(
        TgAuthorizationDrBatchItem.id == operation.batch_item_id,
    ).with_for_update())
    source = session.scalar(select(TgAccountAuthorization).where(
        TgAccountAuthorization.id == operation.source_authorization_id,
    ).with_for_update())
    if not item or not source:
        raise AuthorizationDrError("reconcile_frozen_fact_missing", "Frozen reconciliation facts are missing")
    return operation, item, source


def _require_stopped_runtime(session, node_id: str) -> None:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    node = session.get(AuthorizationDrExecutionNode, node_id)
    if not contract or contract.mode != "off":
        raise AuthorizationDrError("reconcile_runtime_conflict", "DR runtime must remain off")
    if not node or node.active_client_count != 0:
        raise AuthorizationDrError("reconcile_runtime_conflict", "MY node must have zero active clients")


def _require_unknown(operation, *, expected_operation_version: int) -> None:
    valid = operation.status == UNKNOWN_STATUS and operation.remote_call_state == "unknown"
    if not valid or operation.operation_version != expected_operation_version:
        raise AuthorizationDrError("reconcile_operation_conflict", "Unknown operation version changed")
    if operation.lease_token or operation.lease_expires_at:
        raise AuthorizationDrError("reconcile_operation_conflict", "Unknown operation still has an owner lease")


def _validated_evidence(operation, evidence: dict) -> dict:
    required = {
        "kind", "blocker_code", "event_digest", "source_ref", "runtime_image_sha", "node_id", "owner_epoch",
    }
    if set(evidence) != required or evidence.get("kind") != EVIDENCE_KIND:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Typed login failure evidence is invalid")
    blocker = str(evidence.get("blocker_code", ""))
    if blocker not in LOGIN_FAILURE_STATUSES:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Login failure classification is unsupported")
    digest = str(evidence.get("event_digest", ""))
    runtime_sha = str(evidence.get("runtime_image_sha", ""))
    identity_matches = (
        evidence.get("node_id") == operation.owner_node_id
        and evidence.get("owner_epoch") == operation.owner_epoch
    )
    digests_valid = _is_lower_hex(digest, (64,)) and _is_lower_hex(runtime_sha, (40, 64))
    source_ref_valid = 0 < len(str(evidence.get("source_ref", ""))) <= 160
    if not digests_valid or not source_ref_valid or not identity_matches:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Typed login evidence does not match owner facts")
    return {key: evidence[key] for key in sorted(required)}


def _artifact_state(session, operation_id: str) -> str:
    count = session.scalar(select(func.count()).select_from(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation_id,
    ))
    return "central_bundle" if int(count or 0) else "none"


def _evidence_fingerprint(operation, item, source, manifest: dict, artifact_state: str) -> str:
    payload = {
        "operation": [operation.id, operation.operation_version, operation.owner_node_id, operation.owner_epoch],
        "item": [item.id, item.version],
        "source": [source.id, source.fact_version, source.slot_generation, source.is_slot_current],
        "artifact_state": artifact_state,
        "evidence": manifest,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _new_case(operation, item, source, manifest, artifact_state, fingerprint: str, actor: str):
    blocker = manifest["blocker_code"]
    conflict = artifact_state != "none"
    return TgAuthorizationDrReconcileCase(
        tenant_id=operation.tenant_id,
        account_id=operation.account_id,
        operation_id=operation.id,
        status="conflict" if conflict else "decision_ready",
        classification="conflict" if conflict else "confirmed_no_effect",
        recommended_transition="hold" if conflict else LOGIN_FAILURE_STATUSES[blocker],
        blocker_code=blocker,
        expected_operation_version=operation.operation_version,
        expected_item_version=item.version,
        expected_source_fact_version=source.fact_version,
        expected_owner_epoch=operation.owner_epoch,
        expected_node_id=operation.owner_node_id,
        expected_runtime_image_sha=manifest["runtime_image_sha"],
        evidence_fingerprint=fingerprint,
        evidence_manifest=manifest,
        persisted_artifact_state=artifact_state,
        requested_by=actor,
    )


def _case_for_operation(session, operation_id: str, *, lock: bool = False):
    query = select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation_id,
    )
    return session.scalar(query.with_for_update() if lock else query)


def _matching_existing(case, fingerprint: str):
    if case.evidence_fingerprint != fingerprint:
        raise AuthorizationDrError("reconcile_evidence_conflict", "Reconcile evidence changed")
    return case


def _require_apply_contract(case, *, actor: str, approval_ref: str, idempotency_key: str) -> None:
    if case.requested_by == actor:
        raise AuthorizationDrError("approval_actor_conflict", "Reconcile applier must differ from requester")
    if not approval_ref.strip() or not idempotency_key.strip():
        raise AuthorizationDrError("reconcile_approval_required", "Approval and idempotency references are required")
    if case.status != "decision_ready":
        raise AuthorizationDrError("reconcile_case_conflict", "Reconcile case is not ready")


def _require_frozen_facts(case, *, operation, item, source) -> None:
    matches = (
        case.expected_operation_version == operation.operation_version
        and case.expected_item_version == item.version
        and case.expected_source_fact_version == source.fact_version
        and case.expected_owner_epoch == operation.owner_epoch
        and case.expected_node_id == operation.owner_node_id
        and source.id == operation.source_authorization_id
        and operation.candidate_authorization_id is None
        and source.is_slot_current
        and source.protected_from_cleanup
        and bool(source.session_ciphertext)
    )
    if not matches:
        raise AuthorizationDrError("reconcile_frozen_fact_conflict", "Frozen reconciliation facts changed")


def _apply_confirmed_no_effect(operation, item, case) -> None:
    if case.classification != "confirmed_no_effect" or case.persisted_artifact_state != "none":
        raise AuthorizationDrError("reconcile_transition_blocked", "Evidence does not allow no-effect transition")
    operation.status = case.recommended_transition
    operation.remote_call_state = "confirmed_no_effect"
    operation.blocker_code = case.blocker_code
    operation.reconcile_case_id = case.id
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.operation_version += 1
    item.status = case.recommended_transition
    item.outcome = case.blocker_code
    item.blocker_code = case.blocker_code
    item.finished_at = _now()
    item.version += 1


def _finish_case(case, *, actor: str, approval_ref: str, idempotency_key: str) -> None:
    case.status = "applied"
    case.applied_by = actor
    case.approval_ref = approval_ref.strip()
    case.apply_idempotency_key = idempotency_key.strip()
    case.applied_at = _now()


def _require_apply_key_available(session, case, idempotency_key: str) -> None:
    existing = session.scalar(select(TgAuthorizationDrReconcileCase.id).where(
        TgAuthorizationDrReconcileCase.tenant_id == case.tenant_id,
        TgAuthorizationDrReconcileCase.apply_idempotency_key == idempotency_key.strip(),
        TgAuthorizationDrReconcileCase.id != case.id,
    ).limit(1))
    if existing:
        raise AuthorizationDrError("reconcile_idempotency_conflict", "Reconcile apply key was already used")


def _is_lower_hex(value: str, lengths: tuple[int, ...]) -> bool:
    return len(value) in lengths and all(char in "0123456789abcdef" for char in value)


def _idempotent_applied(case, idempotency_key: str):
    if case.apply_idempotency_key != idempotency_key.strip():
        raise AuthorizationDrError("reconcile_idempotency_conflict", "Reconcile apply key changed")
    return case


__all__ = ["apply_operation_reconcile", "preview_operation_reconcile", "reconcile_case_out"]
