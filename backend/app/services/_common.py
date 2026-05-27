"""Shared helpers, singletons, and utility functions used across service modules."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_gateway import create_ai_gateway
from app.auth import CurrentUser, admin_user_payload, normalize_phone
from app.integrations.telegram import create_gateway
from app.models import (
    AuditLog,
    DeveloperAppHealthStatus,
    AiProviderHealthStatus,
    TelegramDeveloperApp,
    Tenant,
    TenantAiSetting,
    AiProvider,
    SchedulingSetting,
    TgAccount,
)
from app.config import get_settings
from app.security import decrypt_secret
from app.timezone import as_beijing_aware, beijing_now

gateway = create_gateway()
ai_gateway = create_ai_gateway()


def _now() -> datetime:
    return beijing_now()


def _as_beijing(value: datetime) -> datetime:
    return as_beijing_aware(value)


def _as_utc(value: datetime) -> datetime:
    """Compatibility alias: business datetimes are normalized to Beijing time."""
    return _as_beijing(value)


def _is_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    return _as_utc(value) < _as_utc(_now())


SUBSCRIPTION_INACTIVE_DETAIL = "subscription inactive"
ALL_FILTER_VALUES = {"all", "全部", "全部状态", "全部类型"}


def normalize_list_filter(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized.lower() in ALL_FILTER_VALUES:
        return None
    return normalized


def system_user_for_tenant(session: Session, tenant_id: int, *, service_name: str, missing_message: str) -> CurrentUser:
    data = admin_user_payload()
    data["name"] = service_name
    return CurrentUser(**data)


def require_system_user_core_features(session: Session, tenant_id: int, *, service_name: str, missing_message: str) -> CurrentUser:
    user = system_user_for_tenant(session, tenant_id, service_name=service_name, missing_message=missing_message)
    return user


def unique_tenant_name(session: Session, base_name: str) -> str:
    candidate = (base_name or "普通用户").strip()[:120] or "普通用户"
    if not session.scalar(select(Tenant.id).where(Tenant.name == candidate)):
        return candidate
    suffix = 2
    while True:
        name = f"{candidate}-{suffix}"
        if not session.scalar(select(Tenant.id).where(Tenant.name == name)):
            return name
        suffix += 1


def activation_plan_days(plan_type: str) -> int:
    if plan_type == "monthly":
        return 30
    if plan_type == "yearly":
        return 365
    raise ValueError("unsupported activation code plan")


def require_tenant(session: Session, tenant_id: int) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    return tenant


def get_or_error(session: Session, model: type, resource_id: int, message: str = "resource not found") -> object:
    """Fetch a resource by ID or raise ValueError. Used by services to avoid repetitive get+check patterns."""
    resource = session.get(model, resource_id)
    if not resource:
        raise ValueError(message)
    return resource


def audit(
    session: Session,
    *,
    tenant_id: int | None,
    actor: str,
    action: str,
    target_type: str,
    target_id: str,
    detail: str = "",
) -> None:
    session.add(
        AuditLog(
            tenant_id=tenant_id,
            actor=actor,
            action=action,
            target_type=target_type,
            target_id=target_id,
            detail=detail,
        )
    )


def mask_phone(phone_number: str) -> str:
    compact = phone_number.replace(" ", "")
    digits = [char for char in compact if char.isdigit()]
    if len(digits) < 7:
        return compact
    suffix = "".join(digits[-4:])
    prefix = compact[: max(3, len(compact) - 8)]
    return f"{prefix}****{suffix}"


def get_account_phone(account: TgAccount) -> str | None:
    if account.phone_ciphertext:
        return decrypt_secret(account.phone_ciphertext)
    return account.phone_masked


def get_runtime_config(session: Session | None = None) -> dict:
    settings = get_settings()
    app_count = 0
    healthy_count = 0
    ai_count = 0
    healthy_ai_count = 0
    mock_fallback_enabled = False
    if session is not None:
        app_count = session.scalar(select(func.count(TelegramDeveloperApp.id))) or 0
        healthy_count = (
            session.scalar(
                select(func.count(TelegramDeveloperApp.id)).where(
                    TelegramDeveloperApp.is_active.is_(True),
                    TelegramDeveloperApp.health_status == DeveloperAppHealthStatus.HEALTHY.value,
                )
            )
            or 0
        )
        ai_count = session.scalar(select(func.count(AiProvider.id))) or 0
        healthy_ai_count = (
            session.scalar(
                select(func.count(AiProvider.id)).where(
                    AiProvider.is_active.is_(True),
                    AiProvider.health_status == AiProviderHealthStatus.HEALTHY.value,
                )
            )
            or 0
        )
        mock_fallback_enabled = bool(
            session.scalar(select(func.count(TenantAiSetting.id)).where(TenantAiSetting.fallback_to_mock.is_(True)))
        )
    return {
        "app_env": settings.app_env,
        "queue_backend": settings.queue_backend,
        "tg_gateway_mode": settings.tg_gateway_mode,
        "telethon_configured": settings.telethon_configured,
        "sync_dispatch_fallback": settings.enable_sync_dispatch_fallback,
        "code_ttl_seconds": settings.login_code_ttl_seconds,
        "developer_app_pool_enabled": app_count > 0,
        "developer_app_count": app_count,
        "developer_app_healthy_count": healthy_count,
        "can_create_tg_account": healthy_count > 0,
        "has_ai_provider": ai_count > 0,
        "ai_enabled": healthy_ai_count > 0,
        "ai_provider_count": ai_count,
        "healthy_ai_provider_count": healthy_ai_count,
        "mock_ai_fallback_enabled": mock_fallback_enabled,
        "avatar_max_bytes": settings.avatar_max_bytes,
        "avatar_allowed_types": list(settings.avatar_allowed_types),
        "show_advanced_debug": False,
    }
