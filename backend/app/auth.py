from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import threading
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
from .timezone import as_beijing_aware, beijing_now


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
    """开发环境验证码存储，支持 TTL 自动过期和线程安全的 token 消费。"""

    def __init__(self) -> None:
        self._challenges: dict[str, dict] = {}
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()

    def _is_expired(self, entry: dict) -> bool:
        """检查条目是否已过期（基于存储时的 TTL 时间戳）。"""
        expires_at = entry.get("_expires_at")
        if expires_at is None:
            return False
        return datetime.now(UTC).timestamp() > expires_at

    def get_challenge(self, challenge_id: str) -> dict | None:
        entry = self._challenges.get(challenge_id)
        if entry is None or self._is_expired(entry):
            self._challenges.pop(challenge_id, None)
            return None
        return entry

    def set_challenge(self, challenge_id: str, data: dict, ttl_seconds: int) -> None:
        data["_expires_at"] = datetime.now(UTC).timestamp() + ttl_seconds
        self._challenges[challenge_id] = data

    def get_token(self, token: str) -> dict | None:
        entry = self._tokens.get(token)
        if entry is None or self._is_expired(entry):
            self._tokens.pop(token, None)
            return None
        return entry

    def set_token(self, token: str, data: dict, ttl_seconds: int) -> None:
        data["_expires_at"] = datetime.now(UTC).timestamp() + ttl_seconds
        self._tokens[token] = data

    def consume_token(self, token: str) -> bool:
        with self._lock:
            entry = self._tokens.get(token)
            if entry is None or self._is_expired(entry):
                return False
            if entry.get("consumed"):
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

    # Lua 脚本实现原子性 check-and-consume，避免竞态条件
    _CONSUME_LUA = """
    local key = KEYS[1]
    local raw = redis.call('GET', key)
    if not raw then return 0 end
    local data = cjson.decode(raw)
    if data['consumed'] == true then return 0 end
    data['consumed'] = true
    local ttl = redis.call('TTL', key)
    local updated = cjson.encode(data)
    if ttl > 0 then
        redis.call('SETEX', key, ttl, updated)
    else
        redis.call('SET', key, updated)
    end
    return 1
    """

    def consume_token(self, token: str) -> bool:
        key = self._token_key(token)
        try:
            result = self._client.eval(self._CONSUME_LUA, 1, key)
            return result == 1
        except Exception:
            # Redis 不可用时回退到非原子操作
            raw = self._client.get(key)
            if not raw:
                return False
            data = json.loads(raw)
            if data.get("consumed"):
                return False
            data["consumed"] = True
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


_PBKDF2_ITERATIONS = 600_000

ALL_PERMISSIONS = [
    "overview.view",
    "accounts.view",
    "accounts.create",
    "accounts.login",
    "accounts.sync",
    "accounts.codes.read",
    "accounts.security.read",
    "accounts.security.batch",
    "accounts.authorizations.manage",
    "accounts.profile.batch_update",
    "accounts.sensitive.read",
    "accounts.delete",
    "accounts.pool_manage",
    "accounts.clone",
    "accounts.manual_send",
    "developer_apps.manage",
    "proxies.manage",
    "targets.view",
    "targets.manage",
    "target_profile.view",
    "target_profile.manage",
    "message_sending.view",
    "message_sending.manage",
    "tasks.view",
    "tasks.manage",
    "tasks.dispatch_control",
    "listeners.view",
    "listeners.manage",
    "rules.view",
    "rules.publish",
    "risk.view",
    "risk.manage",
    "archives.view",
    "archives.manage",
    "archives.export",
    "usage.view",
    "usage.export",
    "materials.view",
    "materials.upload",
    "materials.manage",
    "manual.view",
    "operation_plans.manage",
    "operation_issues.manage",
    "ai.manage",
    "prompt_templates.manage",
    "system.view",
    "system.manage",
    "permissions.view",
    "permissions.manage",
    "audits.view",
    "audits.view_sensitive",
    "audit.export",
]

MENU_VIEW_PERMISSIONS = {
    "overview.view",
    "accounts.view",
    "targets.view",
    "message_sending.view",
    "tasks.view",
    "listeners.view",
    "rules.view",
    "risk.view",
    "archives.view",
    "usage.view",
    "materials.view",
    "manual.view",
    "system.view",
    "permissions.view",
    "audits.view",
}

