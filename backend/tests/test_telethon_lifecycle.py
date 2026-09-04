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


def test_telethon_lifecycle_invalidate_removes_all_metadata_variants(monkeypatch):
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

    assert removed == 3
    remaining_keys = list(TelethonClientLifecycle._cache)
    assert len(remaining_keys) == 1
    remaining_entry = TelethonClientLifecycle._cache[remaining_keys[0]]
    assert remaining_entry.client.name == "session-b:one"
    for key in ("session-a:123:one", "session-a:123:two", "session-a:123:plain"):
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


def test_code_login_persists_and_reuses_exact_flow_challenge(monkeypatch):
    gateway = TelethonTelegramGateway(Settings(login_code_ttl_seconds=300))
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    created_with_sessions: list[str | None] = []
    sign_in_calls: list[dict[str, object]] = []

    class FakeSession:
        def __init__(self, value: str):
            self.value = value

        def save(self):
            return self.value

    class FakeClient:
        def __init__(self, raw_session: str | None):
            self.session = FakeSession(raw_session or "temporary-flow-session")

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def send_code_request(self, phone):
            return SimpleNamespace(phone_code_hash="flow-phone-code-hash")

        async def sign_in(self, **kwargs):
            sign_in_calls.append(kwargs)

    def fake_new_client(_credentials, raw_session=None, _client_metadata=None):
        created_with_sessions.append(raw_session)
        return FakeClient(raw_session)

    monkeypatch.setattr(gateway, "_new_client", fake_new_client)

    challenge = asyncio.run(gateway._start_login_async(77, "code", "+10000000000", credentials))
    status, raw_session = asyncio.run(
        gateway._finish_login_async(
            77,
            "12345",
            None,
            "+10000000000",
            credentials,
            challenge.temporary_session,
            challenge.phone_code_hash,
        )
    )

    assert challenge.temporary_session == "temporary-flow-session"
    assert challenge.phone_code_hash == "flow-phone-code-hash"
    assert created_with_sessions == [None, "temporary-flow-session"]
    assert sign_in_calls == [{"phone": "+10000000000", "code": "12345", "phone_code_hash": "flow-phone-code-hash"}]
    assert status == "在线"
    assert raw_session == "temporary-flow-session"


def test_code_login_persists_session_when_two_fa_is_required(monkeypatch):
    from telethon.errors import SessionPasswordNeededError

    gateway = TelethonTelegramGateway(Settings(login_code_ttl_seconds=300))
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)

    class FakeSession:
        def save(self):
            return "two-fa-temporary-session"

    class FakeClient:
        session = FakeSession()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def sign_in(self, **_kwargs):
            raise SessionPasswordNeededError(None)

    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())

    status, raw_session = asyncio.run(
        gateway._finish_code_login_async("12345", None, "+10000000000", credentials, "temporary", "hash")
    )

    assert status == "等待2FA"
    assert raw_session == "two-fa-temporary-session"


def test_code_login_submits_code_before_available_two_fa_password(monkeypatch):
    from telethon.errors import SessionPasswordNeededError

    gateway = TelethonTelegramGateway(Settings(login_code_ttl_seconds=300))
    credentials = DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1)
    sign_in_calls: list[dict] = []

    class FakeSession:
        def save(self):
            return "authorized-session"

    class FakeClient:
        session = FakeSession()

        async def connect(self):
            return None

        async def disconnect(self):
            return None

        async def sign_in(self, **kwargs):
            sign_in_calls.append(kwargs)
            if "code" in kwargs:
                raise SessionPasswordNeededError(None)

    monkeypatch.setattr(gateway, "_new_client", lambda *_args, **_kwargs: FakeClient())

    status, raw_session = asyncio.run(
        gateway._finish_code_login_async("12345", "2fa", "+10000000000", credentials, "temporary", "hash")
    )

    assert sign_in_calls == [
        {"phone": "+10000000000", "code": "12345", "phone_code_hash": "hash"},
        {"password": "2fa"},
    ]
    assert status == "在线"
    assert raw_session == "authorized-session"


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
