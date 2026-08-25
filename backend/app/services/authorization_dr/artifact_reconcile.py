from __future__ import annotations

from datetime import timedelta
import hashlib
import json
from uuid import uuid4

from sqlalchemy import select

from app.models import (
    AuthorizationDrExecutionNode,
    AuthorizationDrRuntimeContract,
    TelegramDeveloperApp,
    TgAccountAuthorization,
    TgAuthorizationDrBatchItem,
    TgAuthorizationDrOperation,
    TgAuthorizationDrReconcileCase,
    TgAuthorizationDrStageFact,
    TgAuthorizationWakeBundle,
)
from app.services._common import _now, audit
from app.services.developer_apps import credentials_for_developer_app

from .contracts import AuthorizationDrError
from .migration_results import refresh_migration_batch
from .operation_state import owned_operation
from .readiness import MY_NODE_STALE_SECONDS
from .stage_facts import append_stage_fact


RECONCILE_LEASE_SECONDS = 180
CENTRAL_BUNDLE_CLASSIFICATION = "central_bundle_restore_forward"


def claim_artifact_reconcile(session, operation_id: str, node_id: str) -> dict:
    _require_reconcile_runtime(session, node_id)
    operation = session.scalar(select(TgAuthorizationDrOperation).where(
        TgAuthorizationDrOperation.id == operation_id,
    ).with_for_update())
    case = _repair_case(session, operation, node_id)
    _require_claimable(operation, node_id)
    _require_no_active_owner(session, operation.id)
    operation.owner_epoch += 1
    operation.lease_token = uuid4().hex
    operation.lease_expires_at = _now() + timedelta(seconds=RECONCILE_LEASE_SECONDS)
    operation.status = "reconcile_artifact_running"
    operation.reconcile_status = "repair_running"
    operation.operation_version += 1
    session.commit()
    return _claim_out(operation, case)


def artifact_probe_material(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
) -> dict:
    operation = owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    if operation.status != "reconcile_artifact_running":
        raise AuthorizationDrError("reconcile_operation_conflict", "Artifact repair is not running")
    app = session.get(TelegramDeveloperApp, operation.developer_app_id)
    if not app or app.credentials_version != operation.developer_app_credentials_version:
        raise AuthorizationDrError("authorization_version_conflict", "Frozen Developer App changed")
    credentials = credentials_for_developer_app(app)
    return {
        "api_id": credentials.api_id,
        "api_hash": credentials.api_hash,
        "app_name": credentials.app_name,
        "credentials_version": credentials.credentials_version,
    }


def preview_artifact_abandon(
    session, operation_id: str, *, tenant_id: int, expected_operation_version: int,
    observed_ciphertext_digest: str, requested_by: str,
) -> dict:
    operation, case, _item = _abandon_inputs(session, operation_id, tenant_id, lock=False)
    _require_abandonable(operation, case, expected_operation_version, observed_ciphertext_digest)
    fingerprint = _abandon_fingerprint(operation, case, observed_ciphertext_digest, requested_by)
    return _abandon_out(operation, case, fingerprint)


def apply_artifact_abandon(
    session, operation_id: str, *, tenant_id: int, expected_operation_version: int,
    observed_ciphertext_digest: str, requested_by: str, evidence_fingerprint: str,
    actor: str, approval_ref: str, idempotency_key: str,
) -> dict:
    operation, case, item = _abandon_inputs(session, operation_id, tenant_id, lock=True)
    if operation.status == "manual_required" and case.status == "applied":
        return _idempotent_abandon(session, operation, case, evidence_fingerprint)
    expected = _abandon_fingerprint(operation, case, observed_ciphertext_digest, requested_by)
    _require_abandon_approval(requested_by, actor, approval_ref, idempotency_key, evidence_fingerprint, expected)
    _require_abandonable(operation, case, expected_operation_version, observed_ciphertext_digest)
    _apply_abandon_transition(session, operation, case, item, expected, actor, approval_ref, idempotency_key)
    session.commit()
    return _abandon_out(operation, case, expected)


def _abandon_inputs(session, operation_id: str, tenant_id: int, *, lock: bool):
    query = select(TgAuthorizationDrOperation).where(TgAuthorizationDrOperation.id == operation_id)
    operation = session.scalar(query.with_for_update() if lock else query)
    if not operation or operation.tenant_id != tenant_id:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    case_query = select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation.id,
    )
    case = session.scalar(case_query.with_for_update() if lock else case_query)
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    if not case or not item:
        raise AuthorizationDrError("reconcile_case_conflict", "Artifact repair case is unavailable")
    return operation, case, item


