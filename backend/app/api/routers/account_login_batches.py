from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user, require_core_feature_access
from app.database import get_session
from app.schemas.account_login import (
    CodeSourceBindingRevealOut,
    CodeSourceBindingRevealRequest,
    LoginBatchCancelRequest,
    LoginBatchCapabilityOut,
    LoginBatchCreateRequest,
    LoginBatchDetailOut,
    LoginBatchItemOut,
    LoginBatchNotificationAckRequest,
    LoginBatchNotificationOut,
    LoginBatchOut,
    LoginBatchPrecheckOut,
    LoginBatchPrecheckRequest,
    LoginBatchRefreshCredentialRequest,
    LoginBatchRetryRequest,
)
from app.services.account_login import (
    BatchLoginError,
    acknowledge_notification,
    batch_login_capability,
    cancel_login_batch,
    create_login_batch,
    get_login_batch,
    get_login_batch_items,
    list_login_batches,
    list_platform_notifications,
    precheck_login_batch,
    refresh_login_item_credential,
    retry_login_batch_items,
    reveal_account_code_source,
)


router = APIRouter()


def _tenant_id(current_user: CurrentUser) -> int:
    if current_user.tenant_id is None:
        raise HTTPException(status_code=400, detail="tenant context required")
    return current_user.tenant_id


def _require_batch_write(current_user: CurrentUser) -> None:
    require_core_feature_access(current_user)
    ensure_permission(current_user, "accounts.batch_login")
    ensure_permission(current_user, "accounts.login")


def _raise_batch_error(exc: BatchLoginError) -> None:
    status = 400
    if exc.code == "account_batch_login_disabled":
        status = 503
    elif exc.code == "not_found":
        status = 404
    elif exc.code in {"preview_stale", "state_conflict", "idempotency_conflict", "code_source_binding_conflict", "soft_deleted_account_conflict"}:
        status = 409
    raise HTTPException(status_code=status, detail={"code": exc.code, "message": str(exc), "line_no": exc.line_no}) from exc


@router.get("/api/tg-accounts/login-batches/capability", response_model=LoginBatchCapabilityOut)
def get_login_batch_capability(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    return batch_login_capability(session)


@router.post("/api/tg-accounts/login-batches/precheck", response_model=LoginBatchPrecheckOut)
def post_login_batch_precheck(
    payload: LoginBatchPrecheckRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_batch_write(current_user)
    try:
        return precheck_login_batch(session, _tenant_id(current_user), current_user.id, payload.lines_text, payload.pool_id)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.post("/api/tg-accounts/login-batches", response_model=LoginBatchOut)
def post_login_batch(
    payload: LoginBatchCreateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_batch_write(current_user)
    try:
        return create_login_batch(session, _tenant_id(current_user), current_user.id, current_user.name, payload)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.get("/api/tg-accounts/login-batches", response_model=list[LoginBatchOut])
def get_login_batches(
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    return list_login_batches(session, _tenant_id(current_user), limit=limit, offset=offset)


@router.get("/api/tg-accounts/login-batches/{batch_id}", response_model=LoginBatchDetailOut)
def get_login_batch_detail(
    batch_id: int,
    item_limit: int = Query(default=100, ge=1, le=100),
    item_offset: int = Query(default=0, ge=0),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    try:
        tenant_id = _tenant_id(current_user)
        batch = LoginBatchOut.model_validate(get_login_batch(session, tenant_id, batch_id)).model_dump()
        items = get_login_batch_items(session, tenant_id, batch_id, limit=item_limit, offset=item_offset)
        return {**batch, "items": items}
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.post("/api/tg-accounts/login-batches/{batch_id}/retry", response_model=LoginBatchOut)
def post_login_batch_retry(
    batch_id: int,
    payload: LoginBatchRetryRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_batch_write(current_user)
    try:
        return retry_login_batch_items(session, _tenant_id(current_user), batch_id, payload, current_user.name)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.post(
    "/api/tg-accounts/login-batches/{batch_id}/items/{item_id}/refresh-credential",
    response_model=LoginBatchItemOut,
)
def post_login_item_refresh_credential(
    batch_id: int,
    item_id: int,
    payload: LoginBatchRefreshCredentialRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_batch_write(current_user)
    try:
        return refresh_login_item_credential(session, _tenant_id(current_user), batch_id, item_id, payload, current_user.name)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.post("/api/tg-accounts/login-batches/{batch_id}/cancel", response_model=LoginBatchOut)
def post_login_batch_cancel(
    batch_id: int,
    payload: LoginBatchCancelRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_batch_write(current_user)
    try:
        return cancel_login_batch(session, _tenant_id(current_user), batch_id, payload.expected_state_version, current_user.name, payload.reason)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.get("/api/tg-accounts/login-batch-notifications", response_model=list[LoginBatchNotificationOut])
def get_login_batch_notifications(
    unacknowledged: bool = True,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    return list_platform_notifications(session, _tenant_id(current_user), current_user.id, unacknowledged=unacknowledged)


@router.post("/api/tg-accounts/login-batch-notifications/{notification_id}/ack", response_model=LoginBatchNotificationOut)
def post_login_batch_notification_ack(
    notification_id: int,
    payload: LoginBatchNotificationAckRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return acknowledge_notification(session, _tenant_id(current_user), current_user.id, notification_id, payload.expected_version)
    except BatchLoginError as exc:
        _raise_batch_error(exc)


@router.post("/api/tg-accounts/{account_id}/code-source-binding/reveal", response_model=CodeSourceBindingRevealOut)
def post_code_source_binding_reveal(
    account_id: int,
    payload: CodeSourceBindingRevealRequest,
    response: Response,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    ensure_permission(current_user, "accounts.code_source_credentials.read")
    try:
        result = reveal_account_code_source(session, _tenant_id(current_user), account_id, payload.expected_binding_version, current_user.name, payload.reason)
    except BatchLoginError as exc:
        _raise_batch_error(exc)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"
    return result


__all__ = ["router"]
