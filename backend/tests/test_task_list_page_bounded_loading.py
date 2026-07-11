from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Task, Tenant
from app.services.task_center import list_task_page


pytestmark = pytest.mark.no_postgres


def test_task_list_page_loads_full_task_objects_for_current_page_only() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    created_at = datetime(2026, 7, 11, 8, 0, tzinfo=UTC)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="任务轻量索引租户"))
        session.add_all(
            Task(
                id=f"bounded-task-{index}",
                tenant_id=1,
                name=f"有界任务 {index}",
                type="channel_view",
                status="running",
                priority=3,
                type_config={"target_channel_name": f"频道 {index}", "large_unused_config": "x" * 10_000},
                created_at=created_at - timedelta(minutes=index),
                updated_at=created_at - timedelta(minutes=index),
            )
            for index in range(6)
        )
        session.commit()
        session.expunge_all()
        loaded_task_ids: list[str] = []

        def capture_loaded_task(_session: Session, instance: object) -> None:
            if isinstance(instance, Task):
                loaded_task_ids.append(instance.id)

        event.listen(session, "loaded_as_persistent", capture_loaded_task)
        result = list_task_page(
            session,
            tenant_id=1,
            page=2,
            page_size=2,
            task_type=None,
            status=None,
            q="",
            group_key=None,
        )

    assert [item["id"] for item in result.items] == ["bounded-task-2", "bounded-task-3"]
    assert len(loaded_task_ids) == 2
    assert set(loaded_task_ids) == {"bounded-task-2", "bounded-task-3"}
