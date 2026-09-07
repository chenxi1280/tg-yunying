"""Real PostgreSQL migration and planner proof against representative history volume."""
import os
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

from app.models import Action
from app.services.task_center.ai_group_vocabulary_frequency import _history_statement
from tests.test_ai_group_history_indexes import create_history_tables, migration_module


pytestmark = pytest.mark.isolated_postgres
HISTORY_ROWS = 6000


@pytest.fixture
def history_engine(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    schema = "ai_history_" + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={"options": f"-csearch_path={schema}"})
        try:
            with engine.begin() as scoped:
                create_history_tables(scoped, postgres=True)
            yield engine
        finally:
            engine.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def upgrade_indexes(engine):
    migration = migration_module()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        with context.begin_transaction():
            migration.upgrade()
            migration.upgrade()
    return migration


def plan_index_names(node):
    names = {node["Index Name"]} if "Index Name" in node else set()
    for child in node.get("Plans", []):
        names.update(plan_index_names(child))
    return names


def test_migration_uses_index_for_surface_and_full_observation(history_engine):
    migration = upgrade_indexes(history_engine)
    with history_engine.begin() as connection:
        connection.execute(text("""INSERT INTO actions
            SELECT i::text, 1, 'group_ai_chat', 'pending',
                   json_build_object('surface_scope_key', 'scope-' || (i % 100),
                                     'allocation_plan_id', 'plan', 'padding', repeat('x', 1024)),
                   NULL, now() FROM generate_series(1, :rows) AS i"""), {"rows": HISTORY_ROWS})
        connection.execute(text("""INSERT INTO group_context_messages
            SELECT i, 1, i % 100, now(), now() FROM generate_series(1, :rows) AS i"""), {"rows": HISTORY_ROWS})
        connection.execute(text("ANALYZE actions"))
        connection.execute(text("ANALYZE group_context_messages"))
        statement = _history_statement(Action(id="current", tenant_id=1), "scope-1")
        compiled = statement.compile(dialect=connection.dialect, compile_kwargs={"literal_binds": True})
        plan = connection.execute(text("EXPLAIN (ANALYZE, FORMAT JSON) " + str(compiled))).scalar()[0]
        assert "ix_actions_ai_surface_history" in plan_index_names(plan["Plan"])
        assert plan["Plan"]["Actual Rows"] == HISTORY_ROWS // 100
        context_plan = connection.execute(text("""EXPLAIN (ANALYZE, FORMAT JSON)
            SELECT id FROM group_context_messages WHERE tenant_id=1 AND group_id=1
            AND coalesce(sent_at, created_at) >= now() - interval '1 hour'
            ORDER BY coalesce(sent_at, created_at) DESC, id DESC LIMIT 20""")).scalar()[0]
        assert "ix_group_context_messages_observation_recent" in plan_index_names(context_plan["Plan"])
        valid = connection.execute(text("""SELECT bool_and(i.indisvalid)
            FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname=current_schema() AND c.relname IN (:surface, :context)"""),
            {"surface": migration.INDEXES[0][0], "context": migration.INDEXES[1][0]}).scalar()
        assert valid is True
