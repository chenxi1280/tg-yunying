import asyncio
import hashlib
from dataclasses import replace

import pytest
from telethon.crypto import AuthKey
from telethon.sessions import StringSession

from app.config import Settings
from app.integrations.telegram import DeveloperAppCredentials
from app.telethon_lifecycle import TelethonClientLifecycle
from app.telegram_owner.session_identity import authorization_identity
from test_telethon_lifecycle import FakeTelethonClient, FailingDisconnectClient, reset_lifecycle_state

pytestmark = pytest.mark.no_postgres


def raw_session(label='account-a', address='149.154.167.51'):
    session = StringSession()
    session.set_dc(2, address, 443)
    session.auth_key = AuthKey(hashlib.sha256(label.encode()).digest() * 8)
    return session.save()


def credentials():
    return DeveloperAppCredentials(1, 123, 'test', 1)


@pytest.fixture(autouse=True)
def isolated_cache():
    reset_lifecycle_state()
    yield
    reset_lifecycle_state()


def test_authkey_identity_ignores_serialized_dc_address():
    assert authorization_identity(raw_session()) == authorization_identity(raw_session(address='149.154.167.50'))
    assert authorization_identity(raw_session()) != authorization_identity(raw_session('other'))


def test_simultaneous_requests_create_one_main_connection(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings())
    created = []

    class SlowClient(FakeTelethonClient):
        async def connect(self):
            await asyncio.sleep(0.01)
            await super().connect()

    def factory(*args):
        client = SlowClient('shared')
        created.append(client)
        return client

    monkeypatch.setattr(lifecycle, 'new_client', factory)

    async def run():
        return await asyncio.gather(*(lifecycle.get_or_create_client(credentials(), raw_session()) for _ in range(16)))

    results = asyncio.run(run())
    assert len(created) == 1
    assert all(result is created[0] for result in results)


def test_cancelled_waiter_does_not_cancel_shared_connect(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings())
    connected = asyncio.Event()

    class WaitingClient(FakeTelethonClient):
        async def connect(self):
            await connected.wait()
            await super().connect()

    client = WaitingClient('shared')
    monkeypatch.setattr(lifecycle, 'new_client', lambda *args: client)

    async def run():
        creator = asyncio.create_task(lifecycle.get_or_create_client(credentials(), raw_session()))
        await asyncio.sleep(0)
        waiter = asyncio.create_task(lifecycle.get_or_create_client(credentials(), raw_session()))
        await asyncio.sleep(0)
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        connected.set()
        return await creator

    assert asyncio.run(run()) is client
    assert client.connected


def test_failed_disconnect_keeps_original_and_blocks_replacement(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings())
    client = FailingDisconnectClient('original')
    created = []
    monkeypatch.setattr(lifecycle, 'new_client', lambda *args: created.append(client) or client)

    async def run():
        await lifecycle.get_or_create_client(credentials(), raw_session(), {'device_model': 'one'})
        with pytest.raises(ConnectionError, match='disconnect failed'):
            await lifecycle.get_or_create_client(credentials(), raw_session(), {'device_model': 'two'})
        with pytest.raises(ConnectionError, match='disconnect failed'):
            await lifecycle.get_or_create_client(credentials(), raw_session())

    asyncio.run(run())
    assert len(created) == 1
    assert len(lifecycle._cache) == 1


def test_active_authorization_is_not_evicted_or_pruned(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings(telethon_client_cache_size=1, telethon_client_idle_seconds=1))
    monkeypatch.setattr(lifecycle, 'new_client', lambda *args: FakeTelethonClient('client'))
    identities = [authorization_identity(raw_session(label)) for label in ('a', 'b')]

    async def run():
        lifecycle.pin_authorizations(identities)
        for label in ('a', 'b'):
            await lifecycle.get_or_create_client(credentials(), raw_session(label))
        for entry in lifecycle._cache.values():
            entry.last_used_at -= 100
        assert await lifecycle.prune_idle_clients() == 0
        assert len(lifecycle._cache) == 2
        lifecycle.unpin_authorizations(identities)
        assert await lifecycle.prune_idle_clients() == 2

    asyncio.run(run())


def test_same_authkey_with_other_app_fails_without_second_connection(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings())
    created = []
    monkeypatch.setattr(lifecycle, 'new_client', lambda *args: created.append(1) or FakeTelethonClient('one'))

    async def run():
        await lifecycle.get_or_create_client(credentials(), raw_session())
        with pytest.raises(ValueError, match='app_identity_conflict'):
            await lifecycle.get_or_create_client(replace(credentials(), api_id=456), raw_session())

    asyncio.run(run())
    assert created == [1]


def test_request_during_eviction_waits_for_disconnect(monkeypatch):
    lifecycle = TelethonClientLifecycle(Settings(telethon_client_idle_seconds=1))
    closing, closed = asyncio.Event(), asyncio.Event()
    created = []

    class ClosingClient(FakeTelethonClient):
        async def disconnect(self):
            closing.set()
            await closed.wait()
            await super().disconnect()

    def factory(*args):
        client = ClosingClient('old') if not created else FakeTelethonClient('new')
        created.append(client)
        return client

    monkeypatch.setattr(lifecycle, 'new_client', factory)

    async def run():
        await lifecycle.get_or_create_client(credentials(), raw_session())
        for entry in lifecycle._cache.values():
            entry.last_used_at -= 100
        pruning = asyncio.create_task(lifecycle.prune_idle_clients())
        await closing.wait()
        requesting = asyncio.create_task(lifecycle.get_or_create_client(credentials(), raw_session()))
        await asyncio.sleep(0)
        assert len(created) == 1 and not requesting.done()
        closed.set()
        assert await pruning == 1
        client = await requesting
        assert client is created[1] and not created[0].connected

    asyncio.run(run())
