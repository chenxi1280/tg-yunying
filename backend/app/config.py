from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from dotenv import load_dotenv

logger = logging.getLogger(__name__)


ROOT_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = ROOT_DIR.parent

for env_path in (PROJECT_ROOT / ".env", ROOT_DIR / ".env"):
    if env_path.exists():
        load_dotenv(env_path, override=False)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sync_database_url(raw: str) -> str:
    if raw.startswith("sqlite"):
        if os.getenv("APP_ENV") == "test":
            return raw
        raise ValueError("SQLite is only supported for tests. Set DATABASE_URL to a PostgreSQL connection string.")
    if raw.startswith("postgresql+asyncpg://"):
        raw = raw.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if raw.startswith("postgres://"):
        raw = raw.replace("postgres://", "postgresql+psycopg://", 1)
    if raw.startswith("postgresql://"):
        raw = raw.replace("postgresql://", "postgresql+psycopg://", 1)
    if not raw.startswith("postgresql+psycopg://"):
        raise ValueError("Only PostgreSQL via psycopg is supported. Set DATABASE_URL to postgresql+psycopg://...")
    return raw


def _default_queue_backend(app_env: str) -> str:
    if os.getenv("QUEUE_BACKEND"):
        return os.getenv("QUEUE_BACKEND", "sync")
    if app_env == "test":
        return "sync"
    return "redis" if os.getenv("REDIS_URL") else "sync"


@dataclass(frozen=True)
class Settings:
    app_env: str = os.getenv("APP_ENV", "development")
    database_url: str = _sync_database_url(
        os.getenv(
            "TEST_DATABASE_URL"
            if os.getenv("APP_ENV") == "test" and os.getenv("TEST_DATABASE_URL")
            else "DATABASE_URL",
            "postgresql+psycopg://tg_yunying:tg_yunying@127.0.0.1:5432/tg_yunying?connect_timeout=3",
        )
    )
    cors_origins: tuple[str, ...] = tuple(
        origin.strip()
        for origin in os.getenv("CORS_ORIGINS", "http://127.0.0.1:5173,http://localhost:5173").split(",")
        if origin.strip()
    )
    queue_backend: str = _default_queue_backend(app_env)
    redis_url: str = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
    session_secret_key: str = field(
        default_factory=lambda: os.getenv("SESSION_SECRET_KEY") or "dev-only-change-me"
    )
    _session_secret_key_validated: bool = field(default=False, repr=False)

    def __post_init__(self) -> None:
        if self.session_secret_key == "dev-only-change-me":
            if self.app_env == "production":
                raise ValueError(
                    "SESSION_SECRET_KEY must be set to a secure random value in production. "
                    "Do NOT use the default 'dev-only-change-me'."
                )
            generated = secrets.token_urlsafe(32)
            object.__setattr__(self, "session_secret_key", generated)
            logger.warning(
                "SESSION_SECRET_KEY is still the default 'dev-only-change-me'. "
                "A random key has been generated for this session but it will change on restart. "
                "Set SESSION_SECRET_KEY in your .env file for persistence."
            )
    tg_api_id: str | None = os.getenv("TG_API_ID")
    tg_api_hash: str | None = os.getenv("TG_API_HASH")
    tg_gateway_mode: str = os.getenv("TG_GATEWAY_MODE", "mock" if os.getenv("APP_ENV") == "test" else "telethon")
    admin_bootstrap_username: str = os.getenv("ADMIN_USERNAME", os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")).strip() or "admin"
    admin_bootstrap_email: str | None = os.getenv("ADMIN_BOOTSTRAP_EMAIL")
    admin_bootstrap_password: str = os.getenv("ADMIN_PASSWORD", os.getenv("ADMIN_BOOTSTRAP_PASSWORD", "admin123"))
    login_code_ttl_seconds: int = int(os.getenv("LOGIN_CODE_TTL_SECONDS", "180"))
    enable_sync_dispatch_fallback: bool = _bool_env("ENABLE_SYNC_DISPATCH_FALLBACK", True)
    enable_embedded_worker: bool = _bool_env("ENABLE_EMBEDDED_WORKER", os.getenv("APP_ENV", "development") == "development")
    enable_legacy_campaign_worker: bool = _bool_env("ENABLE_LEGACY_CAMPAIGN_WORKER", False)
    enable_legacy_operation_task_worker: bool = _bool_env("ENABLE_LEGACY_OPERATION_TASK_WORKER", False)
    enable_legacy_campaign_routes: bool = field(
        default_factory=lambda: _bool_env("ENABLE_LEGACY_CAMPAIGN_ROUTES", os.getenv("APP_ENV") == "test")
    )
    enable_legacy_operation_task_routes: bool = field(
        default_factory=lambda: _bool_env("ENABLE_LEGACY_OPERATION_TASK_ROUTES", os.getenv("APP_ENV") == "test")
    )
    enable_legacy_review_routes: bool = field(
        default_factory=lambda: _bool_env("ENABLE_LEGACY_REVIEW_ROUTES", os.getenv("APP_ENV") == "test")
    )
    enable_legacy_review_dispatch_gate: bool = _bool_env("ENABLE_LEGACY_REVIEW_DISPATCH_GATE", False)
    embedded_worker_interval_seconds: float = float(os.getenv("EMBEDDED_WORKER_INTERVAL_SECONDS", "2.0"))
    embedded_worker_limit: int = int(os.getenv("EMBEDDED_WORKER_LIMIT", "100"))
    auto_migrate_on_start: bool = _bool_env("AUTO_MIGRATE_ON_START", False)
    seed_demo_data: bool = _bool_env("SEED_DEMO_DATA", False)
    seed_tg_developer_app_from_env: bool = _bool_env("SEED_TG_DEVELOPER_APP_FROM_ENV", False)
    media_root: str = os.getenv("MEDIA_ROOT", str(PROJECT_ROOT / "media"))
    avatar_max_bytes: int = int(os.getenv("AVATAR_MAX_BYTES", str(2 * 1024 * 1024)))
    avatar_allowed_types: tuple[str, ...] = tuple(
        item.strip()
        for item in os.getenv("AVATAR_ALLOWED_TYPES", "image/jpeg,image/png,image/webp").split(",")
        if item.strip()
    )

    @property
    def telethon_configured(self) -> bool:
        return bool(self.tg_api_id and self.tg_api_hash)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
