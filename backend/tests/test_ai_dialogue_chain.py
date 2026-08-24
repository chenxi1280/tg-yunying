from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, FulfillmentRemoteFact, Task
from app.services.task_center.ai_dialogue_chain import (
    WAITING_PARENT_REMOTE_FACT,
    link_existing_dialogue_chain,
    resolve_waiting_dialogue_dependencies,
)


pytestmark = pytest.mark.no_postgres


def test_chain_uses_existing_slots_and_waits_for_typed_remote_fact(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.task_center.ai_dialogue_chain._minimum_group_gap",
        lambda: timedelta(seconds=30),
    )
    with Session(_engine()) as session:
        task = _task()
        parent = _action("parent", account_id=11, due_offset=0, obligation_id="slot-a")
        child = _action("child", account_id=12, due_offset=90, obligation_id="slot-b")
        session.add_all((task, parent, child))
        session.flush()
        original_due = child.scheduled_at

        assert link_existing_dialogue_chain(task, [child, parent], context_mode="silence")
        assert session.query(Action).count() == 2
        assert child.payload["ai_generation_status"] == WAITING_PARENT_REMOTE_FACT
        assert child.scheduled_at == original_due
        assert resolve_waiting_dialogue_dependencies(session, limit=10) == 0

        parent.status = "success"
        session.add(_remote_fact(parent.id, "456"))
        session.flush()

        assert resolve_waiting_dialogue_dependencies(session, limit=10) == 1
        assert child.payload["reply_to_message_id"] == 456
        assert child.payload["dialogue_chain_state"] == "parent_remote_fact_bound"
        assert child.payload["ai_generation_status"] == "pending"
        assert child.scheduled_at == original_due


def test_parent_failure_returns_child_to_original_independent_slot(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.task_center.ai_dialogue_chain._minimum_group_gap",
        lambda: timedelta(seconds=30),
    )
    with Session(_engine()) as session:
        task = _task()
        parent = _action("parent", account_id=11, due_offset=0, obligation_id="slot-a")
        child = _action("child", account_id=12, due_offset=90, obligation_id="slot-b")
        session.add_all((task, parent, child))
        session.flush()
        assert link_existing_dialogue_chain(task, [parent, child], context_mode="bootstrap")
        parent.status = "failed"
        session.flush()

        assert resolve_waiting_dialogue_dependencies(session, limit=10) == 1
        assert child.payload["dialogue_chain_state"] == "parent_failed_independent"
        assert child.payload["reply_to_message_id"] is None
        assert child.payload["ai_generation_status"] == "pending"


def test_chain_is_opt_in_and_respects_existing_pacing_gap(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.task_center.ai_dialogue_chain._minimum_group_gap",
        lambda: timedelta(seconds=60),
    )
    task = _task()
    parent = _action("parent", account_id=11, due_offset=0, obligation_id="slot-a")
    child = _action("child", account_id=12, due_offset=30, obligation_id="slot-b")
    assert not link_existing_dialogue_chain(task, [parent, child], context_mode="silence")
    assert child.payload["ai_generation_status"] == "pending"
    task.type_config = {"ai_dialogue_chain_enabled": False}
    child.scheduled_at += timedelta(seconds=60)
    assert not link_existing_dialogue_chain(task, [parent, child], context_mode="silence")


def test_chain_deadline_accepts_aware_payload_with_naive_runtime(monkeypatch) -> None:
    monkeypatch.setattr(
        "app.services.task_center.ai_dialogue_chain._minimum_group_gap",
        lambda: timedelta(seconds=30),
    )
    monkeypatch.setattr(
        "app.services.task_center.ai_dialogue_chain._now",
        lambda: datetime(2026, 8, 25, 20, 1),
    )
    with Session(_engine()) as session:
        task = _task()
        parent = _action("parent", account_id=11, due_offset=0, obligation_id="slot-a")
        child = _action("child", account_id=12, due_offset=90, obligation_id="slot-b")
        child.payload = {
            **dict(child.payload or {}),
            "obligation_deadline_at": "2026-08-25T12:00:00Z",
        }
        session.add_all((task, parent, child))
        session.flush()
        assert link_existing_dialogue_chain(task, [parent, child], context_mode="silence")

        assert resolve_waiting_dialogue_dependencies(session, limit=10) == 1
        assert child.payload["dialogue_chain_state"] == "parent_failed_independent"


def _engine():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return engine


def _task() -> Task:
    return Task(
        id="task-chain",
        tenant_id=1,
        name="chain",
        type="group_ai_chat",
        status="running",
        type_config={"ai_dialogue_chain_enabled": True},
    )


def _action(action_id: str, *, account_id: int, due_offset: int, obligation_id: str) -> Action:
    due = datetime(2026, 8, 25, 12, 0, tzinfo=timezone.utc) + timedelta(seconds=due_offset)
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-chain",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=account_id,
        scheduled_at=due,
        status="pending",
        obligation_type="quantity_slot",
        obligation_id=obligation_id,
        payload={"group_id": 7, "message_text": "", "ai_generation_status": "pending"},
    )


def _remote_fact(action_id: str, remote_message_id: str) -> FulfillmentRemoteFact:
    return FulfillmentRemoteFact(
        tenant_id=1,
        task_type="group_ai_chat",
        task_id="task-chain",
        obligation_type="quantity_slot",
        obligation_id="slot-a",
        action_id=action_id,
        attempt_id="attempt-a",
        mutation_kind="send_message",
        remote_mutation_key_hash="a" * 64,
        gateway_request_hash="b" * 64,
        fact_kind="remote_message_observed",
        fact_identity_hash="c" * 64,
        outcome={"remote_message_id": remote_message_id},
    )
