from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, Tenant, TenantAiSetting
from app.services.task_center import (
    ai_generation_dispatch,
    ai_generation_runtime_config,
    ai_generation_worker,
)


pytestmark = pytest.mark.no_postgres


def test_v2_runtime_disables_tenant_static_fallback() -> None:
    with Session(_engine()) as session:
        task = _seed_v2_task(session)

        flags = ai_generation_runtime_config.tenant_fallback_flags(session, task)

        assert flags["_ai_group_static_fallback_enabled"] is False


def test_v2_runtime_disables_due_catch_up_static_pipeline(monkeypatch) -> None:
    with Session(_engine()) as session:
        task = _seed_v2_task(session)
        task.type_config = {
            **task.type_config,
            "due_catch_up_pipeline_depth": 4,
        }
        action = Action(
            id="v2-action",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=1,
            status="pending",
            primary_quantity_slot_id="slot-1",
            payload={"group_id": 7, "ai_generation_status": "pending"},
        )
        session.add(action)
        session.flush()
        monkeypatch.setattr(
            ai_generation_worker,
            "tenant_fallback_flags",
            lambda *_args: {"_ai_group_static_fallback_enabled": True},
        )
        monkeypatch.setattr(
            ai_generation_worker,
            "_content_obligation_fallback_ready",
            lambda *_args: True,
        )
        monkeypatch.setattr(
            ai_generation_worker,
            "_due_catch_up_required",
            lambda *_args: True,
        )

        depth = ai_generation_worker._due_catch_up_pipeline_depth(session, action)

        assert depth == 1


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _seed_v2_task(session: Session) -> Task:
    session.add(Tenant(id=1, name="tenant"))
    task = Task(
        id="v2-canary-task",
        tenant_id=1,
        name="AI 活跃群 canary",
        type="group_ai_chat",
        status="running",
        type_config={
            "ai_two_stage_enabled": True,
            "ai_content_route_v2_enabled": True,
        },
    )
    session.add_all((
        task,
        TenantAiSetting(
            tenant_id=1,
            ai_group_static_fallback_enabled=True,
        ),
    ))
    session.flush()
    return task