LEGACY_PERMISSION_MAP = {
    "overview": "overview.view",
    "accounts": "accounts.view",
    "targetManagement": "targets.view",
    "groups": "targets.view",
    "groupManagement": "targets.view",
    "operationTargets": "targets.view",
    "messageSending": "message_sending.view",
    "taskManagement": "tasks.view",
    "listenerCenter": "listeners.view",
    "ruleCenter": "rules.view",
    "riskControl": "risk.view",
    "archives": "archives.view",
    "groupArchives": "archives.view",
    "usageReports": "usage.view",
    "materials": "materials.view",
    "materialCenter": "materials.view",
    "adminManual": "manual.view",
    "systemConfig": "system.view",
    "developerApps": "system.view",
    "aiConfig": "system.view",
    "audits": "audits.view",
    "accounts.view_codes": "accounts.codes.read",
    "accounts.update_profile": "accounts.profile.batch_update",
    "accounts.proxy_bind": "proxies.manage",
    "audits.export": "audit.export",
    "message_sending.create": "message_sending.manage",
    "system.secrets_manage": "ai.manage",
}

ROLE_TEMPLATE_PERMISSIONS = {
    "运营管理员": [
        "overview.view",
        "operation_plans.manage",
        "operation_issues.manage",
        "accounts.view",
        "accounts.sync",
        "accounts.codes.read",
        "accounts.security.read",
        "accounts.security.batch",
        "accounts.authorizations.manage",
        "accounts.profile.batch_update",
        "targets.view",
        "targets.manage",
        "target_profile.view",
        "target_profile.manage",
        "message_sending.view",
        "message_sending.manage",
        "tasks.view",
        "tasks.manage",
        "listeners.view",
        "listeners.manage",
        "rules.view",
        "rules.publish",
        "risk.view",
        "risk.manage",
        "proxies.manage",
        "archives.view",
        "archives.manage",
        "usage.view",
        "usage.export",
        "materials.view",
        "materials.upload",
        "materials.manage",
        "manual.view",
        "audits.view",
        "audit.export",
    ],
    "账号添加专员": ["overview.view", "accounts.view", "accounts.create", "accounts.login", "accounts.sync", "accounts.authorizations.manage"],
    "只读观察员": ["overview.view", "targets.view", "target_profile.view", "listeners.view", "usage.view", "manual.view", "audits.view"],
}

DEFAULT_USER_MENU_PERMISSIONS = ROLE_TEMPLATE_PERMISSIONS["运营管理员"]


def all_permissions() -> list[str]:
    return list(ALL_PERMISSIONS)


def normalize_permissions(values: list[str] | None, *, role: str | None = None, role_template: str | None = None) -> list[str]:
    if role == "系统管理员":
        return ["*"]
    if not values:
        return list(ROLE_TEMPLATE_PERMISSIONS.get(role_template or "运营管理员", DEFAULT_USER_MENU_PERMISSIONS))
    normalized: list[str] = []
    for value in values:
        key = (value or "").strip()
        if not key:
            continue
        mapped = LEGACY_PERMISSION_MAP.get(key, key)
        if mapped in ALL_PERMISSIONS:
            normalized.append(mapped)
    return list(dict.fromkeys(normalized))


def canonical_permission(permission: str) -> str:
    return LEGACY_PERMISSION_MAP.get(permission, permission)


def parse_menu_permissions(raw: str | None, *, role: str | None = None, role_template: str | None = None) -> list[str]:
    raw_values = [item.strip() for item in raw.split(",") if item.strip()] if raw else []
    return normalize_permissions(raw_values, role=role, role_template=role_template)


def format_menu_permissions(values: list[str] | None) -> str:
    if values is None:
        return ""
    return ",".join(normalize_permissions(values))


def hash_password(password: str) -> str:
    """生成密码哈希，使用独立随机 salt，格式: $iterations$salt_b64$hash_b64"""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"${_PBKDF2_ITERATIONS}${base64.urlsafe_b64encode(salt).decode()}${base64.urlsafe_b64encode(digest).decode()}"


def verify_password(password: str, stored_hash: str) -> bool:
    """验证密码，兼容新格式（独立 salt）和旧格式（共享 salt）。"""
    if stored_hash.startswith("$"):
        # 新格式: $iterations$salt_b64$hash_b64
        parts = stored_hash.split("$")
        if len(parts) != 4:
            return False
        iterations = int(parts[1])
        salt = base64.urlsafe_b64decode(parts[2])
        expected = base64.urlsafe_b64decode(parts[3])
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
        return hmac.compare_digest(actual, expected)
    # 旧格式: 共享 salt 的 base64 哈希（向后兼容）
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), get_password_salt(), 120_000)
    return hmac.compare_digest(base64.urlsafe_b64encode(digest).decode("ascii"), stored_hash)


