"""Account pool routes."""
from __future__ import annotations


from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import not_found
from app.api.response_permissions import account_pool_detail_out_for_user
from app.models import AccountPool
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AccountPoolCreate, AccountPoolDetailOut, AccountPoolOut, AccountPoolUpdate,
    ContactOut, DirectMessageTaskCreate, MessageTaskOut,
)
from app.services import (
    account_pool_contacts, account_pool_detail, account_pool_snapshot, create_account_pool,
    create_pool_direct_message_task, ensure_code_receiver_account_pool, ensure_rank_deboost_account_pool,
    list_account_pools, update_account_pool,
)

router = APIRouter()


@router.get("/api/account-pools", response_model=list[AccountPoolOut])
def get_account_pools(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    return list_account_pools(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/account-pools", response_model=AccountPoolOut)
def post_account_pool(
    payload: AccountPoolCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        return create_account_pool(session, payload.model_copy(update={"tenant_id": tenant_id}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/account-pools/code-receiver", response_model=AccountPoolOut)
def post_code_receiver_account_pool(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_core_feature_access(current_user)
    pool = ensure_code_receiver_account_pool(session, resolve_tenant_id(current_user, tenant_id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


@router.post("/api/account-pools/rank-deboost", response_model=AccountPoolOut)
def post_rank_deboost_account_pool(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_core_feature_access(current_user)
    pool = ensure_rank_deboost_account_pool(session, resolve_tenant_id(current_user, tenant_id))
    session.commit()
    session.refresh(pool)
    return account_pool_snapshot(session, pool)


@router.patch("/api/account-pools/{pool_id}", response_model=AccountPoolOut)
def patch_account_pool(
    pool_id: int,
    payload: AccountPoolUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, AccountPool, pool_id)
    try:
        return update_account_pool(session, pool_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/account-pools/{pool_id}/detail", response_model=AccountPoolDetailOut)
def get_account_pool_detail(
    pool_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> dict:
    require_resource_tenant(session, current_user, AccountPool, pool_id)
    try:
        return account_pool_detail_out_for_user(account_pool_detail(session, pool_id), current_user)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/account-pools/{pool_id}/contacts", response_model=list[ContactOut])
def get_account_pool_contacts(
    pool_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list:
    require_resource_tenant(session, current_user, AccountPool, pool_id)
    try:
        return account_pool_contacts(session, pool_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/account-pools/{pool_id}/direct-message-tasks", response_model=MessageTaskOut)
def post_account_pool_direct_message_task(
    pool_id: int,
    payload: DirectMessageTaskCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, AccountPool, pool_id)
    try:
        return create_pool_direct_message_task(session, pool_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
