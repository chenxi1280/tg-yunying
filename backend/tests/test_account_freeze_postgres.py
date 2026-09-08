"""Real PostgreSQL row ordering in a guarded, disposable test schema."""
import importlib.util
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Tenant, TgAccount
from app.services.account_freeze import AccountFrozenBeforeGateway, apply_freeze_observation, guard_account_call_start
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked

pytestmark = pytest.mark.isolated_postgres
NOW = datetime(2026, 9, 8, 4, tzinfo=timezone.utc)


@pytest.fixture
def engine(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    schema = "freeze_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        scoped = create_engine(url, connect_args={"options": f"-csearch_path={schema} -clock_timeout=1000"})
        try:
            Base.metadata.create_all(scoped)
            with Session(scoped) as session:
                session.add(Tenant(id=1, name="freeze test"))
                session.flush()
                session.add(TgAccount(id=1, tenant_id=1, display_name="test", phone_masked="test", status="在线"))
                session.commit()
            yield scoped
        finally:
            scoped.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def _attempt():
    return SimpleNamespace(account_id=1, tenant_id=1, gateway_call_started_at=None, result_snapshot={})


def test_freeze_commit_blocks_cached_worker_and_stale_health(engine):
    with Session(engine) as observer, Session(engine) as worker:
        cached = worker.get(TgAccount, 1)
        apply_freeze_observation(observer.get(TgAccount, 1), frozen=True, observed_at=NOW)
        with pytest.raises(RuntimeResourceBlocked, match="account_freeze_admission_busy"):
            guard_account_call_start(worker, _attempt())
        observer.commit()
        assert cached.telegram_frozen is False
        with pytest.raises(AccountFrozenBeforeGateway):
            guard_account_call_start(worker, _attempt())
        assert not apply_freeze_observation(cached, frozen=False, observed_at=NOW - timedelta(seconds=1))
        assert cached.telegram_frozen
        worker.commit()
    with Session(engine) as session:
        assert session.get(TgAccount, 1).status == "疑似封禁"


def test_call_issuance_serializes_before_freeze_and_migration_preserves_rows(engine):
    with Session(engine) as caller, Session(engine) as observer:
        guard_account_call_start(caller, _attempt())
        with pytest.raises(DBAPIError):
            apply_freeze_observation(observer.get(TgAccount, 1), frozen=True, observed_at=NOW)
        observer.rollback()
        caller.commit()
        assert apply_freeze_observation(observer.get(TgAccount, 1), frozen=True, observed_at=NOW)
        observer.commit()
    path = Path(__file__).parents[1] / "migrations/versions/0228_account_freeze.py"
    spec = importlib.util.spec_from_file_location("freeze_migration", path)
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)
    with engine.begin() as connection:
        migration.op = Operations(MigrationContext.configure(connection))
        with pytest.raises(RuntimeError, match="observations must be preserved"):
            migration.downgrade()
        connection.execute(text("ALTER TABLE tg_accounts DROP COLUMN telegram_frozen, DROP COLUMN telegram_freeze_observed_at"))
        migration.upgrade()
        migration.downgrade()
        migration.upgrade()
        row = connection.execute(text("SELECT id, telegram_frozen, telegram_freeze_observed_at FROM tg_accounts")).one()
        assert tuple(row) == (1, False, None)
