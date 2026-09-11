"""Production SQL semantics and indexed execution on realistic Action history."""
import importlib.util
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from app.services.ai_config import _material_ids_from_value
from app.services.material_action_references import action_material_reference_counts, ACTION_REFERENCE_COUNTS

pytestmark = pytest.mark.isolated_postgres
HISTORY_ROWS = 310_000


def migration_module():
    path = Path(__file__).parents[1] / 'migrations/versions/0234_material_reference_index.py'
    spec = importlib.util.spec_from_file_location('material_reference_migration', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope='function')
def reference_engine(postgres_test_session_lock):
    url = make_url(os.environ['TEST_DATABASE_URL'])
    assert url.database == 'tg_yunying_test'
    schema = 'material_refs_' + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level='AUTOCOMMIT') as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={'options': f'-csearch_path={schema}'})
        try:
            with engine.begin() as scoped:
                scoped.execute(text('CREATE TABLE actions (id text PRIMARY KEY, tenant_id integer, payload json, result json)'))
            yield engine
        finally:
            engine.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def upgrade(engine):
    migration = migration_module()
    with engine.connect() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        with context.begin_transaction():
            migration.upgrade()
            migration.upgrade()
    return migration


def test_extraction_matches_legacy(reference_engine):
    upgrade(reference_engine)
    fixtures = [
        {'material_id': 1, 'material_ids': [1, 'material:02，3 ４', True, 1.0]},
        {'nested': [{'avatar_source': 'material:0001\u00a0٢\u001f3'}]},
        {'materials': {'ignored': 1, 'material_id': 2}},
        {'materials': [[1, '2'], {'unrelated': 3}]},
        {'material_id': False, 'other': [1, 2, 3]},
        {'material_ids': ['-1', '1.0', 'material: 2', 'x3', '0000', '𝟜']},
        [None, 1, '1', {'material_id': -1}], {}, None,
    ]
    wanted = {-1, 0, 1, 2, 3, 4}
    with reference_engine.connect() as connection:
        for fixture in fixtures:
            actual = connection.scalar(text('SELECT material_reference_ids_v1(CAST(:doc AS json))'),
                                       {'doc': json.dumps(fixture)})
            assert {int(value) for value in actual} & wanted == _material_ids_from_value(fixture, wanted)


def test_counts_tenant_dedup_update_and_delete(reference_engine):
    upgrade(reference_engine)
    with Session(reference_engine) as session:
        session.execute(text('INSERT INTO actions VALUES (:id, :tenant, CAST(:payload AS json), CAST(:result AS json))'), [
            {'id': 'a', 'tenant': 1, 'payload': json.dumps({'material_ids': [1, 1, 2]}), 'result': json.dumps({'nested': {'material_id': 1}})},
            {'id': 'b', 'tenant': 1, 'payload': json.dumps({'material_id': 1}), 'result': None},
            {'id': 'c', 'tenant': 2, 'payload': json.dumps({'material_id': 1}), 'result': None},
        ])
        assert action_material_reference_counts(session, 1, material_ids={1, 2, 9}) == {1: 2, 2: 1}
        assert action_material_reference_counts(session, 2, material_ids={1}) == {1: 1}
        session.execute(text("UPDATE actions SET payload=CAST(:payload AS json), result=NULL WHERE id='a'"), {"payload": json.dumps({"material_id": 2})})
        session.execute(text("DELETE FROM actions WHERE id='b'"))
        assert action_material_reference_counts(session, 1, material_ids={1, 2}) == {2: 1}
        assert action_material_reference_counts(session, 1, material_ids=set()) == {}


def index_names(node):
    names = {node['Index Name']} if 'Index Name' in node else set()
    for child in node.get('Plans', []):
        names.update(index_names(child))
    return names


def test_index_on_existing_history_and_bounded_results(reference_engine):
    with reference_engine.begin() as connection:
        connection.execute(text('''INSERT INTO actions SELECT i::text, 1,
            json_build_object('unrelated', i), '{}'::json
            FROM generate_series(1, :rows) i'''), {'rows': HISTORY_ROWS})
        connection.execute(text("UPDATE actions SET payload=CAST(:payload AS json) WHERE id IN ('1','2')"), {"payload": json.dumps({"material_id": 1})})
    migration = upgrade(reference_engine)
    with Session(reference_engine) as session:
        session.execute(text('ANALYZE actions'))
        params = {'tenant_id': 1, 'material_ids': ['1', '2']}
        plan = session.execute(text('EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) ' + str(ACTION_REFERENCE_COUNTS)), params).scalar()[0]
        assert migration.INDEX_NAME in index_names(plan['Plan'])
        assert action_material_reference_counts(session, 1, material_ids={1, 2}) == {1: 2}
        assert len(session.identity_map) == 0
        print(json.dumps({'history_rows': HISTORY_ROWS, 'execution_ms': plan['Execution Time'],
                          'shared_hit_blocks': plan['Plan']['Shared Hit Blocks'], 'plan': plan}))


def test_migration_rejects_different_function_body(reference_engine):
    migration = upgrade(reference_engine)
    with reference_engine.begin() as connection:
        connection.execute(text("CREATE OR REPLACE FUNCTION material_reference_ids_v1(document json) RETURNS text[] LANGUAGE sql IMMUTABLE AS $$ SELECT ARRAY[]::text[] $$"))
    with reference_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        with pytest.raises(RuntimeError, match='function_definition_mismatch'):
            migration.upgrade()


def test_migration_rejects_different_index_definition(reference_engine):
    migration = migration_module()
    with reference_engine.begin() as connection:
        connection.execute(text(f'CREATE INDEX {migration.INDEX_NAME} ON actions (tenant_id)'))
    with reference_engine.connect() as connection:
        context = MigrationContext.configure(connection)
        migration.op = Operations(context)
        with pytest.raises(RuntimeError, match='index_state_or_definition_mismatch'):
            migration.upgrade()
