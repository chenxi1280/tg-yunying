from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant
from app.services.tenant_bot_settings import (
    delete_tenant_bot_webhook,
    refresh_tenant_bot_webhook,
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
def test_tenant_bot_settings_registers_and_verifies_webhook(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    calls: list[tuple[str, str]] = []

    monkeypatch.setattr("app.services.tenant_bot_settings.encrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings._public_base_url", lambda: "https://tgyunying.telema.cn")
    monkeypatch.setattr(
        "app.services.tenant_bot_settings.set_telegram_webhook",
        lambda token, url: calls.append((token, url)) or _api_result(True, "set"),
    )
    monkeypatch.setattr(
        "app.services.tenant_bot_settings.get_telegram_webhook_info",
        lambda token: _api_result(True, "get", {"url": calls[-1][1]}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        payload = update_tenant_bot_settings(
            session,
            1,
            {"admin_chat_id": "1001", "telegram_bot_token": "bot-token"},
            "pytest",
        )

    assert calls == [("bot-token", payload["telegram_bot_webhook_url"])]
    assert payload["telegram_bot_webhook_status"] == "registered"
    assert payload["telegram_bot_webhook_url"].startswith("https://tgyunying.telema.cn/api/telegram-bot/webhook/1/")
    assert payload["telegram_bot_webhook_current_url"] == payload["telegram_bot_webhook_url"]
    assert payload["telegram_bot_webhook_last_checked_at"]


@pytest.mark.no_postgres
def test_tenant_bot_settings_records_webhook_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings._public_base_url", lambda: "https://tgyunying.telema.cn")
    monkeypatch.setattr("app.services.tenant_bot_settings.set_telegram_webhook", lambda _token, _url: _api_result(False, "bad token"))
    monkeypatch.setattr("app.services.tenant_bot_settings.get_telegram_webhook_info", lambda _token: _api_result(True, "unused", {"url": ""}))

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        payload = update_tenant_bot_settings(
            session,
            1,
            {"admin_chat_id": "1001", "telegram_bot_token": "bot-token"},
            "pytest",
        )

    assert payload["telegram_bot_webhook_status"] == "registration_failed"
    assert "bad token" in payload["telegram_bot_last_error"]
    assert payload["telegram_bot_webhook_current_url"] == ""


@pytest.mark.no_postgres
def test_tenant_bot_settings_records_webhook_url_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings._public_base_url", lambda: "https://tgyunying.telema.cn")
    monkeypatch.setattr("app.services.tenant_bot_settings.set_telegram_webhook", lambda _token, _url: _api_result(True, "set"))
    monkeypatch.setattr(
        "app.services.tenant_bot_settings.get_telegram_webhook_info",
        lambda _token: _api_result(True, "get", {"url": "https://old.example.com/api/telegram-bot/webhook/1/old"}),
    )

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.commit()

        payload = update_tenant_bot_settings(
            session,
            1,
            {"admin_chat_id": "1001", "telegram_bot_token": "bot-token"},
            "pytest",
        )

    assert payload["telegram_bot_webhook_status"] == "url_mismatch"
    assert payload["telegram_bot_webhook_current_url"] == "https://old.example.com/api/telegram-bot/webhook/1/old"
    assert "不一致" in payload["telegram_bot_last_error"]


@pytest.mark.no_postgres
def test_tenant_bot_test_message_does_not_mark_failed_webhook_registered(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fake_send(_bot_token: str, _chat_id: str, _text: str):
        from app.services.notifications import NotificationResult

        return NotificationResult(True, "sent")

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings._public_base_url", lambda: "https://tgyunying.telema.cn")
    monkeypatch.setattr("app.services.tenant_bot_settings.send_telegram_bot_message", fake_send)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001",
                telegram_bot_token_ciphertext="bot-token",
                telegram_bot_webhook_secret="secret-1",
                telegram_bot_webhook_status="registration_failed",
                telegram_bot_last_error="bad token",
            )
        )
        session.commit()

        result = send_tenant_bot_test_message(session, 1)
        payload = tenant_bot_settings_payload(session.get(Tenant, 1))

    assert result.ok is True
    assert payload["telegram_bot_webhook_status"] == "registration_failed"
    assert payload["telegram_bot_last_error"] == "bad token"


@pytest.mark.no_postgres
def test_tenant_bot_webhook_delete_updates_status(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    deleted: list[str] = []

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr(
        "app.services.tenant_bot_settings.delete_telegram_webhook",
        lambda token: deleted.append(token) or _api_result(True, "deleted"),
    )

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001",
                telegram_bot_token_ciphertext="bot-token",
                telegram_bot_webhook_secret="secret-1",
                telegram_bot_webhook_status="registered",
            )
        )
        session.commit()

        payload = delete_tenant_bot_webhook(session, 1, "pytest")

    assert deleted == ["bot-token"]
    assert payload["telegram_bot_webhook_status"] == "deleted"
    assert payload["telegram_bot_webhook_current_url"] == ""


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
    monkeypatch.setattr("app.services.tenant_bot_settings._public_base_url", lambda: "https://tgyunying.telema.cn")
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


@pytest.mark.no_postgres
def test_tenant_bot_test_message_sends_to_all_configured_admin_chats(monkeypatch: pytest.MonkeyPatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    sent: list[tuple[str, str, str]] = []

    def fake_send(bot_token: str, chat_id: str, text: str):
        from app.services.notifications import NotificationResult

        sent.append((bot_token, chat_id, text))
        return NotificationResult(True, f"ok:{chat_id}")

    monkeypatch.setattr("app.services.tenant_bot_settings.decrypt_secret", lambda value: value)
    monkeypatch.setattr("app.services.tenant_bot_settings.send_telegram_bot_message", fake_send)

    with Session(engine) as session:
        session.add(
            Tenant(
                id=1,
                name="默认运营空间",
                admin_chat_id="1001, 1002\n1003",
                telegram_bot_token_ciphertext="bot-token",
                telegram_bot_webhook_secret="secret-1",
                ai_group_bot_enabled=True,
            )
        )
        session.commit()

        result = send_tenant_bot_test_message(session, 1)

    assert result.ok is True
    assert sent == [
        ("bot-token", "1001", "TG Bot 配置测试消息"),
        ("bot-token", "1002", "TG Bot 配置测试消息"),
        ("bot-token", "1003", "TG Bot 配置测试消息"),
    ]


def _api_result(ok: bool, detail: str, data: dict | None = None):
    from app.services.telegram_bot_api import TelegramBotApiResult

    return TelegramBotApiResult(ok, detail, data or {})