def _require_abandonable(operation, case, expected_version: int, observed_digest: str) -> None:
    manifest = dict(case.evidence_manifest or {})
    valid = case.status == "repair_approved" and case.classification in {
        "local_only_bundle", "inventory_ahead_of_central",
    }
    valid = valid and operation.status in {"reconcile_artifact_ready", "reconcile_artifact_running"}
    valid = valid and operation.candidate_authorization_id is None and operation.operation_version == expected_version
    valid = valid and observed_digest == manifest.get("ciphertext_digest")
    lease_clear = operation.lease_expires_at is None or operation.lease_expires_at <= _now()
    if not valid or not lease_clear:
        raise AuthorizationDrError("reconcile_transition_blocked", "Legacy artifact abandonment evidence changed")


def _abandon_fingerprint(operation, case, observed_digest: str, requested_by: str) -> str:
    payload = {
        "operation": [operation.id, operation.operation_version, operation.owner_epoch],
        "case": [case.id, case.evidence_fingerprint, case.status],
        "artifact": [1, observed_digest, "missing_durable_recovery_metadata"],
        "requested_by": requested_by.strip(),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _require_abandon_approval(requested_by, actor, approval_ref, idempotency_key, observed, expected) -> None:
    valid = requested_by.strip() and actor.strip() and requested_by.strip() != actor.strip()
    valid = valid and approval_ref.strip() and idempotency_key.strip() and observed == expected
    if not valid:
        raise AuthorizationDrError("reconcile_approval_required", "Artifact abandonment approval is incomplete")


def _apply_abandon_transition(session, operation, case, item, fingerprint, actor, approval_ref, key) -> None:
    append_stage_fact(
        session, operation, stage="artifact_recovery_abandoned", manifest_digest=fingerprint,
        evidence_manifest={"ciphertext_digest": dict(case.evidence_manifest)["ciphertext_digest"]},
    )
    operation.status = "manual_required"
    operation.blocker_code = "legacy_bundle_key_unrecoverable"
    operation.remote_call_state = "reconciled_hold"
    operation.reconcile_status = "applied"
    operation.reconciled_at = _now()
    operation.finished_at = _now()
    operation.lease_token = ""
    operation.lease_expires_at = None
    operation.operation_version += 1
    case.status = "applied"
    case.applied_at = _now()
    item.status = "manual_required"
    item.outcome = operation.blocker_code
    item.blocker_code = operation.blocker_code
    item.finished_at = _now()
    item.version += 1
    refresh_migration_batch(session, item.id)
    audit(
        session, tenant_id=operation.tenant_id, actor=actor, action="收口不可恢复旧版 DR 工件",
        target_type="tg_authorization_dr_operation", target_id=operation.id,
        detail=f"approval_ref={approval_ref.strip()}; idempotency_key={key.strip()}",
    )


def _idempotent_abandon(session, operation, case, observed: str) -> dict:
    fact = session.scalar(select(TgAuthorizationDrStageFact).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
        TgAuthorizationDrStageFact.stage == "artifact_recovery_abandoned",
    ))
    if not fact or observed != fact.manifest_digest or operation.blocker_code != "legacy_bundle_key_unrecoverable":
        raise AuthorizationDrError("reconcile_evidence_conflict", "Artifact abandonment evidence changed")
    return _abandon_out(operation, case, observed)


def _abandon_out(operation, case, fingerprint: str) -> dict:
    return {
        "operation_id": operation.id, "account_id": operation.account_id,
        "operation_status": operation.status, "operation_version": operation.operation_version,
        "case_status": case.status, "blocker_code": operation.blocker_code,
        "evidence_fingerprint": fingerprint,
    }


def _require_reconcile_runtime(session, node_id: str) -> AuthorizationDrExecutionNode:
    contract = session.get(AuthorizationDrRuntimeContract, 1)
    node = session.get(AuthorizationDrExecutionNode, node_id)
    cutoff = _now() - timedelta(seconds=MY_NODE_STALE_SECONDS)
    valid_node = node and node.status == "ready" and node.active_client_count == 0
    fresh = node and node.last_heartbeat_at and node.last_heartbeat_at > cutoff
    if not contract or contract.mode != "off" or contract.mutation_hold_reason:
        raise AuthorizationDrError("reconcile_runtime_conflict", "DR runtime must remain off without a mutation hold")
    if not valid_node or not fresh:
        raise AuthorizationDrError("malaysia_wake_unavailable", "MY reconcile node is not ready")
    return node


