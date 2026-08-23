from __future__ import annotations

from sqlalchemy import select

from app.models import TgAuthorizationDrStageFact

from .contracts import AuthorizationDrError
from .operation_state import owned_operation


STAGES = frozenset({
    "remote_login_started",
    "remote_login_confirmed",
    "local_copy_verified",
    "snapshot_copy_verified",
    "inventory_persisted",
    "central_receipt_committed",
    "restore_probe_passed",
    "slot_committed",
    "artifact_recovery_abandoned",
    "orphan_revoke_started",
})


def record_operation_stage(
    session,
    operation_id: str,
    *,
    node_id: str,
    owner_epoch: int,
    lease_token: str,
    stage: str,
    manifest_digest: str,
    evidence_manifest: dict,
) -> TgAuthorizationDrStageFact:
    operation = owned_operation(
        session,
        operation_id,
        node_id=node_id,
        owner_epoch=owner_epoch,
        lease_token=lease_token,
    )
    return append_stage_fact(
        session,
        operation,
        stage=stage,
        manifest_digest=manifest_digest,
        evidence_manifest=evidence_manifest,
    )


def append_stage_fact(session, operation, *, stage: str, manifest_digest: str, evidence_manifest: dict):
    _validate_stage(stage, manifest_digest, evidence_manifest)
    existing = session.scalar(select(TgAuthorizationDrStageFact).where(
        TgAuthorizationDrStageFact.operation_id == operation.id,
        TgAuthorizationDrStageFact.stage == stage,
    ))
    if existing:
        if existing.manifest_digest != manifest_digest or existing.evidence_manifest != evidence_manifest:
            raise AuthorizationDrError("reconcile_evidence_conflict", "Operation stage fact changed")
        return existing
    fact = TgAuthorizationDrStageFact(
        operation_id=operation.id,
        node_id=operation.owner_node_id,
        owner_epoch=operation.owner_epoch,
        stage=stage,
        manifest_digest=manifest_digest,
        evidence_manifest=evidence_manifest,
    )
    session.add(fact)
    return fact


def _validate_stage(stage: str, manifest_digest: str, evidence_manifest: dict) -> None:
    if stage not in STAGES:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Operation stage is unsupported")
    if len(manifest_digest) != 64 or any(char not in "0123456789abcdef" for char in manifest_digest):
        raise AuthorizationDrError("reconcile_evidence_invalid", "Operation stage digest is invalid")
    allowed = {
        "bundle_generation", "ciphertext_digest", "inventory_sequence",
        "fingerprint", "candidate_hash_digest", "remote_set_digest",
    }
    if set(evidence_manifest) - allowed:
        raise AuthorizationDrError("reconcile_evidence_invalid", "Operation stage manifest contains unsupported fields")


__all__ = ["append_stage_fact", "record_operation_stage"]
