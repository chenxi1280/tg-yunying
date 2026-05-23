from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.common.http import not_found
from app.database import get_session
from app.schemas import (
    OperationPlanApplyOut,
    OperationPlanCreate,
    OperationPlanGenerateOut,
    OperationPlanGenerateRequest,
    OperationPlanGenerationRunOut,
    OperationPlanOut,
    OperationPlanPreviewOut,
    OperationPlanUpdate,
)
from app.services import (
    apply_operation_plan_to_linked_tasks,
    archive_operation_plan,
    copy_operation_plan,
    create_operation_plan,
    generate_operation_plan_tasks,
    get_operation_plan,
    list_operation_plan_runs,
    list_operation_plans,
    pause_operation_plan,
    preview_operation_plan,
    resume_operation_plan,
    update_operation_plan,
)


router = APIRouter()


@router.get("/api/operation-plans", response_model=list[OperationPlanOut])
def get_operation_plans(session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return list_operation_plans(session, current_user.tenant_id or 1)


@router.post("/api/operation-plans", response_model=OperationPlanOut)
def post_operation_plan(payload: OperationPlanCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_operation_plan(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/operation-plans/{plan_id}", response_model=OperationPlanOut)
def get_operation_plan_detail(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return get_operation_plan(session, current_user.tenant_id or 1, plan_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/operation-plans/{plan_id}", response_model=OperationPlanOut)
def patch_operation_plan(plan_id: int, payload: OperationPlanUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_operation_plan(session, current_user.tenant_id or 1, plan_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/generate-preview", response_model=OperationPlanPreviewOut)
def post_operation_plan_generate_preview(plan_id: int, payload: OperationPlanGenerateRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return preview_operation_plan(session, current_user.tenant_id or 1, plan_id, payload or OperationPlanGenerateRequest(), current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/generate-tasks", response_model=OperationPlanGenerateOut)
def post_operation_plan_generate_tasks(plan_id: int, payload: OperationPlanGenerateRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return generate_operation_plan_tasks(session, current_user.tenant_id or 1, plan_id, payload or OperationPlanGenerateRequest(), current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/apply-to-linked-tasks", response_model=OperationPlanApplyOut)
def post_operation_plan_apply(plan_id: int, payload: OperationPlanGenerateRequest | None = None, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return apply_operation_plan_to_linked_tasks(session, current_user.tenant_id or 1, plan_id, payload or OperationPlanGenerateRequest(), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/pause", response_model=OperationPlanOut)
def post_operation_plan_pause(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return pause_operation_plan(session, current_user.tenant_id or 1, plan_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/resume", response_model=OperationPlanOut)
def post_operation_plan_resume(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return resume_operation_plan(session, current_user.tenant_id or 1, plan_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/copy", response_model=OperationPlanOut)
def post_operation_plan_copy(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return copy_operation_plan(session, current_user.tenant_id or 1, plan_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-plans/{plan_id}/archive", response_model=OperationPlanOut)
def post_operation_plan_archive(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return archive_operation_plan(session, current_user.tenant_id or 1, plan_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/operation-plans/{plan_id}/runs", response_model=list[OperationPlanGenerationRunOut])
def get_operation_plan_runs(plan_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return list_operation_plan_runs(session, current_user.tenant_id or 1, plan_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


__all__ = ["router"]
