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


def _validate_dispatcher_recycle_settings(settings: object) -> None:
    if (
        settings.image_verification_contract_enabled
        and not settings.dispatcher_recycle_enabled
    ):
        raise ValueError(
            "IMAGE_VERIFICATION_CONTRACT_ENABLED requires "
            "DISPATCHER_RECYCLE_ENABLED"
        )
    if not settings.dispatcher_recycle_enabled:
        return
    thresholds = (
        settings.dispatcher_recycle_soft_rss_bytes,
        settings.dispatcher_recycle_soft_cgroup_bytes,
        settings.dispatcher_recycle_ocr_attempt_limit,
        settings.dispatcher_recycle_max_uptime_seconds,
    )
    if not any(value > 0 for value in thresholds):
        raise ValueError(
            "DISPATCHER_RECYCLE_ENABLED requires at least one threshold"
        )
    if settings.dispatcher_recycle_lease_seconds <= 0:
        raise ValueError("DISPATCHER_RECYCLE_LEASE_SECONDS must be positive")
    if settings.dispatcher_gateway_shutdown_timeout_seconds <= 0:
        raise ValueError(
            "DISPATCHER_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS must be positive"
        )


def _validate_production_ocr_isolation(settings: object) -> None:
    if not settings.image_verification_contract_enabled:
        return
    if (
        settings.app_env == "production"
        and settings.image_verification_ocr_backend != "remote"
    ):
        raise ValueError(
            "Production image verification contract requires "
            "IMAGE_VERIFICATION_OCR_BACKEND=remote"
        )


def _validate_dispatch_runtime_settings(settings: object) -> None:
    if settings.dispatch_runtime_shard_total < 1:
        raise ValueError("DISPATCH_RUNTIME_SHARD_TOTAL must be positive")
    if settings.db_pool_control_reserve < 0:
        raise ValueError("DB_POOL_CONTROL_RESERVE must not be negative")
    connection_budget = settings.db_pool_size + settings.db_max_overflow
    if connection_budget <= settings.db_pool_control_reserve:
        raise ValueError("DB_POOL_CONTROL_RESERVE exhausts the database pool")
    if settings.dispatch_shard_stale_seconds < 1:
        raise ValueError("DISPATCH_SHARD_STALE_SECONDS must be positive")
    if settings.app_env != "production":
        return
    if settings.enable_embedded_worker:
        raise ValueError(
            "Production requires ENABLE_EMBEDDED_WORKER=false"
        )
    effective = min(
        settings.dispatcher_concurrency,
        connection_budget - settings.db_pool_control_reserve,
    )
    expected_scope_capacity = effective * settings.dispatch_runtime_shard_total
    if settings.dispatcher_scope_capacity != expected_scope_capacity:
        raise ValueError(
            "DISPATCHER_SCOPE_CAPACITY must equal the configured shard capacity"
        )
    if (
        settings.worker_role == "dispatcher"
        and settings.account_shard_total != settings.dispatch_runtime_shard_total
    ):
        raise ValueError(
            "Dispatcher ACCOUNT_SHARD_TOTAL must equal DISPATCH_RUNTIME_SHARD_TOTAL"
        )


