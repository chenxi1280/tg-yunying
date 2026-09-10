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

pytestmark = pytest.mark.no_postgres


@pytest.fixture(autouse=True)
def synthetic_session_identity(monkeypatch):
    import hashlib

    monkeypatch.setattr("app.telegram_owner.session_identity.authorization_identity",
                        lambda raw: hashlib.sha256(raw.encode()).hexdigest())


class FakeTelethonClient:
    def __init__(self, name: str) -> None:
        self.name = name
        self.connected = False
        self.disconnect_count = 0

    async def connect(self) -> None:
        self.connected = True

    def is_connected(self) -> bool:
        return self.connected

    async def disconnect(self) -> None:
        self.connected = False
        self.disconnect_count += 1


class FailingConnectClient(FakeTelethonClient):
    async def connect(self) -> None:
        self.connected = True
        raise ConnectionError("Connection to Telegram failed 5 time(s)")


class FailingDisconnectClient(FakeTelethonClient):
    fail_disconnect = True

    async def disconnect(self) -> None:
        self.disconnect_count += 1
        if self.fail_disconnect:
            raise ConnectionError("disconnect failed")
        self.connected = False


def reset_lifecycle_state() -> None:
    TelethonClientLifecycle._cache.clear()
    TelethonClientLifecycle._creating.clear()
    TelethonClientLifecycle._active_keys.clear()
    TelethonClientLifecycle._loop = None
    TelethonClientLifecycle._loop_thread = None
    TelethonClientLifecycle.set_runtime_role("all")


def test_planner_role_cannot_create_telethon_runtime() -> None:
    reset_lifecycle_state()
    lifecycle = TelethonClientLifecycle(Settings())
    TelethonClientLifecycle.set_runtime_role("planner")

    async def remote_operation():
        return "unexpected"

    with pytest.raises(RuntimeError, match="planner_remote_io_forbidden"):
        lifecycle.run(remote_operation())

    assert TelethonClientLifecycle._loop is None
    TelethonClientLifecycle.set_runtime_role("all")


def test_telethon_lifecycle_enforces_cache_limit(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=1,
        telethon_client_idle_seconds=3600,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    clients: list[FakeTelethonClient] = []

    def fake_new_client(_credentials, raw_session, client_metadata=None):
        client = FakeTelethonClient(raw_session or "")
        clients.append(client)
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        first = await lifecycle.get_or_create_client(credentials, "session-1")
        second = await lifecycle.get_or_create_client(credentials, "session-2")
        return first, second

    first_client, second_client = asyncio.run(scenario())

    assert first_client.disconnect_count == 1
    assert second_client.is_connected() is True
    assert len(TelethonClientLifecycle._cache) == 1


def test_telethon_lifecycle_uses_attempt_connect_timeout_override(monkeypatch):
    reset_lifecycle_state()
    lifecycle = TelethonClientLifecycle(
        Settings(telethon_client_connect_timeout_seconds=15),
    )
    credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
    )
    client = FakeTelethonClient("session-override")
    observed: list[float] = []
    original_wait_for = asyncio.wait_for

    async def record_wait_for(awaitable, timeout):
        observed.append(float(timeout))
        return await original_wait_for(awaitable, timeout=timeout)

    monkeypatch.setattr(lifecycle, "new_client", lambda *_args, **_kwargs: client)
    monkeypatch.setattr("app.telethon_lifecycle.asyncio.wait_for", record_wait_for)

    asyncio.run(
        lifecycle.get_or_create_client(
            credentials,
            "session-override",
            connect_timeout_seconds=5,
        ),
    )

    assert observed == [5.0]
    assert client.is_connected() is True


