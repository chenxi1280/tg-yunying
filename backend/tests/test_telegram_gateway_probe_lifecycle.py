import asyncio
import sys
import threading
import time
import types
from types import SimpleNamespace
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

from app.config import Settings
from app.integrations.telegram import (
    AccountHealth,
    DeveloperAppCredentials,
    SendResult,
    TelethonTelegramGateway,
)
from app.telethon_lifecycle import (
    TelethonClientLifecycle,
    TelethonOperationTimeout,
    shutdown_telethon_lifecycle,
    shutdown_telethon_lifecycle_strict,
)


from test_telethon_lifecycle import reset_lifecycle_state

pytestmark = pytest.mark.no_postgres

@pytest.mark.no_postgres
def test_account_health_uses_dedicated_probe_timeout(monkeypatch):
    settings = Settings(account_online_probe_timeout_seconds=7)
    gateway = TelethonTelegramGateway(settings)
    observed = {}

    def run_probe(coro, timeout_seconds=None):
        coro.close()
        observed["timeout_seconds"] = timeout_seconds
        return AccountHealth(status="在线", health_score=95, detail="ok")

    monkeypatch.setattr(gateway._lifecycle, "run", run_probe)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    assert gateway.check_account_health("session", credentials).status == "在线"
    assert observed == {"timeout_seconds": 13}


@pytest.mark.no_postgres
def test_account_health_uses_ephemeral_client_and_disconnects(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())
    calls: list[str] = []

    class FakeClient:
        async def connect(self):
            calls.append("connect")

        async def is_user_authorized(self):
            calls.append("authorized")
            return True

        async def get_me(self):
            calls.append("get_me")

        async def __call__(self, request):
            from telethon import types as tl_types
            calls.append("get_app_config")
            return tl_types.help.AppConfig(1, tl_types.JsonObject([]))

        async def disconnect(self):
            calls.append("disconnect")

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(
        gateway,
        "_get_or_create_client",
        lambda *_args, **_kwargs: pytest.fail("health probe must not use the persistent client cache"),
    )
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    health = asyncio.run(gateway._health_async("encrypted-session", credentials))

    assert health.status == "在线"
    assert calls == ["connect", "authorized", "get_me", "get_app_config", "disconnect"]


@pytest.mark.no_postgres
def test_material_cache_uses_ephemeral_client_and_disconnects(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())
    calls: list[str] = []

    class FakeClient:
        async def connect(self):
            calls.append("connect")

        async def is_user_authorized(self):
            calls.append("authorized")
            return True

        async def disconnect(self):
            calls.append("disconnect")

    async def fake_cache(_client, source, peer, caption, _map_error):
        calls.append(f"cache:{source}:{peer}:{caption}")
        return SendResult(True, remote_message_id="cached-1")

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr("app.integrations.telegram.gateway.telethon_content.cache_material_source", fake_cache)
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(
        gateway,
        "_get_or_create_client",
        lambda *_args, **_kwargs: pytest.fail("material cache must not use the persistent client cache"),
    )
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    result = asyncio.run(
        gateway._cache_material_source_async(
            "encrypted-session",
            "/app/media/avatar.jpg",
            "cache-peer",
            "caption",
            credentials,
        )
    )

    assert result.ok is True
    assert calls == ["connect", "authorized", "cache:/app/media/avatar.jpg:cache-peer:caption", "disconnect"]


@pytest.mark.no_postgres
def test_account_health_isolated_runs_on_calling_thread(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())
    caller_thread = threading.get_ident()
    observed_threads: list[int] = []

    class FakeClient:
        async def connect(self):
            observed_threads.append(threading.get_ident())

        async def is_user_authorized(self):
            observed_threads.append(threading.get_ident())
            return True

        async def get_me(self):
            observed_threads.append(threading.get_ident())

        async def __call__(self, request):
            from telethon import types as tl_types
            observed_threads.append(threading.get_ident())
            return tl_types.help.AppConfig(1, tl_types.JsonObject([]))

        async def disconnect(self):
            observed_threads.append(threading.get_ident())

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr(
        gateway,
        "_run",
        lambda *_args, **_kwargs: pytest.fail("isolated probe must not use process lifecycle"),
    )
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    assert gateway.check_account_health_isolated("encrypted-session", credentials).status == "在线"
    assert observed_threads == [caller_thread] * 5


@pytest.mark.no_postgres
def test_account_health_isolated_keeps_outer_hard_deadline(monkeypatch):
    settings = Settings(account_online_probe_timeout_seconds=0.01)
    gateway = TelethonTelegramGateway(settings)

    class FakeClient:
        async def connect(self):
            return None

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                await asyncio.sleep(0.15)
                raise

        async def disconnect(self):
            return None

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    monkeypatch.setattr("app.integrations.telegram.gateway.ACCOUNT_HEALTH_DISCONNECT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr("app.integrations.telegram.gateway.ACCOUNT_HEALTH_RUN_GRACE_SECONDS", 0.01)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    started = time.monotonic()

    with pytest.raises(TimeoutError):
        gateway.check_account_health_isolated("encrypted-session", credentials)

    assert time.monotonic() - started < 0.1


def test_account_health_preserves_probe_error_when_disconnect_fails(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())

    class FakeClient:
        async def connect(self):
            raise ConnectionError("probe-connect-error")

        async def disconnect(self):
            raise RuntimeError("cleanup-disconnect-error")

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    with pytest.raises(ConnectionError, match="probe-connect-error"):
        asyncio.run(gateway._health_async("encrypted-session", credentials))


def test_account_health_timeout_waits_for_bounded_disconnect(monkeypatch):
    reset_lifecycle_state()
    calls: list[str] = []
    settings = Settings(account_online_probe_timeout_seconds=0.01)
    gateway = TelethonTelegramGateway(settings)

    class FakeClient:
        async def connect(self):
            calls.append("connect")

        async def is_user_authorized(self):
            return True

        async def get_me(self):
            await asyncio.sleep(60)

        async def disconnect(self):
            calls.append("disconnect_start")
            await asyncio.sleep(0.02)
            calls.append("disconnect_done")

    monkeypatch.setattr("app.integrations.telegram.gateway.decrypt_session", lambda _value: "raw-session")
    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    try:
        with pytest.raises(FutureTimeoutError):
            gateway.check_account_health("encrypted-session", credentials)
        assert calls == ["connect", "disconnect_start", "disconnect_done"]
    finally:
        shutdown_telethon_lifecycle(timeout_seconds=1)
