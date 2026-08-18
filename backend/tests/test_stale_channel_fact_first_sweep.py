from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant
from app.services._common import _now
from app.services.task_center import dispatcher


pytestmark = pytest.mark.no_postgres


def test_stale_channel_sweep_excludes_fact_first_actions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    today = _now().date()
    stale_date = (today - timedelta(days=1)).isoformat()

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.flush()
        session.add_all(_tasks())
        session.flush()
        session.add_all(_actions(stale_date))
        session.commit()

        assert dispatcher._skip_stale_channel_daily_actions(session, today=today) == 1
        assert session.get(Action, "legacy-stale-action").status == "skipped"
        assert session.get(Action, "current-stale-action").status == "pending"


def _tasks() -> list[Task]:
    return [
        Task(
            id="legacy-stale-view",
            tenant_id=1,
            name="legacy stale",
            type="channel_view",
            status="running",
        ),
        Task(
            id="current-stale-view",
            tenant_id=1,
            name="current stale",
            type="channel_view",
            status="running",
            fulfillment_contract_version="fact_first_v3",
        ),
    ]


def _actions(stale_date: str) -> list[Action]:
    return [
        _action("legacy-stale-action", "legacy-stale-view", stale_date),
        _action("current-stale-action", "current-stale-view", stale_date),
    ]


def _action(action_id: str, task_id: str, stale_date: str) -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id=task_id,
        task_type="channel_view",
        action_type="view_message",
        status="pending",
        scheduled_at=_now(),
        payload={"execution_date": stale_date},
    )
