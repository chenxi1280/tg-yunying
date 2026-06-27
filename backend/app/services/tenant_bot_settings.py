from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.models import Tenant
from app.security import decrypt_secret, encrypt_secret

from ._common import audit
from .notifications import NotificationResult, send_telegram_bot_message


TEST_MESSAGE_TEXT = "TG Bot 配置测试消息"
WEBHOOK_STATUS_NOT_CONFIGURED = "not_configured"


def tenant_bot_settings_payload(tenant: Tenant) -> dict:
    configured = bool(tenant.telegram_bot_token_ciphertext)
    return {
        "tenant_id": tenant.id,
        "admin_chat_id": tenant.admin_chat_id,
        "telegram_bot_configured": configured,
        "telegram_bot_token_configured": configured,
        "telegram_bot_token_preview": "已配置" if configured else "",
        "telegram_bot_token": None,
        "ai_group_bot_enabled": bool(getattr(tenant, "ai_group_bot_enabled", False)),
        "telegram_bot_webhook_secret": tenant.telegram_bot_webhook_secret,
        "telegram_bot_webhook_url": _webhook_url(tenant),
        "telegram_bot_webhook_status": tenant.telegram_bot_webhook_status or WEBHOOK_STATUS_NOT_CONFIGURED,
        "telegram_bot_last_error": tenant.telegram_bot_last_error,
        "notify_ai_failures_enabled": bool(tenant.notify_ai_failures_enabled),
    }


def update_tenant_bot_settings(session: Session, tenant_id: int, payload: dict, actor: str) -> dict:
    tenant = _tenant_or_error(session, tenant_id)
    data = dict(payload or {})
    if "admin_chat_id" in data and data["admin_chat_id"] is not None:
        tenant.admin_chat_id = str(data["admin_chat_id"]).strip()
    if data.get("telegram_bot_token"):
        tenant.telegram_bot_token_ciphertext = encrypt_secret(str(data["telegram_bot_token"]).strip())
    if "ai_group_bot_enabled" in data and data["ai_group_bot_enabled"] is not None:
        tenant.ai_group_bot_enabled = bool(data["ai_group_bot_enabled"])
    if "notify_ai_failures_enabled" in data and data["notify_ai_failures_enabled"] is not None:
        tenant.notify_ai_failures_enabled = bool(data["notify_ai_failures_enabled"])
    _ensure_webhook_secret(tenant)
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="更新TG Bot配置",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=f"chat={bool(tenant.admin_chat_id)}; bot={tenant.telegram_bot_configured}; ai_group={tenant.ai_group_bot_enabled}",
    )
    session.commit()
    session.refresh(tenant)
    return tenant_bot_settings_payload(tenant)


def send_tenant_bot_test_message(session: Session, tenant_id: int) -> NotificationResult:
    tenant = _tenant_or_error(session, tenant_id)
    if not tenant.admin_chat_id or not tenant.telegram_bot_token_ciphertext:
        raise ValueError("Telegram Bot token or admin chat id not configured")
    bot_token = decrypt_secret(tenant.telegram_bot_token_ciphertext)
    if not bot_token:
        raise ValueError("Telegram Bot token decrypts to empty")
    result = send_telegram_bot_message(bot_token, tenant.admin_chat_id, TEST_MESSAGE_TEXT)
    tenant.telegram_bot_last_error = "" if result.ok else result.detail
    audit(
        session,
        tenant_id=tenant.id,
        actor="tenant-bot-settings",
        action="TG Bot测试发送" if result.ok else "TG Bot测试发送失败",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=result.detail,
    )
    session.commit()
    return result


def resolve_tenant_bot_webhook(session: Session, tenant_id: int, webhook_secret: str) -> Tenant:
    tenant = _tenant_or_error(session, tenant_id)
    if not tenant.telegram_bot_webhook_secret or webhook_secret != tenant.telegram_bot_webhook_secret:
        raise PermissionError("Telegram Bot webhook secret invalid")
    if not tenant.telegram_bot_configured:
        raise PermissionError("Telegram Bot 未配置")
    return tenant


def _tenant_or_error(session: Session, tenant_id: int) -> Tenant:
    tenant = session.get(Tenant, tenant_id)
    if not tenant:
        raise ValueError("tenant not found")
    return tenant


def _ensure_webhook_secret(tenant: Tenant) -> None:
    if not tenant.telegram_bot_webhook_secret:
        tenant.telegram_bot_webhook_secret = secrets.token_urlsafe(24)[:48]
    if not tenant.telegram_bot_webhook_status:
        tenant.telegram_bot_webhook_status = WEBHOOK_STATUS_NOT_CONFIGURED


def _webhook_url(tenant: Tenant) -> str:
    if not tenant.telegram_bot_webhook_secret:
        return ""
    return f"/api/telegram-bot/webhook/{tenant.id}/{tenant.telegram_bot_webhook_secret}"
