from __future__ import annotations

from datetime import datetime, timezone
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task


pytestmark = pytest.mark.no_postgres


def test_align_and_wake_modifies_task_config_correctly():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        t = Task(
            id="6407d98f-e6af-4df8-a10b-806135bf24ff",
            tenant_id=1,
            name="郑州楼凤",
            type="group_ai_chat",
            status="running",
            type_config={"ai_content_route_v2_enabled": True, "ai_model": "MiniMax-M2.5"},
            next_run_at=datetime(2026, 8, 30, 15, 0, tzinfo=timezone.utc),
        )
        session.add(t)
        session.commit()

        # Update
        cfg = dict(t.type_config or {})
        cfg["ai_content_route_v2_enabled"] = False
        cfg["ai_model"] = ""
        t.type_config = cfg
        t.next_run_at = datetime.now(timezone.utc)
        session.commit()

        loaded = session.get(Task, "6407d98f-e6af-4df8-a10b-806135bf24ff")
        assert loaded.type_config["ai_content_route_v2_enabled"] is False
        assert loaded.type_config["ai_model"] == ""
