"""AI configuration, prompt templates, tenant AI settings, scheduling, and materials routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.auth import CurrentUser, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import forbidden, not_found
from app.models import ContentKeywordRule, Material, PromptTemplate, SchedulingSetting, TenantAiSetting
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AiProviderCreate, AiProviderOut, AiProviderUpdate,
    ContentKeywordRuleCreate, ContentKeywordRuleOut, ContentKeywordRuleUpdate,
    MaterialCreate, MaterialOut, MaterialUpdate,
    PromptTemplateCreate, PromptTemplateOut, PromptTemplateUpdate,
    SchedulingSettingOut, SchedulingSettingUpdate,
    TenantAiSettingOut, TenantAiSettingUpdate,
)
from app.services import (
    check_ai_provider, create_ai_provider, create_content_keyword_rule, create_material, create_prompt_template,
    get_scheduling_setting, get_tenant_ai_setting,
    list_ai_providers, list_content_keyword_rules, list_materials, list_prompt_templates,
    update_ai_provider, update_content_keyword_rule, update_material, update_prompt_template,
    update_scheduling_setting, update_tenant_ai_setting,
)

router = APIRouter()


# ── AI Providers ──

@router.get("/api/ai-providers", response_model=list[AiProviderOut])
def get_ai_providers(
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_ai_providers(session)


@router.post("/api/ai-providers", response_model=AiProviderOut)
def post_ai_provider(
    payload: AiProviderCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    return create_ai_provider(session, payload, current_user.name)


@router.patch("/api/ai-providers/{provider_id}", response_model=AiProviderOut)
def patch_ai_provider(
    provider_id: int,
    payload: AiProviderUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return update_ai_provider(session, provider_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/ai-providers/{provider_id}/check", response_model=AiProviderOut)
def post_ai_provider_check(
    provider_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    if not current_user.is_platform_admin:
        raise forbidden("platform admin required")
    try:
        return check_ai_provider(session, provider_id, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Prompt Templates ──

@router.get("/api/prompt-templates", response_model=list[PromptTemplateOut])
def get_prompt_templates(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> Sequence[PromptTemplate]:
    if current_user.is_platform_admin and tenant_id is None:
        return list_prompt_templates(session, None)
    return list_prompt_templates(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/prompt-templates", response_model=PromptTemplateOut)
def post_prompt_template(
    payload: PromptTemplateCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> PromptTemplate:
    require_core_feature_access(current_user)
    tenant_id = payload.tenant_id
    if tenant_id is None and not current_user.is_platform_admin:
        raise forbidden("platform admin required for platform template")
    if tenant_id is not None:
        resolve_tenant_id(current_user, tenant_id)
    return create_prompt_template(session, payload, current_user.name)


@router.patch("/api/prompt-templates/{template_id}", response_model=PromptTemplateOut)
def patch_prompt_template(
    template_id: int,
    payload: PromptTemplateUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> PromptTemplate:
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, PromptTemplate, template_id)
        return update_prompt_template(session, template_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Tenant AI Settings ──

@router.get("/api/tenant-ai-settings", response_model=TenantAiSettingOut)
def get_tenant_ai_settings(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> TenantAiSetting:
    return get_tenant_ai_setting(session, resolve_tenant_id(current_user, tenant_id))


@router.patch("/api/tenant-ai-settings", response_model=TenantAiSettingOut)
def patch_tenant_ai_settings(
    payload: TenantAiSettingUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> TenantAiSetting:
    require_core_feature_access(current_user)
    return update_tenant_ai_setting(session, resolve_tenant_id(current_user, tenant_id), payload, current_user.name)


# ── Scheduling Settings ──

@router.get("/api/scheduling-settings", response_model=SchedulingSettingOut)
def get_scheduling_settings(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> SchedulingSetting:
    if current_user.is_platform_admin and tenant_id is None:
        return get_scheduling_setting(session, None)
    return get_scheduling_setting(session, resolve_tenant_id(current_user, tenant_id))


@router.patch("/api/scheduling-settings", response_model=SchedulingSettingOut)
def patch_scheduling_settings(
    payload: SchedulingSettingUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> SchedulingSetting:
    require_core_feature_access(current_user)
    target_tenant_id = None if current_user.is_platform_admin and tenant_id is None else resolve_tenant_id(current_user, tenant_id)
    return update_scheduling_setting(session, target_tenant_id, payload, current_user.name)


# ── Materials ──

@router.get("/api/materials", response_model=list[MaterialOut])
def get_materials(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_materials(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/materials", response_model=MaterialOut)
def post_material(
    payload: MaterialCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    return create_material(session, payload.model_copy(update={"tenant_id": tenant_id}))


@router.patch("/api/materials/{material_id}", response_model=MaterialOut)
def patch_material(
    material_id: int,
    payload: MaterialUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Material, material_id)
        return update_material(session, material_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


# ── Content keyword rules ──

@router.get("/api/content-keyword-rules", response_model=list[ContentKeywordRuleOut])
def get_content_keyword_rules(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_content_keyword_rules(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/content-keyword-rules", response_model=ContentKeywordRuleOut)
def post_content_keyword_rule(
    payload: ContentKeywordRuleCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ContentKeywordRule:
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        return create_content_keyword_rule(session, payload.model_copy(update={"tenant_id": tenant_id}), current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.patch("/api/content-keyword-rules/{rule_id}", response_model=ContentKeywordRuleOut)
def patch_content_keyword_rule(
    rule_id: int,
    payload: ContentKeywordRuleUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
) -> ContentKeywordRule:
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, ContentKeywordRule, rule_id)
        return update_content_keyword_rule(session, rule_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
