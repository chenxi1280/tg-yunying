import asyncio
import sys
import threading
import time
import types
from concurrent.futures import TimeoutError as FutureTimeoutError

import pytest

from app.config import Settings
from app.integrations.telegram import AccountHealth, DeveloperAppCredentials, TelethonTelegramGateway
from app.telethon_lifecycle import TelethonClientLifecycle, shutdown_telethon_lifecycle

pytestmark = pytest.mark.no_postgres


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


def reset_lifecycle_state() -> None:
    TelethonClientLifecycle._cache.clear()
    TelethonClientLifecycle._loop = None
    TelethonClientLifecycle._loop_thread = None


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


def test_telethon_lifecycle_cache_key_includes_proxy(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(
        telethon_client_cache_size=10,
        telethon_client_idle_seconds=3600,
        telethon_client_connect_timeout_seconds=1,
        telethon_operation_timeout_seconds=1,
    )
    lifecycle = TelethonClientLifecycle(settings)
    first_credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        proxy_id=31,
        proxy_protocol="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
    )
    second_credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        proxy_id=32,
        proxy_protocol="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1081,
    )
    clients: list[FakeTelethonClient] = []

    def fake_new_client(credentials, raw_session, client_metadata=None):
        client = FakeTelethonClient(f"{credentials.proxy_id}:{raw_session}")
        clients.append(client)
        return client

    monkeypatch.setattr(lifecycle, "new_client", fake_new_client)

    async def scenario():
        first = await lifecycle.get_or_create_client(first_credentials, "same-session")
        second = await lifecycle.get_or_create_client(second_credentials, "same-session")
        return first, second

    first_client, second_client = asyncio.run(scenario())

    assert first_client is not second_client
    assert [client.name for client in clients] == ["31:same-session", "32:same-session"]


def test_telethon_lifecycle_passes_proxy_to_new_client(monkeypatch):
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)
    credentials = DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
        proxy_id=31,
        proxy_protocol="socks5",
        proxy_host="127.0.0.1",
        proxy_port=1080,
        proxy_username="user",
        proxy_password="pass",
    )
    captured: dict[str, object] = {}

    class FakeTelegramClient:
        def __init__(self, session, api_id, api_hash, **kwargs):
            captured.update({"session": session, "api_id": api_id, "api_hash": api_hash, **kwargs})

    monkeypatch.setattr("telethon.TelegramClient", FakeTelegramClient)
    monkeypatch.setattr("telethon.sessions.StringSession", lambda value="": f"session:{value}")
    monkeypatch.setitem(sys.modules, "socks", types.SimpleNamespace(SOCKS5=1, SOCKS4=2, HTTP=3))

    lifecycle.new_client(credentials, "raw")

    assert captured["api_id"] == 123
    assert captured["api_hash"] == "hash"
    assert captured["proxy"][1:] == ("127.0.0.1", 1080, True, "user", "pass")


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


def test_telethon_lifecycle_cache_key_includes_client_metadata(monkeypatch):
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
    assert [client.name for client in clients] == ["same-session:one", "same-session:two"]


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

    with pytest.raises(ValueError, match="不支持的代理协议"):
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

    with pytest.raises(FutureTimeoutError):
        lifecycle.run(slow_operation(), timeout_seconds=0.01)

    assert cancelled.wait(timeout=1)
    shutdown_telethon_lifecycle(timeout_seconds=1)


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
    assert calls == ["connect", "authorized", "get_me", "disconnect"]


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
    assert observed_threads == [caller_thread] * 4


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
