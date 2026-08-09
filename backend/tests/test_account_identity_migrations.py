from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text

PROJECT_ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.no_postgres


def _migration(filename: str):
    path = PROJECT_ROOT / "backend/migrations/versions" / filename
    spec = importlib.util.spec_from_file_location(filename.removesuffix(".py"), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _operations(connection):
    return Operations(MigrationContext.configure(connection))


def test_name_claim_migration_backfills_only_normal_name_keepers():
    migration = _migration("0143_account_profile_name_claims.py")
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        connection.execute(text("""
            CREATE TABLE tg_accounts (
                id INTEGER PRIMARY KEY,
                tenant_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                profile_sync_status TEXT NOT NULL,
                avatar_object_key TEXT NOT NULL,
                account_identity TEXT NOT NULL,
                created_at DATETIME NOT NULL,
                deleted_at DATETIME
            )
        """))
        connection.execute(text("""
            INSERT INTO tg_accounts VALUES
                (1, 1, '海盐日记', '未同步', '', 'normal', '2026-01-01', NULL),
                (2, 1, '海盐日记', '已同步', '', 'normal', '2026-01-02', NULL),
                (3, 1, '接码名字', '已同步', '', 'code_receiver', '2026-01-01', NULL)
        """))
        migration.op = _operations(connection)
        migration.upgrade()
        rows = connection.execute(text("""
            SELECT account_id, display_name
            FROM tg_account_profile_name_claims
            ORDER BY account_id
        """)).all()

    assert rows == [(2, "海盐日记")]


def test_avatar_source_migration_is_idempotent():
    migration = _migration("0144_avatar_material_sources.py")
    engine = create_engine("sqlite+pysqlite:///:memory:", future=True)
    with engine.begin() as connection:
        migration.op = _operations(connection)
        migration.upgrade()
        migration.upgrade()
        tables = set(connection.execute(text("SELECT name FROM sqlite_master WHERE type='table'")).scalars())
        migration.downgrade()

    assert "avatar_material_sources" in tables
