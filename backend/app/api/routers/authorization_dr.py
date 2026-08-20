from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, resolve_tenant_id
from app.common.http import forbidden
from app.config import get_settings
from app.database import get_session
from app.security import encrypt_secret
from app.schemas.authorization_dr import (
    DrBatchOut,
    DrClaimOut,
    DrClaimRequest,
    DrLoginCodeOut,
    DrLoginFailureRequest,
    DrLoginMaterialOut,
    DrMigrationApprovalRequest,
    DrMigrationPreviewRequest,
    DrNodeHeartbeatRequest,
    DrNodeOut,
    DrOperationOut,
    DrOwnerRequest,
    DrRestoreProbeRequest,
    DrWakeBundleRequest,
)
from app.services.authorization_dr import (
    AuthorizationDrError,
    CopyReceipt,
    RestoreProbeReceipt,
    WakeBundleReceipt,
    approve_migration_batch,
    claim_migration_operation,
    commit_migration_slot,
    commit_wake_bundle_receipt,
    list_execution_nodes,
    mark_login_remote_failed,
    mark_login_remote_started,
    mark_login_remote_unknown,
    migration_login_material,
    migration_batch_out,
    operation_out,
    preview_migration_batch,
    poll_migration_login_code,
    record_node_heartbeat,
    record_restore_probe,
    renew_migration_lease,
)


router = APIRouter()


def _require_manage(current_user: CurrentUser) -> None:
    if not current_user.has_permission("system.manage"):
        raise forbidden("system.manage required")


def _dr_http_error(exc: AuthorizationDrError) -> HTTPException:
    conflict_markers = ("conflict", "changed", "duplicated", "unknown")
    status = 409 if any(marker in exc.code for marker in conflict_markers) else 422
    return HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc)})


def _internal_node_identity(
    authorization: str = Header(default=""),
    x_dr_node_id: str = Header(default="", alias="X-DR-Node-ID"),
    x_client_cert_verified: str = Header(default="", alias="X-Client-Cert-Verified"),
) -> str:
    settings = get_settings()
    if not settings.authorization_dr_internal_token:
        raise HTTPException(status_code=503, detail="authorization DR internal identity is not configured")
    expected = f"Bearer {settings.authorization_dr_internal_token}"
    if not hmac.compare_digest(authorization, expected):
        raise HTTPException(status_code=401, detail="invalid authorization DR internal identity")
    if settings.authorization_dr_require_mtls and x_client_cert_verified != "SUCCESS":
        raise HTTPException(status_code=401, detail="verified mTLS client certificate is required")
    if not x_dr_node_id:
        raise HTTPException(status_code=401, detail="X-DR-Node-ID is required")
    return x_dr_node_id


