from alembic.config import Config
from alembic.script import ScriptDirectory
import pytest
from sqlalchemy import Column, Integer, MetaData, Table, UniqueConstraint
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.database import BACKEND_DIR, Base
from app import models  # noqa: F401
from migrations.legacy_bootstrap import (
    ENGINE_ADDED_COLUMNS, LEGACY_BOOTSTRAP_TABLES, legacy_bootstrap_metadata,
)


pytestmark = pytest.mark.no_postgres
ALEMBIC_REVISION_MAX_LENGTH = 32


def _signature(metadata):
    return {name: (tuple(table.c.keys()), frozenset(table.constraints), frozenset(table.foreign_keys),
                   frozenset(table.indexes)) for name, table in metadata.tables.items()}


def test_bootstrap_does_not_mutate_runtime_metadata():
    before = _signature(Base.metadata)
    first = legacy_bootstrap_metadata(Base.metadata)
    second = legacy_bootstrap_metadata(Base.metadata)
    assert before == _signature(Base.metadata)
    assert set(first.tables) == set(second.tables) == LEGACY_BOOTSTRAP_TABLES
    for name, columns in ENGINE_ADDED_COLUMNS.items():
        assert columns <= set(Base.metadata.tables[name].c.keys())
        assert not columns.intersection(first.tables[name].c.keys())


def test_bootstrap_foreign_keys_and_ddl_are_resolvable():
    metadata = legacy_bootstrap_metadata(Base.metadata)
    for table in metadata.tables.values():
        for foreign_key in table.foreign_keys:
            assert foreign_key.column.table.name in LEGACY_BOOTSTRAP_TABLES
        assert str(CreateTable(table).compile(dialect=postgresql.dialect()))


def test_comment_bootstrap_restores_only_legacy_identity():
    table = legacy_bootstrap_metadata(Base.metadata).tables["channel_message_comments"]
    constraints = [tuple(item.columns.keys()) for item in table.constraints if isinstance(item, UniqueConstraint)]
    assert constraints == [("tenant_id", "channel_target_id", "channel_message_id", "comment_message_id")]
    assert "discussion_peer_id" in Base.metadata.tables["channel_message_comments"].c


def test_future_tables_cannot_enter_initial_bootstrap():
    source = MetaData()
    for table in Base.metadata.tables.values():
        table.to_metadata(source)
    Table("future_revision_owned_table", source, Column("id", Integer, primary_key=True))
    assert "future_revision_owned_table" not in legacy_bootstrap_metadata(source).tables


def test_revision_chain_fits_real_alembic_version_column():
    config = Config(str(BACKEND_DIR / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_DIR / "migrations"))
    script = ScriptDirectory.from_config(config)
    assert script.get_heads() == ["0225_account_group_revisions"]
    assert all(len(item.revision) <= ALEMBIC_REVISION_MAX_LENGTH for item in script.walk_revisions())
