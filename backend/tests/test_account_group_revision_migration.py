import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Column, Integer, MetaData, Table, create_engine, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.test_engagement_upgrade_postgres import upgrade_database


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0225_account_group_revisions.py"
TABLES = {"account_group_membership_revisions", "account_group_state_revisions"}


def _migration(connection):
    spec = importlib.util.spec_from_file_location("account_group_revision_0225", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


@pytest.mark.no_postgres
def test_sqlite_upgrade_and_downgrade_preserve_original_tables():
    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        migration = _migration(connection)
        migration.upgrade()
        assert TABLES <= set(inspect(connection).get_table_names())
        assert not any(foreign["referred_table"] == "account_pools"
            for name in TABLES for foreign in inspect(connection).get_foreign_keys(name))
        migration.downgrade()
        assert set(inspect(connection).get_table_names()) == {"tenants"}


def test_postgres_downgrade_upgrade_is_transactional_and_unique_revision_is_enforced(upgrade_database):
    from app.models import AccountGroupStateRevision, Tenant
    from app.services._common import _now

    database = upgrade_database
    with database.begin() as connection:
        Tenant.__table__.create(connection)
        _migration(connection).upgrade()
    with Session(database) as session:
        connection = session.connection()
        migration = _migration(connection)
        initial = set(inspect(connection).get_table_names())
        migration.downgrade()
        assert not TABLES & set(inspect(connection).get_table_names())
        migration.upgrade()
        assert set(inspect(connection).get_table_names()) == initial
        session.add(Tenant(id=954_620, name="迁移验证"))
        session.flush()
        values = dict(tenant_id=954_620, account_pool_id=954_621, revision=1,
            group_state={}, state_hash="test", actor="test", reason="migration", effective_at=_now())
        session.add(AccountGroupStateRevision(**values))
        session.flush()
        with pytest.raises(IntegrityError), session.begin_nested():
            session.add(AccountGroupStateRevision(**values))
            session.flush()
        with pytest.raises(RuntimeError, match="downgrade_requires_empty_evidence"):
            migration.downgrade()
        assert TABLES <= set(inspect(connection).get_table_names())
        assert session.scalar(text("SELECT count(*) FROM account_group_state_revisions")) == 1
        session.rollback()
    with Session(database) as session:
        assert TABLES <= set(inspect(session.connection()).get_table_names())
        assert session.scalar(text("SELECT count(*) FROM account_group_state_revisions")) == 0


@pytest.mark.no_postgres
@pytest.mark.parametrize("table_name", sorted(TABLES))
def test_sqlite_downgrade_keeps_nonempty_original_evidence(table_name):
    from app.database import Base
    from app.services._common import _now

    engine = create_engine("sqlite:///:memory:")
    metadata = MetaData()
    Table("tenants", metadata, Column("id", Integer, primary_key=True))
    metadata.create_all(engine)
    with engine.begin() as connection:
        migration = _migration(connection)
        migration.upgrade()
        connection.execute(metadata.tables["tenants"].insert().values(id=1))
        values = dict(id="original", tenant_id=1, account_pool_id=11, revision=1,
            actor="test", reason="original", effective_at=_now())
        if table_name == "account_group_membership_revisions":
            values.update(member_account_ids=[11], member_contracts=[], member_set_hash="set",
                membership_hash="members")
        else:
            values.update(group_state={}, state_hash="state")
        table = Base.metadata.tables[table_name]
        connection.execute(table.insert().values(**values))
        with pytest.raises(RuntimeError, match="downgrade_requires_empty_evidence"):
            migration.downgrade()
        assert TABLES <= set(inspect(connection).get_table_names())
        assert connection.execute(table.select()).mappings().one()["id"] == "original"
