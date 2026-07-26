from __future__ import annotations

from types import SimpleNamespace

import pytest


pytestmark = pytest.mark.no_postgres


class _FakeRedis:
    def __init__(self, *, rate_limited: bool = False) -> None:
        self.rate_limited = rate_limited
        self.slots: dict[str, str] = {}
        self.bucket_keys: list[str] = []

    def set(self, key, token, *, nx, ex):  # noqa: ANN001
        if nx and str(key) in self.slots:
            return False
        self.slots[str(key)] = str(token)
        return True

    def eval(self, _script, key_count, *args):  # noqa: ANN001
        if "bucket_key" not in str(_script):
            key, token = str(args[0]), str(args[1])
            if self.slots.get(key) == token:
                del self.slots[key]
                return 1
            return 0
        self.bucket_keys.append(str(args[0]))
        return [0, 7] if self.rate_limited else [1, 0]


def _settings():
    return SimpleNamespace(
        redis_url="redis://voice-profile-test",
        voice_profile_provider_rate_per_minute=30,
        voice_profile_provider_concurrency=2,
        voice_profile_provider_lease_seconds=120,
    )


def test_voice_profile_provider_limiter_uses_an_independent_bucket_and_releases_slot(monkeypatch):
    limits = __import__("app.services.task_center.account_voice_profile_generation_limits", fromlist=["*"])
    client = _FakeRedis()
    monkeypatch.setattr(limits, "get_settings", _settings)
    monkeypatch.setattr(limits, "_redis_client", lambda _url: client)

    reservation = limits.reserve_voice_profile_provider(tenant_id=1, provider_id=7)

    assert client.bucket_keys == ["rate:ai:voice_profile:1:7"]
    assert reservation.provider == "7"
    assert len(client.slots) == 1
    reservation.release()
    assert client.slots == {}


def test_voice_profile_provider_rate_limit_releases_slot_without_consuming_an_attempt(monkeypatch):
    limits = __import__("app.services.task_center.account_voice_profile_generation_limits", fromlist=["*"])
    client = _FakeRedis(rate_limited=True)
    monkeypatch.setattr(limits, "get_settings", _settings)
    monkeypatch.setattr(limits, "_redis_client", lambda _url: client)

    with pytest.raises(limits.VoiceProfileProviderRateLimitedError) as raised:
        limits.reserve_voice_profile_provider(tenant_id=1, provider_id=7)

    assert raised.value.wait_seconds == 7
    assert client.slots == {}
