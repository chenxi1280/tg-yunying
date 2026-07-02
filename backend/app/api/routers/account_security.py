"""TG account security hardening and profile initialization routes."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.common.http import not_found
from app.database import get_session
from app.models import TgAccount, TgAccountSecurityBatch
from app.repositories.tenant import require_resource_tenant
from app.schemas.account_security import (
    AccountSecurityBatchCreate,
    AccountSecurityBatchOut,
    AccountSecurityDetailOut,
    ManagedTwoFaOut,
    ManagedTwoFaRequest,
    AccountSecurityPrecheckOut,
    AccountSecurityPrecheckRequest,
    AccountSecurityRetryRequest,
    AccountSecuritySnapshotOut,
    AccountSecuritySummaryOut,
    DeviceCleanupConfirmRequest,
    DeviceCleanupPrecheckOut,
)
from app.services.account_security import (
    account_security_batch_detail,
    account_security_detail,
    account_security_summary,
    cancel_account_security_batch,
    cleanup_devices_from_precheck,
    create_account_security_batch,
    create_device_cleanup_precheck,
    list_account_security_batches,
    precheck_account_security_batch,
    refresh_account_security,
    retry_account_security_batch,
    rotate_managed_two_fa_password,
    save_managed_two_fa_password,
)

router = APIRouter()

SECURITY_ACTION_TYPES = {"cleanup_devices", "set_two_fa"}
STANDBY_SESSION_ACTION_TYPES = {"provision_standby_session", "self_heal_session"}
PROFILE_ACTION_TYPES = {"update_profile", "update_username", "update_avatar"}


def _require_batch_action_permissions(current_user: CurrentUser, action_types: list[str]) -> None:
    actions = set(action_types or [])
    if actions & SECURITY_ACTION_TYPES and not current_user.has_permission("accounts.security.batch"):
        raise HTTPException(status_code=403, detail="accounts.security.batch required")
    if actions & STANDBY_SESSION_ACTION_TYPES and not current_user.has_permission("accounts.security.session_manage"):
        raise HTTPException(status_code=403, detail="accounts.security.session_manage required")
    if actions & PROFILE_ACTION_TYPES and not current_user.has_permission("accounts.profile.batch_update"):
        raise HTTPException(status_code=403, detail="accounts.profile.batch_update required")
    unknown = actions - SECURITY_ACTION_TYPES - STANDBY_SESSION_ACTION_TYPES - PROFILE_ACTION_TYPES
    if unknown:
        raise HTTPException(status_code=400, detail=f"unknown action_types: {', '.join(sorted(unknown))}")


def _require_retry_batch_permissions(
    session: Session,
    tenant_id: int,
    batch_id: int,
    current_user: CurrentUser,
) -> None:
    batch = session.get(TgAccountSecurityBatch, batch_id)
    if not batch or batch.tenant_id != tenant_id:
        raise ValueError("batch not found")
    _require_batch_action_permissions(current_user, _stored_batch_action_types(batch))


def _stored_batch_action_types(batch: TgAccountSecurityBatch) -> list[str]:
    try:
        raw = json.loads(batch.action_types or "[]")
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="invalid batch action_types") from exc
    if not isinstance(raw, list):
        raise HTTPException(status_code=400, detail="invalid batch action_types")
    return [str(action) for action in raw if str(action)]


def _require_reason(reason: str) -> str:
    reason = (reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="操作原因不能为空")
    return reason


@router.get("/api/tg-accounts/security/summary", response_model=AccountSecuritySummaryOut)
def get_account_security_summary(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return account_security_summary(session, resolve_tenant_id(current_user, tenant_id))


@router.get("/api/tg-accounts/{account_id}/security", response_model=AccountSecurityDetailOut)
def get_account_security_detail(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return account_security_detail(session, current_user.tenant_id or 1, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/refresh", response_model=AccountSecuritySnapshotOut)
def post_account_security_refresh(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return refresh_account_security(session, current_user.tenant_id or 1, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/cleanup-devices", response_model=AccountSecurityBatchOut)
def post_account_security_cleanup_devices(
    account_id: int,
    payload: AccountSecurityBatchCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, ["cleanup_devices"])
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        payload.account_ids = [account_id]
        payload.action_types = ["cleanup_devices"]
        payload.confirm_text = payload.confirm_text or "确认"
        payload.reason = _require_reason(payload.reason)
        return create_account_security_batch(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/devices/cleanup/precheck", response_model=DeviceCleanupPrecheckOut)
def post_account_devices_cleanup_precheck(
    account_id: int,
    payload: AccountSecurityPrecheckRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, ["cleanup_devices"])
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        _require_reason(payload.reason)
        return create_device_cleanup_precheck(session, current_user.tenant_id or 1, account_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/devices/cleanup", response_model=DeviceCleanupPrecheckOut)
def post_account_devices_cleanup(
    account_id: int,
    payload: DeviceCleanupConfirmRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, ["cleanup_devices"])
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        _require_reason(payload.reason)
        return cleanup_devices_from_precheck(session, current_user.tenant_id or 1, account_id, payload.precheck_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/set-2fa", response_model=AccountSecurityBatchOut)
def post_account_security_set_2fa(
    account_id: int,
    payload: AccountSecurityBatchCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, ["set_two_fa"])
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        payload.account_ids = [account_id]
        payload.action_types = ["set_two_fa"]
        payload.confirm_text = payload.confirm_text or "确认"
        payload.reason = _require_reason(payload.reason)
        return create_account_security_batch(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/managed-2fa", response_model=ManagedTwoFaOut)
def post_account_security_managed_2fa(
    account_id: int,
    payload: ManagedTwoFaRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    if not current_user.has_permission("accounts.security.credential_manage"):
        raise HTTPException(status_code=403, detail="accounts.security.credential_manage required")
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return save_managed_two_fa_password(session, current_user.tenant_id or 1, account_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/managed-2fa/rotate", response_model=ManagedTwoFaOut)
def post_account_security_managed_2fa_rotate(
    account_id: int,
    payload: ManagedTwoFaRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    if not current_user.has_permission("accounts.security.credential_manage"):
        raise HTTPException(status_code=403, detail="accounts.security.credential_manage required")
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return rotate_managed_two_fa_password(session, current_user.tenant_id or 1, account_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/security/update-profile", response_model=AccountSecurityBatchOut)
def post_account_security_update_profile(
    account_id: int,
    payload: AccountSecurityBatchCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, ["update_profile", "update_username", "update_avatar"])
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        payload.account_ids = [account_id]
        payload.action_types = payload.action_types or ["update_profile", "update_username", "update_avatar"]
        _require_batch_action_permissions(current_user, payload.action_types)
        payload.confirm_text = payload.confirm_text or "确认"
        payload.reason = _require_reason(payload.reason)
        return create_account_security_batch(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/security-batches/precheck", response_model=AccountSecurityPrecheckOut)
def post_account_security_batch_precheck(
    payload: AccountSecurityPrecheckRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, payload.action_types)
    try:
        return precheck_account_security_batch(session, resolve_tenant_id(current_user, tenant_id), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/security-batches/profile-preview", response_model=AccountSecurityPrecheckOut)
def post_account_security_profile_preview(
    payload: AccountSecurityPrecheckRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    payload.action_types = payload.action_types or ["update_profile", "update_username", "update_avatar"]
    _require_batch_action_permissions(current_user, payload.action_types)
    try:
        return precheck_account_security_batch(session, resolve_tenant_id(current_user, tenant_id), payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/security-batches", response_model=AccountSecurityBatchOut)
def post_account_security_batch(
    payload: AccountSecurityBatchCreate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    _require_batch_action_permissions(current_user, payload.action_types)
    payload.reason = _require_reason(payload.reason)
    try:
        return create_account_security_batch(session, resolve_tenant_id(current_user, tenant_id), payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tg-accounts/security-batches", response_model=list[AccountSecurityBatchOut])
def get_account_security_batches(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_account_security_batches(session, resolve_tenant_id(current_user, tenant_id))


@router.get("/api/tg-accounts/security-batches/{batch_id}", response_model=AccountSecurityBatchOut)
def get_account_security_batch(
    batch_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return account_security_batch_detail(session, current_user.tenant_id or 1, batch_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/security-batches/{batch_id}/retry", response_model=AccountSecurityBatchOut)
def post_account_security_batch_retry(
    batch_id: int,
    payload: AccountSecurityRetryRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        _require_retry_batch_permissions(session, current_user.tenant_id or 1, batch_id, current_user)
        return retry_account_security_batch(session, current_user.tenant_id or 1, batch_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/security-batches/{batch_id}/cancel", response_model=AccountSecurityBatchOut)
def post_account_security_batch_cancel(
    batch_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        return cancel_account_security_batch(session, current_user.tenant_id or 1, batch_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
