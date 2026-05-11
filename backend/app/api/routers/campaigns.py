"""Legacy Campaign and AI draft routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.common.http import not_found
from app.database import get_session
from app.models import AiDraft, Campaign
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AiDraftOut, AiDraftUpdate, ApproveAllRequest, ApproveDraftRequest,
    CampaignCreate, CampaignDetailOut, CampaignOut, CampaignRecommendAccountsRequest,
    GenerateDraftsRequest, MessageTaskOut, RecommendedAccountOut,
)
from app.services import (
    approve_all_drafts, approve_draft, campaign_detail,
    cancel_campaign, create_campaign, filter_campaigns,
    generate_drafts, list_ai_drafts_for_tenant, recommend_campaign_accounts, reject_ai_draft,
    update_ai_draft,
)

router = APIRouter()
legacy_router = APIRouter()


# ── Campaigns ──

@legacy_router.get("/api/campaigns", response_model=list[CampaignOut])
def list_campaigns(
    tenant_id: int | None = None,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[Campaign]:
    try:
        return filter_campaigns(session, resolve_tenant_id(current_user, tenant_id), page, page_size, search, status)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@legacy_router.post("/api/campaigns", response_model=CampaignOut)
def post_campaign(
    payload: CampaignCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Campaign:
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        return create_campaign(session, payload.model_copy(update={"tenant_id": tenant_id}))
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@legacy_router.get("/api/campaigns/{campaign_id}/detail", response_model=CampaignDetailOut)
def get_campaign_detail(
    campaign_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        require_resource_tenant(session, current_user, Campaign, campaign_id)
        return campaign_detail(session, campaign_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@legacy_router.post("/api/campaigns/recommend-accounts", response_model=list[RecommendedAccountOut])
def post_campaign_recommend_accounts(
    payload: CampaignRecommendAccountsRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    return recommend_campaign_accounts(session, payload.model_copy(update={"tenant_id": tenant_id}), tenant_id)


@legacy_router.post("/api/campaigns/{campaign_id}/generate-drafts", response_model=list[AiDraftOut])
def post_generate_drafts(
    campaign_id: int,
    payload: GenerateDraftsRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Campaign, campaign_id)
        return generate_drafts(session, campaign_id, payload, current_user)
    except ValueError as exc:
        if "Token 余额不足" in str(exc):
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        raise not_found(str(exc)) from exc


@legacy_router.post("/api/campaigns/{campaign_id}/approve-all", response_model=list[MessageTaskOut])
def post_approve_all(
    campaign_id: int,
    payload: ApproveAllRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Campaign, campaign_id)
        return approve_all_drafts(session, campaign_id, payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/api/campaigns/{campaign_id}/cancel", response_model=CampaignOut)
def post_cancel_campaign(
    campaign_id: int,
    payload: ApproveDraftRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Campaign, campaign_id)
        return cancel_campaign(session, campaign_id, payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── AI Drafts ──

@legacy_router.get("/api/ai-drafts", response_model=list[AiDraftOut])
def list_ai_drafts(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[AiDraft]:
    return list_ai_drafts_for_tenant(session, resolve_tenant_id(current_user, tenant_id))


@legacy_router.post("/api/ai-drafts/{draft_id}/approve", response_model=MessageTaskOut)
def post_approve_draft(
    draft_id: int,
    payload: ApproveDraftRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, AiDraft, draft_id)
        return approve_draft(session, draft_id, payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.patch("/api/ai-drafts/{draft_id}", response_model=AiDraftOut)
def patch_ai_draft(
    draft_id: int,
    payload: AiDraftUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, AiDraft, draft_id)
        return update_ai_draft(session, draft_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@legacy_router.post("/api/ai-drafts/{draft_id}/reject", response_model=AiDraftOut)
def post_reject_ai_draft(
    draft_id: int,
    payload: ApproveDraftRequest,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, AiDraft, draft_id)
        return reject_ai_draft(session, draft_id, payload.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


router.include_router(legacy_router)
