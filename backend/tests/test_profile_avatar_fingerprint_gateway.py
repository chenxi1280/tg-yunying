from __future__ import annotations

import asyncio
import hashlib
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image

import app.integrations.telegram.gateway as gateway_module
from app.config import Settings
from app.integrations.telegram.contracts import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway


pytestmark = pytest.mark.no_postgres


class _AvatarClient:
    def __init__(self, data: bytes | None) -> None:
        self.data = data

    async def get_me(self):
        photo = SimpleNamespace(photo_id=991) if self.data is not None else None
        return SimpleNamespace(photo=photo)

    async def download_profile_photo(self, _me, *, file):
        assert file is bytes
        return self.data


def _credentials() -> DeveloperAppCredentials:
    return DeveloperAppCredentials(app_id=1, api_id=12345, api_hash="hash", credentials_version=1)


def _image_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (32, 32), color=(10, 120, 220)).save(output, format="PNG")
    return output.getvalue()


def test_pull_profile_avatar_fingerprint_hashes_remote_bytes(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())
    data = _image_bytes()
    client = _AvatarClient(data)

    async def authorized(*_args, **_kwargs):
        return client

    monkeypatch.setattr(gateway, "_authorized_client", authorized)
    result = asyncio.run(gateway._pull_profile_avatar_fingerprint_async("session", _credentials()))

    assert result is not None
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size_bytes == len(data)
    assert result.remote_photo_id == "991"
    assert len(result.perceptual_hash) == 16


def test_pull_profile_avatar_fingerprint_returns_none_when_remote_has_no_photo(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())

    async def authorized(*_args, **_kwargs):
        return _AvatarClient(None)

    monkeypatch.setattr(gateway, "_authorized_client", authorized)
    result = asyncio.run(gateway._pull_profile_avatar_fingerprint_async("session", _credentials()))

    assert result is None


def test_pull_profile_avatar_fingerprint_exposes_empty_download(monkeypatch):
    gateway = TelethonTelegramGateway(Settings())

    async def authorized(*_args, **_kwargs):
        return _AvatarClient(b"")

    monkeypatch.setattr(gateway, "_authorized_client", authorized)

    with pytest.raises(RuntimeError, match="download returned empty bytes"):
        asyncio.run(gateway._pull_profile_avatar_fingerprint_async("session", _credentials()))


class _ProfileClient:
    def __init__(self, first_name: str, last_name: str = "") -> None:
        self.first_name = first_name
        self.last_name = last_name

    async def is_user_authorized(self) -> bool:
        return True

    async def __call__(self, _request):
        return SimpleNamespace(first_name=self.first_name, last_name=self.last_name)


def _profile_gateway(monkeypatch, client: _ProfileClient) -> TelethonTelegramGateway:
    gateway = TelethonTelegramGateway(Settings())

    async def create_client(*_args, **_kwargs):
        return client

    monkeypatch.setattr(gateway_module, "decrypt_session", lambda _value: "session")
    monkeypatch.setattr(gateway, "_get_or_create_client", create_client)
    return gateway


def test_update_profile_accepts_exact_remote_name(monkeypatch):
    gateway = _profile_gateway(monkeypatch, _ProfileClient("游泳🍵"))

    result = asyncio.run(gateway._update_profile_async("session", _credentials(), "游泳🍵", "", "", None))

    assert result.ok is True


def test_update_profile_rejects_telegram_name_normalization(monkeypatch):
    from app.services.account_profile_name_generation import SYMBOLS

    gateway = _profile_gateway(monkeypatch, _ProfileClient("游泳"))

    result = asyncio.run(gateway._update_profile_async("session", _credentials(), "游泳⭐", "", "", None))

    assert result.ok is False
    assert result.failure_type == "profile_remote_mismatch"
    assert "⭐" not in SYMBOLS
