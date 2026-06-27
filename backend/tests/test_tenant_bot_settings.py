from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant
from app.services.tenant_bot_settings import (
    resolve_tenant_bot_webhook,
    send_tenant_bot_test_message,
    tenant_bot_settings_payload,
    update_tenant_bot_settings,
)


@pytest.mark.no_postgres
def test_tenant_bot_settings_generates_secret_and_hides_token() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        payload = update_tenant_bot_settings(
            session,
            1,
            {
                "admin_chat_id": " 1001 ",
                "telegram_bot_token": "bot-token",
                "ai_group_bot_enabled": True,
            },
            "pytest",
        )

    assert payload["telegram_bot_configured"] is True
    assert payload["telegram_bot_token_configured"] is True
    assert payload["telegram_bot_token_preview"] == "已配置"
    assert payload["telegram_bot_token"] is None
    assert payload["admin_chat_id"] == "1001"
    assert payload["ai_group_bot_enabled"] is True
    assert payload["telegram_bot_webhook_secret"]


@pytest.mark.no_postgres
def test_tenant_bot_webhook_resolves_only_matching_secret() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001",
                telegram_bot_token_ciphertext="encrypted",
                telegram_bot_webhook_secret="secret-1",
                ai_group_bot_enabled=True,
            )
        )
        session.commit()

        tenant = resolve_tenant_bot_webhook(session, 1, "secret-1")
        with pytest.raises(PermissionError):
            resolve_tenant_bot_webhook(session, 1, "bad-secret")

    assert tenant.id == 1


@pytest.mark.no_postgres
def test_tenant_bot_test_message_uses_configured_admin_chat(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sent: list[tuple[str, str, str]] = []

    def fake_send(bot_token: str, chat_id: str, text: str):
        from app.services.notifications import NotificationResult

        sent.append((bot_token, chat_id, text))
        return NotificationResult(True, "ok")

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings.send_telegram_bot_message", fake_send)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001",
                telegram_bot_token_ciphertext="bot-token",
                telegram_bot_webhook_secret="secret-1",
                ai_group_bot_enabled=True,
            )
        )
        session.commit()

        result = send_tenant_bot_test_message(session, 1)
        payload = tenant_bot_settings_payload(session.get(Tenant, 1))

    assert result.ok is True
    assert sent == [("bot-token", "1001", "TG Bot 配置测试消息")]
    assert payload["telegram_bot_webhook_url"].endswith("/api/telegram-bot/webhook/1/secret-1")
