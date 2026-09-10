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

def _listener_credentials() -> DeveloperAppCredentials:
    return DeveloperAppCredentials(
        app_id=1,
        api_id=123,
        api_hash="hash",
        credentials_version=1,
    )


def _recording_run(observed: dict[str, float]):
    def run(coro, timeout_seconds=None):
        observed["rpc_timeout"] = timeout_seconds
        return asyncio.run(coro)

    return run


def test_channel_listener_fetch_caps_rpc_and_connect_timeouts(monkeypatch) -> None:
    gateway = TelethonTelegramGateway(Settings(
        listener_fetch_timeout_seconds=30,
        telethon_client_connect_timeout_seconds=15,
    ))
    observed: dict[str, float] = {}

    async def authorized(*_args, connect_timeout_seconds=None, **_kwargs):
        observed["connect_timeout"] = connect_timeout_seconds
        return object()

    async def fetch(*_args, **_kwargs):
        return []

    monkeypatch.setattr(gateway, "_authorized_client", authorized)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.telethon_content.fetch_channel_messages",
        fetch,
    )
    monkeypatch.setattr(gateway, "_run", _recording_run(observed))

    assert gateway.fetch_channel_messages(
        1,
        "@channel",
        session_ciphertext="session",
        credentials=_listener_credentials(),
    ) == []
    assert observed == {"connect_timeout": 5.0, "rpc_timeout": 10.0}


def test_group_listener_caller_cannot_expand_hard_timeouts(monkeypatch) -> None:
    gateway = TelethonTelegramGateway(Settings(
        listener_fetch_timeout_seconds=30,
        telethon_client_connect_timeout_seconds=15,
    ))
    observed: dict[str, float] = {}

    async def authorized(*_args, connect_timeout_seconds=None, **_kwargs):
        observed["connect_timeout"] = connect_timeout_seconds
        return object()

    async def fetch(*_args, **_kwargs):
        return []

    monkeypatch.setattr(gateway, "_authorized_client", authorized)
    monkeypatch.setattr(
        "app.integrations.telegram.gateway.telethon_content.fetch_group_messages",
        fetch,
    )
    monkeypatch.setattr(gateway, "_run", _recording_run(observed))

    assert gateway.fetch_group_messages(
        1,
        "@group",
        session_ciphertext="session",
        credentials=_listener_credentials(),
        timeout_seconds=60,
        connect_timeout_seconds=60,
    ) == []
    assert observed == {"connect_timeout": 5.0, "rpc_timeout": 10.0}


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
