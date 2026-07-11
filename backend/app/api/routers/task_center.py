from __future__ import annotations

import csv
from datetime import date
from io import StringIO

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user
from app.config import get_settings
from app.common.http import not_found
from app.database import get_session
from app.timezone import beijing_now
from app.schemas import (
    ActionOut,
    ChannelCapacityCheckOut,
    ChannelCapacityCheckRequest,
    ChannelCommentTaskConfigUpdate,
    ChannelCommentTaskCreate,
    ChannelCommentTaskPreviewRequest,
    ChannelLikeTaskConfigUpdate,
    ChannelLikeTaskCreate,
    ChannelViewTaskConfigUpdate,
    ChannelViewTaskCreate,
    ExecutionAttemptOut,
    GenerateTaskPreviewOut,
    GroupAIChatTaskConfigUpdate,
    GroupAIChatTaskPreviewRequest,
    GroupAIChatTaskCreate,
    GroupMembershipAdmissionTaskCreate,
    GroupRelayTaskConfigUpdate,
    GroupRelayTaskCreate,
    RecommendTaskAccountsRequest,
    RecommendedTaskAccountOut,
    ReviewApproveRequest,
    ReviewQueueOut,
    ReviewRejectRequest,
    SearchJoinGroupTaskConfigUpdate,
    SearchJoinGroupTaskCreate,
    SearchRankDeboostExemptGroupResponse,
    SearchRankDeboostTaskConfigUpdate,
    SearchRankDeboostTaskCreate,
    TaskDetailOut,
    TaskAccountCoverageItemOut,
    TaskAICycleOut,
    TaskMessageGroupOut,
    TaskMembershipItemOut,
    TaskListPageOut,
    TaskOut,
    TaskRelayBatchOut,
    TaskActionReasonRequest,
    TaskPrecheckOut,
    TaskPrecheckRequest,
    TaskRetryRequest,
    TaskSettingsUpdate,
    TaskSourceFilterOverrideRequest,
    TaskUpdate,
)
from app.services.task_center import (
    ReviewStateError,
    approve_review,
    check_channel_capacity,
    create_and_start_channel_comment_task,
    create_and_start_channel_like_task,
    create_and_start_channel_view_task,
    create_and_start_group_ai_chat_task,
    create_and_start_group_membership_admission_task,
    create_and_start_group_relay_task,
    create_and_start_search_join_group_task,
    create_and_start_search_rank_deboost_task,
    create_channel_comment_task,
    create_channel_like_task,
    create_channel_view_task,
    create_group_ai_chat_task,
    create_group_membership_admission_task,
    create_group_relay_task,
    create_search_join_group_task,
    create_search_rank_deboost_task,
    delete_task,
    generate_channel_comment_preview,
    generate_group_ai_chat_preview,
    get_task_detail,
    list_ai_cycles_page,
    list_task_account_coverage_page,
    list_actions_page,
    list_action_attempts,
    list_membership_admission_items_page,
    list_membership_items_page,
    list_message_groups_page,
    list_relay_batches_page,
    list_reviews,
    list_task_page,
    mark_membership_admission_manual_handled,
    membership_admission_failure_rows,
    list_tasks,
    pause_task,
    precheck_task_creation,
    recommend_accounts,
    reject_review,
    reroll_search_rank_deboost_exempt_group,
    reset_task,
    resume_task,
    refresh_task_detail_stats,
    retry_failed_membership_admission_items,
    retry_membership_admission_item,
    retry_membership_admission_rescue,
    retry_task,
    start_task,
    stop_task,
    add_task_source_filter_override,
    update_task,
    update_channel_comment_config,
    update_channel_like_config,
    update_channel_view_config,
    update_group_ai_chat_config,
    update_group_relay_config,
    update_search_join_group_config,
    update_search_rank_deboost_config,
    update_task_settings,
)

router = APIRouter()
legacy_review_router = APIRouter()


def _set_page_headers(response: Response, total: int, page: int, page_size: int) -> None:
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)
    response.headers["X-Page-Size"] = str(page_size)


