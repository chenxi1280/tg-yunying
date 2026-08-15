from __future__ import annotations

import io
import time
import urllib.error
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai_gateway import AiGateway, AiProviderRateLimited, _retry_after_seconds
from app.config import get_settings
from app.database import Base
from app.models import AiProvider
from app.services.task_center import provider_admission
from app.services.task_center.provider_admission import (
    ProviderAdmissionBlocked,
    ProviderAdmissionUnavailable,
    begin_provider_call,
    ensure_claim_admission,
    extend_provider_cooldown,
    provider_admission_key,
    release_provider_probe,
    settle_provider_success,
)


pytestmark = pytest.mark.no_postgres


class FakeAdmissionRedis:
    """覆盖 provider_admission 所需的最小 Redis 语义（hash + NX SET + Lua）。"""

    def __init__(self) -> None:
        self.hash: dict[str, dict[str, str]] = {}
        self.strings: dict[str, str] = {}
        self.ttl: dict[str, int] = {}

    # -- hash -----------------------------------------------------------
    def hgetall(self, key):
        return dict(self.hash.get(str(key), {}))

    def hset(self, key, mapping=None, **kwargs):
        target = self.hash.setdefault(str(key), {})
        for field, value in (mapping or {}).items():
            target[str(field)] = str(value)
        return 1

    def hincrby(self, key, field, amount=1):
        target = self.hash.setdefault(str(key), {})
        current = int(target.get(str(field), "0"))
        target[str(field)] = str(current + int(amount))
        return current + int(amount)

    def expire(self, key, ttl):
        self.ttl[str(key)] = int(ttl)
        return True

    # -- string ---------------------------------------------------------
    def set(self, key, value, nx=False, ex=None):
        key_text = str(key)
        if nx and key_text in self.strings:
            return False
        self.strings[key_text] = str(value)
        if ex is not None:
            self.ttl[key_text] = int(ex)
        return True

    # -- lua ------------------------------------------------------------
    def eval(self, script, numkeys, *args):
        if "source_status" in script:
            key = str(args[0])
            new_retry_at = float(args[1])
            max_retry_at = float(args[2])
            reason = str(args[3])
            ttl = int(args[4])
            target_hash = self.hash.setdefault(key, {})
            current = float(target_hash.get("retry_at", "0"))
            target = min(max(current, new_retry_at), max_retry_at)
            version = int(target_hash.get("version", "0")) + 1
            target_hash.update({
                "retry_at": repr(target),
                "reason": reason,
                "source_status": "cooldown",
                "version": str(version),
            })
            self.ttl[key] = ttl
            return repr(target)
        if "del" in script:
            key, token = str(args[0]), str(args[1])
            if self.strings.get(key) == token:
                del self.strings[key]
                return 1
            return 0
        raise AssertionError(f"unexpected lua script: {script[:80]}")


class BrokenAdmissionRedis:
    def hgetall(self, _key):
        raise ConnectionError("redis down")


def _provider(*, base_url: str = "https://api.minimax.example/v1") -> AiProvider:
    return AiProvider(
        id=3,
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url=base_url,
        model_name="MiniMax-M3",
        api_key_ciphertext="cipher-text",
        api_key_header="Authorization",
        is_active=True,
    )


def _sqlite_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _enable_admission(monkeypatch, fake_redis: FakeAdmissionRedis | None = None) -> None:
    settings = SimpleNamespace(
        redis_url="redis://cache",
        ai_provider_admission_enabled=True,
        ai_provider_admission_config_version="v1",
        ai_provider_cooldown_default_seconds=30,
        ai_provider_cooldown_max_seconds=3600,
        ai_provider_probe_ttl_seconds=60,
    )
    monkeypatch.setattr(provider_admission, "get_settings", lambda: settings)
    if fake_redis is not None:
        monkeypatch.setattr(provider_admission, "_redis_client", lambda _url: fake_redis)


# ---------------------------------------------------------------------------
# claim-side fence
# ---------------------------------------------------------------------------


