from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError


os.environ.setdefault("APP_ENV", "test")
BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

for env_path in (PROJECT_ROOT / ".env", BACKEND_ROOT / ".env"):
    if env_path.exists():
        load_dotenv(env_path, override=False)

os.environ["ADMIN_BOOTSTRAP_USERNAME"] = "admin@demo.local"
os.environ["ADMIN_BOOTSTRAP_EMAIL"] = "admin@demo.local"
os.environ["ADMIN_BOOTSTRAP_PASSWORD"] = "admin123"
os.environ["TG_API_ID"] = ""
os.environ["TG_API_HASH"] = ""
os.environ["WORKER_ROLE"] = "all"
os.environ["ACCOUNT_SHARD_TOTAL"] = "1"
os.environ["ACCOUNT_SHARD_INDEX"] = "0"
os.environ["ENABLE_REDIS_ACCOUNT_INFLIGHT"] = "false"
os.environ.setdefault("AUTO_MIGRATE_ON_START", "true")
os.environ["ENABLE_EMBEDDED_WORKER"] = "false"


def _normalize_postgres_url(raw_url: str) -> str:
    if raw_url.startswith("postgresql+asyncpg://"):
        return raw_url.replace("postgresql+asyncpg://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgresql://"):
        return raw_url.replace("postgresql://", "postgresql+psycopg://", 1)
    if raw_url.startswith("postgres://"):
        return raw_url.replace("postgres://", "postgresql+psycopg://", 1)
    return raw_url


def _postgres_test_database_url() -> str:
    raw_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL")
    if not raw_url:
        raise RuntimeError("TEST_DATABASE_URL or DATABASE_URL must point to a PostgreSQL test database")
    database_url = _normalize_postgres_url(raw_url)
    if not database_url.startswith("postgresql+psycopg://"):
        raise RuntimeError("Integration tests require PostgreSQL; set TEST_DATABASE_URL to postgresql+psycopg://...")
    os.environ["DATABASE_URL"] = database_url
    os.environ["TEST_DATABASE_URL"] = database_url
    return database_url


def _reset_test_database(database_url: str) -> None:
    engine = create_engine(database_url, future=True, isolation_level="AUTOCOMMIT")
    with engine.connect() as connection:
        connection.execute(text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(text("CREATE SCHEMA IF NOT EXISTS public"))
    engine.dispose()


def _selected_tests_require_postgres(items: list[pytest.Item]) -> bool:
    return any(item.get_closest_marker("no_postgres") is None for item in items)


def pytest_configure(config):
    config.addinivalue_line("markers", "no_postgres: does not require PostgreSQL test database reset")


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(session, config, items):
    if not _selected_tests_require_postgres(items):
        return
    try:
        _reset_test_database(_postgres_test_database_url())
    except (RuntimeError, SQLAlchemyError) as exc:
        raise pytest.UsageError(
            "PostgreSQL test database is required for the selected tests, "
            "but reset failed. Check TEST_DATABASE_URL/DATABASE_URL and database connectivity."
        ) from exc


def pytest_runtest_setup(item):
    from app.services.task_center.listener_runtime import reset_listener_runtime_cache

    reset_listener_runtime_cache()


def pytest_runtest_teardown(item, nextitem):
    from app.services.task_center.listener_runtime import reset_listener_runtime_cache

    reset_listener_runtime_cache()
