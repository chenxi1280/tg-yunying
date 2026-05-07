"""Shared helpers, singletons, and utility functions used across service modules."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.ai_gateway import create_ai_gateway
from app.auth import normalize_phone
from app.gateways import create_gateway
from app.models import (
    AuditLog,
    AppUser,
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

gateway = create_gateway()
ai_gateway = create_ai_gateway()


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _is_expired(value: datetime | None) -> bool:
    if value is None:
        return False
    return _as_utc(value) < _as_utc(_now())


def subscription_days_remaining(user: AppUser) -> int:
    if not user.subscription_expires_at:
        return 0
    remaining = user.subscription_expires_at - _now()
    return max(0, int((remaining.total_seconds() + 86399) // 86400))


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
