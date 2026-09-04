from importlib import import_module

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy import create_engine, inspect

from app.database import Base
from migrations.legacy_bootstrap import legacy_bootstrap_metadata


pytestmark = pytest.mark.no_postgres


def test_lineage_index_upgrade_downgrade_preserves_job_evidence(monkeypatch):
    migration = import_module("migrations.versions.0218_provider_lineage")
    runtime_indexes = {index.name for index in Base.metadata.tables["generation_jobs"].indexes}
    bootstrap_indexes = {index.name for index in legacy_bootstrap_metadata(Base.metadata).tables["generation_jobs"].indexes}
    assert migration.INDEX in runtime_indexes and migration.INDEX not in bootstrap_indexes
    database = create_engine("sqlite:///:memory:")
    with database.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE generation_jobs (id TEXT, tenant_id INTEGER, task_id TEXT, obligation_type TEXT, obligation_id TEXT, state TEXT)")
        connection.exec_driver_sql("INSERT INTO generation_jobs VALUES ('job', 1, 'task', 'post_comment', 'quantity', 'unknown')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        assert inspect(connection).get_indexes("generation_jobs")[0]["column_names"] == [
            "tenant_id", "task_id", "obligation_type", "obligation_id"]
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT id, state FROM generation_jobs").one() == ("job", "unknown")
    database.dispose()
