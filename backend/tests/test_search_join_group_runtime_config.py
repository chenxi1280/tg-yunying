from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.services.task_center.executors import build_task_plan

from test_search_join_group_executor import _bind_search_join_environment, _task, session


@pytest.mark.no_postgres
def test_search_join_planner_keeps_type_hourly_cap_when_pacing_cap_is_none(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(pacing_config={"max_actions_per_hour": None})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 2
    stats = task.stats["search_join_stats"]["hourly_execution"]

    assert stats["max_actions_per_hour"] == 4
    assert stats["capacity"] == 4
    assert stats["last_planned_count"] == 2


@pytest.mark.no_postgres
def test_search_join_planner_respects_explicit_zero_pacing_hourly_cap(session: Session) -> None:
    _bind_search_join_environment(session, [101, 102])
    task = _task(pacing_config={"max_actions_per_hour": 0})
    session.add(task)
    session.commit()

    assert build_task_plan(session, task) == 0
    stats = task.stats["search_join_stats"]["hourly_execution"]

    assert stats["max_actions_per_hour"] == 0
    assert stats["capacity"] == 0
    assert stats["last_planned_count"] == 0
