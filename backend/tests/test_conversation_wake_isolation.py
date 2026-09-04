from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ContextTurn, StageWakeOutbox, Task, TgGroup, InteractionOpportunity
from app.services.task_center.engagement_target_scope import ensure_task_target_scope_claims
from app.services.task_center.engagement_conversation import project_group_context_message
from app.services.task_center import engagement_conversation_wake as wakes
from test_engagement_conversation import session, _message, NOW  # noqa: F401

pytestmark = pytest.mark.no_postgres


def _two_turns(session):
    ensure_task_target_scope_claims(session, session.get(Task, "group-task"))
    group = session.get(TgGroup, 10)
    for number in (1, 2):
        message = _message(session, number, 500+number, NOW-timedelta(seconds=10), "问题？")
        message.sender_peer_id = f"human-{number}"
        project_group_context_message(session, group, message)
    session.commit()
    return list(session.scalars(select(ContextTurn).order_by(ContextTurn.anchor_event_id)))


def test_single_wake_only_materializes_its_own_turn(session):
    turns = _two_turns(session)
    assert wakes.drain_due_conversation_wakes(session, now_value=NOW, limit=1) == 1
    assert sorted(turn.state for turn in turns) == ["assembling", "closed"]
    assert len(list(session.scalars(select(InteractionOpportunity)))) == 1
    assert sorted(session.scalars(select(StageWakeOutbox.state))) == ["delivered", "pending"]


def test_failed_wake_rolls_back_only_itself_and_processes_next(session, monkeypatch, caplog):
    turns = _two_turns(session)
    original = wakes.materialize_turn
    def materialize(session, task, turn, *, current):
        original(session, task, turn, current=current)
        if turn.id == turns[0].id:
            raise ValueError("injected_wake_failure")
    monkeypatch.setattr(wakes, "materialize_turn", materialize)
    assert wakes.drain_due_conversation_wakes(session, now_value=NOW) == 1
    assert turns[0].state == "assembling" and turns[1].state == "closed"
    first_wake = session.scalar(select(StageWakeOutbox).where(StageWakeOutbox.aggregate_id == turns[0].id))
    assert first_wake.state == "pending" and first_wake.delivered_at is None
    assert "conversation_wake_failed" in caplog.text
