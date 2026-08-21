from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.database import get_session
from app.schemas.authorization_dr import (
    DrArtifactClaimOut,
    DrArtifactProbeMaterialOut,
    DrOwnerRequest,
    DrStageFactRequest,
)
from app.services.authorization_dr import (
    AuthorizationDrError,
    artifact_probe_material,
    claim_artifact_reconcile,
    record_operation_stage,
)

from .authorization_dr import _dr_http_error, _internal_node_identity


router = APIRouter()


@router.post(
    "/internal/v1/authorization-dr/operations/{operation_id}/reconcile-claim",
    response_model=DrArtifactClaimOut,
)
def post_artifact_reconcile_claim(
    operation_id: str,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        return claim_artifact_reconcile(session, operation_id, node_id)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post(
    "/internal/v1/authorization-dr/operations/{operation_id}/reconcile-probe-material",
    response_model=DrArtifactProbeMaterialOut,
)
def post_artifact_probe_material(
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        return artifact_probe_material(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/stage-facts")
def post_operation_stage_fact(
    operation_id: str,
    payload: DrStageFactRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    evidence = {
        "bundle_generation": payload.bundle_generation,
        "ciphertext_digest": payload.ciphertext_digest,
        "inventory_sequence": payload.inventory_sequence,
    }
    evidence = {key: value for key, value in evidence.items() if value not in {"", 0}}
    try:
        fact = record_operation_stage(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
            stage=payload.stage,
            manifest_digest=payload.manifest_digest,
            evidence_manifest=evidence,
        )
        session.commit()
        return {"stage_fact_id": fact.id, "stage": fact.stage}
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


__all__ = ["router"]
