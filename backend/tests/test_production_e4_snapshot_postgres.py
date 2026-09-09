import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.models import Task, Tenant
from app.services.task_center.production_e4_scope import configure_readonly_snapshot
from tests.postgres_pacing_e4_fixture import factory as factory


TENANT_ID = 990_711
TASK_ID = "e4-snapshot-task"
pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


def test_readonly_snapshot_keeps_committed_concurrent_update_outside_report(factory):
    with factory() as writer:
        writer.add(Tenant(id=TENANT_ID, name="snapshot test"))
        writer.flush()
        writer.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="before", type="group_ai_chat"))
        writer.commit()
    statement = select(Task.name).where(Task.id == TASK_ID)
    with factory() as reader, factory() as writer:
        configure_readonly_snapshot(reader)
        assert reader.scalar(statement) == "before"
        writer.execute(update(Task).where(Task.id == TASK_ID).values(name="after"))
        writer.commit()
        assert reader.scalar(statement) == "before"
        reader.rollback()
        assert reader.scalar(statement) == "after"


def test_readonly_snapshot_rejects_persistent_write(factory):
    with factory() as session:
        configure_readonly_snapshot(session)
        with pytest.raises(DBAPIError) as caught:
            session.execute(update(Task).where(Task.id == TASK_ID).values(name="forbidden"))
        assert caught.value.orig.sqlstate == "25006"
        session.rollback()
