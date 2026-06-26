from datetime import UTC, datetime, timedelta

import pytest
from fastapi import HTTPException

from app import auth
from app.auth import RedisCaptchaStore, consume_captcha_token


class _FailingAtomicRedisClient:
    def __init__(self) -> None:
        self.get_calls = 0
        self.setex_calls = 0
        self.ttl_calls = 0

    def eval(self, *_args):
        raise RuntimeError("redis eval unavailable")

    def get(self, _key):
        self.get_calls += 1
        expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return f'{{"expires_at":"{expires_at.isoformat()}","consumed":false}}'

    def ttl(self, _key):
        self.ttl_calls += 1
        return 300

    def setex(self, *_args):
        self.setex_calls += 1


class _FailingCaptchaStore:
    def get_token(self, _token: str) -> dict:
        expires_at = datetime.now(UTC) + timedelta(minutes=5)
        return {"expires_at": expires_at.isoformat(), "consumed": False}

    def consume_token(self, _token: str) -> bool:
        raise RuntimeError("captcha token store unavailable")


def test_redis_captcha_token_consume_fails_closed_when_atomic_script_fails():
    client = _FailingAtomicRedisClient()
    store = RedisCaptchaStore.__new__(RedisCaptchaStore)
    store._client = client

    with pytest.raises(RuntimeError, match="captcha token store unavailable"):
        store.consume_token("captcha-token")

    assert client.get_calls == 0
    assert client.ttl_calls == 0
    assert client.setex_calls == 0


def test_consume_captcha_token_reports_store_failure_without_consuming(monkeypatch):
    monkeypatch.setattr(auth, "_captcha_store", _FailingCaptchaStore())

    with pytest.raises(HTTPException) as exc_info:
        consume_captcha_token("captcha-token")

    assert exc_info.value.status_code == 503
    assert exc_info.value.detail == "captcha token store unavailable"