@router.post("/api/tasks/group-ai-chat", response_model=TaskOut)
def post_group_ai_chat_task(payload: GroupAIChatTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_group_ai_chat_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/group-ai-chat/create-and-start", response_model=TaskOut)
def post_group_ai_chat_create_and_start(payload: GroupAIChatTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_group_ai_chat_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/group-relay", response_model=TaskOut)
def post_group_relay_task(payload: GroupRelayTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_group_relay_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/group-relay/create-and-start", response_model=TaskOut)
def post_group_relay_create_and_start(payload: GroupRelayTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_group_relay_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/group-membership-admission", response_model=TaskOut)
def post_group_membership_admission_task(payload: GroupMembershipAdmissionTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_group_membership_admission_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/group-membership-admission/create-and-start", response_model=TaskOut)
def post_group_membership_admission_create_and_start(payload: GroupMembershipAdmissionTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_group_membership_admission_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-view", response_model=TaskOut)
def post_channel_view_task(payload: ChannelViewTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_channel_view_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-view/create-and-start", response_model=TaskOut)
def post_channel_view_create_and_start(payload: ChannelViewTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_channel_view_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-like", response_model=TaskOut)
def post_channel_like_task(payload: ChannelLikeTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_channel_like_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-like/create-and-start", response_model=TaskOut)
def post_channel_like_create_and_start(payload: ChannelLikeTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_channel_like_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-comment", response_model=TaskOut)
def post_channel_comment_task(payload: ChannelCommentTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_channel_comment_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/channel-comment/create-and-start", response_model=TaskOut)
def post_channel_comment_create_and_start(payload: ChannelCommentTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_channel_comment_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/search-join-group", response_model=TaskOut)
def post_search_join_group_task(payload: SearchJoinGroupTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_search_join_group_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/search-join-group/create-and-start", response_model=TaskOut)
def post_search_join_group_create_and_start(payload: SearchJoinGroupTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_search_join_group_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/search_rank_deboost", response_model=TaskOut)
def post_search_rank_deboost_task(payload: SearchRankDeboostTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_search_rank_deboost_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/search_rank_deboost/create_and_start", response_model=TaskOut)
def post_search_rank_deboost_create_and_start(payload: SearchRankDeboostTaskCreate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return create_and_start_search_rank_deboost_task(session, current_user.tenant_id or 1, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tasks", response_model=list[TaskOut])
def get_tasks(
    type: str | None = None,  # noqa: A002 - public query shape.
    status: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_tasks(session, current_user.tenant_id or 1, type, status)


@router.get("/api/tasks/page", response_model=TaskListPageOut)
def get_task_page(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    type: str | None = None,  # noqa: A002 - public query shape.
    status: str | None = None,
    q: str = Query(default="", max_length=160),
    group_key: str | None = Query(default=None, max_length=240),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_task_page(
        session,
        tenant_id=current_user.tenant_id or 1,
        page=page,
        page_size=page_size,
        task_type=type,
        status=status,
        q=q,
        group_key=group_key,
    )


@router.post("/api/tasks/channel-capacity-check", response_model=ChannelCapacityCheckOut)
def post_channel_capacity_check(payload: ChannelCapacityCheckRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    return check_channel_capacity(session, current_user.tenant_id or 1, payload)


@router.post("/api/tasks/precheck", response_model=TaskPrecheckOut)
def post_task_precheck(payload: TaskPrecheckRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return precheck_task_creation(session, current_user.tenant_id or 1, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/tasks/{task_id}", response_model=TaskDetailOut)
def get_task(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return get_task_detail(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/tasks/{task_id}", response_model=TaskOut)
def patch_task(task_id: str, payload: TaskUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_task(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/tasks/{task_id}/settings", response_model=TaskOut)
def patch_task_settings(task_id: str, payload: TaskSettingsUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_task_settings(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/{task_id}/source-filter-overrides", response_model=TaskOut)
def post_task_source_filter_override(task_id: str, payload: TaskSourceFilterOverrideRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return add_task_source_filter_override(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/tasks/{task_id}", status_code=204)
def delete_task_route(task_id: str, payload: TaskActionReasonRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        delete_task(session, current_user.tenant_id or 1, task_id, current_user.name, payload.reason)
        return Response(status_code=204)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/tasks/{task_id}/group-ai-chat", response_model=TaskOut)
def patch_group_ai_chat_config(task_id: str, payload: GroupAIChatTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_group_ai_chat_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/group-relay", response_model=TaskOut)
def patch_group_relay_config(task_id: str, payload: GroupRelayTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_group_relay_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/channel-view", response_model=TaskOut)
def patch_channel_view_config(task_id: str, payload: ChannelViewTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_channel_view_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/channel-like", response_model=TaskOut)
def patch_channel_like_config(task_id: str, payload: ChannelLikeTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_channel_like_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/channel-comment", response_model=TaskOut)
def patch_channel_comment_config(task_id: str, payload: ChannelCommentTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_channel_comment_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/search-join-group", response_model=TaskOut)
def patch_search_join_group_config(task_id: str, payload: SearchJoinGroupTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_search_join_group_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/tasks/{task_id}/search_rank_deboost_config", response_model=TaskOut)
def patch_search_rank_deboost_config(task_id: str, payload: SearchRankDeboostTaskConfigUpdate, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return update_search_rank_deboost_config(session, current_user.tenant_id or 1, task_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/{task_id}/search_rank_deboost_reroll_exempt_group", response_model=SearchRankDeboostExemptGroupResponse)
def post_search_rank_deboost_reroll_exempt_group(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return reroll_search_rank_deboost_exempt_group(session, current_user.tenant_id or 1, task_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/tasks/{task_id}/start", response_model=TaskOut)
def post_task_start(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return start_task(session, current_user.tenant_id or 1, task_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/pause", response_model=TaskOut)
def post_task_pause(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return pause_task(session, current_user.tenant_id or 1, task_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/resume", response_model=TaskOut)
def post_task_resume(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return resume_task(session, current_user.tenant_id or 1, task_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/stop", response_model=TaskOut)
def post_task_stop(task_id: str, payload: TaskActionReasonRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return stop_task(session, current_user.tenant_id or 1, task_id, current_user.name, payload.reason)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/retry", response_model=TaskOut)
def post_task_retry(
    task_id: str,
    payload: TaskRetryRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return retry_task(session, current_user.tenant_id or 1, task_id, payload or TaskRetryRequest(), current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/reset", response_model=TaskOut)
def post_task_reset(task_id: str, payload: TaskActionReasonRequest, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return reset_task(session, current_user.tenant_id or 1, task_id, current_user.name, payload.reason)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tasks/{task_id}/stats")
def get_task_stats(task_id: str, session: Session = Depends(get_session), current_user: CurrentUser = Depends(get_current_user)):
    try:
        return refresh_task_detail_stats(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tasks/{task_id}/account-coverage", response_model=list[TaskAccountCoverageItemOut])
def get_task_account_coverage(
    task_id: str,
    response: Response,
    coverage_date: str | None = Query(default=None, alias="date"),
    state: str | None = None,
    blocker_code: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        selected_date = date.fromisoformat(coverage_date) if coverage_date else beijing_now().date()
        rows, total = list_task_account_coverage_page(
            session,
            current_user.tenant_id or 1,
            task_id,
            coverage_date=selected_date,
            state=state,
            blocker_code=blocker_code,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    _set_page_headers(response, total, page, page_size)
    return rows


@router.get("/api/tasks/{task_id}/actions", response_model=list[ActionOut])
def get_task_actions(
    task_id: str,
    response: Response,
    status: str | None = None,
    action_type: str | None = None,
    account_id: int | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    sort_by: str = Query(default="scheduled_at", pattern="^(scheduled_at|executed_at|created_at|status|action_type|account_id)$"),
    sort_order: str = Query(default="desc", pattern="^(asc|desc)$"),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    rows, total = list_actions_page(
        session,
        current_user.tenant_id or 1,
        task_id,
        status=status,
        action_type=action_type,
        account_id=account_id,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)
    response.headers["X-Page-Size"] = str(page_size)
    return rows


@router.get("/api/tasks/{task_id}/ai-cycles", response_model=list[TaskAICycleOut])
def get_task_ai_cycles(
    task_id: str,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows, total = list_ai_cycles_page(session, current_user.tenant_id or 1, task_id, page=page, page_size=page_size)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    _set_page_headers(response, total, page, page_size)
    return rows


@router.get("/api/tasks/{task_id}/message-groups", response_model=list[TaskMessageGroupOut])
def get_task_message_groups(
    task_id: str,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows, total = list_message_groups_page(session, current_user.tenant_id or 1, task_id, page=page, page_size=page_size)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    _set_page_headers(response, total, page, page_size)
    return rows


@router.get("/api/tasks/{task_id}/relay-batches", response_model=list[TaskRelayBatchOut])
def get_task_relay_batches(
    task_id: str,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows, total = list_relay_batches_page(session, current_user.tenant_id or 1, task_id, page=page, page_size=page_size)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    _set_page_headers(response, total, page, page_size)
    return rows


@router.get("/api/tasks/{task_id}/membership-items", response_model=list[TaskMembershipItemOut])
def get_task_membership_items(
    task_id: str,
    response: Response,
    status: str | None = None,
    phase: str | None = None,
    account_id: int | None = None,
    manual_required: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows, total = list_membership_items_page(
            session,
            current_user.tenant_id or 1,
            task_id,
            status=status,
            phase=phase,
            account_id=account_id,
            manual_required=manual_required,
            page=page,
            page_size=page_size,
        )
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    response.headers["X-Total-Count"] = str(total)
    response.headers["X-Page"] = str(page)
    response.headers["X-Page-Size"] = str(page_size)
    return rows


@router.get("/api/tasks/{task_id}/membership-admission/items", response_model=list[dict])
def get_task_membership_admission_items(
    task_id: str,
    response: Response,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=50, ge=1, le=200),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows, total = list_membership_admission_items_page(session, current_user.tenant_id or 1, task_id, page=page, page_size=page_size)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    _set_page_headers(response, total, page, page_size)
    return rows


@router.post("/api/tasks/{task_id}/membership-admission/items/{item_id}/retry", response_model=TaskDetailOut)
def post_membership_admission_item_retry(
    task_id: str,
    item_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        retry_membership_admission_item(session, current_user.tenant_id or 1, task_id, item_id)
        return get_task_detail(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/membership-admission/retry-failed", response_model=TaskDetailOut)
def post_membership_admission_retry_failed(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        retry_failed_membership_admission_items(session, current_user.tenant_id or 1, task_id)
        return get_task_detail(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/membership-admission/items/{item_id}/manual-handled", response_model=TaskDetailOut)
def post_membership_admission_manual_handled(
    task_id: str,
    item_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        mark_membership_admission_manual_handled(session, current_user.tenant_id or 1, task_id, item_id)
        return get_task_detail(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/{task_id}/membership-admission/items/{item_id}/retry-rescue", response_model=TaskDetailOut)
def post_membership_admission_rescue_retry(
    task_id: str,
    item_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        retry_membership_admission_rescue(session, current_user.tenant_id or 1, task_id, item_id)
        return get_task_detail(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/tasks/{task_id}/membership-admission/failures.csv")
def get_membership_admission_failures_csv(
    task_id: str,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        rows = membership_admission_failure_rows(session, current_user.tenant_id or 1, task_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    columns = ["account_id", "display_name", "username", "phase", "manual_required", "failure_type", "failure_detail", "test_message_id", "delete_status", "permission_failure_count", "rescue_status", "rescue_failure_detail"]
    buffer = StringIO()
    writer = csv.DictWriter(buffer, fieldnames=columns)
    writer.writeheader()
    writer.writerows(rows)
    return Response(
        content="\ufeff" + buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="membership-admission-{task_id}-failures.csv"'},
    )


@router.get("/api/tasks/{task_id}/actions/{action_id}/attempts", response_model=list[ExecutionAttemptOut])
def get_task_action_attempts(
    task_id: str,
    action_id: str,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return list_action_attempts(session, current_user.tenant_id or 1, task_id, action_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/tasks/recommend-accounts", response_model=list[RecommendedTaskAccountOut])
def post_task_recommend_accounts(
    payload: RecommendTaskAccountsRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return recommend_accounts(session, current_user.tenant_id or 1, payload)


@router.post("/api/tasks/group-ai-chat/generate-preview", response_model=GenerateTaskPreviewOut)
def post_group_ai_chat_generate_preview(
    payload: GroupAIChatTaskPreviewRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return generate_group_ai_chat_preview(session, current_user.tenant_id or 1, payload)


@router.post("/api/tasks/channel-comment/generate-preview", response_model=GenerateTaskPreviewOut)
def post_channel_comment_generate_preview(
    payload: ChannelCommentTaskPreviewRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return generate_channel_comment_preview(session, current_user.tenant_id or 1, payload)


@legacy_review_router.get("/api/review-queue", response_model=list[ReviewQueueOut])
def get_review_queue(
    status: str | None = "pending",
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_reviews(session, current_user.tenant_id or 1, status)


@legacy_review_router.post("/api/review/{review_id}/approve", response_model=ReviewQueueOut)
def post_review_approve(
    review_id: str,
    payload: ReviewApproveRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return approve_review(session, current_user.tenant_id or 1, review_id, payload or ReviewApproveRequest(), current_user.name)
    except ReviewStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@legacy_review_router.post("/api/review/{review_id}/reject", response_model=ReviewQueueOut)
def post_review_reject(
    review_id: str,
    payload: ReviewRejectRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return reject_review(session, current_user.tenant_id or 1, review_id, payload or ReviewRejectRequest(), current_user.name)
    except ReviewStateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise not_found(str(exc)) from exc


if get_settings().enable_legacy_review_routes:
    router.include_router(legacy_review_router)


__all__ = ["router"]
