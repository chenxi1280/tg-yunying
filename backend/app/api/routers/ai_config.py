"""AI configuration, prompt templates, tenant AI settings, scheduling, and materials routes."""
from __future__ import annotations


from collections.abc import Sequence

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from sqlalchemy.orm import Session

from app.auth import CurrentUser, ensure_permission, get_current_user, require_core_feature_access, resolve_tenant_id
from app.database import get_session
from app.common.http import forbidden, not_found
from app.models import ContentKeywordRule, Material, PromptTemplate, SchedulingSetting, TenantAiSetting
from app.repositories.tenant import require_resource_tenant
from app.schemas import (
    AiProviderCreate, AiProviderOut, AiProviderUpdate,
    ContentKeywordRuleCreate, ContentKeywordRuleOut, ContentKeywordRuleUpdate,
    MaterialActionRequest, MaterialCreate, MaterialOut, MaterialUpdate,
    PromptTemplateCreate, PromptTemplateOut, PromptTemplateUpdate,
    SchedulingSettingOut, SchedulingSettingUpdate,
    TenantAiSettingOut, TenantAiSettingUpdate,
)
from app.schemas.account_environment import AccountEnvironmentBindingOut, AccountEnvironmentBindingPatch
from app.schemas.ai_config import (
    AiAccountVoiceProfileAuditOut,
    AiAccountVoiceProfileBatchRebuildOut,
    AiAccountVoiceProfileBatchRebuildRequest,
    AiAccountVoiceProfileBatchStatusOut,
    AiAccountVoiceProfileBatchStatusRequest,
    AiAccountVoiceProfileOut,
    AiAccountVoiceProfileRollbackRequest,
    AiAccountVoiceProfileUpdate,
    AiAccountVoiceProfileVersionOut,
    MaterialCacheConfigOut,
    MaterialCacheConfigUpdate,
    MaterialCacheHealthOut,
    MaterialGroupCreate,
    MaterialGroupOut,
    MaterialGroupUpdate,
    MaterialImportResultOut,
    MaterialReferencesOut,
    MaterialVersionHistoryOut,
)
from app.services.account_environment import (
    list_account_environment_bindings,
    patch_account_environment_binding,
)
from app.services.account_environment_observations import refresh_account_environment_observations
from app.services.task_center.account_voice_profiles import (
    batch_rebuild_voice_profiles,
    generate_voice_profiles_with_ai,
    list_voice_profiles,
    patch_voice_profile,
    rebuild_voice_profile,
)
from app.services.task_center.account_voice_profile_bulk import batch_update_voice_profile_status
from app.services.task_center.account_voice_profile_versions import (
    list_voice_profile_audits,
    list_voice_profile_versions,
    rollback_voice_profile,
)
from app.services import (
    check_ai_provider, create_ai_provider, create_content_keyword_rule, create_material, create_material_group, create_prompt_template,
    disable_material,
    get_scheduling_setting, get_tenant_ai_setting,
    list_ai_providers, list_content_keyword_rules, list_material_groups, list_materials, list_material_version_history, list_prompt_templates,
    material_references,
    refresh_material_cache,
    restore_material,
    update_ai_provider, update_content_keyword_rule, update_material, update_material_group, update_prompt_template,
    update_scheduling_setting, update_tenant_ai_setting,
)
from app.services.ai_config import (
    create_material_zip_import,
    create_uploaded_material,
    create_uploaded_materials,
    get_material,
    get_material_cache_config,
    get_material_import_result,
    list_material_import_results,
    material_cache_health,
    update_material_cache_config,
)

router = APIRouter()

