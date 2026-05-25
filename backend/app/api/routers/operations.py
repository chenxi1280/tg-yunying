"""Operation targets, channel messages, operation tasks, and manual records."""
from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user
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
    OperationTargetAdmissionRetryRequest,
    OperationTargetDetailOut,
    OperationTargetMessageSyncOut,
    OperationTargetOut,
    OperationTargetsSyncOut,
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
    retry_operation_target_admission,
    retry_operation_task,
    sync_channel_message_comments,
    sync_operation_target_messages,
    sync_all_operation_targets,
    update_operation_target_account_policy,
    update_operation_target,
)
from app.services.target_learning import (
    clear_learning_profile,
    get_learning_profile_payload,
    list_learning_samples_payload,
    rebuild_learning_profile,
    set_learning_enabled,
    update_learning_sample_status,
)
from app.services.target_learning_versions import list_learning_profile_versions, restore_learning_profile_version

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
        return operation_target_detail(session, current_user.tenant_id or 1, target_id, include_learning_profile=current_user.has_permission("target_learning.view"))
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-targets/{target_id}/sync-messages", response_model=OperationTargetMessageSyncOut)
def post_operation_target_sync_messages(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return sync_operation_target_messages(
            session,
            current_user.tenant_id or 1,
            target_id,
            current_user.name,
            include_learning_profile=current_user.has_permission("target_learning.view"),
        )
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-targets/sync-all", response_model=OperationTargetsSyncOut)
def post_operation_targets_sync_all(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return sync_all_operation_targets(session, current_user.tenant_id or 1, current_user.name)


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


@router.post("/api/operation-targets/{target_id}/admission/retry", response_model=OperationTargetDetailOut)
def post_operation_target_admission_retry(
    target_id: int,
    payload: OperationTargetAdmissionRetryRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return retry_operation_target_admission(session, current_user.tenant_id or 1, target_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/operation-targets/{target_id}/learning-profile")
def get_operation_target_learning_profile(target_id: int, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.view")
    try:
        return get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/operation-targets/{target_id}/learning-samples")
def get_operation_target_learning_samples(
    target_id: int,
    profile_scene: str = "",
    learning_status: str = "",
    reject_reason: str = "",
    downweight_reason: str = "",
    sent_from: str = "",
    sent_to: str = "",
    page: int = 1,
    page_size: int = 50,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, "target_learning.view")
    try:
        filters = {
            "profile_scene": profile_scene,
            "learning_status": learning_status,
            "reject_reason": reject_reason,
            "downweight_reason": downweight_reason,
            "sent_from": sent_from,
            "sent_to": sent_to,
            "page": page,
            "page_size": page_size,
        }
        return list_learning_samples_payload(session, current_user.tenant_id or 1, target_id, filters)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/operation-targets/{target_id}/learning-versions")
def get_operation_target_learning_versions(target_id: int, profile_scene: str = "group_chat", session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.view")
    try:
        return list_learning_profile_versions(session, current_user.tenant_id or 1, target_id, profile_scene)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/operation-targets/{target_id}/learning-versions/{version_id}/restore")
def post_operation_target_learning_version_restore(target_id: int, version_id: str, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.rebuild")
    try:
        restore_learning_profile_version(session, current_user.tenant_id or 1, target_id, version_id, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/operation-targets/{target_id}/learning/rebuild")
def post_operation_target_learning_rebuild(target_id: int, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.rebuild")
    try:
        get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
        rebuild_learning_profile(session, target_id, str(payload.get("profile_scene") or "group_chat"), actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/operation-targets/{target_id}/learning/disable")
def post_operation_target_learning_disable(target_id: int, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.manage")
    return _set_target_learning_enabled(session, current_user, target_id, payload, False)


@router.post("/api/operation-targets/{target_id}/learning/enable")
def post_operation_target_learning_enable(target_id: int, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.manage")
    return _set_target_learning_enabled(session, current_user, target_id, payload, True)


@router.post("/api/operation-targets/{target_id}/learning/clear-profile")
def post_operation_target_learning_clear(target_id: int, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.manage")
    try:
        clear_learning_profile(session, current_user.tenant_id or 1, target_id, str(payload.get("profile_scene") or "group_chat"), actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/operation-targets/{target_id}/learning-samples/{sample_id}")
def patch_operation_target_learning_sample(target_id: int, sample_id: str, payload: dict, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    ensure_permission(current_user, "target_learning.manage")
    try:
        sample = update_learning_sample_status(session, current_user.tenant_id or 1, sample_id, str(payload.get("learning_status") or ""), actor=current_user.name, reason=str(payload.get("reason") or ""))
        if sample.target_id != target_id:
            raise ValueError("学习样本不属于当前目标")
        session.commit()
        return {"id": sample.id, "learning_status": sample.learning_status}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _set_target_learning_enabled(session: Session, current_user: CurrentUser, target_id: int, payload: dict, enabled: bool):
    try:
        set_learning_enabled(session, current_user.tenant_id or 1, target_id, str(payload.get("profile_scene") or "group_chat"), enabled, actor=current_user.name, reason=str(payload.get("reason") or ""))
        session.commit()
        return get_learning_profile_payload(session, current_user.tenant_id or 1, target_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
