"""Group and verification task routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import not_found
from app.models import TgAccount, TgGroup, VerificationTask
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AuthorizeGroupRequest, GroupDetailOut, GroupOut, GroupPolicyUpdate,
    VerificationChallengeContextOut, VerificationTaskBatchResolveOut, VerificationTaskConfirmRequest,
    VerificationTaskOut, VerificationTaskResponseRequest,
)
from app.services import (
    authorize_group, confirm_verification_task, dismiss_verification_task,
    filter_groups, get_verification_challenge_context, group_detail, list_verification_tasks,
    refresh_verification_challenge_context, resolve_group_restriction_batch,
    resolve_group_restriction_task, submit_verification_response,
    update_group_policy,
)

router = APIRouter()


# ── Groups ──

@router.get("/api/groups", response_model=list[GroupOut])
def list_groups(
    tenant_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[TgGroup]:
    try:
        return filter_groups(session, resolve_tenant_id(current_user, tenant_id), page, page_size, search, status)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/groups/{group_id}", response_model=GroupOut)
def patch_group(
    group_id: int,
    payload: GroupPolicyUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgGroup, group_id)
        return update_group_policy(session, group_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/groups/{group_id}/detail", response_model=GroupDetailOut)
def get_group_detail(
    group_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, TgGroup, group_id)
        return group_detail(session, group_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/groups/{group_id}/authorize", response_model=GroupOut)
def post_group_authorize(
    group_id: int,
    payload: AuthorizeGroupRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, TgGroup, group_id)
        return authorize_group(session, group_id, payload.auth_status, payload.actor)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/groups/{group_id}/verification-tasks", response_model=list[VerificationTaskOut])
def get_group_verification_tasks(
    group_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_resource_tenant(session, current_user, TgGroup, group_id)
    group = session.get(TgGroup, group_id)
    return list_verification_tasks(session, group.tenant_id, group_id=group.id)


# ── Verification Tasks ──

@router.get("/api/verification-tasks", response_model=list[VerificationTaskOut])
def get_verification_tasks(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_verification_tasks(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/verification-tasks/{task_id}/confirm-action", response_model=VerificationTaskOut)
def post_verification_task_confirm(
    task_id: int,
    payload: VerificationTaskConfirmRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return confirm_verification_task(session, task_id, payload.actor if payload else current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/verification-tasks/{task_id}/resolve-group-restriction", response_model=VerificationTaskOut)
def post_verification_task_resolve_group_restriction(
    task_id: int,
    payload: VerificationTaskConfirmRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return resolve_group_restriction_task(session, task_id, payload.actor if payload else current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/verification-tasks/{task_id}/resolve-group-restriction-batch", response_model=VerificationTaskBatchResolveOut)
def post_verification_task_resolve_group_restriction_batch(
    task_id: int,
    payload: VerificationTaskConfirmRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return resolve_group_restriction_batch(session, task_id, payload.actor if payload else current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/verification-tasks/{task_id}/challenge-context", response_model=VerificationChallengeContextOut)
def get_verification_task_challenge_context(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return get_verification_challenge_context(session, task_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/verification-tasks/{task_id}/refresh-challenge-context", response_model=VerificationChallengeContextOut)
def post_verification_task_refresh_challenge_context(
    task_id: int,
    payload: VerificationTaskConfirmRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return refresh_verification_challenge_context(session, task_id, payload.actor if payload else current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.post("/api/verification-tasks/{task_id}/submit-response", response_model=VerificationTaskOut)
def post_verification_task_response(
    task_id: int,
    payload: VerificationTaskResponseRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return submit_verification_response(session, task_id, payload.response_text, payload.actor or current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/verification-tasks/{task_id}/dismiss", response_model=VerificationTaskOut)
def post_verification_task_dismiss(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    require_resource_tenant(session, current_user, VerificationTask, task_id)
    try:
        return dismiss_verification_task(session, task_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
