"""Operation targets, channel messages, operation tasks, and manual records."""
from __future__ import annotations

from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy.orm import Session

from app.api.pagination import pagination_headers
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
    OperationTargetInviteLinkExportOut,
    OperationTargetLifecycleImpactOut,
    OperationTargetLifecycleResultOut,
    OperationTargetLifecycleUpdate,
    OperationTargetMessageSyncOut,
    OperationTargetOut,
    OperationTargetReactivateRequest,
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
    export_operation_target_invite_link,
    filter_channel_message_comments,
    filter_channel_messages,
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
from app.services.operation_target_list import OperationTargetListQuery, list_operation_targets_page


router = APIRouter()
legacy_operation_task_router = APIRouter()
BOUNDED_TARGET_QUERY_PARAMS = frozenset({"page", "page_size", "q", "ids", "linked_group_id", "capability"})
DEFAULT_TARGET_PAGE = 1
DEFAULT_TARGET_PAGE_SIZE = 20
MAX_SELECTED_TARGET_IDS = 100


def _target_paging(request: Request, page: int | None, page_size: int | None) -> tuple[int | None, int | None]:
    bounded = any(name in request.query_params for name in BOUNDED_TARGET_QUERY_PARAMS)
    if not bounded:
        return None, None
    return page or DEFAULT_TARGET_PAGE, page_size or DEFAULT_TARGET_PAGE_SIZE


def _selected_target_ids(ids: list[int] | None) -> tuple[int, ...]:
    values = tuple(ids or ())
    if len(values) > MAX_SELECTED_TARGET_IDS:
        raise HTTPException(status_code=422, detail=f"ids must contain at most {MAX_SELECTED_TARGET_IDS} values")
    if any(value < 1 for value in values):
        raise HTTPException(status_code=422, detail="ids must contain positive integers")
    return values


@router.get("/api/operation-targets", response_model=list[OperationTargetOut])
def get_operation_targets(
    request: Request,
    response: Response,
    target_type: str | None = None,
    account_id: int | None = None,
    page: int | None = Query(default=None, ge=1),
    page_size: int | None = Query(default=None, ge=1, le=100),
    q: str = Query(default="", max_length=120),
    ids: list[int] | None = Query(default=None),
    linked_group_id: int | None = Query(default=None, ge=1),
    capability: str | None = Query(default=None, pattern="^(send|listen|archive|task)$"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> list[dict]:
    normalized_page, normalized_page_size = _target_paging(request, page, page_size)
    try:
        rows, total = list_operation_targets_page(
            session,
            OperationTargetListQuery(
                tenant_id=current_user.tenant_id or 1,
                page=normalized_page,
                page_size=normalized_page_size,
                target_type=target_type,
                account_id=account_id,
                q=q,
                ids=_selected_target_ids(ids),
                linked_group_id=linked_group_id,
                capability=capability,
            ),
        )
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    if normalized_page is not None and normalized_page_size is not None:
        response.headers.update(
            pagination_headers(total_count=total, page=normalized_page, page_size=normalized_page_size)
        )
    return rows


@router.get("/api/operation-targets/{target_id}/detail", response_model=OperationTargetDetailOut)
def get_operation_target_detail(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return operation_target_detail(session, current_user.tenant_id or 1, target_id, include_learning_profile=current_user.has_permission("target_profile.view"))
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
            include_learning_profile=current_user.has_permission("target_profile.view"),
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


@router.post(
    "/api/operation-targets/{target_id}/lifecycle-impact-preview",
    response_model=OperationTargetLifecycleImpactOut,
)
def post_operation_target_lifecycle_impact_preview(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTargetLifecycleImpactOut:
    ensure_permission(current_user, "targets.manage")
    from app.services.task_center.target_lifecycle import preview_target_group_dissolution

    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != (current_user.tenant_id or 1):
        raise not_found("target not found")
    impact = preview_target_group_dissolution(session, target=target)
    return OperationTargetLifecycleImpactOut(
        unstarted_action_count=impact.unstarted_action_count,
        unknown_action_count=impact.unknown_action_count,
        unstarted_message_task_count=impact.unstarted_message_task_count,
        unknown_message_task_count=impact.unknown_message_task_count,
        unstarted_operation_task_count=impact.unstarted_operation_task_count,
        unknown_operation_task_count=impact.unknown_operation_task_count,
        coverage_count=impact.coverage_count,
        single_target_task_count=impact.single_target_task_count,
    )


@router.patch(
    "/api/operation-targets/{target_id}/lifecycle",
    response_model=OperationTargetLifecycleResultOut,
)
def patch_operation_target_lifecycle(
    target_id: int,
    payload: OperationTargetLifecycleUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTargetLifecycleResultOut:
    ensure_permission(current_user, "targets.manage")
    from app.services.task_center.target_lifecycle import (
        mark_target_group_dissolved,
        mark_target_ref_invalid,
    )

    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != (current_user.tenant_id or 1):
        raise not_found("target not found")
    try:
        if payload.lifecycle_status == "group_dissolved":
            result = mark_target_group_dissolved(
                session,
                target=target,
                actor=current_user.name,
                reason=payload.reason,
                evidence_ref=payload.evidence_ref,
                expected_version=payload.expected_lifecycle_version,
            )
        else:
            result = mark_target_ref_invalid(
                session,
                target=target,
                actor=current_user.name,
                reason=payload.reason,
                evidence_ref=payload.evidence_ref,
                expected_version=payload.expected_lifecycle_version,
            )
        session.commit()
    except LookupError as exc:
        raise HTTPException(status_code=409, detail="lifecycle_version_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return OperationTargetLifecycleResultOut(
        target_id=result.target.id,
        lifecycle_status=result.target.lifecycle_status,
        reference_revision=int(result.target.reference_revision or 1),
        lifecycle_version=int(result.target.lifecycle_version or 1),
        evidence_ref=result.target.lifecycle_detail,
        skipped_actions=result.skipped_actions,
        skipped_message_tasks=result.skipped_message_tasks,
        skipped_operation_tasks=result.skipped_operation_tasks,
        blocked_coverage=result.blocked_coverage,
        paused_tasks=result.paused_tasks,
    )


@router.post("/api/operation-targets/{target_id}/reactivate", response_model=OperationTargetOut)
def post_operation_target_reactivate(
    target_id: int,
    payload: OperationTargetReactivateRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> OperationTarget:
    ensure_permission(current_user, "targets.manage")
    from app.services.task_center.target_lifecycle import reactivate_target

    target = session.get(OperationTarget, target_id)
    if not target or target.tenant_id != (current_user.tenant_id or 1):
        raise not_found("target not found")
    try:
        target = reactivate_target(
            session,
            target=target,
            actor=current_user.name,
            reason=payload.reason,
            evidence_ref=payload.evidence_ref,
            expected_version=payload.expected_lifecycle_version,
            new_peer_id=payload.tg_peer_id,
            new_username=payload.username,
        )
        session.commit()
        session.refresh(target)
        return target
    except LookupError as exc:
        raise HTTPException(status_code=409, detail="lifecycle_version_conflict") from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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


@router.post("/api/operation-targets/{target_id}/invite-link/export", response_model=OperationTargetInviteLinkExportOut)
def post_operation_target_invite_link_export(
    target_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return export_operation_target_invite_link(session, current_user.tenant_id or 1, target_id, current_user.name)
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
