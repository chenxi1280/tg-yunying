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
DEFAULT_BOOTSTRAP_ADMIN_PASSWORD = "admin123"

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
        if self.app_env == "production" and self.admin_bootstrap_password.strip() in {"", DEFAULT_BOOTSTRAP_ADMIN_PASSWORD}:
            raise ValueError(
                "ADMIN_BOOTSTRAP_PASSWORD or ADMIN_PASSWORD must be set to a non-default value in production. "
                "Do NOT use the default 'admin123'."
            )
    tg_api_id: str | None = os.getenv("TG_API_ID")
    tg_api_hash: str | None = os.getenv("TG_API_HASH")
    tg_gateway_mode: str = os.getenv("TG_GATEWAY_MODE", "mock" if os.getenv("APP_ENV") == "test" else "telethon")
    admin_bootstrap_username: str = os.getenv("ADMIN_USERNAME", os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")).strip() or "admin"
    admin_bootstrap_email: str | None = os.getenv("ADMIN_BOOTSTRAP_EMAIL")
    admin_bootstrap_password: str = os.getenv("ADMIN_PASSWORD", os.getenv("ADMIN_BOOTSTRAP_PASSWORD", DEFAULT_BOOTSTRAP_ADMIN_PASSWORD))
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
    worker_role: str = os.getenv("WORKER_ROLE", "all")
    action_claim_limit: int = int(os.getenv("ACTION_CLAIM_LIMIT", "100"))
    action_claim_seconds: int = int(os.getenv("ACTION_CLAIM_SECONDS", "60"))
    action_lease_seconds: int = int(os.getenv("ACTION_LEASE_SECONDS", "1800"))
    dispatcher_concurrency: int = int(os.getenv("DISPATCHER_CONCURRENCY", "20"))
    account_shard_total: int = int(os.getenv("ACCOUNT_SHARD_TOTAL", "1"))
    account_shard_index: int = int(os.getenv("ACCOUNT_SHARD_INDEX", "0"))
    enable_redis_account_inflight: bool = _bool_env("ENABLE_REDIS_ACCOUNT_INFLIGHT", False)
    redis_account_inflight_seconds: int = int(os.getenv("REDIS_ACCOUNT_INFLIGHT_SECONDS", "1800"))
    enable_global_account_online_keepalive: bool = _bool_env("ENABLE_GLOBAL_ACCOUNT_ONLINE_KEEPALIVE", True)
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    telethon_client_cache_size: int = int(os.getenv("TELETHON_CLIENT_CACHE_SIZE", "200"))
    telethon_client_idle_seconds: int = int(os.getenv("TELETHON_CLIENT_IDLE_SECONDS", "900"))
    telethon_client_connect_timeout_seconds: float = float(os.getenv("TELETHON_CLIENT_CONNECT_TIMEOUT_SECONDS", "15"))
    telethon_operation_timeout_seconds: float = float(os.getenv("TELETHON_OPERATION_TIMEOUT_SECONDS", "300"))
    max_pending_global: int = int(os.getenv("MAX_PENDING_GLOBAL", "10000"))
    max_pending_per_task: int = int(os.getenv("MAX_PENDING_PER_TASK", "1000"))
    oldest_pending_age_seconds: int = int(os.getenv("OLDEST_PENDING_AGE_SECONDS", "3600"))
    enable_redis_token_bucket: bool = _bool_env("ENABLE_REDIS_TOKEN_BUCKET", False)
    redis_token_fail_closed: bool = _bool_env("REDIS_TOKEN_FAIL_CLOSED", True)
    global_tg_rate_per_second: float = float(os.getenv("GLOBAL_TG_RATE_PER_SECOND", "30"))
    task_rate_per_minute: int = int(os.getenv("TASK_RATE_PER_MINUTE", "120"))
    task_type_rate_per_minute: int = int(os.getenv("TASK_TYPE_RATE_PER_MINUTE", "300"))
    account_rate_per_hour: int = int(os.getenv("ACCOUNT_RATE_PER_HOUR", "120"))
    proxy_rate_per_minute: int = int(os.getenv("PROXY_RATE_PER_MINUTE", "300"))
    target_rate_per_minute: int = int(os.getenv("TARGET_RATE_PER_MINUTE", "60"))
    media_rate_per_minute: int = int(os.getenv("MEDIA_RATE_PER_MINUTE", "30"))
    task_type_token_weights: str = os.getenv("TASK_TYPE_TOKEN_WEIGHTS", "group_ai_chat=2,group_relay=1,channel_view=1,channel_like=1,channel_comment=1,message_send=1")
    enable_runtime_retention_cleanup: bool = _bool_env("ENABLE_RUNTIME_RETENTION_CLEANUP", os.getenv("APP_ENV") == "production")
    runtime_detail_retention_days: int = int(os.getenv("RUNTIME_DETAIL_RETENTION_DAYS", "5"))
    runtime_metric_retention_days: int = int(os.getenv("RUNTIME_METRIC_RETENTION_DAYS", "3"))
    runtime_metric_retention_batch_size: int = int(os.getenv("RUNTIME_METRIC_RETENTION_BATCH_SIZE", "20000"))
    runtime_metric_cleanup_interval_seconds: int = int(os.getenv("RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS", "60"))
    auto_migrate_on_start: bool = _bool_env("AUTO_MIGRATE_ON_START", False)
    seed_demo_data: bool = _bool_env("SEED_DEMO_DATA", False)
    seed_tg_developer_app_from_env: bool = _bool_env("SEED_TG_DEVELOPER_APP_FROM_ENV", False)
    media_root: str = field(default_factory=lambda: os.getenv("MEDIA_ROOT", str(PROJECT_ROOT / "media")))
    source_media_cache_peer_id: str = field(default_factory=lambda: os.getenv("SOURCE_MEDIA_CACHE_PEER_ID", ""))
    material_cache_peer_id: str = field(default_factory=lambda: os.getenv("MATERIAL_CACHE_PEER_ID", ""))
    material_max_bytes: int = field(default_factory=lambda: int(os.getenv("MATERIAL_MAX_BYTES", str(20 * 1024 * 1024))))
    material_url_deep_probe_enabled: bool = field(
        default_factory=lambda: _bool_env("MATERIAL_URL_DEEP_PROBE_ENABLED", os.getenv("APP_ENV") != "test")
    )
    material_url_probe_timeout_seconds: float = field(default_factory=lambda: float(os.getenv("MATERIAL_URL_PROBE_TIMEOUT_SECONDS", "5.0")))
    material_url_probe_max_redirects: int = field(default_factory=lambda: int(os.getenv("MATERIAL_URL_PROBE_MAX_REDIRECTS", "3")))
    public_app_base_url: str = field(default_factory=lambda: os.getenv("PUBLIC_APP_BASE_URL", "").strip().rstrip("/"))
    material_allowed_upload_types: tuple[str, ...] = field(
        default_factory=lambda: tuple(
            item.strip()
            for item in os.getenv(
                "MATERIAL_ALLOWED_UPLOAD_TYPES",
                "image/jpeg,image/png,image/webp,image/gif,application/x-tgsticker,video/webm,video/mp4,application/pdf",
            ).split(",")
            if item.strip()
        )
    )
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