def _repair_case(session, operation, node_id: str) -> TgAuthorizationDrReconcileCase:
    if not operation:
        raise AuthorizationDrError("migration_operation_not_found", "Migration operation does not exist")
    case = session.scalar(select(TgAuthorizationDrReconcileCase).where(
        TgAuthorizationDrReconcileCase.operation_id == operation.id,
    ).with_for_update())
    valid = case and case.status == "repair_approved"
    valid = valid and case.classification in {
        "local_only_bundle", "inventory_ahead_of_central", CENTRAL_BUNDLE_CLASSIFICATION,
    }
    if not valid:
        raise AuthorizationDrError("reconcile_case_conflict", "Artifact repair is not approved")
    source_id = operation.source_authorization_id or operation.code_source_authorization_id
    source = session.get(TgAccountAuthorization, source_id)
    item = session.get(TgAuthorizationDrBatchItem, operation.batch_item_id)
    node = session.get(AuthorizationDrExecutionNode, node_id)
    initial = operation.status == "reconcile_artifact_ready" and operation.candidate_authorization_id is None
    initial = initial and operation.operation_version == case.expected_operation_version + 1
    progressed = _central_progress_matches(session, operation, case)
    pre_bundle_retry = _pre_bundle_retry_matches(session, operation)
    frozen = case and operation.reconcile_case_id == case.id and (initial or progressed or pre_bundle_retry)
    frozen = frozen and item and item.version == case.expected_item_version
    frozen = frozen and _reconcile_source_matches(operation, case, source)
    frozen = frozen and node and node.runtime_image_sha == case.expected_runtime_image_sha
    if not frozen:
        raise AuthorizationDrError("reconcile_case_conflict", "Artifact repair is not approved")
    return case


def _reconcile_source_matches(operation, case, source) -> bool:
    if not source or source.fact_version != case.expected_source_fact_version:
        return False
    if not source.is_slot_current or not source.protected_from_cleanup:
        return False
    if case.classification == CENTRAL_BUNDLE_CLASSIFICATION:
        return bool(
            operation.source_authorization_id is None
            and operation.code_source_authorization_id == source.id
        )
    return operation.source_authorization_id == source.id


def _require_claimable(operation, node_id: str) -> None:
    retry_statuses = {"reconcile_artifact_running", "bundle_copies_verified", "ready_for_slot_commit", "slot_commit_prepared"}
    expired = operation.lease_expires_at is not None and operation.lease_expires_at <= _now()
    retryable = operation.status in retry_statuses and expired
    if operation.status != "reconcile_artifact_ready" and not retryable:
        raise AuthorizationDrError("reconcile_operation_conflict", "Artifact repair is not claimable")
    if operation.owner_node_id != node_id:
        raise AuthorizationDrError("reconcile_operation_conflict", "Artifact repair owner or status changed")
    if operation.status == "reconcile_artifact_ready" and (operation.lease_token or operation.lease_expires_at):
        raise AuthorizationDrError("reconcile_operation_conflict", "Artifact repair already has a lease")


def _central_progress_matches(session, operation, case) -> bool:
    if operation.status not in {"reconcile_artifact_running", "bundle_copies_verified", "ready_for_slot_commit", "slot_commit_prepared"}:
        return False
    bundle = session.scalar(select(TgAuthorizationWakeBundle).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ))
    manifest = dict(case.evidence_manifest or {})
    return bool(
        bundle
        and bundle.bundle_generation == manifest["bundle_generation"]
        and bundle.ciphertext_digest == manifest["ciphertext_digest"]
        and operation.candidate_authorization_id == bundle.authorization_id
    )


def _pre_bundle_retry_matches(session, operation) -> bool:
    if operation.status != "reconcile_artifact_running" or operation.candidate_authorization_id is not None:
        return False
    bundle = session.scalar(select(TgAuthorizationWakeBundle.id).where(
        TgAuthorizationWakeBundle.operation_id == operation.id,
    ).limit(1))
    return not bundle and operation.remote_call_state == "unknown" and operation.reconcile_status == "repair_running"


def _require_no_active_owner(session, operation_id: str) -> None:
    active = session.scalar(select(TgAuthorizationDrOperation.id).where(
        TgAuthorizationDrOperation.id != operation_id,
        TgAuthorizationDrOperation.lease_expires_at > _now(),
    ).limit(1))
    if active:
        raise AuthorizationDrError("malaysia_owner_fencing_unproven", "Another MY operation has an active lease")


def _claim_out(operation, case) -> dict:
    manifest = dict(case.evidence_manifest or {})
    return {
        "operation_id": operation.id,
        "account_id": operation.account_id,
        "owner_node_id": operation.owner_node_id,
        "owner_epoch": operation.owner_epoch,
        "lease_token": operation.lease_token,
        "lease_expires_at": operation.lease_expires_at,
        "target_generation": operation.target_generation,
        "developer_app_id": operation.developer_app_id,
        "egress_id": operation.egress_id,
        "egress_version": operation.egress_version,
        "classification": case.classification,
        "expected_ciphertext_digest": manifest["ciphertext_digest"],
        "expected_inventory_sequence": manifest["inventory_sequence"],
    }


__all__ = [
    "apply_artifact_abandon", "artifact_probe_material", "claim_artifact_reconcile",
    "preview_artifact_abandon",
]
