from __future__ import annotations

import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv
import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session


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

RULE_BINDING_REQUIRED_TEST_TASK_TYPES = frozenset({"group_relay", "group_ai_chat", "channel_comment"})
TEST_RULE_SET_ID_BASE = 900_000_000
TEST_RULE_VERSION_ID_BASE = 901_000_000
AUTO_BOUND_TASKS_SESSION_KEY = "auto_bound_rule_tasks"


@pytest.fixture(autouse=True)
def keep_default_group_send_tests_inside_active_window(monkeypatch):
    from app.services.task_center import group_send_limits

    current_now = group_send_limits._now
    monkeypatch.setattr(
        group_send_limits,
        "_now",
        lambda: current_now().replace(hour=12, minute=0, second=0, microsecond=0),
    )


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
    config.addinivalue_line(
        "markers",
        "allow_missing_rule_binding: opt out of default test rule binding for negative runtime-gate cases",
    )


@pytest.fixture(autouse=True)
def bind_required_rule_versions_for_executor_tests(request):
    if request.node.get_closest_marker("allow_missing_rule_binding"):
        yield
        return
    cleanup_task_ids: set[str] = set()

    def before_flush(session, _flush_context, _instances):  # noqa: ANN001
        _bind_required_rule_versions(session)

    def after_flush(session, _flush_context):  # noqa: ANN001
        _collect_auto_bound_task_ids(session, cleanup_task_ids)

    event.listen(Session, "before_flush", before_flush)
    event.listen(Session, "after_flush_postexec", after_flush)
    try:
        yield
    finally:
        event.remove(Session, "before_flush", before_flush)
        event.remove(Session, "after_flush_postexec", after_flush)
        _soft_delete_auto_bound_tasks(cleanup_task_ids)


def _bind_required_rule_versions(session: Session) -> None:
    from app.models import Task

    for task in [item for item in session.new if isinstance(item, Task)]:
        if task.type not in RULE_BINDING_REQUIRED_TEST_TASK_TYPES:
            continue
        if _has_rule_binding(task.type_config or {}):
            continue
        tenant_id = int(task.tenant_id or 1)
        _ensure_test_rule_version(session, tenant_id)
        task.type_config = {
            **(task.type_config or {}),
            "rule_set_version_id": _test_rule_version_id(tenant_id),
        }
        session.info.setdefault(AUTO_BOUND_TASKS_SESSION_KEY, []).append(task)


def _has_rule_binding(type_config: dict) -> bool:
    return bool(type_config.get("rule_set_id") or type_config.get("rule_set_version_id"))


def _ensure_test_rule_version(session: Session, tenant_id: int) -> None:
    from app.models import RuleSet, RuleSetVersion
    from app.services._common import _now

    version_id = _test_rule_version_id(tenant_id)
    cache_key = f"test_rule_version:{tenant_id}"
    if session.info.get(cache_key):
        return
    session.info[cache_key] = True
    if session.get(RuleSetVersion, version_id):
        return
    rule_set_id = _test_rule_set_id(tenant_id)
    session.add(
        RuleSet(
            id=rule_set_id,
            tenant_id=tenant_id,
            name="测试默认已发布规则",
            status="active",
            task_types=sorted(RULE_BINDING_REQUIRED_TEST_TASK_TYPES),
            active_version_id=version_id,
        )
    )
    session.add(
        RuleSetVersion(
            id=version_id,
            tenant_id=tenant_id,
            rule_set_id=rule_set_id,
            version=1,
            status="published",
            filters={},
            output_checks={},
            transforms={},
            routing={},
            account_strategy={},
            rate_limits={},
            retry_policy={},
            created_by="test",
            published_by="test",
            published_at=_now(),
        )
    )


def _collect_auto_bound_task_ids(session: Session, cleanup_task_ids: set[str]) -> None:
    if _session_uses_sqlite(session):
        session.info.pop(AUTO_BOUND_TASKS_SESSION_KEY, None)
        return
    tasks = session.info.pop(AUTO_BOUND_TASKS_SESSION_KEY, [])
    cleanup_task_ids.update(str(task.id) for task in tasks if task.id)


def _soft_delete_auto_bound_tasks(task_ids: set[str]) -> None:
    if not task_ids:
        return
    from app.database import SessionLocal
    from app.models import Task
    from app.services._common import _now

    with SessionLocal() as session:
        tasks = session.query(Task).filter(Task.id.in_(sorted(task_ids)), Task.deleted_at.is_(None)).all()
        for task in tasks:
            task.deleted_at = _now()
            task.deleted_by = "test"
            task.delete_reason = "auto rule binding cleanup"
        session.commit()


def _session_uses_sqlite(session: Session) -> bool:
    return session.get_bind().url.get_backend_name() == "sqlite"


def _test_rule_set_id(tenant_id: int) -> int:
    return -(TEST_RULE_SET_ID_BASE + tenant_id)


def _test_rule_version_id(tenant_id: int) -> int:
    return -(TEST_RULE_VERSION_ID_BASE + tenant_id)


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
