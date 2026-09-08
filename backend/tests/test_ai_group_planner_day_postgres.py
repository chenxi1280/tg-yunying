"""Validate independent calendar commits against real PostgreSQL locks."""
import os
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Action, Task, TaskDayLedger, TaskGroupDailyTarget
from app.services.task_center.ai_group_planner_day import prepare_ai_group_task_day
from test_ai_group_planner_day import NOW, TASK_ID, seed_calendar_task

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]


@pytest.fixture
def factory(postgres_test_session_lock):
    url = make_url(os.environ["TEST_DATABASE_URL"])
    assert url.database == "tg_yunying_test"
    schema = "group_day_test_" + uuid4().hex
    admin = create_engine(url)
    with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        engine = create_engine(url, connect_args={
            "options": f"-csearch_path={schema} -clock_timeout=1000"})
        try:
            Base.metadata.create_all(engine)
            result = sessionmaker(bind=engine, autoflush=False)
            with result() as session:
                seed_calendar_task(session)
                session.commit()
            yield result
        finally:
            engine.dispose()
            connection.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
    admin.dispose()


def test_body_rollback_cannot_erase_calendar_or_move_unknown(factory):
    assert prepare_ai_group_task_day(factory, TASK_ID, now=NOW)
    with factory() as body:
        body.get(Task, TASK_ID).last_error = "test later planning failure"
        body.rollback()
    assert not prepare_ai_group_task_day(factory, TASK_ID, now=NOW)
    with factory() as session:
        current = session.scalar(select(TaskDayLedger).where(
            TaskDayLedger.task_id == TASK_ID, TaskDayLedger.obligation_local_date == NOW.date()))
        assert session.query(TaskGroupDailyTarget).filter_by(task_day_ledger_id=current.id).count() == 1
        old = session.get(Action, "old-unknown")
        assert old.status == "unknown_after_send"
        assert old.payload["task_day_ledger_id"] != current.id


def test_calendar_respects_task_lock_and_pause(factory):
    with factory() as operator:
        task = operator.scalar(select(Task).where(Task.id == TASK_ID).with_for_update())
        assert not prepare_ai_group_task_day(factory, TASK_ID, now=NOW)
        task.status = "paused"
        operator.commit()
    assert not prepare_ai_group_task_day(factory, TASK_ID, now=NOW)
    with factory() as session:
        assert session.query(TaskDayLedger).filter_by(
            task_id=TASK_ID, obligation_local_date=NOW.date()).count() == 0
