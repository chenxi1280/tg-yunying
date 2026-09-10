"""No-op channel projections must not wait on unrelated planner transactions."""
import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.models import Task
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services.task_center import telegram_update_channels as channels
from app.services.task_center.service import pause_task
from tests import test_runtime_retention_protection_postgres as postgres_fixtures
import test_group_clone_update_collector as collector_tests
from test_group_clone_channel_lifecycle import TASK_ID, PEER_ID

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = postgres_fixtures.database


def _seed(database, errors, task_type="group_clone"):
    with Session(database) as session:
        collector_tests._seed_runtime(session)
        task = session.get(Task, TASK_ID)
        task.type = task_type
        task.stats = {"clone_start_state": "running", "telegram_update_channel_errors": errors}
        state_id = session.scalar(select(TelegramAuthorizationUpdateState.id))
        session.commit()
        return state_id


@pytest.mark.parametrize("task_type", ["group_clone", "group_ai_chat", "channel_comment"])
@pytest.mark.parametrize("errors,detail", [({}, None), ({"-10099": "other"}, None), ({PEER_ID: "same"}, "same")])
def test_noop_projection_does_not_block_on_planner_task_lock(database, task_type, errors, detail):
    state_id = _seed(database, errors, task_type)
    with Session(database) as planner:
        task = planner.scalar(select(Task).where(Task.id == TASK_ID).with_for_update())
        task.stats = {**task.stats, "concurrent_progress": "preserved"}
        planner.flush()
        with Session(database) as collector:
            collector.execute(text("SET LOCAL lock_timeout='100ms'"))
            if detail is None:
                channels.clear_channel_error_from_tasks(collector, state_id, PEER_ID)
            else:
                channels.project_channel_error_to_tasks(collector, state_id, PEER_ID, detail=detail)
            collector.commit()
        planner.commit()
    with Session(database) as readback:
        assert readback.get(Task, TASK_ID).stats["concurrent_progress"] == "preserved"


def test_actual_error_clear_remains_serialized_and_preserves_committed_pause(database):
    state_id = _seed(database, {PEER_ID: "failed", "-10099": "other"})
    with Session(database, autoflush=False) as collector:
        cached = collector.get(Task, TASK_ID)
        assert cached.status == "running"
        with Session(database) as operator:
            operator.scalar(select(Task).where(Task.id == TASK_ID).with_for_update())
            collector.execute(text("SET LOCAL lock_timeout='100ms'"))
            with pytest.raises(OperationalError, match="lock timeout"):
                channels.clear_channel_error_from_tasks(collector, state_id, PEER_ID)
            collector.rollback()
            pause_task(operator, 1, TASK_ID, "channel-error-pause-test")
        channels.clear_channel_error_from_tasks(collector, state_id, PEER_ID)
        collector.commit()
    with Session(database) as readback:
        task = readback.get(Task, TASK_ID)
        assert task.status == "paused"
        assert task.stats["clone_start_state"] == "paused"
        assert task.stats["telegram_update_channel_errors"] == {"-10099": "other"}
