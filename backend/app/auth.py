from __future__ import annotations

import base64
import hashlib
import hmac
import json
import random
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Annotated, Protocol

from fastapi import Depends, Header, HTTPException
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from .config import get_settings
from .database import get_session
from .models import AppUser, Tenant
from .security import get_password_salt, get_token_key


# ---------------------------------------------------------------------------
# Captcha store — Redis-backed (multi-worker safe) or in-memory (dev fallback)
# ---------------------------------------------------------------------------

class CaptchaStore(Protocol):
    def get_challenge(self, challenge_id: str) -> dict | None: ...
    def set_challenge(self, challenge_id: str, data: dict, ttl_seconds: int) -> None: ...
    def get_token(self, token: str) -> dict | None: ...
    def set_token(self, token: str, data: dict, ttl_seconds: int) -> None: ...
    def consume_token(self, token: str) -> bool: ...


class InMemoryCaptchaStore:
    def __init__(self) -> None:
        self._challenges: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}

    def get_challenge(self, challenge_id: str) -> dict | None:
        return self._challenges.get(challenge_id)

    def set_challenge(self, challenge_id: str, data: dict, ttl_seconds: int) -> None:
        self._challenges[challenge_id] = data

    def get_token(self, token: str) -> dict | None:
        return self._tokens.get(token)

    def set_token(self, token: str, data: dict, ttl_seconds: int) -> None:
        self._tokens[token] = data

    def consume_token(self, token: str) -> bool:
        entry = self._tokens.get(token)
        if entry is None or entry.get("consumed"):
            return False
        entry["consumed"] = True
        return True


class RedisCaptchaStore:
    def __init__(self, redis_url: str) -> None:
        try:
            from redis import Redis
        except ImportError as exc:
            raise RuntimeError("redis package is not installed") from exc
        self._client = Redis.from_url(redis_url, decode_responses=True)

    def _challenge_key(self, challenge_id: str) -> str:
        return f"tg_yunying:captcha_challenge:{challenge_id}"

    def _token_key(self, token: str) -> str:
        return f"tg_yunying:captcha_token:{token}"

    def get_challenge(self, challenge_id: str) -> dict | None:
        raw = self._client.get(self._challenge_key(challenge_id))
        return json.loads(raw) if raw else None

    def set_challenge(self, challenge_id: str, data: dict, ttl_seconds: int) -> None:
        key = self._challenge_key(challenge_id)
        self._client.setex(key, ttl_seconds, json.dumps(data, default=str))

    def get_token(self, token: str) -> dict | None:
        raw = self._client.get(self._token_key(token))
        return json.loads(raw) if raw else None

    def set_token(self, token: str, data: dict, ttl_seconds: int) -> None:
        key = self._token_key(token)
        self._client.setex(key, ttl_seconds, json.dumps(data, default=str))

    def consume_token(self, token: str) -> bool:
        key = self._token_key(token)
        raw = self._client.get(key)
        if not raw:
            return False
        data = json.loads(raw)
        if data.get("consumed"):
            return False
        data["consumed"] = True
        # Re-set with flag; keep original TTL
        ttl = self._client.ttl(key)
        if ttl > 0:
            self._client.setex(key, ttl, json.dumps(data, default=str))
        return True


_captcha_store: CaptchaStore | None = None


def _get_captcha_store() -> CaptchaStore:
    global _captcha_store
    if _captcha_store is None:
        settings = get_settings()
        if settings.queue_backend == "redis" and settings.redis_url:
            _captcha_store = RedisCaptchaStore(settings.redis_url)
        else:
            _captcha_store = InMemoryCaptchaStore()
    return _captcha_store


def hash_password(password: str) -> str:
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), get_password_salt(), 120_000)
    return base64.urlsafe_b64encode(digest).decode("ascii")


def verify_password(password: str, stored_hash: str) -> bool:
    return hmac.compare_digest(hash_password(password), stored_hash)


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(get_token_key(), payload, hashlib.sha256).digest()).decode("ascii")


def create_access_token(user: AppUser) -> str:
    payload = {
        "sub": user.id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "exp": int((datetime.now(UTC) + timedelta(hours=12)).timestamp()),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{encoded}.{_sign(encoded.encode('ascii'))}"


def decode_access_token(token: str) -> dict:
    try:
        encoded, signature = token.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=401, detail="invalid token") from exc
    expected = _sign(encoded.encode("ascii"))
    if not hmac.compare_digest(signature, expected):
        raise HTTPException(status_code=401, detail="invalid token")
    payload = json.loads(base64.urlsafe_b64decode(encoded.encode("ascii")))
    if int(payload.get("exp", 0)) < int(datetime.now(UTC).timestamp()):
        raise HTTPException(status_code=401, detail="token expired")
    return payload


@dataclass(frozen=True)
class CurrentUser:
    id: int
    tenant_id: int | None
    name: str
    role: str
    email: str
    phone: str | None
    tenant_name: str | None
    subscription_status: str
    subscription_started_at: datetime | None
    subscription_expires_at: datetime | None
    subscription_days_remaining: int
    can_use_core_features: bool

    @property
    def is_platform_admin(self) -> bool:
        return self.role == "系统管理员"

    @property
    def is_end_user(self) -> bool:
        return self.role == "普通用户"


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    compact = re.sub(r"[^\d+]", "", phone.strip())
    return compact or None


