from __future__ import annotations

import secrets

from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import Tenant
from app.models.enums import now
from app.security import decrypt_secret, encrypt_secret

from ._common import audit
from app.admin_chats import send_admin_chat_broadcast
from .notifications import NotificationResult, send_telegram_bot_message
from .telegram_bot_api import delete_telegram_webhook, get_telegram_webhook_info, set_telegram_webhook


TEST_MESSAGE_TEXT = "TG Bot 配置测试消息"
WEBHOOK_STATUS_NOT_CONFIGURED = "not_configured"
WEBHOOK_STATUS_REGISTERING = "registering"
WEBHOOK_STATUS_REGISTERED = "registered"
WEBHOOK_STATUS_REGISTRATION_FAILED = "registration_failed"
WEBHOOK_STATUS_URL_MISMATCH = "url_mismatch"
WEBHOOK_STATUS_QUERY_FAILED = "query_failed"
WEBHOOK_STATUS_DELETED = "deleted"


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
        "telegram_bot_webhook_current_url": tenant.telegram_bot_webhook_current_url,
        "telegram_bot_webhook_last_checked_at": tenant.telegram_bot_webhook_last_checked_at,
        "telegram_bot_webhook_status": tenant.telegram_bot_webhook_status or WEBHOOK_STATUS_NOT_CONFIGURED,
        "telegram_bot_last_error": tenant.telegram_bot_last_error,
        "notify_ai_failures_enabled": bool(tenant.notify_ai_failures_enabled),
    }


def update_tenant_bot_settings(session: Session, tenant_id: int, payload: dict, actor: str) -> dict:
    tenant = _tenant_or_error(session, tenant_id)
    data = dict(payload or {})
    if "admin_chat_id" in data and data["admin_chat_id"] is not None:
        tenant.admin_chat_id = str(data["admin_chat_id"]).strip()
    if "telegram_bot_token" in data and data["telegram_bot_token"] is not None:
        if not _update_bot_token(tenant, str(data["telegram_bot_token"]).strip()):
            _audit_bot_config_update(session, tenant, actor)
            session.commit()
            session.refresh(tenant)
            return tenant_bot_settings_payload(tenant)
    if "ai_group_bot_enabled" in data and data["ai_group_bot_enabled"] is not None:
        tenant.ai_group_bot_enabled = bool(data["ai_group_bot_enabled"])
    if "notify_ai_failures_enabled" in data and data["notify_ai_failures_enabled"] is not None:
        tenant.notify_ai_failures_enabled = bool(data["notify_ai_failures_enabled"])
    _ensure_webhook_secret(tenant)
    _sync_webhook_after_config_change(tenant)
    _audit_bot_config_update(session, tenant, actor)
    session.commit()
    session.refresh(tenant)
    return tenant_bot_settings_payload(tenant)


def refresh_tenant_bot_webhook(session: Session, tenant_id: int, actor: str) -> dict:
    tenant = _tenant_or_error(session, tenant_id)
    _ensure_webhook_secret(tenant)
    _sync_webhook_after_config_change(tenant)
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="刷新TG Bot Webhook",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=tenant.telegram_bot_webhook_status or "",
    )
    session.commit()
    session.refresh(tenant)
    return tenant_bot_settings_payload(tenant)


def delete_tenant_bot_webhook(session: Session, tenant_id: int, actor: str) -> dict:
    tenant = _tenant_or_error(session, tenant_id)
    token = _decrypted_bot_token(tenant)
    if token:
        result = delete_telegram_webhook(token)
        if not result.ok:
            _mark_webhook_failure(tenant, tenant.telegram_bot_webhook_status or WEBHOOK_STATUS_REGISTERED, result.detail)
            session.commit()
            session.refresh(tenant)
            return tenant_bot_settings_payload(tenant)
    _mark_webhook_deleted(tenant)
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="删除TG Bot Webhook",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=tenant.telegram_bot_last_error or WEBHOOK_STATUS_DELETED,
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
    summary = send_admin_chat_broadcast(
        bot_token=bot_token,
        raw_admin_chat_id=tenant.admin_chat_id,
        text=TEST_MESSAGE_TEXT,
        sender=send_telegram_bot_message,
    )
    result = NotificationResult(summary.ok, summary.detail)
    if not result.ok:
        tenant.telegram_bot_last_error = result.detail
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


