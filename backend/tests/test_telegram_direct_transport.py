from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials
from app.services.developer_apps import credentials_for_developer_app
from app.telethon_lifecycle import TelethonClientLifecycle

pytestmark = pytest.mark.no_postgres


def _credentials(**values):
    return DeveloperAppCredentials(
        app_id=1, api_id=123, api_hash="test", credentials_version=1, **values,
    )


@pytest.mark.parametrize("fields", [
    {"proxy_protocol": "socks5"}, {"proxy_id": 7}, {"proxy_host": "127.0.0.1"}, {"proxy_port": 1080},
    {"proxy_username": "test"}, {"proxy_password": "test"},
])
def test_proxy_credentials_cannot_create_or_reuse_client(monkeypatch, fields):
    lifecycle = TelethonClientLifecycle(Settings())
    monkeypatch.setattr(lifecycle, "_assert_remote_io_allowed", lambda: None)
    def must_not_create(*args, **kwargs):
        raise AssertionError("proxy credentials reached client construction")
    credentials = _credentials(**fields)
    with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
        lifecycle.new_client(credentials, "unused")
    monkeypatch.setattr(lifecycle, "new_client", must_not_create)
    with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
        asyncio.run(lifecycle.get_or_create_client(credentials, "unused"))


def test_direct_client_has_no_proxy(monkeypatch):
    seen = {}
    monkeypatch.setattr("telethon.TelegramClient", lambda *args, **kw: seen.update(kw))
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value: value)
    lifecycle = TelethonClientLifecycle(Settings())
    monkeypatch.setattr(lifecycle, "_assert_remote_io_allowed", lambda: None)
    lifecycle.new_client(_credentials(), "unused")
    assert seen["proxy"] is None


def test_explicit_proxy_selection_is_rejected_without_secret_decryption(monkeypatch):
    app = SimpleNamespace(is_active=True, health_status="健康", api_hash_ciphertext="unused")
    monkeypatch.setattr("app.services.developer_apps.decrypt_secret", lambda value: pytest.fail("proxy selection reached secret decryption"))
    app.id, app.api_id, app.credentials_version, app.app_name = 1, 123, 1, "test"
    with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
        credentials_for_developer_app(app, SimpleNamespace(id=7))