def test_telethon_lifecycle_passes_client_metadata_to_new_client(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    captured: dict[str, object] = {}

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured.update(kwargs)

    metadata = {
        "device_model": "iPhone 15",
        "system_version": "iOS 17.5",
        "app_version": "10.14.1",
        "lang_code": "zh",
        "system_lang_code": "zh-CN",
        "platform": "ios",
        "client_identity_key": "identity-1",
    }
    monkeypatch.setattr("telethon.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value="": f"session:{value}")

    lifecycle.new_client(credentials, "raw", metadata)

    assert captured["device_model"] == "iPhone 15"
    assert captured["system_version"] == "iOS 17.5"
    assert captured["app_version"] == "10.14.1"
    assert captured["lang_code"] == "zh"
    assert captured["system_lang_code"] == "zh-CN"
    assert "platform" not in captured


def test_metadata_change_disconnects_original_before_replacement(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=10,
        telethon_client_idle_seconds=3600,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    clients: list[FakeTelethonClient] = []

    def fake_new_client(_credentials, raw_session, client_metadata=None):
        client = FakeTelethonClient(f"{raw_session}:{client_metadata['client_identity_key']}")
        clients.append(client)
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        first = await lifecycle.get_or_create_client(credentials, "same-session", {"client_identity_key": "one"})
        second = await lifecycle.get_or_create_client(credentials, "same-session", {"client_identity_key": "two"})
        return first, second

    first_client, second_client = asyncio.run(scenario())

    assert first_client is not second_client
    assert not first_client.is_connected()
    assert len(TelethonClientLifecycle._cache) == 1
    assert [client.name for client in clients] == ["same-session:one", "same-session:two"]


def test_invalidate_removes_one_authkey_owner_and_preserves_other_authorizations(monkeypatch):
    """RC-6.4：invalidate 必须清掉同 session 的全部 metadata 变体，且不误删其他 session。"""
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=10,
        telethon_client_idle_seconds=3600,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    other_credentials = DeveloperAppCredentials(app_id=1, api_id=456, api_hash="hash", credentials_version=1)
    created: dict[str, FakeTelethonClient] = {}

    def fake_new_client(_credentials, raw_session, client_metadata=None):
        identity = (client_metadata or {}).get("client_identity_key") or "plain"
        client = FakeTelethonClient(f"{raw_session}:{identity}")
        created[f"{raw_session}:{int(_credentials.api_id)}:{identity}"] = client
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        await lifecycle.get_or_create_client(credentials, "session-a", {"client_identity_key": "one"})
        await lifecycle.get_or_create_client(credentials, "session-a", {"client_identity_key": "two"})
        await lifecycle.get_or_create_client(credentials, "session-a")
        await lifecycle.get_or_create_client(other_credentials, "session-b", {"client_identity_key": "one"})
        removed = await lifecycle.invalidate_client(credentials, "session-a")
        return removed

    removed = asyncio.run(scenario())

    assert removed == 1
    remaining_keys = list(TelethonClientLifecycle._cache)
    assert len(remaining_keys) == 1
    remaining_entry = TelethonClientLifecycle._cache[remaining_keys[0]]
    assert remaining_entry.client.name == "session-b:one"
    for key in ("session-a:123:one", "session-a:123:two"):
        assert created[key].disconnect_count >= 1
    assert created["session-b:456:one"].disconnect_count == 0


def test_telethon_lifecycle_rejects_unknown_proxy_protocol(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        proxy_protocol="ftp",
        proxy_host="127.0.0.1",
        proxy_port=1080,
    )

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            raise AssertionError("unsupported proxy protocol must fail before client creation")

    monkeypatch.setattr("telethon.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value="": f"session:{value}")

    with pytest.raises(ValueError, match="telegram_account_proxy_forbidden"):
        lifecycle.new_client(credentials, "raw")


def test_telethon_lifecycle_prunes_idle_clients(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=10,
        telethon_client_idle_seconds=1,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    clients: list[FakeTelethonClient] = []

    def fake_new_client(_credentials, raw_session, client_metadata=None):
        client = FakeTelethonClient(raw_session or "")
        clients.append(client)
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        client = await lifecycle.get_or_create_client(credentials, "session-1")
        for entry in TelethonClientLifecycle._cache.values():
            entry.last_used_at -= 3600
        pruned = await lifecycle.prune_idle_clients()
        return client, pruned

    client, pruned = asyncio.run(scenario())

    assert pruned == 1
    assert client.disconnect_count == 1
    assert TelethonClientLifecycle._cache == {}


def test_telethon_lifecycle_disconnects_new_client_after_connect_failure(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=10,
        telethon_client_idle_seconds=3600,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    clients: list[FailingConnectClient] = []

    def fake_new_client(_credentials, raw_session, client_metadata=None):
        client = FailingConnectClient(raw_session or "")
        clients.append(client)
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        with pytest.raises(ConnectionError, match="Connection to Telegram failed"):
            await lifecycle.get_or_create_client(credentials, "bad-session")

    asyncio.run(scenario())

    assert clients[0].disconnect_count == 1
    assert TelethonClientLifecycle._cache == {}


def test_shutdown_telethon_lifecycle_stops_background_loop():
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)

    assert lifecycle.run(asyncio.sleep(0, result="ok")) == "ok"
    assert TelethonClientLifecycle._loop is not None

    assert shutdown_telethon_lifecycle(timeout_seconds=1) == 0
    assert TelethonClientLifecycle._loop is None
    assert TelethonClientLifecycle._loop_thread is None


def test_strict_shutdown_keeps_failed_client_and_loop_for_retry(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
    )
    client = FailingDisconnectClient("strict")
    monkeypatch.setattr(lifecycle, "new_client", lambda *_args, **_kwargs: client)
    lifecycle.run(lifecycle.get_or_create_client(credentials, "session"))

    with pytest.raises(RuntimeError, match="disconnect failed"):
        shutdown_telethon_lifecycle_strict(timeout_seconds=1)

    assert len(TelethonClientLifecycle._cache) == 1
    assert TelethonClientLifecycle._loop is not None
    client.fail_disconnect = False
    assert shutdown_telethon_lifecycle_strict(timeout_seconds=1) == 1
    assert TelethonClientLifecycle._cache == {}


def test_telethon_lifecycle_cancels_coroutine_after_operation_timeout():
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)
    cancelled = threading.Event()

    async def slow_operation():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    with pytest.raises(TelethonOperationTimeout) as captured:
        lifecycle.run(slow_operation(), timeout_seconds=0.01)

    assert captured.value.transport_termination_acknowledged is True
    assert cancelled.wait(timeout=1)
    shutdown_telethon_lifecycle(timeout_seconds=1)


def test_telethon_timeout_reports_runner_that_ignores_initial_cancellation():
    reset_lifecycle_state()
    lifecycle = TelethonClientLifecycle(
        Settings(telethon_operation_timeout_seconds=1)
    )
    finished = threading.Event()

    async def cancellation_delayed_operation():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
        finally:
            finished.set()

    with pytest.raises(TelethonOperationTimeout) as captured:
        lifecycle.run(cancellation_delayed_operation(), timeout_seconds=0.01)

    assert captured.value.transport_termination_acknowledged is False
    assert finished.wait(timeout=1)
    assert captured.value.termination_event.wait(timeout=1)
    shutdown_telethon_lifecycle(timeout_seconds=1)
