from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    AiProvider,
    AiProviderHealthStatus,
    TenantAiProviderRouteItem,
    TenantAiProviderRouteSet,
    TenantAiSetting,
)
from app.schemas.ai_config import (
    DEFAULT_AI_MAX_TOKENS_LIMIT,
    MINIMAX_MAX_TOKENS_LIMIT,
    TenantAiSettingUpdate,
)

from ._common import _now, audit, require_tenant


SETTING_FIELDS = (
    "default_provider_id",
    "ai_enabled",
    "fallback_to_mock",
    "ai_group_model_fallback_enabled",
    "ai_provider_route_fallback_enabled",
    "ai_group_grok_fallback_enabled",
    "ai_group_static_fallback_enabled",
    "temperature",
    "max_tokens",
)


def get_tenant_ai_setting(session: Session, tenant_id: int) -> TenantAiSetting:
    require_tenant(session, tenant_id)
    setting = session.scalar(select(TenantAiSetting).where(
        TenantAiSetting.tenant_id == tenant_id,
    ))
    if setting:
        return setting
    default_provider_id = session.scalar(select(AiProvider.id).order_by(AiProvider.id))
    setting = TenantAiSetting(tenant_id=tenant_id, default_provider_id=default_provider_id)
    session.add(setting)
    session.commit()
    session.refresh(setting)
    return setting


def update_tenant_ai_setting(
    session: Session,
    tenant_id: int,
    payload: TenantAiSettingUpdate,
    actor: str,
) -> TenantAiSetting:
    setting = get_tenant_ai_setting(session, tenant_id)
    data = payload.model_dump(exclude_unset=True)
    provider_id = data.get("default_provider_id", setting.default_provider_id)
    try:
        _validate_update(session, setting, data, provider_id)
    except ValueError as exc:
        _record_validation_failure(session, setting, data, actor, exc)
        raise
    for field in SETTING_FIELDS:
        if field in data:
            setattr(setting, field, data[field])
    setting.updated_at = _now()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="更新客户AI配置",
        target_type="tenant_ai_setting",
        target_id=str(setting.id),
    )
    session.commit()
    session.refresh(setting)
    return setting


def _validate_update(
    session: Session,
    setting: TenantAiSetting,
    data: dict,
    provider_id: int | None,
) -> None:
    if "default_provider_id" in data and provider_id != setting.default_provider_id:
        _validate_default_provider(session, provider_id)
    enabling_route = (
        data.get("ai_provider_route_fallback_enabled") is True
        and not setting.ai_provider_route_fallback_enabled
    )
    if enabling_route:
        _validate_route_fallback(session, setting.tenant_id)
    _validate_token_limit(session, provider_id, data.get("max_tokens"))


def _validate_default_provider(session: Session, provider_id: int | None) -> None:
    if provider_id is None:
        return
    provider = session.get(AiProvider, provider_id)
    if provider is None:
        raise ValueError("default AI provider not found")
    if not provider.credential_enabled or not provider.is_active:
        raise ValueError("default AI provider is disabled")
    if provider.health_status != AiProviderHealthStatus.HEALTHY.value:
        raise ValueError("default AI provider is unhealthy")


def _validate_route_fallback(session: Session, tenant_id: int) -> None:
    provider_ids = session.scalars(
        select(TenantAiProviderRouteItem.provider_id)
        .join(TenantAiProviderRouteSet)
        .join(AiProvider, AiProvider.id == TenantAiProviderRouteItem.provider_id)
        .where(
            TenantAiProviderRouteSet.tenant_id == tenant_id,
            TenantAiProviderRouteSet.purpose == "group_realize_general",
            TenantAiProviderRouteSet.status == "active",
            TenantAiProviderRouteItem.enabled.is_(True),
            AiProvider.credential_enabled.is_(True),
            AiProvider.is_active.is_(True),
            AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
        )
    )
    if len(set(provider_ids)) < 2:
        raise ValueError("provider route fallback requires at least two healthy active providers")


def _validate_token_limit(
    session: Session,
    provider_id: int | None,
    max_tokens: int | None,
) -> None:
    if max_tokens is None:
        return
    provider = session.get(AiProvider, provider_id) if provider_id else None
    limit = MINIMAX_MAX_TOKENS_LIMIT if provider and _is_minimax(provider) else DEFAULT_AI_MAX_TOKENS_LIMIT
    if max_tokens > limit:
        raise ValueError(f"最大 Token 超过当前模型上限：{limit}")


def _is_minimax(provider: AiProvider) -> bool:
    text = f"{provider.provider_name} {provider.base_url} {provider.model_name}".lower()
    return "minimax" in text or "minimaxi" in text


def _record_validation_failure(
    session: Session,
    setting: TenantAiSetting,
    data: dict,
    actor: str,
    error: ValueError,
) -> None:
    tenant_id = setting.tenant_id
    setting_id = str(setting.id)
    detail = json.dumps(
        {
            "changed_fields": sorted(data),
            "error_code": _validation_error_code(str(error)),
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    session.rollback()
    audit(
        session,
        tenant_id=tenant_id,
        actor=actor,
        action="更新客户AI配置失败",
        target_type="tenant_ai_setting",
        target_id=setting_id,
        detail=detail,
    )
    session.commit()


def _validation_error_code(message: str) -> str:
    codes = {
        "default AI provider not found": "default_provider_not_found",
        "default AI provider is disabled": "default_provider_disabled",
        "default AI provider is unhealthy": "default_provider_unhealthy",
        "provider route fallback requires at least two healthy active providers": (
            "provider_route_candidates_insufficient"
        ),
    }
    if message.startswith("最大 Token 超过当前模型上限"):
        return "max_tokens_exceeds_provider_limit"
    return codes.get(message, "tenant_ai_setting_validation_failed")


__all__ = ["get_tenant_ai_setting", "update_tenant_ai_setting"]
