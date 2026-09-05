from importlib import import_module

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from app.models import NegativeOutcomeCircuitState, NegativeOutcomePolicyRevision, ReactionIntentPolicyRevision, SourceReactionIntentDecision

pytestmark = pytest.mark.no_postgres


def test_revision_graph_fits_alembic_version_column():
    script = ScriptDirectory.from_config(Config("alembic.ini"))
    capacity = MigrationContext.configure(dialect_name="postgresql")._version.c.version_num.type.length
    assert all(len(revision.revision) <= capacity for revision in script.walk_revisions())
    assert script.get_current_head() == "0225_account_group_revisions"


def test_0223_upgrade_downgrade_and_model_parity(monkeypatch):
    migration = import_module("migrations.versions.0223_unified_burst_negative_outcome")
    with create_engine("sqlite:///:memory:").begin() as connection:
        for table in ("tenants", "tg_accounts", "tasks"):
            connection.exec_driver_sql(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)")
        connection.exec_driver_sql("CREATE TABLE context_turns (id TEXT PRIMARY KEY, tenant_id INTEGER, canonical_peer_id TEXT)")
        connection.exec_driver_sql("INSERT INTO context_turns VALUES ('old', 1, '-1001')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        for model in (NegativeOutcomeCircuitState, NegativeOutcomePolicyRevision, ReactionIntentPolicyRevision, SourceReactionIntentDecision):
            columns = {c["name"]: c["nullable"] for c in inspect(connection).get_columns(model.__tablename__)}
            assert columns == {c.name: c.nullable for c in model.__table__.columns}
        scope = inspect(connection).get_unique_constraints("negative_outcome_circuit_states")[0]
        assert scope["column_names"] == ["tenant_id", "route", "peer_id", "account_id"]
        assert connection.exec_driver_sql("SELECT author_peer_id FROM context_turns WHERE id='old'").scalar_one() == ""
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT id FROM context_turns").scalar_one() == "old"
