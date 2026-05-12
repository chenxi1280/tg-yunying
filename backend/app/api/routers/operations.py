"""Operation targets, channel messages, operation tasks, and manual records."""
from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.config import get_settings
from app.database import get_session
from app.common.http import not_found
from app.models import ChannelMessage, ChannelMessageComment, ManualOperationRecord, OperationTarget, OperationTask, OperationTaskAttempt
from app.schemas import (
    ChannelMessageCreate,
    ChannelMessageCommentOut,
    ChannelMessageCommentSyncOut,
    ChannelMessageOut,
    ManualOperationRecordOut,
    OperationTargetCreate,
    OperationTargetAccountUpdate,
    OperationTargetDetailOut,
    OperationTargetMessageSyncOut,
    OperationTargetOut,
    OperationTargetUpdate,
    OperationTaskAttemptOut,
    OperationTaskCreate,
    OperationTaskOut,
)
from app.services import (
    cancel_operation_task,
    create_channel_message,
    create_operation_target,
    create_operation_task,
    dispatch_operation_task,
    filter_channel_message_comments,
    filter_channel_messages,
    filter_operation_targets,
    filter_operation_tasks,
    list_manual_operations,
    list_operation_attempts,
    operation_target_detail,
    retry_operation_task,
    sync_channel_message_comments,
    sync_operation_target_messages,
    update_operation_target_account_policy,
    update_operation_target,
)

router = APIRouter()
legacy_operation_task_router = APIRouter()


@router.get("/api/operation-targets", response_model=list[OperationTargetOut])
def get_operation_targets(
    target_type: str | None = None,
    account_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[OperationTarget]:
    try:
        return filter_operation_targets(session, current_user.tenant_id or 1, target_type, account_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/operation-targets/{target_id}/detail", response_model=OperationTargetDetailOut)
def get_operation_target_detail(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return operation_target_detail(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-targets/{target_id}/sync-messages", response_model=OperationTargetMessageSyncOut)
def post_operation_target_sync_messages(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return sync_operation_target_messages(session, current_user.tenant_id or 1, target_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-targets", response_model=OperationTargetOut)
def post_operation_target(
    payload: OperationTargetCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTarget:
    try:
        return create_operation_target(session, payload.model_copy(update={"tenant_id": current_user.tenant_id or 1}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/operation-targets/{target_id}", response_model=OperationTargetOut)
def patch_operation_target(
    target_id: int,
    payload: OperationTargetUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTarget:
    try:
        return update_operation_target(session, current_user.tenant_id or 1, target_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/operation-targets/{target_id}/accounts/{account_id}", response_model=OperationTargetDetailOut)
def patch_operation_target_account_policy(
    target_id: int,
    account_id: int,
    payload: OperationTargetAccountUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return update_operation_target_account_policy(session, current_user.tenant_id or 1, target_id, account_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/channel-messages", response_model=list[ChannelMessageOut])
def get_channel_messages(
    channel_target_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[ChannelMessage]:
    return filter_channel_messages(session, current_user.tenant_id or 1, channel_target_id)


@router.post("/api/channel-messages", response_model=ChannelMessageOut)
def post_channel_message(
    payload: ChannelMessageCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ChannelMessage:
    try:
        return create_channel_message(session, payload.model_copy(update={"tenant_id": current_user.tenant_id or 1}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/channel-comments", response_model=list[ChannelMessageCommentOut])
def get_channel_comments(
    channel_target_id: int | None = None,
    channel_message_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[ChannelMessageComment]:
    return filter_channel_message_comments(session, current_user.tenant_id or 1, channel_target_id, channel_message_id)


@router.post("/api/channel-messages/{channel_message_id}/sync-comments", response_model=ChannelMessageCommentSyncOut)
def post_channel_message_sync_comments(
    channel_message_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return sync_channel_message_comments(session, current_user.tenant_id or 1, channel_message_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@legacy_operation_task_router.get("/api/operation-tasks", response_model=list[OperationTaskOut])
def get_operation_tasks(
    status: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[OperationTask]:
    return filter_operation_tasks(session, current_user.tenant_id or 1, status)


@legacy_operation_task_router.post("/api/operation-tasks", response_model=OperationTaskOut)
def post_operation_task(
    payload: OperationTaskCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTask:
    try:
        return create_operation_task(session, payload.model_copy(update={"tenant_id": current_user.tenant_id or 1}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_operation_task_router.post("/api/operation-tasks/{task_id}/dispatch", response_model=OperationTaskOut)
def post_operation_task_dispatch(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTask:
    try:
        return dispatch_operation_task(session, task_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_operation_task_router.post("/api/operation-tasks/{task_id}/retry", response_model=OperationTaskOut)
def post_operation_task_retry(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTask:
    try:
        return retry_operation_task(session, task_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_operation_task_router.post("/api/operation-tasks/{task_id}/cancel", response_model=OperationTaskOut)
def post_operation_task_cancel(
    task_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTask:
    try:
        return cancel_operation_task(session, task_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_operation_task_router.get("/api/operation-task-attempts", response_model=list[OperationTaskAttemptOut])
def get_operation_task_attempts(
    task_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[OperationTaskAttempt]:
    return list_operation_attempts(session, current_user.tenant_id or 1, task_id)


@router.get("/api/manual-operation-records", response_model=list[ManualOperationRecordOut])
def get_manual_operation_records(
    account_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[ManualOperationRecord]:
    return list_manual_operations(session, current_user.tenant_id or 1, account_id)


if get_settings().enable_legacy_operation_task_routes:
    router.include_router(legacy_operation_task_router)
