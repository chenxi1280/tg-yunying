from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.common.http import not_found
from app.database import SessionLocal, get_session
from app.models import MessageTask
from app.repositories.tenant import require_resource_tenant
from app.schemas import ApproveDraftRequest, MessageSendBatchCreate, MessageSendTaskCreate, MessageTaskOut, RetryTaskRequest
from app.schemas.risk_control import RiskPreflightOut
from app.services import (
    cancel_message_task,
    create_message_send_task,
    create_message_send_tasks_batch,
    dispatch_task,
    filter_tasks,
    get_message_task,
    precheck_message_task,
    retry_task,
)

router = APIRouter()


@router.get("/api/message-send-tasks", response_model=list[MessageTaskOut])
@router.get("/api/message-tasks", response_model=list[MessageTaskOut])
def list_message_tasks(
    tenant_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[MessageTask]:
    try:
        return filter_tasks(session, resolve_tenant_id(current_user, tenant_id), page, page_size, search, status)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/message-send-tasks/{task_id}", response_model=MessageTaskOut)
def get_message_send_task(
    task_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> MessageTask:
    try:
        return get_message_task(session, resolve_tenant_id(current_user, tenant_id), task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/message-send-tasks/{task_id}/precheck", response_model=RiskPreflightOut)
def post_message_send_task_precheck(
    task_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        return precheck_message_task(session, resolve_tenant_id(current_user, tenant_id), task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/message-send-tasks", response_model=MessageTaskOut)
def post_message_send_task(
    payload: MessageSendTaskCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        task = create_message_send_task(session, payload, current_user.name, resolve_tenant_id(current_user, None))
        if payload.dispatch_now and task.planned_delay_seconds == 0:
            return dispatch_task(SessionLocal, task.id)
        return task
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/message-send-tasks/batch", response_model=list[MessageTaskOut])
def post_message_send_tasks_batch(
    payload: MessageSendBatchCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        return create_message_send_tasks_batch(session, payload, current_user.name, resolve_tenant_id(current_user, None))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/message-send-tasks/{task_id}/dispatch", response_model=MessageTaskOut)
@router.post("/api/message-tasks/{task_id}/dispatch", response_model=MessageTaskOut)
def post_dispatch_task(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, MessageTask, task_id)
        return dispatch_task(SessionLocal, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/message-send-tasks/{task_id}/retry", response_model=MessageTaskOut)
@router.post("/api/message-tasks/{task_id}/retry", response_model=MessageTaskOut)
def post_retry_task(
    task_id: int,
    payload: RetryTaskRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, MessageTask, task_id)
        return retry_task(SessionLocal, task_id, payload.actor, payload.dispatch_now)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/message-send-tasks/{task_id}/cancel", response_model=MessageTaskOut)
@router.post("/api/message-tasks/{task_id}/cancel", response_model=MessageTaskOut)
def post_cancel_task(
    task_id: int,
    payload: ApproveDraftRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, MessageTask, task_id)
        return cancel_message_task(session, task_id, payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