def _as_utc(dt: datetime | None) -> datetime | None:
    """Convert a naive datetime (assumed UTC from DB) to aware UTC, or pass through if already aware."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


def compute_subscription_status(user: AppUser) -> str:
    if user.role == "系统管理员":
        return "active"
    if user.subscription_status == "pending_activation":
        return "pending_activation"
    expires_at = _as_utc(user.subscription_expires_at)
    if expires_at is not None and expires_at < datetime.now(UTC):
        return "expired"
    return "active"


def compute_subscription_days_remaining(user: AppUser) -> int:
    expires_at = _as_utc(user.subscription_expires_at)
    if not expires_at:
        return 0
    remaining = expires_at - datetime.now(UTC)
    return max(0, int((remaining.total_seconds() + 86399) // 86400))


def can_user_use_core_features(user: AppUser) -> bool:
    if user.role == "系统管理员":
        return True
    return compute_subscription_status(user) == "active"


def require_core_feature_access(current_user: CurrentUser) -> None:
    if current_user.is_platform_admin:
        return
    if not current_user.can_use_core_features:
        raise HTTPException(status_code=403, detail="subscription inactive")


def create_captcha_challenge() -> dict:
    now = datetime.now(UTC)
    challenge_id = base64.urlsafe_b64encode(hashlib.sha256(f"{now.timestamp()}:{random.random()}".encode("utf-8")).digest()[:18]).decode("ascii")
    target = random.randint(72, 96)
    expires_at = now + timedelta(minutes=5)
    store = _get_captcha_store()
    store.set_challenge(
        challenge_id,
        {"target": target, "expires_at": expires_at.isoformat(), "consumed": False},
        ttl_seconds=330,  # 5.5 min — slightly longer than logical expiry
    )
    return {
        "challenge_id": challenge_id,
        "slider_min": 0,
        "slider_max": 100,
        "target_value": target,
        "expires_at": expires_at,
    }


def verify_captcha_challenge(challenge_id: str, slider_value: int) -> dict:
    store = _get_captcha_store()
    challenge = store.get_challenge(challenge_id)
    now = datetime.now(UTC)
    if not challenge:
        raise HTTPException(status_code=400, detail="captcha challenge expired")
    expires_at = datetime.fromisoformat(challenge["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise HTTPException(status_code=400, detail="captcha challenge expired")
    if challenge["consumed"]:
        raise HTTPException(status_code=400, detail="captcha challenge already used")
    if abs(int(slider_value) - int(challenge["target"])) > 2:
        raise HTTPException(status_code=400, detail="captcha verification failed")
    # Mark consumed
    challenge["consumed"] = True
    store.set_challenge(challenge_id, challenge, ttl_seconds=300)
    token_seed = f"{challenge_id}:{slider_value}:{now.timestamp()}".encode("utf-8")
    captcha_token = base64.urlsafe_b64encode(hashlib.sha256(token_seed).digest()[:24]).decode("ascii")
    expires_at = now + timedelta(minutes=10)
    store.set_token(
        captcha_token,
        {"expires_at": expires_at.isoformat(), "consumed": False},
        ttl_seconds=660,  # 11 min
    )
    return {"captcha_token": captcha_token, "expires_at": expires_at}


def consume_captcha_token(captcha_token: str) -> None:
    store = _get_captcha_store()
    token = store.get_token(captcha_token)
    now = datetime.now(UTC)
    if not token:
        raise HTTPException(status_code=400, detail="captcha token expired")
    expires_at = datetime.fromisoformat(token["expires_at"])
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at < now:
        raise HTTPException(status_code=400, detail="captcha token expired")
    if not store.consume_token(captcha_token):
        raise HTTPException(status_code=400, detail="captcha token already used")


def serialize_user(session: Session, user: AppUser) -> dict:
    tenant_name = None
    if user.tenant_id:
        tenant = session.get(Tenant, user.tenant_id)
        tenant_name = tenant.name if tenant else None
    subscription_status = compute_subscription_status(user)
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "name": user.name,
        "role": user.role,
        "email": user.email,
        "phone": user.phone,
        "tenant_name": tenant_name,
        "subscription_status": subscription_status,
        "subscription_started_at": user.subscription_started_at,
        "subscription_expires_at": user.subscription_expires_at,
        "subscription_days_remaining": compute_subscription_days_remaining(user),
        "can_use_core_features": can_user_use_core_features(user),
    }


def authenticate_user(session: Session, identifier: str, password: str) -> AppUser | None:
    normalized_phone = normalize_phone(identifier)
    user = session.scalar(
        select(AppUser).where(
            AppUser.is_active.is_(True),
            or_(AppUser.email == identifier, AppUser.phone == normalized_phone),
        )
    )
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = datetime.now(UTC)
    session.commit()
    return user


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    session: Session = Depends(get_session),
) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = decode_access_token(authorization.split(" ", 1)[1])
    user = session.get(AppUser, int(payload["sub"]))
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="user not found")
    return CurrentUser(**serialize_user(session, user))


def require_platform_admin(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency: raise 403 unless the caller is a platform admin."""
    if not current_user.is_platform_admin:
        from app.common.http import forbidden

        raise forbidden("platform admin required")
    return current_user


def resolve_tenant_id(current_user: CurrentUser, requested_tenant_id: int | None = None) -> int:
    if current_user.is_platform_admin:
        if requested_tenant_id is not None:
            return requested_tenant_id
        if current_user.tenant_id is not None:
            return current_user.tenant_id
        return 1
    if current_user.tenant_id is None:
        raise HTTPException(status_code=403, detail="user has no tenant")
    if requested_tenant_id is not None and requested_tenant_id != current_user.tenant_id:
        raise HTTPException(status_code=403, detail="cross-tenant access denied")
    return current_user.tenant_id
