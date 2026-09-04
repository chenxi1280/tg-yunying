from datetime import timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.models import ContextTurn, ConversationTurnClaim, InteractionOpportunity, Task, TgGroup
from app.services.task_center import engagement_circuit_probe as probes
from app.services.task_center import engagement_runtime_circuit as circuits
from app.services.task_center.engagement_conversation import (
    materialize_due_turns,
    project_group_context_message,
)
from app.timezone import BEIJING_TZ, as_beijing_aware
from test_engagement_conversation import NOW, _message, session  # noqa: F401


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize("offset", [BEIJING_TZ, timezone.utc])
@pytest.mark.parametrize("expired", [False, True])
def test_aware_turn_deadline_materializes_without_stopping_planner(session, offset, expired):
    task, group = session.get(Task, "group-task"), session.get(TgGroup, 10)
    message = _message(session, 1, 501, NOW - timedelta(seconds=8), "几点开始？")
    project_group_context_message(session, group, message)
    turn = session.scalar(select(ContextTurn))
    turn.closed_at = as_beijing_aware(turn.closed_at).astimezone(offset)
    current = NOW + timedelta(minutes=2) if expired else NOW

    materialize_due_turns(session, task, group, now_value=current)

    opportunity = session.scalar(select(InteractionOpportunity))
    assert opportunity.state == ("missed" if expired else "admitted")
    assert (session.scalar(select(ConversationTurnClaim)) is None) == expired
    assert turn.state == "closed"


@pytest.mark.parametrize("offset", [BEIJING_TZ, timezone.utc])
@pytest.mark.parametrize("state", ["open", "half_open"])
@pytest.mark.parametrize("expired", [False, True])
def test_probe_claim_handles_database_aware_deadlines(offset, state, expired):
    deadline = as_beijing_aware(NOW + timedelta(seconds=-1 if expired else 1))
    circuit = SimpleNamespace(state=state, opened_until=deadline.astimezone(offset),
                              probe_lease_until=deadline.astimezone(offset))
    assert probes._claimable(circuit, NOW) == expired


@pytest.mark.parametrize("offset", [BEIJING_TZ, timezone.utc])
@pytest.mark.parametrize("expired", [False, True])
def test_circuit_gate_returns_typed_blocker_for_database_aware_time(monkeypatch, offset, expired):
    deadline = as_beijing_aware(NOW + timedelta(seconds=-1 if expired else 1))
    circuit = SimpleNamespace(state="open", opened_until=deadline.astimezone(offset))
    monkeypatch.setattr(circuits, "_now", lambda: NOW)
    monkeypatch.setattr(circuits, "_locked_state", lambda *args, **kwargs: circuit)

    blocker = circuits.circuit_blocker(None, tenant_id=1, account_id=11, route_key="", egress_key="")

    assert blocker[0] == ("execution_circuit_probe_pending" if expired else "execution_circuit_open")
