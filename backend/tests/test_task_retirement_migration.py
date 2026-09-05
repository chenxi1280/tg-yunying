import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from tests.test_engagement_upgrade_postgres import upgrade_database


MIGRATION_PATH = Path(__file__).resolve().parents[1] / "migrations/versions/0226_task_retirement.py"


def _migration(connection):
    spec = importlib.util.spec_from_file_location("task_retirement_0226", MIGRATION_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(connection))
    return module


def test_populated_retirement_migration_enforces_mapping_and_refuses_loss_of_evidence(upgrade_database):
    with upgrade_database.begin() as connection:
        connection.execute(text("CREATE TABLE tasks (id varchar(36) PRIMARY KEY, status varchar(20), next_run_at timestamptz)"))
        connection.execute(text("INSERT INTO tasks (id,status) VALUES ('old','running'),('new','draft')"))
        _migration(connection).upgrade()
    with Session(upgrade_database) as session:
        connection = session.connection()
        migration = _migration(connection)
        assert session.scalar(text("SELECT status FROM tasks WHERE id='old'")) == "running"
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(text("UPDATE tasks SET retired_at=now(), replaced_by_task_id='new' WHERE id='old'"))
        session.execute(text("UPDATE tasks SET status='stopped', retired_at=now(), replaced_by_task_id='new' WHERE id='old'"))
        with pytest.raises(RuntimeError, match="downgrade_requires_empty_evidence"):
            migration.downgrade()
        assert session.scalar(text("SELECT replaced_by_task_id FROM tasks WHERE id='old'")) == "new"
        with pytest.raises(IntegrityError), session.begin_nested():
            session.execute(text("DELETE FROM tasks WHERE id='new'"))
        session.rollback()
    with upgrade_database.begin() as connection:
        migration = _migration(connection)
        migration.downgrade()
        migration.upgrade()
        assert connection.scalar(text("SELECT count(*) FROM tasks")) == 2