def _validate_account_batch_login_settings(settings: object) -> None:
    if settings.account_batch_login_mode not in {"off", "reconcile_only", "enabled"}:
        raise ValueError("ACCOUNT_BATCH_LOGIN_MODE must be off, reconcile_only, or enabled")
    if settings.account_batch_login_max_lines < 1 or settings.account_batch_login_max_lines > 200:
        raise ValueError("ACCOUNT_BATCH_LOGIN_MAX_LINES must be between 1 and 200")
    if settings.account_batch_login_item_deadline_seconds < 1:
        raise ValueError("ACCOUNT_BATCH_LOGIN_ITEM_DEADLINE_SECONDS must be positive")
    if not 1 <= settings.account_batch_login_code_wait_seconds <= settings.account_batch_login_item_deadline_seconds:
        raise ValueError("ACCOUNT_BATCH_LOGIN_CODE_WAIT_SECONDS must fit within the item deadline")
    if settings.account_batch_login_poll_interval_seconds < 1:
        raise ValueError("ACCOUNT_BATCH_LOGIN_POLL_INTERVAL_SECONDS must be positive")
    if settings.account_batch_login_credential_ttl_seconds < settings.account_batch_login_item_deadline_seconds:
        raise ValueError("ACCOUNT_BATCH_LOGIN_CREDENTIAL_TTL_SECONDS must cover the item deadline")
    if settings.account_batch_login_reconcile_seconds < settings.account_batch_login_item_deadline_seconds:
        raise ValueError("ACCOUNT_BATCH_LOGIN_RECONCILE_SECONDS must cover the item deadline")
    if settings.account_batch_login_worker_concurrency < 1:
        raise ValueError("ACCOUNT_BATCH_LOGIN_WORKER_CONCURRENCY must be positive")
    versions = [value.strip() for value in settings.account_batch_phone_fingerprint_versions.split(",")]
    if not versions or any(not value.isdigit() or int(value) < 1 for value in versions):
        raise ValueError("ACCOUNT_BATCH_PHONE_FINGERPRINT_VERSIONS must contain positive integers")
    if settings.account_batch_phone_fingerprint_version not in {int(value) for value in versions}:
        raise ValueError("current phone fingerprint version must be accepted")
    if settings.account_batch_login_mode == "off":
        return
    if settings.account_batch_login_developer_app_concurrency < 1:
        raise ValueError("batch login reconciliation requires ACCOUNT_BATCH_LOGIN_DEVELOPER_APP_CONCURRENCY")
    if settings.account_batch_login_mode != "enabled":
        return
    if settings.account_batch_login_host_concurrency < 1:
        raise ValueError("enabled batch login requires ACCOUNT_BATCH_LOGIN_HOST_CONCURRENCY")
    if settings.account_batch_login_host_min_interval_seconds <= 0:
        raise ValueError("enabled batch login requires ACCOUNT_BATCH_LOGIN_HOST_MIN_INTERVAL_SECONDS")


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
    authorization_dr_internal_token: str = os.getenv("AUTHORIZATION_DR_INTERNAL_TOKEN", "")
    authorization_dr_require_mtls: bool = _bool_env("AUTHORIZATION_DR_REQUIRE_MTLS", True)
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
        if self.account_online_probe_concurrency < 1:
            raise ValueError("ACCOUNT_ONLINE_PROBE_CONCURRENCY must be a positive integer")
        if self.account_online_probe_timeout_seconds <= 0:
            raise ValueError("ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS must be greater than zero")
        if self.voice_profile_reconcile_interval_seconds <= 0:
            raise ValueError("VOICE_PROFILE_RECONCILE_INTERVAL_SECONDS must be greater than zero")
        if self.voice_profile_reconcile_batch_limit < 1:
            raise ValueError("VOICE_PROFILE_RECONCILE_BATCH_LIMIT must be positive")
        if self.voice_profile_provider_rate_per_minute < 1:
            raise ValueError("VOICE_PROFILE_PROVIDER_RATE_PER_MINUTE must be positive")
        if self.voice_profile_provider_concurrency < 1:
            raise ValueError("VOICE_PROFILE_PROVIDER_CONCURRENCY must be positive")
        if self.voice_profile_provider_lease_seconds < 30:
            raise ValueError("VOICE_PROFILE_PROVIDER_LEASE_SECONDS must be at least 30")
        if self.ai_provider_cooldown_default_seconds < 1:
            raise ValueError("AI_PROVIDER_COOLDOWN_DEFAULT_SECONDS must be positive")
        if self.ai_provider_cooldown_max_seconds < self.ai_provider_cooldown_default_seconds:
            raise ValueError("AI_PROVIDER_COOLDOWN_MAX_SECONDS must be >= AI_PROVIDER_COOLDOWN_DEFAULT_SECONDS")
        if self.ai_provider_probe_ttl_seconds < 10:
            raise ValueError("AI_PROVIDER_PROBE_TTL_SECONDS must be at least 10")
        _validate_dispatcher_recycle_settings(self)
        _validate_production_ocr_isolation(self)
        _validate_dispatch_runtime_settings(self)
        _validate_account_batch_login_settings(self)
    tg_api_id: str | None = os.getenv("TG_API_ID")
    tg_api_hash: str | None = os.getenv("TG_API_HASH")
    tg_gateway_mode: str = os.getenv("TG_GATEWAY_MODE", "mock" if os.getenv("APP_ENV") == "test" else "telethon")
    admin_bootstrap_username: str = os.getenv("ADMIN_USERNAME", os.getenv("ADMIN_BOOTSTRAP_USERNAME", "admin")).strip() or "admin"
    admin_bootstrap_email: str | None = os.getenv("ADMIN_BOOTSTRAP_EMAIL")
    admin_bootstrap_password: str = os.getenv("ADMIN_PASSWORD", os.getenv("ADMIN_BOOTSTRAP_PASSWORD", DEFAULT_BOOTSTRAP_ADMIN_PASSWORD))
    login_code_ttl_seconds: int = int(os.getenv("LOGIN_CODE_TTL_SECONDS", "300"))
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
    account_batch_login_mode: str = os.getenv("ACCOUNT_BATCH_LOGIN_MODE", "off").strip().lower()
    account_batch_login_max_lines: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_MAX_LINES", "200"))
    account_batch_login_item_deadline_seconds: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_ITEM_DEADLINE_SECONDS", "300"))
    account_batch_login_code_wait_seconds: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_CODE_WAIT_SECONDS", "120"))
    account_batch_login_poll_interval_seconds: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_POLL_INTERVAL_SECONDS", "3"))
    account_batch_login_credential_ttl_seconds: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_CREDENTIAL_TTL_SECONDS", "86400"))
    account_batch_login_reconcile_seconds: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_RECONCILE_SECONDS", "86400"))
    account_batch_login_worker_concurrency: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_WORKER_CONCURRENCY", "4"))
    account_batch_login_host_concurrency: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_HOST_CONCURRENCY", "0"))
    account_batch_login_host_min_interval_seconds: float = float(os.getenv("ACCOUNT_BATCH_LOGIN_HOST_MIN_INTERVAL_SECONDS", "0"))
    account_batch_login_developer_app_concurrency: int = int(os.getenv("ACCOUNT_BATCH_LOGIN_DEVELOPER_APP_CONCURRENCY", "0"))
    account_batch_phone_fingerprint_version: int = int(os.getenv("ACCOUNT_BATCH_PHONE_FINGERPRINT_VERSION", "1"))
    account_batch_phone_fingerprint_versions: str = os.getenv("ACCOUNT_BATCH_PHONE_FINGERPRINT_VERSIONS", "1")
    voice_profile_reconcile_interval_seconds: float = float(os.getenv("VOICE_PROFILE_RECONCILE_INTERVAL_SECONDS", "120"))
    voice_profile_reconcile_batch_limit: int = int(os.getenv("VOICE_PROFILE_RECONCILE_BATCH_LIMIT", "100"))
    voice_profile_provider_rate_per_minute: int = int(os.getenv("VOICE_PROFILE_PROVIDER_RATE_PER_MINUTE", "30"))
    voice_profile_provider_concurrency: int = int(os.getenv("VOICE_PROFILE_PROVIDER_CONCURRENCY", "2"))
    voice_profile_provider_lease_seconds: int = int(os.getenv("VOICE_PROFILE_PROVIDER_LEASE_SECONDS", "120"))
    ai_provider_admission_enabled: bool = _bool_env(
        "AI_PROVIDER_ADMISSION_ENABLED",
        os.getenv("APP_ENV", "development") != "test",
    )
    ai_provider_admission_config_version: str = os.getenv("AI_PROVIDER_ADMISSION_CONFIG_VERSION", "v1")
    ai_provider_cooldown_default_seconds: int = int(os.getenv("AI_PROVIDER_COOLDOWN_DEFAULT_SECONDS", "30"))
    ai_provider_cooldown_max_seconds: int = int(os.getenv("AI_PROVIDER_COOLDOWN_MAX_SECONDS", "3600"))
    ai_provider_probe_ttl_seconds: int = int(os.getenv("AI_PROVIDER_PROBE_TTL_SECONDS", "60"))
    account_soft_pacing_min_gap_seconds: int = int(os.getenv("ACCOUNT_SOFT_PACING_MIN_GAP_SECONDS", "20"))
    # 群级发送最小间隔：只做突发下界（消除跨账号同秒并发），不得低于最大任务
    # 高峰曲线密度（4800 目标 × 峰值权重 10/110 ≈ 436 条/小时 ≈ 8.3 秒/条），
    # 否则会系统性压制 DueSet 合法吞吐。
    ai_group_send_pacing_min_gap_seconds: int = int(os.getenv("AI_GROUP_SEND_PACING_MIN_GAP_SECONDS", "8"))
    # 回复目标历史近因窗口：回复目标池只取最近 N 天成功发送（目标池上限 20 条，
    # 生产任务日产量百级，7 天充足）。无界扫描会在历史积累后拖慢 planner 事务
    # 并长时间持有 task 行锁（2026-08-17 线上事故：25 分钟/轮、发送坍塌）。
    ai_reply_target_history_window_days: int = int(os.getenv("AI_REPLY_TARGET_HISTORY_WINDOW_DAYS", "7"))
    action_claim_limit: int = int(os.getenv("ACTION_CLAIM_LIMIT", "100"))
    action_claim_seconds: int = int(os.getenv("ACTION_CLAIM_SECONDS", "60"))
    dispatcher_claim_scope: str = os.getenv("DISPATCHER_CLAIM_SCOPE", "task_center_dispatch")
    dispatcher_scope_capacity: int = int(os.getenv("DISPATCHER_SCOPE_CAPACITY", "26"))
    dispatch_runtime_shard_total: int = int(
        os.getenv("DISPATCH_RUNTIME_SHARD_TOTAL", "2")
    )
    db_pool_control_reserve: int = int(
        os.getenv("DB_POOL_CONTROL_RESERVE", "2")
    )
    dispatch_shard_stale_seconds: int = int(
        os.getenv("DISPATCH_SHARD_STALE_SECONDS", "120")
    )
    dispatch_topology_fingerprint_schema_version: str = os.getenv(
        "DISPATCH_TOPOLOGY_FINGERPRINT_SCHEMA_VERSION",
        "dispatch_topology_v1",
    )
    dispatch_rebuild_contract_version: str = os.getenv(
        "DISPATCH_REBUILD_CONTRACT_VERSION",
        "dispatch-rebuild-v3",
    )
    action_lease_seconds: int = int(os.getenv("ACTION_LEASE_SECONDS", "1800"))
    # PRD §2.20.2 RC-3: search_join_membership 子动作的 lease_timeout 默认 180s，
    # 覆盖 join 请求发送 → TG 服务端处理 → membership 事件回推的全链路。
    search_join_membership_lease_seconds: int = int(os.getenv("SEARCH_JOIN_MEMBERSHIP_LEASE_SECONDS", "180"))
    # PRD §2.20.2 RC-3: UAS 补偿确认终态关闭超时（秒），默认 10 分钟。
    search_join_membership_confirmation_timeout_seconds: int = int(os.getenv("SEARCH_JOIN_MEMBERSHIP_CONFIRMATION_TIMEOUT_SECONDS", "600"))
    dispatcher_concurrency: int = int(os.getenv("DISPATCHER_CONCURRENCY", "20"))
    search_dispatcher_concurrency: int = int(
        os.getenv("SEARCH_DISPATCHER_CONCURRENCY", "20")
    )
    image_verification_contract_enabled: bool = _bool_env(
        "IMAGE_VERIFICATION_CONTRACT_ENABLED",
        False,
    )
    image_verification_contract_version: str = os.getenv(
        "IMAGE_VERIFICATION_CONTRACT_VERSION",
        "",
    )
    image_verification_callback_acceptance_seconds: float = float(
        os.getenv("IMAGE_VERIFICATION_CALLBACK_ACCEPTANCE_SECONDS", "0")
    )
    image_verification_callback_headroom_seconds: float = float(
        os.getenv("IMAGE_VERIFICATION_CALLBACK_HEADROOM_SECONDS", "0")
    )
    image_verification_model_tail_budget_seconds: float = float(
        os.getenv("IMAGE_VERIFICATION_MODEL_TAIL_BUDGET_SECONDS", "0")
    )
    image_verification_model_timeout_seconds: float = float(
        os.getenv("IMAGE_VERIFICATION_MODEL_TIMEOUT_SECONDS", "30")
    )
    image_verification_reasoning_retry_min_budget_seconds: float = float(
        os.getenv(
            "IMAGE_VERIFICATION_REASONING_RETRY_MIN_BUDGET_SECONDS",
            "0",
        )
    )
    image_verification_model_concurrency: int = int(
        os.getenv(
            "IMAGE_VERIFICATION_MODEL_CONCURRENCY",
            os.getenv("DISPATCHER_CONCURRENCY", "20"),
        )
    )
    image_verification_ocr_backend: str = os.getenv(
        "IMAGE_VERIFICATION_OCR_BACKEND",
        "local",
    ).strip().lower()
    image_verification_worker_url: str = os.getenv(
        "IMAGE_VERIFICATION_WORKER_URL",
        "",
    ).strip().rstrip("/")
    image_verification_worker_token: str = os.getenv(
        "IMAGE_VERIFICATION_WORKER_TOKEN",
        "",
    )
    dispatcher_recycle_enabled: bool = _bool_env(
        "DISPATCHER_RECYCLE_ENABLED",
        False,
    )
    dispatcher_recycle_soft_rss_bytes: int = int(
        os.getenv("DISPATCHER_RECYCLE_SOFT_RSS_BYTES", "0")
    )
    dispatcher_recycle_soft_cgroup_bytes: int = int(
        os.getenv("DISPATCHER_RECYCLE_SOFT_CGROUP_BYTES", "0")
    )
    dispatcher_recycle_ocr_attempt_limit: int = int(
        os.getenv("DISPATCHER_RECYCLE_OCR_ATTEMPT_LIMIT", "0")
    )
    dispatcher_recycle_max_uptime_seconds: float = float(
        os.getenv("DISPATCHER_RECYCLE_MAX_UPTIME_SECONDS", "0")
    )
    dispatcher_recycle_lease_seconds: int = int(
        os.getenv("DISPATCHER_RECYCLE_LEASE_SECONDS", "0")
    )
    dispatcher_gateway_shutdown_timeout_seconds: float = float(
        os.getenv("DISPATCHER_GATEWAY_SHUTDOWN_TIMEOUT_SECONDS", "0")
    )
    daily_coverage_plan_batch_limit: int = int(os.getenv("DAILY_COVERAGE_PLAN_BATCH_LIMIT", "20"))
    planner_resource_sample_interval_seconds: int = int(
        os.getenv("PLANNER_RESOURCE_SAMPLE_INTERVAL_SECONDS", "10")
    )
    account_shard_total: int = int(os.getenv("ACCOUNT_SHARD_TOTAL", "1"))
    account_shard_index: int = int(os.getenv("ACCOUNT_SHARD_INDEX", "0"))
    enable_redis_account_inflight: bool = _bool_env("ENABLE_REDIS_ACCOUNT_INFLIGHT", False)
    redis_account_inflight_seconds: int = int(os.getenv("REDIS_ACCOUNT_INFLIGHT_SECONDS", "1800"))
    enable_global_account_online_keepalive: bool = _bool_env("ENABLE_GLOBAL_ACCOUNT_ONLINE_KEEPALIVE", True)
    account_online_probe_concurrency: int = int(os.getenv("ACCOUNT_ONLINE_PROBE_CONCURRENCY", "32"))
    account_online_probe_timeout_seconds: float = float(os.getenv("ACCOUNT_ONLINE_PROBE_TIMEOUT_SECONDS", "30"))
    db_pool_size: int = int(os.getenv("DB_POOL_SIZE", "5"))
    db_max_overflow: int = int(os.getenv("DB_MAX_OVERFLOW", "10"))
    db_pool_timeout: int = int(os.getenv("DB_POOL_TIMEOUT", "30"))
    db_pool_recycle: int = int(os.getenv("DB_POOL_RECYCLE", "1800"))
    telethon_client_cache_size: int = int(os.getenv("TELETHON_CLIENT_CACHE_SIZE", "200"))
    telethon_client_idle_seconds: int = int(os.getenv("TELETHON_CLIENT_IDLE_SECONDS", "900"))
    telethon_client_connect_timeout_seconds: float = float(os.getenv("TELETHON_CLIENT_CONNECT_TIMEOUT_SECONDS", "15"))
    telethon_operation_timeout_seconds: float = float(os.getenv("TELETHON_OPERATION_TIMEOUT_SECONDS", "300"))
    listener_fetch_timeout_seconds: float = float(os.getenv("LISTENER_FETCH_TIMEOUT_SECONDS", "30"))
    rank_deboost_egress_probe_url: str = os.getenv("RANK_DEBOOST_EGRESS_PROBE_URL", "https://api.ipify.org")
    grok_cli_enabled: bool = _bool_env("GROK_CLI_ENABLED", False)
    grok_cli_bin: str = os.getenv("GROK_CLI_BIN", "/root/.grok/bin/grok")
    grok_cli_model: str = os.getenv("GROK_CLI_MODEL", "grok-4.5")
    grok_cli_timeout_seconds: int = int(os.getenv("GROK_CLI_TIMEOUT_SECONDS", "90"))
    grok_cli_lock_path: str = os.getenv("GROK_CLI_LOCK_PATH", "/tmp/tgyunying-grok-cli.lock")
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
    runtime_detail_retention_batch_size: int = int(os.getenv("RUNTIME_DETAIL_RETENTION_BATCH_SIZE", "2000"))
    runtime_detail_cleanup_interval_seconds: int = int(os.getenv("RUNTIME_DETAIL_CLEANUP_INTERVAL_SECONDS", "60"))
    runtime_metric_retention_days: int = int(os.getenv("RUNTIME_METRIC_RETENTION_DAYS", "3"))
    runtime_resource_raw_retention_hours: int = int(os.getenv("RUNTIME_RESOURCE_RAW_RETENTION_HOURS", "24"))
    runtime_resource_rollup_retention_days: int = int(os.getenv("RUNTIME_RESOURCE_ROLLUP_RETENTION_DAYS", "7"))
    runtime_metric_retention_batch_size: int = int(os.getenv("RUNTIME_METRIC_RETENTION_BATCH_SIZE", "20000"))
    runtime_metric_cleanup_interval_seconds: int = int(os.getenv("RUNTIME_METRIC_CLEANUP_INTERVAL_SECONDS", "300"))
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