def test_claim_admission_blocks_when_only_active_provider_in_cooldown(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    key = provider_admission_key(provider)
    fake.hash[key] = {
        "retry_at": repr(time.time() + 120),
        "reason": "http_429",
        "source_status": "cooldown",
        "version": "1",
    }
    with _sqlite_session() as session:
        session.add(provider)
        session.commit()

        with pytest.raises(ProviderAdmissionBlocked) as exc_info:
            ensure_claim_admission(session)

    assert exc_info.value.reason == "all_active_providers_cooldown"
    assert exc_info.value.wait_seconds >= 100


def test_claim_admission_allows_when_cooldown_expired(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    fake.hash[provider_admission_key(provider)] = {
        "retry_at": repr(time.time() - 5),
        "reason": "",
        "source_status": "cooldown",
        "version": "1",
    }
    with _sqlite_session() as session:
        session.add(provider)
        session.commit()

        assert ensure_claim_admission(session) is None


def test_claim_admission_fails_closed_when_redis_unreadable(monkeypatch):
    _enable_admission(monkeypatch, BrokenAdmissionRedis())
    with _sqlite_session() as session:
        session.add(_provider())
        session.commit()

        with pytest.raises(ProviderAdmissionUnavailable):
            ensure_claim_admission(session)


def test_claim_admission_disabled_by_default_under_test_env(monkeypatch):
    monkeypatch.setattr(provider_admission, "get_settings", lambda: get_settings())
    fake = FakeAdmissionRedis()
    monkeypatch.setattr(provider_admission, "_redis_client", lambda _url: fake)
    with _sqlite_session() as session:
        session.add(_provider())
        session.commit()

        # APP_ENV=test 下默认关闭，不触碰 Redis
        assert ensure_claim_admission(session) is None
        assert fake.hash == {}


# ---------------------------------------------------------------------------
# pre-call fence + probe token
# ---------------------------------------------------------------------------


def test_begin_provider_call_requires_single_probe_when_key_missing(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()

    first = begin_provider_call(provider)
    assert first is not None and first.probe_key

    # 第二个进程在 probe 未释放时必须等待，不能形成惊群
    with pytest.raises(ProviderAdmissionBlocked) as exc_info:
        begin_provider_call(provider)
    assert exc_info.value.reason == "provider_probe_in_flight"

    release_provider_probe(first)
    assert fake.strings.get(first.probe_key) is None
    second = begin_provider_call(provider)
    assert second is not None


def test_begin_provider_call_blocked_by_active_cooldown(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    key = provider_admission_key(provider)
    fake.hash[key] = {
        "retry_at": repr(time.time() + 60),
        "reason": "http_429",
        "source_status": "cooldown",
        "version": "2",
    }

    with pytest.raises(ProviderAdmissionBlocked) as exc_info:
        begin_provider_call(provider)
    assert exc_info.value.reason == "http_429"


def test_expired_cooldown_allows_only_one_recovery_probe(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    key = provider_admission_key(provider)
    fake.hash[key] = {
        "retry_at": repr(time.time() - 1),
        "reason": "http_429",
        "source_status": "cooldown",
        "version": "2",
    }

    first = begin_provider_call(provider)
    assert first is not None and first.probe_key
    with pytest.raises(ProviderAdmissionBlocked) as exc_info:
        begin_provider_call(provider)
    assert exc_info.value.reason == "provider_probe_in_flight"


def test_provider_epoch_is_independent_of_local_timezone():
    assert abs(provider_admission._now_epoch() - time.time()) < 1


def test_settle_success_marks_open_and_next_call_skips_probe(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()

    lease = begin_provider_call(provider)
    settle_provider_success(lease)

    state = fake.hash[provider_admission_key(provider)]
    assert state["source_status"] == "open"
    assert float(state["retry_at"]) == 0
    assert int(state["version"]) == 1

    # open marker 存在时无需再抢 probe
    next_lease = begin_provider_call(provider)
    assert next_lease is not None and next_lease.probe_key == ""


def test_settle_success_exposes_redis_write_failure(caplog):
    class BrokenSettleRedis:
        def hset(self, *_args, **_kwargs):
            raise ConnectionError("redis write down")

    lease = provider_admission.ProviderProbeLease(
        BrokenSettleRedis(),
        "ai:provider:admission:v1:test",
        "ai:provider:admission:v1:test:probe",
        "token",
    )

    settle_provider_success(lease)

    assert "provider admission success settlement failed" in caplog.text


def test_extend_provider_cooldown_uses_atomic_max_and_cap(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    provider = _provider()
    key = provider_admission_key(provider)

    now = time.time()
    fake.hash[key] = {
        "retry_at": repr(now + 60),
        "reason": "http_429:first",
        "source_status": "cooldown",
        "version": "1",
    }

    # 更短的 Retry-After 不能缩短已有 cooldown（原子 max）
    target = extend_provider_cooldown(provider, 10, reason="http_429:short")
    assert target >= now + 59

    # 更长的 Retry-After 延长 cooldown
    target = extend_provider_cooldown(provider, 300, reason="http_429:long")
    assert target >= now + 299

    # 超过配置上限时封顶
    target = extend_provider_cooldown(provider, 999_999, reason="http_429:huge")
    assert target <= now + 3600 + 1
    assert fake.hash[key]["source_status"] == "cooldown"
    assert int(fake.hash[key]["version"]) == 4


def test_extend_provider_cooldown_unreadable_state_fails_closed(monkeypatch):
    class HalfBrokenRedis(BrokenAdmissionRedis):
        def eval(self, *_args):
            raise ConnectionError("redis down")

    _enable_admission(monkeypatch, HalfBrokenRedis())
    with pytest.raises(ProviderAdmissionUnavailable):
        extend_provider_cooldown(_provider(), 30, reason="http_429")


def test_mock_provider_and_disabled_setting_skip_fence(monkeypatch):
    fake = FakeAdmissionRedis()
    _enable_admission(monkeypatch, fake)
    assert begin_provider_call(_provider(base_url="mock://unit")) is None

    settings = SimpleNamespace(
        redis_url="redis://cache",
        ai_provider_admission_enabled=False,
        ai_provider_admission_config_version="v1",
    )
    monkeypatch.setattr(provider_admission, "get_settings", lambda: settings)
    assert begin_provider_call(_provider()) is None


# ---------------------------------------------------------------------------
# gateway 429 typing
# ---------------------------------------------------------------------------


def test_gateway_raises_rate_limited_with_retry_after(monkeypatch):
    headers = {"Retry-After": "7"}
    error = urllib.error.HTTPError(
        "https://api.example.com/v1/chat/completions",
        429,
        "Too Many Requests",
        headers,
        io.BytesIO(b'{"error":"rate limited"}'),
    )

    def _raise(_request, timeout=None):  # noqa: ANN001
        raise error

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    gateway = AiGateway()
    credentials = SimpleNamespace(
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        model_name="MiniMax-M3",
        api_key="secret",
        api_key_header="Authorization",
    )

    with pytest.raises(AiProviderRateLimited) as exc_info:
        gateway._post_openai_compatible(credentials, "prompt", 0.7, 128)

    assert exc_info.value.status_code == 429
    assert exc_info.value.retry_after_seconds == 7


def test_gateway_rate_limited_falls_back_to_default_without_retry_after(monkeypatch):
    error = urllib.error.HTTPError(
        "https://api.example.com/v1/chat/completions",
        429,
        "Too Many Requests",
        {"Retry-After": "not-a-number"},
        io.BytesIO(b"rate limited"),
    )

    def _raise(_request, timeout=None):  # noqa: ANN001
        raise error

    monkeypatch.setattr("urllib.request.urlopen", _raise)
    credentials = SimpleNamespace(
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url="https://api.example.com/v1",
        model_name="MiniMax-M3",
        api_key="secret",
        api_key_header="Authorization",
    )

    with pytest.raises(AiProviderRateLimited) as exc_info:
        AiGateway()._post_openai_compatible(credentials, "prompt", 0.7, 128)

    assert exc_info.value.retry_after_seconds is None


def test_retry_after_accepts_http_date():
    retry_at = datetime.now(UTC) + timedelta(seconds=30)

    seconds = _retry_after_seconds({"Retry-After": format_datetime(retry_at, usegmt=True)})

    assert seconds is not None
    assert 1 <= seconds <= 30
