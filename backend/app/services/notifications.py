from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Tenant
from app.security import decrypt_secret

from ._common import audit


@dataclass(frozen=True)
class NotificationResult:
    ok: bool
    detail: str = ""


def send_telegram_bot_message(bot_token: str, chat_id: str, text: str) -> NotificationResult:
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = urllib.parse.urlencode(
        {
            "chat_id": chat_id,
            "text": text[:3500],
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = response.read().decode("utf-8", errors="ignore")
            data = json.loads(body or "{}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        return NotificationResult(False, f"Telegram Bot HTTP {exc.code}: {detail[:300]}")
    except Exception as exc:  # noqa: BLE001 - notification failures are non-blocking.
        return NotificationResult(False, str(exc))
    if not data.get("ok"):
        return NotificationResult(False, str(data)[:300])
    return NotificationResult(True, "sent")


def notify_ai_failure(
    session: Session,
    *,
    tenant_id: int,
    title: str,
    detail: str,
    target_type: str,
    target_id: str,
) -> NotificationResult:
    tenant = session.get(Tenant, tenant_id)
    if not tenant or not tenant.notify_ai_failures_enabled:
        return NotificationResult(True, "notification disabled")
    if not tenant.admin_chat_id or not tenant.telegram_bot_token_ciphertext:
        result = NotificationResult(False, "Telegram Bot token or admin chat id not configured")
        audit(
            session,
            tenant_id=tenant_id,
            actor="notification-service",
            action="AI失败通知失败",
            target_type=target_type,
            target_id=target_id,
            detail=result.detail,
        )
        return result
    bot_token = decrypt_secret(tenant.telegram_bot_token_ciphertext)
    if not bot_token:
        result = NotificationResult(False, "Telegram Bot token decrypts to empty")
        audit(
            session,
            tenant_id=tenant_id,
            actor="notification-service",
            action="AI失败通知失败",
            target_type=target_type,
            target_id=target_id,
            detail=result.detail,
        )
        return result
    message = f"{title}\n目标: {target_type} #{target_id}\n原因: {detail[:1200]}"
    result = send_telegram_bot_message(bot_token, tenant.admin_chat_id, message)
    audit(
        session,
        tenant_id=tenant_id,
        actor="notification-service",
        action="AI失败通知" if result.ok else "AI失败通知失败",
        target_type=target_type,
        target_id=target_id,
        detail=result.detail,
    )
    return result


__all__ = ["NotificationResult", "notify_ai_failure", "send_telegram_bot_message"]
