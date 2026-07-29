from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models import Task, Tenant
from scripts import takeover_all_task_fulfillment as takeover_script


pytestmark = pytest.mark.no_postgres


def test_structural_blocker_pauses_only_invalid_task(
    monkeypatch,
) -> None:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, future=True)
    with session_factory() as session:
        session.add(Tenant(id=1, name="单用户"))
        session.add(
            Task(
                id="invalid-legacy-search",
                tenant_id=1,
                name="缺少纯点击字段",
                type="search_join_group",
                status="running",
                type_config={},
            )
        )
        session.commit()
    monkeypatch.setattr(takeover_script, "SessionLocal", session_factory)

    preview = takeover_script.run_takeover(apply=False)
    applied = takeover_script.run_takeover(apply=True)

    assert preview["failures"] == []
    assert preview["blockers"][0]["persisted"] is False
    assert applied["failures"] == []
    assert applied["blockers"][0]["persisted"] is True
    with session_factory() as session:
        task = session.get(Task, "invalid-legacy-search")
        assert task is not None
        assert task.status == "paused"
        assert task.next_run_at is None
        assert task.stats["fulfillment_takeover_blocker_code"] == (
            "task_contract_invalid"
        )


def test_unexpected_value_error_fails_takeover(
    monkeypatch,
) -> None:
    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, *_args):
            return object()

        def rollback(self):
            return None

    monkeypatch.setattr(
        takeover_script,
        "_task_ids",
        lambda _tenant_id: ["unexpected"],
    )
    monkeypatch.setattr(
        takeover_script,
        "SessionLocal",
        FakeSession,
    )
    monkeypatch.setattr(
        takeover_script,
        "takeover_task",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(ValueError("bug")),
    )

    result = takeover_script.run_takeover(apply=True)

    assert result["blockers"] == []
    assert result["failures"] == [
        {"task_id": "unexpected", "error": "ValueError:bug"}
    ]
