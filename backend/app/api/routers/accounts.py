"""TG account, login, profile, contacts, and account clone routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import not_found
from app.models import (
    AccountCloneItem, AccountClonePlan, AccountPool, TgAccount, VerificationTask,
)
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AccountCloneItemOut, AccountClonePlanCreate, AccountClonePlanOut,
    AccountDetailOut, AccountGroupOut, AccountOut, AccountSyncRecordOut,
    AvatarUploadOut, ContactOut, DirectMessageTaskCreate, GroupOut,
    LoginFlowOut, LoginStartRequest, LoginVerifyRequest, MessageTaskOut,
    MoveAccountPoolRequest, ProfileSyncRecordOut, TgAccountCreate,
    TgAccountProfileUpdate, VerificationCodeOut, VerificationTaskOut,
)
from app.services import (
    account_clone_plan_detail, account_clone_plans, account_contacts,
    account_detail, account_groups, account_message_records,
    check_qr_login, confirm_account_clone_plan, create_account,
    create_account_clone_plan, create_direct_message_task,
    filter_accounts, health_check_account, list_account_sync_records,
    list_login_flows, list_profile_sync_records, list_verification_codes,
    list_verification_tasks, move_account_pool,
    poll_account_verification_codes, queue_account_sync_now,
    retry_account_clone_item, retry_account_profile_sync,
    start_login, sync_account_contacts, sync_groups,
    update_account_profile, upload_account_avatar, verify_login,
)

router = APIRouter()


# ── Account listing / CRUD ──

@router.get("/api/tg-accounts", response_model=list[AccountOut])
def list_accounts(
    tenant_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    pool_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[TgAccount]:
    try:
        return filter_accounts(session, resolve_tenant_id(current_user, tenant_id), page, page_size, search, status, pool_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts", response_model=AccountOut)
def post_account(
    payload: TgAccountCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> TgAccount:
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        return create_account(session, payload.model_copy(update={"tenant_id": tenant_id}))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/move-pool", response_model=AccountOut)
def post_account_move_pool(
    account_id: int,
    payload: MoveAccountPoolRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> TgAccount:
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, TgAccount, account_id)
    require_resource_tenant(session, current_user, AccountPool, payload.pool_id)
    try:
        return move_account_pool(session, account_id, payload.pool_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Login ──

@router.post("/api/tg-accounts/{account_id}/login/start", response_model=LoginFlowOut)
def post_login_start(
    account_id: int,
    payload: LoginStartRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return start_login(session, account_id, payload.method, current_user.name, payload.force)
    except ValueError as exc:
        if "already online" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/login/verify", response_model=AccountOut)
def post_login_verify(
    account_id: int,
    payload: LoginVerifyRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return verify_login(session, account_id, payload.code, payload.password_2fa)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/login/qr/check", response_model=AccountOut)
def post_qr_check(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return check_qr_login(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/login-flows", response_model=list[LoginFlowOut])
def get_login_flows(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return list_login_flows(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Health / Sync ──

@router.post("/api/tg-accounts/{account_id}/health-check", response_model=AccountOut)
def post_account_health_check(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return health_check_account(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/sync-groups", response_model=list[GroupOut])
def post_sync_groups(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return sync_groups(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/sync-records", response_model=list[AccountSyncRecordOut])
def get_account_sync_records(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return list_account_sync_records(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/sync-now", response_model=list[AccountSyncRecordOut])
def post_account_sync_now(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return queue_account_sync_now(session, account_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── Detail / Profile ──

@router.get("/api/tg-accounts/{account_id}/detail", response_model=AccountDetailOut)
def get_account_detail(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return account_detail(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/avatar", response_model=AvatarUploadOut)
async def post_account_avatar(
    account_id: int,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        data = await file.read()
        return upload_account_avatar(session, account_id, file.filename or "avatar", file.content_type or "", data, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tg-accounts/{account_id}/profile", response_model=AccountOut)
def patch_account_profile(
    account_id: int,
    payload: TgAccountProfileUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return update_account_profile(session, account_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/profile-sync/retry", response_model=ProfileSyncRecordOut)
def post_account_profile_sync_retry(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return retry_account_profile_sync(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/profile-sync-records", response_model=list[ProfileSyncRecordOut])
def get_account_profile_sync_records(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return list_profile_sync_records(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Groups / Contacts / Messages ──

@router.get("/api/tg-accounts/{account_id}/groups", response_model=list[AccountGroupOut])
def get_account_groups(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return account_groups(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/contacts", response_model=list[ContactOut])
def get_account_contacts(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return account_contacts(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/contacts/sync", response_model=list[ContactOut])
def post_account_contacts_sync(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return sync_account_contacts(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/message-records", response_model=list[MessageTaskOut])
def get_account_message_records(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return account_message_records(session, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Verification codes ──

@router.get("/api/tg-accounts/{account_id}/verification-codes", response_model=list[VerificationCodeOut])
def get_account_verification_codes(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return list_verification_codes(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/verification-codes/poll", response_model=list[VerificationCodeOut])
def post_account_verification_codes_poll(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return poll_account_verification_codes(session, account_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tg-accounts/{account_id}/direct-message-tasks", response_model=MessageTaskOut)
def post_account_direct_message_task(
    account_id: int,
    payload: DirectMessageTaskCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgAccount, account_id)
        return create_direct_message_task(session, account_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tg-accounts/{account_id}/verification-tasks", response_model=list[VerificationTaskOut])
def get_account_verification_tasks(
    account_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_resource_tenant(session, current_user, TgAccount, account_id)
    account = session.get(TgAccount, account_id)
    return list_verification_tasks(session, account.tenant_id, account_id=account.id)


# ── Account Clone Plans ──

@router.get("/api/account-clone-plans", response_model=list[AccountClonePlanOut])
def get_account_clone_plans(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return account_clone_plans(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/account-clone-plans", response_model=AccountClonePlanOut)
def post_account_clone_plan(
    payload: AccountClonePlanCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    require_resource_tenant(session, current_user, TgAccount, payload.source_account_id)
    target_ids = [*payload.target_account_ids, *([payload.target_account_id] if payload.target_account_id else [])]
    if not target_ids:
        raise HTTPException(status_code=400, detail="target account required")
    for target_id in set(target_ids):
        require_resource_tenant(session, current_user, TgAccount, target_id)
    try:
        return create_account_clone_plan(session, payload.model_copy(update={"tenant_id": tenant_id}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/account-clone-plans/{plan_id}", response_model=AccountClonePlanOut)
def get_account_clone_plan(
    plan_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_resource_tenant(session, current_user, AccountClonePlan, plan_id)
    try:
        return account_clone_plan_detail(session, plan_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/account-clone-plans/{plan_id}/confirm", response_model=AccountClonePlanOut)
def post_account_clone_plan_confirm(
    plan_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, AccountClonePlan, plan_id)
    try:
        return confirm_account_clone_plan(session, plan_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/account-clone-items/{item_id}/retry", response_model=AccountCloneItemOut)
def post_account_clone_item_retry(
    item_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, AccountCloneItem, item_id)
    try:
        return retry_account_clone_item(session, item_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
