import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import DBAPIError

from app.database import SessionLocal
from app.models import Task, Tenant
from app.services.task_center.production_e4_scope import configure_readonly_snapshot


TENANT_ID = 990_711
TASK_ID = "e4-snapshot-task"


def test_readonly_snapshot_keeps_committed_concurrent_update_outside_report():
    with SessionLocal() as writer:
        writer.add(Tenant(id=TENANT_ID, name="snapshot test"))
        writer.flush()
        writer.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name="before", type="group_ai_chat"))
        writer.commit()
    statement = select(Task.name).where(Task.id == TASK_ID)
    with SessionLocal() as reader, SessionLocal() as writer:
        configure_readonly_snapshot(reader)
        assert reader.scalar(statement) == "before"
        writer.execute(update(Task).where(Task.id == TASK_ID).values(name="after"))
        writer.commit()
        assert reader.scalar(statement) == "before"
        reader.rollback()
        assert reader.scalar(statement) == "after"


def test_readonly_snapshot_rejects_persistent_write():
    with SessionLocal() as session:
        configure_readonly_snapshot(session)
        with pytest.raises(DBAPIError) as caught:
            session.execute(update(Task).where(Task.id == TASK_ID).values(name="forbidden"))
        assert caught.value.orig.sqlstate == "25006"
        session.rollback()