def _audit_bot_config_update(session: Session, tenant: Tenant, actor: str) -> None:
    audit(
        session,
        tenant_id=tenant.id,
        actor=actor,
        action="更新TG Bot配置",
        target_type="tenant",
        target_id=str(tenant.id),
        detail=f"chat={bool(tenant.admin_chat_id)}; bot={tenant.telegram_bot_configured}; ai_group={tenant.ai_group_bot_enabled}",
    )


def _update_bot_token(tenant: Tenant, raw_token: str) -> bool:
    if raw_token:
        tenant.telegram_bot_token_ciphertext = encrypt_secret(raw_token)
        return True
    token = _decrypted_bot_token(tenant)
    if token:
        result = delete_telegram_webhook(token)
        if not result.ok:
            _mark_webhook_failure(tenant, tenant.telegram_bot_webhook_status or WEBHOOK_STATUS_REGISTERED, result.detail)
            return False
    tenant.telegram_bot_token_ciphertext = ""
    _mark_webhook_deleted(tenant)
    return True


def _sync_webhook_after_config_change(tenant: Tenant) -> None:
    if not tenant.telegram_bot_token_ciphertext or not tenant.admin_chat_id:
        _mark_webhook_not_configured(tenant)
        return
    expected_url = _webhook_url(tenant)
    token = _decrypted_bot_token(tenant)
    if not token:
        _mark_webhook_failure(tenant, WEBHOOK_STATUS_REGISTRATION_FAILED, "Telegram Bot token decrypts to empty")
        return
    if not expected_url:
        _mark_webhook_failure(tenant, WEBHOOK_STATUS_REGISTRATION_FAILED, "PUBLIC_APP_BASE_URL 未配置，无法生成公网 webhook URL")
        return
    tenant.telegram_bot_webhook_status = WEBHOOK_STATUS_REGISTERING
    set_result = set_telegram_webhook(token, expected_url)
    if not set_result.ok:
        _mark_webhook_failure(tenant, WEBHOOK_STATUS_REGISTRATION_FAILED, set_result.detail)
        return
    _verify_webhook_url(tenant, token, expected_url)


def _verify_webhook_url(tenant: Tenant, bot_token: str, expected_url: str) -> None:
    info = get_telegram_webhook_info(bot_token)
    tenant.telegram_bot_webhook_last_checked_at = now()
    current_url = str((info.data or {}).get("url") or "").strip()
    tenant.telegram_bot_webhook_current_url = current_url
    if not info.ok:
        _mark_webhook_failure(tenant, WEBHOOK_STATUS_QUERY_FAILED, info.detail)
        return
    if current_url != expected_url:
        detail = f"Telegram webhook URL 与系统期望不一致：expected={expected_url}; current={current_url or '-'}"
        _mark_webhook_failure(tenant, WEBHOOK_STATUS_URL_MISMATCH, detail)
        return
    tenant.telegram_bot_webhook_status = WEBHOOK_STATUS_REGISTERED
    tenant.telegram_bot_last_error = ""


def _mark_webhook_failure(tenant: Tenant, status: str, detail: str) -> None:
    tenant.telegram_bot_webhook_status = status
    tenant.telegram_bot_last_error = detail[:1000]


def _mark_webhook_not_configured(tenant: Tenant) -> None:
    tenant.telegram_bot_webhook_status = WEBHOOK_STATUS_NOT_CONFIGURED
    tenant.telegram_bot_webhook_current_url = ""
    tenant.telegram_bot_webhook_last_checked_at = now()
    tenant.telegram_bot_last_error = ""


def _mark_webhook_deleted(tenant: Tenant) -> None:
    tenant.telegram_bot_webhook_status = WEBHOOK_STATUS_DELETED
    tenant.telegram_bot_webhook_current_url = ""
    tenant.telegram_bot_webhook_last_checked_at = now()


def _decrypted_bot_token(tenant: Tenant) -> str:
    if not tenant.telegram_bot_token_ciphertext:
        return ""
    return decrypt_secret(tenant.telegram_bot_token_ciphertext).strip()


def _public_base_url() -> str:
    return get_settings().public_app_base_url


def _webhook_url(tenant: Tenant) -> str:
    base_url = _public_base_url()
    if not tenant.telegram_bot_webhook_secret or not base_url:
        return ""
    return f"{base_url}/api/telegram-bot/webhook/{tenant.id}/{tenant.telegram_bot_webhook_secret}"