AI_VOICE_PROFILE_MANAGE_PERMISSION = "ai_voice_profiles.manage"
ACCOUNT_MASKS_VIEW_PERMISSION = "account_masks.view"
ACCOUNT_ENVIRONMENT_MANAGE_PERMISSION = "account_environment.manage"


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
    try:
        return update_tenant_ai_setting(session, resolve_tenant_id(current_user, tenant_id), payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ── AI Account Voice Profiles ──

def _require_voice_profile_manage(current_user: CurrentUser) -> None:
    ensure_permission(current_user, AI_VOICE_PROFILE_MANAGE_PERMISSION)


def _require_account_masks_view(current_user: CurrentUser) -> None:
    ensure_permission(current_user, ACCOUNT_MASKS_VIEW_PERMISSION)


def _require_account_environment_manage(current_user: CurrentUser) -> None:
    ensure_permission(current_user, ACCOUNT_ENVIRONMENT_MANAGE_PERMISSION)


@router.get("/api/ai-account-voice-profiles", response_model=list[AiAccountVoiceProfileOut])
def get_ai_account_voice_profiles(
    search: str = "",
    profile_status: str = "",
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_account_masks_view(current_user)
    return list_voice_profiles(
        session,
        tenant_id=resolve_tenant_id(current_user, tenant_id),
        search=search,
        profile_status=profile_status,
    )


@router.get("/api/account-environment-bindings", response_model=list[AccountEnvironmentBindingOut])
def get_account_environment_bindings(
    search: str = "",
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_account_masks_view(current_user)
    return list_account_environment_bindings(
        session,
        tenant_id=resolve_tenant_id(current_user, tenant_id),
        search=search,
    )


@router.patch("/api/account-environment-bindings/{account_id}", response_model=AccountEnvironmentBindingOut)
def patch_account_environment_binding_route(
    account_id: int,
    payload: AccountEnvironmentBindingPatch,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_account_environment_manage(current_user)
    try:
        row = patch_account_environment_binding(
            session,
            tenant_id=resolve_tenant_id(current_user, tenant_id),
            account_id=account_id,
            payload=payload,
            actor=current_user.name,
        )
        session.commit()
        return row
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/account-environment-bindings/refresh-observations", response_model=list[AccountEnvironmentBindingOut])
def post_account_environment_observation_refresh(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_account_environment_manage(current_user)
    rows = refresh_account_environment_observations(
        session,
        tenant_id=resolve_tenant_id(current_user, tenant_id),
        actor=current_user.name,
    )
    session.commit()
    return rows


@router.patch("/api/ai-account-voice-profiles/{account_id}", response_model=AiAccountVoiceProfileOut)
def patch_ai_account_voice_profile(
    account_id: int,
    payload: AiAccountVoiceProfileUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_voice_profile_manage(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        patch_voice_profile(session, tenant_id=target_tenant_id, account_id=account_id, patch=payload.model_dump(exclude_unset=True), actor=current_user.name)
        session.commit()
        return _account_voice_profile_projection(session, target_tenant_id, account_id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/ai-account-voice-profiles/{account_id}/rebuild", response_model=AiAccountVoiceProfileOut)
def rebuild_ai_account_voice_profile(
    account_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_voice_profile_manage(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        rebuild_voice_profile(
            session,
            tenant_id=target_tenant_id,
            account_id=account_id,
            generator=generate_voice_profiles_with_ai(session, tenant_id=target_tenant_id),
            actor=current_user.name,
        )
        session.commit()
        return _account_voice_profile_projection(session, target_tenant_id, account_id)
    except (RuntimeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/ai-account-voice-profiles/{account_id}/versions", response_model=list[AiAccountVoiceProfileVersionOut])
def get_ai_account_voice_profile_versions(
    account_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return list_voice_profile_versions(session, tenant_id=resolve_tenant_id(current_user, tenant_id), account_id=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/ai-account-voice-profiles/{account_id}/audits", response_model=list[AiAccountVoiceProfileAuditOut])
def get_ai_account_voice_profile_audits(
    account_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return list_voice_profile_audits(session, tenant_id=resolve_tenant_id(current_user, tenant_id), account_id=account_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/api/ai-account-voice-profiles/{account_id}/rollback", response_model=AiAccountVoiceProfileOut)
def rollback_ai_account_voice_profile(
    account_id: int,
    payload: AiAccountVoiceProfileRollbackRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_voice_profile_manage(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        rollback_voice_profile(
            session,
            tenant_id=target_tenant_id,
            account_id=account_id,
            source_version=payload.source_version,
            actor=current_user.name,
        )
        session.commit()
        return _account_voice_profile_projection(session, target_tenant_id, account_id)
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/ai-account-voice-profiles/batch-rebuild", response_model=AiAccountVoiceProfileBatchRebuildOut)
def batch_rebuild_ai_account_voice_profiles(
    payload: AiAccountVoiceProfileBatchRebuildRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_voice_profile_manage(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        result = batch_rebuild_voice_profiles(
            session,
            tenant_id=target_tenant_id,
            account_ids=payload.account_ids,
            generator=generate_voice_profiles_with_ai(session, tenant_id=target_tenant_id),
            actor=current_user.name,
            missing_only=payload.missing_only,
        )
        session.commit()
        return result
    except (RuntimeError, ValueError) as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/ai-account-voice-profiles/batch-status", response_model=AiAccountVoiceProfileBatchStatusOut)
def batch_update_ai_account_voice_profile_status(
    payload: AiAccountVoiceProfileBatchStatusRequest,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    _require_voice_profile_manage(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        result = batch_update_voice_profile_status(
            session,
            tenant_id=target_tenant_id,
            account_ids=payload.account_ids,
            status=payload.status,
            actor=current_user.name,
        )
        session.commit()
        return result
    except ValueError as exc:
        session.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _account_voice_profile_projection(session: Session, tenant_id: int, account_id: int) -> dict:
    rows = list_voice_profiles(session, tenant_id=tenant_id, search="")
    for row in rows:
        if int(row["account_id"]) == int(account_id):
            return row
    raise HTTPException(status_code=404, detail="account not found")


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


@router.get("/api/material-groups", response_model=list[MaterialGroupOut])
def get_material_groups(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_material_groups(session, resolve_tenant_id(current_user, tenant_id))


@router.post("/api/material-groups", response_model=MaterialGroupOut)
def post_material_group(
    payload: MaterialGroupCreate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        return create_material_group(session, resolve_tenant_id(current_user, tenant_id), payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/material-groups/{group_id}", response_model=MaterialGroupOut)
def patch_material_group(
    group_id: int,
    payload: MaterialGroupUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        return update_material_group(session, resolve_tenant_id(current_user, tenant_id), group_id, payload, current_user.name)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/materials/cache/health", response_model=MaterialCacheHealthOut)
def get_material_cache_health(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return material_cache_health(session, resolve_tenant_id(current_user, tenant_id))


@router.get("/api/materials/cache/config", response_model=MaterialCacheConfigOut)
def get_materials_cache_config(
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return get_material_cache_config(session, resolve_tenant_id(current_user, tenant_id))


@router.patch("/api/materials/cache/config", response_model=MaterialCacheConfigOut)
def patch_materials_cache_config(
    payload: MaterialCacheConfigUpdate,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        update_kwargs = {
            "session": session,
            "tenant_id": resolve_tenant_id(current_user, tenant_id),
            "material_cache_input": payload.material_cache_input,
            "source_media_cache_input": payload.source_media_cache_input,
            "actor": current_user.name,
        }
        if "material_cache_account_id" in payload.model_fields_set:
            update_kwargs["material_cache_account_id"] = payload.material_cache_account_id
        return update_material_cache_config(
            **update_kwargs,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/materials/{material_id}", response_model=MaterialOut)
def get_material_detail(
    material_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return get_material(session, resolve_tenant_id(current_user, tenant_id), material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.get("/api/materials/{material_id}/versions", response_model=MaterialVersionHistoryOut)
def get_material_versions(
    material_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return list_material_version_history(session, resolve_tenant_id(current_user, tenant_id), material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/materials/{material_id}/versions", response_model=MaterialOut)
def post_material_version(
    material_id: int,
    payload: MaterialUpdate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Material, material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    try:
        return update_material(session, material_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/materials/{material_id}/references", response_model=MaterialReferencesOut)
def get_material_references(
    material_id: int,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return material_references(session, resolve_tenant_id(current_user, tenant_id), material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/materials/{material_id}/refresh-cache", response_model=MaterialOut)
def post_material_refresh_cache(
    material_id: int,
    payload: MaterialActionRequest | None = None,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    try:
        require_resource_tenant(session, current_user, Material, material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    try:
        return refresh_material_cache(session, target_tenant_id, material_id, current_user.name, reason=(payload.reason if payload else ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/materials", response_model=MaterialOut)
def post_material(
    payload: MaterialCreate,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    tenant_id = resolve_tenant_id(current_user, payload.tenant_id)
    try:
        return create_material(session, payload.model_copy(update={"tenant_id": tenant_id}), current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/materials/upload", response_model=MaterialOut)
async def post_material_upload(
    title: str = Form(...),
    material_type: str = Form("图片"),
    tags: str = Form(""),
    caption: str = Form(""),
    emoji_asset_kind: str = Form(""),
    tenant_id: int | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    data = await file.read()
    try:
        return create_uploaded_material(
            session,
            tenant_id=target_tenant_id,
            title=title,
            material_type=material_type,
            tags=tags,
            caption=caption,
            filename=file.filename or "material",
            content_type=file.content_type or "",
            data=data,
            emoji_asset_kind=emoji_asset_kind,
            actor=current_user.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/materials/upload/zip", response_model=MaterialImportResultOut)
async def post_material_upload_zip(
    title: str = Form("素材包"),
    material_type: str = Form("图片"),
    tags: str = Form(""),
    caption: str = Form(""),
    tenant_id: int | None = Form(None),
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    data = await file.read()
    try:
        return create_material_zip_import(
            session,
            tenant_id=target_tenant_id,
            title=title,
            material_type=material_type,
            tags=tags,
            caption=caption,
            filename=file.filename or "materials.zip",
            data=data,
            actor=current_user.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/material-imports", response_model=list[MaterialImportResultOut])
def get_material_imports(
    tenant_id: int | None = None,
    limit: int = 20,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    return list_material_import_results(session, tenant_id=resolve_tenant_id(current_user, tenant_id), limit=limit)


@router.get("/api/material-imports/{import_id}", response_model=MaterialImportResultOut)
def get_material_import(
    import_id: str,
    tenant_id: int | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    try:
        return get_material_import_result(session, tenant_id=resolve_tenant_id(current_user, tenant_id), import_id=import_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc


@router.post("/api/materials/upload/batch", response_model=list[MaterialOut])
async def post_material_upload_batch(
    title: str = Form("素材"),
    material_type: str = Form("图片"),
    tags: str = Form(""),
    caption: str = Form(""),
    emoji_asset_kind: str = Form(""),
    tenant_id: int | None = Form(None),
    files: list[UploadFile] = File(...),
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    target_tenant_id = resolve_tenant_id(current_user, tenant_id)
    file_payloads: list[tuple[str, str, bytes]] = []
    for file in files:
        file_payloads.append((file.filename or "material", file.content_type or "", await file.read()))
    try:
        return create_uploaded_materials(
            session,
            tenant_id=target_tenant_id,
            title=title,
            material_type=material_type,
            tags=tags,
            caption=caption,
            files=file_payloads,
            emoji_asset_kind=emoji_asset_kind,
            actor=current_user.name,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    try:
        return update_material(session, material_id, payload, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/materials/{material_id}/disable", response_model=MaterialOut)
def post_material_disable(
    material_id: int,
    payload: MaterialActionRequest | None = None,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Material, material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    try:
        return disable_material(session, material_id, current_user.name, reason=(payload.reason if payload else ""))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/materials/{material_id}/restore", response_model=MaterialOut)
def post_material_restore(
    material_id: int,
    session: Session = Depends(get_session),
    current_user: CurrentUser = Depends(get_current_user),
):
    require_core_feature_access(current_user)
    try:
        require_resource_tenant(session, current_user, Material, material_id)
    except ValueError as exc:
        raise not_found(str(exc)) from exc
    try:
        return restore_material(session, material_id, current_user.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


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
