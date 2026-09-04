from datetime import timedelta, timezone

import pytest

from app.models import Action, ConversationTurnClaim, Task, TgGroup
from app.services.task_center.engagement_conversation import (
    bind_conversation_turn_claim, interaction_reply_targets,
    validate_conversation_turn_claim_for_gateway,
)
from app.services.task_center.engagement_target_scope import ensure_task_target_scope_claims
from tests.test_engagement_conversation import NOW, _bound_reply_action, _message, session  # noqa: F401

pytestmark = pytest.mark.no_postgres

@pytest.mark.parametrize("terminal", ["served", "stale", "missed", "unknown_after_send", "post_send_intercepted"])
def test_terminal_claim_cannot_be_resurrected_by_repeated_binding(session, terminal) -> None:
    action, target = _bound_reply_action(session, local_id=191, remote_id=591, action_id="bound", content="问题？")
    claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    session.autoflush = False
    claim.state = terminal
    with pytest.raises(RuntimeError, match="claim_terminal"):
        bind_conversation_turn_claim(session, action)
    assert claim.state == terminal


def test_failed_action_cannot_be_replaced_to_restart_same_turn(session) -> None:
    action, target = _bound_reply_action(session, local_id=192, remote_id=592, action_id="bound", content="问题？")
    action.status = "failed"
    replacement = Action(
        id="replacement", tenant_id=1, task_id=action.task_id, task_type=action.task_type,
        action_type=action.action_type, task_lifecycle_epoch=action.task_lifecycle_epoch,
        account_id=action.account_id, status="pending", payload=dict(action.payload),
    )
    session.add(replacement)
    session.flush()
    with pytest.raises(RuntimeError, match="claim_already_bound"):
        bind_conversation_turn_claim(session, replacement)
    assert session.get(ConversationTurnClaim, target["conversation_turn_claim_id"]).action_id == action.id


def test_frozen_claim_account_cannot_change_even_on_same_action(session) -> None:
    action, target = _bound_reply_action(session, local_id=193, remote_id=593, action_id="bound", content="问题？")
    action.account_id = 99
    with pytest.raises(RuntimeError, match="claim_mismatch"):
        bind_conversation_turn_claim(session, action)
    assert validate_conversation_turn_claim_for_gateway(session, action, now_value=NOW) == (False, "conversation_turn_claim_mismatch")
    assert session.get(ConversationTurnClaim, target["conversation_turn_claim_id"]).account_id == 11


def test_utc_human_event_is_still_fresh_on_beijing_runtime_clock(session) -> None:
    task, group = session.get(Task, "group-task"), session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    sent_at = (NOW - timedelta(hours=8, seconds=7)).replace(tzinfo=timezone.utc)
    message = _message(session, 194, 594, sent_at, "实时问题？")
    targets = interaction_reply_targets(session, task, group, context_rows=[message], now_value=NOW)
    assert len(targets) == 1
    assert targets[0]["message_id"] == 594
