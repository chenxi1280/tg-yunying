"""A committed operator pause wins over Collector/consumer ORM caches."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Task
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from app.services.task_center.service import pause_task
from tests import test_runtime_retention_protection_postgres as postgres_fixtures
import test_group_clone_update_collector as collector_tests
from test_group_clone_channel_lifecycle import TASK_ID, _apply

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = postgres_fixtures.database


def _seed(database):
    with Session(database) as session:
        collector_tests._seed_runtime(session)
        session.commit()


def test_committed_pause_is_reloaded_before_collector_and_consumer(database):
    _seed(database)
    with Session(database, autoflush=False) as collector:
        cached_task = collector.get(Task, TASK_ID)
        assert cached_task.status == "running"
        with Session(database, autoflush=False) as operator:
            pause_task(operator, 1, TASK_ID, "postgres-pause-test")
        _apply(collector, "slice", final=False)
        collector.commit()
        _apply(collector, "live", final=True)
        assert consume_clone_deliveries(collector, cached_task) == 0
        collector.commit()
    with Session(database) as readback:
        task = readback.get(Task, TASK_ID)
        assert task.status == "paused"
        assert task.stats["clone_start_state"] == "paused"


def test_collector_serializes_with_operator_pause(database):
    _seed(database)
    with Session(database, autoflush=False) as collector:
        _apply(collector, "slice", final=False)
        with Session(database) as contender:
            contender.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                contender.scalar(select(Task).where(Task.id == TASK_ID).with_for_update())
        collector.commit()
        _apply(collector, "live", final=True)
        collector.commit()
        with Session(database, autoflush=False) as operator:
            pause_task(operator, 1, TASK_ID, "postgres-pause-test")
        _apply(collector, "too_long", final=False)
        collector.commit()
    with Session(database) as readback:
        assert readback.get(Task, TASK_ID).status == "paused"
        assert readback.scalar(select(TelegramAuthorizationUpdateState)) is not None
