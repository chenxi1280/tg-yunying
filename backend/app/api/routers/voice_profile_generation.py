"""Durable account-mask generation job routes."""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user, resolve_tenant_id
from app.database import get_session
from app.schemas.ai_config import (
    AiAccountVoiceProfileGenerationItemRetryRequest,
    AiAccountVoiceProfileGenerationJobCreateRequest,
    AiAccountVoiceProfileGenerationJobDetailOut,
    AiAccountVoiceProfileGenerationJobListOut,
    AiAccountVoiceProfileGenerationRetryOut,
)
from app.services.task_center.account_voice_profile_generation_jobs import retry_voice_profile_generation_item
from app.services.task_center.account_voice_profile_generation_management import (
    create_voice_profile_generation_job,
    list_voice_profile_generation_jobs,
    voice_profile_generation_item_detail,
    voice_profile_generation_job_detail,
)


router = APIRouter()
MANAGE_PERMISSION = "ai_voice_profiles.manage"
VIEW_PERMISSION = "account_masks.view"


@router.post(
    "/api/ai-account-voice-profile-generation-jobs",
    response_model=AiAccountVoiceProfileGenerationJobDetailOut,
    status_code=202,
)
def post_voice_profile_generation_job(
    payload: AiAccountVoiceProfileGenerationJobCreateRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, MANAGE_PERMISSION)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        result = create_voice_profile_generation_job(
            session,
            tenant_id=target_tenant_id,
            account_ids=payload.account_ids,
            mode=payload.mode,
            rebuild_existing=payload.rebuild_existing,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            actor=current_user.name,
        )
        session.commit()
        return voice_profile_generation_job_detail(session, tenant_id=target_tenant_id, job_id=result.job.id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get(
    "/api/ai-account-voice-profile-generation-jobs",
    response_model=AiAccountVoiceProfileGenerationJobListOut,
)
def get_voice_profile_generation_jobs(
    status: str = "",
    account_id: int | None = None,
    offset: int = 0,
    limit: int = 100,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, VIEW_PERMISSION)
    return {
        "items": list_voice_profile_generation_jobs(
            session,
            tenant_id=resolve_tenant_id(current_user, tenant_id),
            status=status,
            account_id=account_id,
            offset=offset,
            limit=limit,
        ),
        "offset": max(0, offset),
        "limit": min(200, max(1, limit)),
    }


@router.get(
    "/api/ai-account-voice-profile-generation-jobs/{job_id}",
    response_model=AiAccountVoiceProfileGenerationJobDetailOut,
)
def get_voice_profile_generation_job(
    job_id: str,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, VIEW_PERMISSION)
    try:
        return voice_profile_generation_job_detail(
            session,
            tenant_id=resolve_tenant_id(current_user, tenant_id),
            job_id=job_id,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post(
    "/api/ai-account-voice-profile-generation-items/{item_id}/retry",
    response_model=AiAccountVoiceProfileGenerationRetryOut,
    status_code=202,
)
def post_voice_profile_generation_item_retry(
    item_id: str,
    payload: AiAccountVoiceProfileGenerationItemRetryRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    ensure_permission(current_user, MANAGE_PERMISSION)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        item = retry_voice_profile_generation_item(
            session,
            tenant_id=target_tenant_id,
            item_id=item_id,
            expected_status=payload.expected_status,
            expected_profile_version=payload.expected_profile_version,
            idempotency_key=payload.idempotency_key,
            reason=payload.reason,
            actor=current_user.name,
        )
        session.commit()
        return {
            "job": voice_profile_generation_job_detail(session, tenant_id=target_tenant_id, job_id=item.job_id),
            "item": voice_profile_generation_item_detail(session, tenant_id=target_tenant_id, item_id=item.id),
        }
    except LookupError as exc:
        session.rollback()
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        session.rollback()
        status_code = 409 if "conflict" in str(exc) else 400
        raise HTTPException(status_code=status_code, detail=str(exc)) from exc
