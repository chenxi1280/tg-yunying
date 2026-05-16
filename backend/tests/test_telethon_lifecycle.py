import asyncio

from app.config import Settings
from app.gateways import DeveloperAppCredentials
from app.telethon_lifecycle import TelethonClientLifecycle, shutdown_telethon_lifecycle


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

    def fake_new_client(_credentials, raw_session):
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

    def fake_new_client(_credentials, raw_session):
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


def test_shutdown_telethon_lifecycle_stops_background_loop():
    reset_lifecycle_state()
    settings = Settings(telethon_operation_timeout_seconds=1)
    lifecycle = TelethonClientLifecycle(settings)

    assert lifecycle.run(asyncio.sleep(0, result="ok")) == "ok"
    assert TelethonClientLifecycle._loop is not None

    assert shutdown_telethon_lifecycle(timeout_seconds=1) == 0
    assert TelethonClientLifecycle._loop is None
    assert TelethonClientLifecycle._loop_thread is None
