import importlib

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from app.models import AccountGroupAdmissionFact, Task, TaskGroupBotAdmission, Tenant, TgAccount, TgGroup
from app.services._common import _now
from app.services.task_center.task_group_bot_admission_state import restart_with_gap
from tests.test_runtime_retention_protection_postgres import database as database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def _seed(session):
    session.add(Tenant(id=1, name="gap QA"))
    session.flush()
    session.add_all([
        Task(id="gap-task", tenant_id=1, type="group_ai_chat", name="QA", status="running"),
        TgAccount(id=11, tenant_id=1, display_name="QA", phone_masked="11", session_ciphertext="QA"),
        TgGroup(id=12, tenant_id=1, title="QA", tg_peer_id="-10012"),
    ])
    session.flush()
    row = TaskGroupBotAdmission(id="gap-admission", tenant_id=1, task_id="gap-task",
        target_group_id=12, account_id=11, state="observing", observation_version=50,
        no_prompt_pass_at=_now(), surface_identity={}, surface_identity_hash="test", version=1)
    session.add(row)
    session.commit()


def test_concurrent_failure_results_preserve_one_observation_revision(database):
    with Session(database) as seed:
        _seed(seed)
    with Session(database) as first, Session(database) as second:
        a = first.get(TaskGroupBotAdmission, "gap-admission")
        b = second.get(TaskGroupBotAdmission, "gap-admission")
        assert restart_with_gap(first, a, "TimeoutError").code == "c2_observation_gap"
        second.execute(text("SET LOCAL lock_timeout='100ms'"))
        with pytest.raises(DBAPIError) as error:
            restart_with_gap(second, b, "TimeoutError")
        assert error.value.orig.sqlstate == "55P03"
        second.rollback()
        first.commit()
    with Session(database) as readback:
        row = readback.get(TaskGroupBotAdmission, "gap-admission")
        assert row.consecutive_observation_gaps == 1
        assert row.observation_version == 51
        assert len(list(readback.scalars(select(AccountGroupAdmissionFact)))) == 1


def test_late_failure_result_cannot_overwrite_committed_observation(database):
    with Session(database) as seed:
        _seed(seed)
    with Session(database) as slow, Session(database) as fast:
        stale = slow.get(TaskGroupBotAdmission, "gap-admission")
        current = fast.get(TaskGroupBotAdmission, "gap-admission")
        restart_with_gap(fast, current, "TimeoutError")
        fast.commit()
        with pytest.raises(ValueError, match="c2_observation_version_conflict"):
            restart_with_gap(slow, stale, "TimeoutError")
        slow.rollback()
        assert stale.consecutive_observation_gaps == 1


def test_migration_defaults_existing_rows_and_preserves_recorded_failures(database, monkeypatch):
    migration = importlib.import_module("migrations.versions.0229_admission_gap_count")
    with Session(database) as seed:
        _seed(seed)
    with database.begin() as connection:
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.downgrade()
        migration.upgrade()
        columns = {column["name"]: column for column in inspect(connection).get_columns("task_group_bot_admissions")}
        assert columns["consecutive_observation_gaps"]["nullable"] is False
        assert connection.scalar(text("SELECT consecutive_observation_gaps FROM task_group_bot_admissions")) == 0
        connection.execute(text("UPDATE task_group_bot_admissions SET consecutive_observation_gaps=1"))
        with pytest.raises(RuntimeError, match="failure evidence must be preserved"):
            migration.downgrade()
        assert connection.scalar(text("SELECT consecutive_observation_gaps FROM task_group_bot_admissions")) == 1