def is_legacy_password_hash(stored_hash: str) -> bool:
    """检查密码哈希是否为旧格式（共享 salt）。"""
    return not stored_hash.startswith("$")


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(get_token_key(), payload, hashlib.sha256).digest()).decode("ascii")


def create_access_token(user: AppUser) -> str:
    payload = {
        "sub": user.id,
        "tenant_id": user.tenant_id,
        "role": user.role,
        "permission_version": user.permission_version,
        "exp": int((datetime.now(UTC) + timedelta(hours=12)).timestamp()),
    }
    encoded = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode("utf-8")).decode("ascii")
    return f"{encoded}.{_sign(encoded.encode('ascii'))}"


def create_admin_access_token() -> str:
    payload = {
        "sub": "admin",
        "tenant_id": 1,
        "role": "系统管理员",
        "permission_version": 1,
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
    role_template: str
    email: str
    phone: str | None
    tenant_name: str | None
    subscription_status: str
    subscription_started_at: datetime | None
    subscription_expires_at: datetime | None
    subscription_days_remaining: int
    can_use_core_features: bool
    token_balance: int
    token_quota_total: int
    menu_permissions: list[str]
    permissions: list[str]
    permission_version: int
    is_active: bool
    is_super_admin: bool = False

    @property
    def is_platform_admin(self) -> bool:
        return self.is_super_admin or self.role == "系统管理员"

    @property
    def is_end_user(self) -> bool:
        return self.role == "普通用户"

    def has_permission(self, permission: str) -> bool:
        canonical = canonical_permission(permission)
        return self.is_platform_admin or "*" in self.permissions or canonical in self.permissions


def normalize_phone(phone: str | None) -> str | None:
    if not phone:
        return None
    compact = re.sub(r"[^\d+]", "", phone.strip())
    return compact or None


def _as_utc(dt: datetime | None) -> datetime | None:
    """Convert a business datetime to an aware Beijing datetime."""
    if dt is None:
        return None
    return as_beijing_aware(dt)


def compute_subscription_status(user: AppUser) -> str:
    if user.role == "系统管理员":
        return "active"
    if user.subscription_status == "pending_activation":
        return "pending_activation"
    expires_at = _as_utc(user.subscription_expires_at)
    if expires_at is not None and expires_at < as_beijing_aware(beijing_now()):
        return "expired"
    return "active"


def compute_subscription_days_remaining(user: AppUser) -> int:
    expires_at = _as_utc(user.subscription_expires_at)
    if not expires_at:
        return 0
    remaining = expires_at - as_beijing_aware(beijing_now())
    return max(0, int((remaining.total_seconds() + 86399) // 86400))


def can_user_use_core_features(user: AppUser) -> bool:
    if user.role == "系统管理员":
        return True
    return compute_subscription_status(user) == "active"


def require_core_feature_access(current_user: CurrentUser) -> None:
    return


def admin_user_payload() -> dict:
    settings = get_settings()
    username = settings.admin_bootstrap_username
    email = settings.admin_bootstrap_email or f"{username}@local.admin"
    return {
        "id": 0,
        "tenant_id": 1,
        "name": username,
        "role": "系统管理员",
        "role_template": "系统管理员",
        "email": email,
        "phone": None,
        "tenant_name": "默认运营空间",
        "subscription_status": "active",
        "subscription_started_at": None,
        "subscription_expires_at": None,
        "subscription_days_remaining": 0,
        "can_use_core_features": True,
        "token_balance": 0,
        "token_quota_total": 0,
        "menu_permissions": ["*"],
        "permissions": ["*"],
        "permission_version": 1,
        "is_active": True,
        "is_super_admin": True,
    }


def authenticate_admin(identifier: str, password: str) -> bool:
    settings = get_settings()
    expected_identifier = settings.admin_bootstrap_username
    expected_email = settings.admin_bootstrap_email or f"{expected_identifier}@local.admin"
    identifier = (identifier or "").strip()
    password_ok = hmac.compare_digest(password or "", settings.admin_bootstrap_password)
    identifier_ok = identifier in {expected_identifier, expected_email}
    return bool(identifier_ok and password_ok)


def create_captcha_challenge() -> dict:
    now = datetime.now(UTC)
    challenge_id = secrets.token_urlsafe(18)
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    target = "".join(secrets.choice(alphabet) for _ in range(5))
    expires_at = now + timedelta(minutes=5)
    store = _get_captcha_store()
    store.set_challenge(
        challenge_id,
        {"target": target, "expires_at": expires_at.isoformat(), "consumed": False},
        ttl_seconds=330,  # 5.5 min — slightly longer than logical expiry
    )
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="132" height="44" viewBox="0 0 132 44">
<rect width="132" height="44" rx="8" fill="#f5f7f3"/>
<path d="M8 31 C32 5, 56 43, 80 14 S112 40, 126 12" stroke="#9aa7a0" stroke-width="1.5" fill="none" opacity=".55"/>
<path d="M10 13 L122 33 M18 36 L116 8" stroke="#c6d1ca" stroke-width="1" opacity=".65"/>
<text x="66" y="29" text-anchor="middle" font-family="ui-monospace, SFMono-Regular, Menlo, monospace" font-size="24" font-weight="800" letter-spacing="4" fill="#17362c">{target}</text>
</svg>"""
    return {
        "challenge_id": challenge_id,
        "image_data_url": "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii"),
        "expires_at": expires_at,
    }


def get_challenge_target(challenge_id: str) -> str | None:
    """从验证码 store 中读取 challenge 的目标值（仅供测试使用）。"""
    store = _get_captcha_store()
    challenge = store.get_challenge(challenge_id)
    if challenge is None:
        return None
    target = challenge.get("target")
    return str(target) if target is not None else None


def verify_captcha_challenge(challenge_id: str, captcha_value: str) -> dict:
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
    normalized_value = re.sub(r"\s+", "", captcha_value or "").upper()
    if normalized_value != str(challenge["target"]).upper():
        raise HTTPException(status_code=400, detail="captcha verification failed")
    # Mark consumed
    challenge["consumed"] = True
    store.set_challenge(challenge_id, challenge, ttl_seconds=300)
    captcha_token = secrets.token_urlsafe(24)
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
    permissions = parse_menu_permissions(user.menu_permissions, role=user.role, role_template=user.role_template)
    return {
        "id": user.id,
        "tenant_id": user.tenant_id,
        "name": user.name,
        "role": user.role,
        "role_template": user.role_template,
        "email": user.email,
        "phone": user.phone,
        "tenant_name": tenant_name,
        "subscription_status": subscription_status,
        "subscription_started_at": user.subscription_started_at,
        "subscription_expires_at": user.subscription_expires_at,
        "subscription_days_remaining": compute_subscription_days_remaining(user),
        "can_use_core_features": can_user_use_core_features(user),
        "token_balance": user.token_balance,
        "token_quota_total": user.token_quota_total,
        "menu_permissions": permissions,
        "permissions": permissions,
        "permission_version": user.permission_version,
        "is_active": user.is_active,
        "is_super_admin": user.role == "系统管理员",
    }


def authenticate_user(session: Session, identifier: str, password: str) -> AppUser | None:
    identifier = identifier.strip()
    normalized_phone = normalize_phone(identifier)
    conditions = [AppUser.email == identifier.lower(), AppUser.name == identifier]
    if normalized_phone:
        conditions.append(AppUser.phone == normalized_phone)
    user = session.scalar(
        select(AppUser).where(
            AppUser.is_active.is_(True),
            or_(*conditions),
        )
    )
    if not user:
        return None
    if not verify_password(password, user.password_hash):
        return None
    user.last_login_at = beijing_now()
    session.commit()
    return user


def get_current_user(
    authorization: Annotated[str | None, Header()] = None,
    session: Session = Depends(get_session),
) -> CurrentUser:
    return current_user_from_authorization(authorization, session)


def current_user_from_authorization(authorization: str | None, session: Session) -> CurrentUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    payload = decode_access_token(authorization.split(" ", 1)[1])
    if payload.get("sub") == "admin":
        return CurrentUser(**admin_user_payload())
    try:
        user_id = int(payload.get("sub", 0))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=401, detail="invalid token subject") from exc
    user = session.get(AppUser, user_id)
    if not user or not user.is_active:
        raise HTTPException(status_code=401, detail="user inactive")
    if int(payload.get("permission_version", 0)) != int(user.permission_version):
        raise HTTPException(status_code=401, detail="permission version expired")
    return CurrentUser(**serialize_user(session, user))


def require_permission(permission: str):
    def dependency(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
        if not current_user.has_permission(permission):
            from app.common.http import forbidden

            raise forbidden("permission denied")
        return current_user

    return dependency


def ensure_permission(current_user: CurrentUser, permission: str) -> None:
    if not current_user.has_permission(permission):
        from app.common.http import forbidden

        raise forbidden("permission denied")


def require_platform_admin(current_user: CurrentUser = Depends(get_current_user)) -> CurrentUser:
    """FastAPI dependency: raise 403 unless the caller is a platform admin."""
    if not current_user.is_platform_admin:
        from app.common.http import forbidden

        raise forbidden("platform admin required")
    return current_user


def resolve_tenant_id(current_user: CurrentUser, requested_tenant_id: int | None = None) -> int:
    return 1