@router.get("/api/tg-accounts/dr-execution-nodes", response_model=list[DrNodeOut])
def get_dr_execution_nodes(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.has_permission("system.view"):
        raise forbidden("system.view required")
    return list_execution_nodes(session)


@router.post("/api/system/authorization-dr-migrations/preview", response_model=DrBatchOut)
def post_migration_preview(
    *,
    payload: DrMigrationPreviewRequest,
    tenant_id: int | None = None,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_manage(current_user)
    resolved_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        batch = preview_migration_batch(
            session,
            resolved_tenant_id,
            payload.account_ids,
            idempotency_key=idempotency_key,
            actor=current_user.name,
        )
        return migration_batch_out(session, batch.id, resolved_tenant_id)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/api/system/authorization-dr-migrations/{batch_id}/approve", response_model=DrBatchOut)
def post_migration_approval(
    *,
    batch_id: str,
    payload: DrMigrationApprovalRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_manage(current_user)
    resolved_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        batch = approve_migration_batch(
            session,
            batch_id,
            expected_version=payload.expected_version,
            approval_ref=payload.approval_ref,
            actor=current_user.name,
        )
        if batch.tenant_id != resolved_tenant_id:
            raise AuthorizationDrError("migration_batch_not_found", "Migration batch does not exist")
        return migration_batch_out(session, batch.id, resolved_tenant_id)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.get("/api/system/authorization-dr-migrations/{batch_id}", response_model=DrBatchOut)
def get_migration_batch(
    *,
    batch_id: str,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.has_permission("system.view"):
        raise forbidden("system.view required")
    try:
        return migration_batch_out(session, batch_id, resolve_tenant_id(current_user, tenant_id))
    except AuthorizationDrError as exc:
        raise _dr_http_error(exc) from exc


@router.get("/api/tg-accounts/authorization-dr/operations/{operation_id}", response_model=DrOperationOut)
def get_dr_operation(
    *,
    operation_id: str,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.has_permission("system.view"):
        raise forbidden("system.view required")
    try:
        return operation_out(session, operation_id, resolve_tenant_id(current_user, tenant_id))
    except AuthorizationDrError as exc:
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/nodes/heartbeat", response_model=DrNodeOut)
def post_dr_node_heartbeat(
    payload: DrNodeHeartbeatRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        return record_node_heartbeat(
            session,
            node_id,
            region_code=payload.region_code,
            purpose=payload.purpose,
            capability_version=payload.capability_version,
            standby_egress_id=payload.standby_egress_id,
            active_client_count=payload.active_client_count,
            node_version=payload.node_version,
        )
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/claim", response_model=DrClaimOut)
def post_dr_operation_claim(
    *,
    payload: DrClaimRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        claim = claim_migration_operation(session, node_id)
        if claim is None:
            return Response(status_code=204)
        return claim
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/login-started", response_model=DrOperationOut)
def post_dr_login_started(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        operation = mark_login_remote_started(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return operation_out(session, operation.id, operation.tenant_id)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/lease-renew", response_model=DrOperationOut)
def post_dr_lease_renew(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        operation = renew_migration_lease(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return operation_out(session, operation.id, operation.tenant_id)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/login-material", response_model=DrLoginMaterialOut)
def post_dr_login_material(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        return migration_login_material(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/login-code", response_model=DrLoginCodeOut)
def post_dr_login_code(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        code = poll_migration_login_code(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return {"code": code}
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/login-unknown", status_code=204)
def post_dr_login_unknown(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        mark_login_remote_unknown(session, operation_id, node_id=node_id, owner_epoch=payload.owner_epoch)
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/login-failed", status_code=204)
def post_dr_login_failed(
    *,
    operation_id: str,
    payload: DrLoginFailureRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        mark_login_remote_failed(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
            blocker_code=payload.blocker_code,
        )
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle")
def post_dr_wake_bundle(
    *,
    operation_id: str,
    payload: DrWakeBundleRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        receipt = WakeBundleReceipt(
            bundle_generation=payload.bundle_generation,
            ciphertext_digest=payload.ciphertext_digest,
            wrapped_dek_ciphertext=payload.wrapped_dek_ciphertext,
            kms_key_ref_digest=payload.kms_key_ref_digest,
            kms_key_version=payload.kms_key_version,
            auth_key_fingerprint_digest=payload.auth_key_fingerprint_digest,
            telegram_user_id_digest=payload.telegram_user_id_digest,
            authorization_fingerprint_digest=payload.authorization_fingerprint_digest,
            remote_authorization_hash_ciphertext=encrypt_secret(payload.remote_authorization_hash),
            inventory_sequence=payload.inventory_sequence,
            inventory_manifest_digest=payload.inventory_manifest_digest,
            copies=tuple(CopyReceipt(**copy.model_dump()) for copy in payload.copies),
        )
        bundle = commit_wake_bundle_receipt(
            session,
            operation_id,
            receipt,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return {
            "bundle_id": bundle.id,
            "bundle_generation": bundle.bundle_generation,
            "receipt_status": bundle.receipt_status,
            "recoverable_copy_count": bundle.recoverable_copy_count,
        }
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/wake-bundle/restore-probe")
def post_dr_restore_probe(
    *,
    operation_id: str,
    payload: DrRestoreProbeRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        fields = payload.model_dump(exclude={"owner_epoch", "lease_token"})
        fact = record_restore_probe(
            session,
            operation_id,
            RestoreProbeReceipt(**fields),
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return {"probe_id": fact.id, "status": fact.status, "observed_at": fact.observed_at}
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


@router.post("/internal/v1/authorization-dr/operations/{operation_id}/slot-commit")
def post_dr_slot_commit(
    *,
    operation_id: str,
    payload: DrOwnerRequest,
    node_id: str = Depends(_internal_node_identity),
    session: Session = Depends(get_session),
):
    try:
        decision = commit_migration_slot(
            session,
            operation_id,
            node_id=node_id,
            owner_epoch=payload.owner_epoch,
            lease_token=payload.lease_token,
        )
        return {
            "decision_id": decision.id,
            "decision_generation": decision.decision_generation,
            "status": decision.status,
            "recovery_gate_status": decision.recovery_gate_status,
        }
    except AuthorizationDrError as exc:
        session.rollback()
        raise _dr_http_error(exc) from exc


__all__ = ["router"]
