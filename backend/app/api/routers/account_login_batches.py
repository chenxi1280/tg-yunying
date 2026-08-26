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
    LoginBatchPostInitializationOut,
    LoginBatchPrecheckOut,
    LoginBatchPrecheckRequest,
    LoginBatchRefreshCredentialRequest,
    LoginBatchRetryRequest,
    PostLoginAbcApproveRequest,
    PostLoginAbcPreviewOut,
    PostLoginAbcPreviewRequest,
    PostLoginAbcRequestOut,
    PostLoginInitializationActionRequest,
    PostLoginTwoFaCandidateRequest,
    PostLoginTwoFaEmailRequest,
)
from app.services.account_login.batches import (
    cancel_login_batch,
    create_login_batch,
    get_login_batch,
    get_login_batch_items,
    list_login_batches,
    refresh_login_item_credential,
    retry_login_batch_items,
)
from app.services.account_login.binding import reveal_account_code_source
from app.services.account_login.contracts import BatchLoginError
from app.services.account_login.notifications import (
    acknowledge_notification,
    list_platform_notifications,
)
from app.services.account_login.preview import (
    batch_login_capability,
    precheck_login_batch,
)
from app.services.account_post_login_init.abc import (
    approve_post_login_abc_request,
    list_post_login_abc_requests,
    preview_post_login_abc_request,
)
from app.services.account_post_login_init.read import (
    post_login_initialization_detail,
    post_login_initialization_out,
)
from app.services.account_post_login_init.reconcile import (
    assume_execution_owner,
    confirm_two_fa_email,
    request_post_login_reconciliation,
    request_two_fa_reset,
    submit_two_fa_candidate,
)


router = APIRouter()
LOGIN_BATCH_DETAIL_ITEM_LIMIT = 200


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
    item_limit: int = Query(default=LOGIN_BATCH_DETAIL_ITEM_LIMIT, ge=1, le=LOGIN_BATCH_DETAIL_ITEM_LIMIT),
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


@router.get(
    "/api/tg-accounts/login-batches/{batch_id}/items/{item_id}/post-initialization",
    response_model=LoginBatchPostInitializationOut,
)
def get_login_item_post_initialization(
    batch_id: int,
    item_id: int,
    *,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    try:
        return post_login_initialization_detail(
            session,
            _tenant_id(current_user),
            batch_id=batch_id,
            item_id=item_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/tg-accounts/post-login-initializations/{initialization_id}/reconcile",
    response_model=LoginBatchPostInitializationOut,
)
def post_post_login_initialization_reconcile(
    initialization_id: int,
    payload: PostLoginInitializationActionRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "system.manage")
    return _post_init_action(
        session,
        current_user,
        action=request_post_login_reconciliation,
        initialization_id=initialization_id,
        payload=payload,
    )


@router.post(
    "/api/tg-accounts/post-login-initializations/{initialization_id}/two-fa-current-candidate",
    response_model=LoginBatchPostInitializationOut,
)
def post_post_login_two_fa_candidate(
    initialization_id: int,
    payload: PostLoginTwoFaCandidateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.security.credential_manage")
    return _post_init_action(
        session,
        current_user,
        action=submit_two_fa_candidate,
        initialization_id=initialization_id,
        payload=payload,
        candidate_password=payload.candidate_password,
    )


@router.post(
    "/api/tg-accounts/post-login-initializations/{initialization_id}/two-fa-reset",
    response_model=LoginBatchPostInitializationOut,
)
def post_post_login_two_fa_reset(
    initialization_id: int,
    payload: PostLoginInitializationActionRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.security.credential_manage")
    return _post_init_action(
        session,
        current_user,
        action=request_two_fa_reset,
        initialization_id=initialization_id,
        payload=payload,
    )


@router.post(
    "/api/tg-accounts/post-login-initializations/{initialization_id}/two-fa-email-confirmation",
    response_model=LoginBatchPostInitializationOut,
)
def post_post_login_two_fa_email(
    initialization_id: int,
    payload: PostLoginTwoFaEmailRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.security.credential_manage")
    return _post_init_action(
        session,
        current_user,
        action=confirm_two_fa_email,
        initialization_id=initialization_id,
        payload=payload,
        confirmation_code=payload.confirmation_code,
    )


@router.post(
    "/api/tg-accounts/post-login-initializations/{initialization_id}/assume-execution-owner",
    response_model=LoginBatchPostInitializationOut,
)
def post_post_login_assume_owner(
    initialization_id: int,
    payload: PostLoginInitializationActionRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "system.manage")
    return _post_init_action(
        session,
        current_user,
        action=assume_execution_owner,
        initialization_id=initialization_id,
        payload=payload,
    )


def _post_init_action(
    session,
    current_user,
    *,
    action,
    initialization_id,
    payload,
    **extra,
):
    try:
        owner = action(
            session,
            _tenant_id(current_user),
            initialization_id,
            expected_version=payload.expected_version,
            actor=current_user.name,
            reason=payload.reason,
            **extra,
        )
        return post_login_initialization_out(session, owner)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


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


@router.get(
    "/api/tg-accounts/post-login-abc-requests",
    response_model=list[PostLoginAbcRequestOut],
)
def get_post_login_abc_requests(
    limit: int = Query(default=100, ge=1, le=200),
    batch_id: int | None = Query(default=None, ge=1),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "accounts.view")
    return list_post_login_abc_requests(
        session,
        _tenant_id(current_user),
        limit=limit,
        batch_id=batch_id,
    )


@router.post(
    "/api/tg-accounts/post-login-abc-requests/{request_id}/preview",
    response_model=PostLoginAbcPreviewOut,
)
def post_post_login_abc_preview(
    request_id: int,
    payload: PostLoginAbcPreviewRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "system.manage")
    try:
        return preview_post_login_abc_request(
            session,
            _tenant_id(current_user),
            request_id,
            deployed_release_sha=payload.deployed_release_sha,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/api/tg-accounts/post-login-abc-requests/{request_id}/approve",
    response_model=PostLoginAbcRequestOut,
)
def post_post_login_abc_approve(
    request_id: int,
    payload: PostLoginAbcApproveRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "system.manage")
    try:
        return approve_post_login_abc_request(
            session,
            _tenant_id(current_user),
            request_id,
            expected_version=payload.expected_version,
            deployed_release_sha=payload.deployed_release_sha,
            expected_fingerprint=payload.expected_fingerprint,
            approved_by=current_user.name,
            approval_ref=payload.approval_ref,
        )
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=409, detail=str(exc)) from exc


__all__ = ["router"]
