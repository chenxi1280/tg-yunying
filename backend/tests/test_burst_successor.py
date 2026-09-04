from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.models import ContextTurn, ConversationTurnClaim, ExecutionAttempt, GenerationJob, Task, TgGroup
from app.services.task_center.engagement_conversation import (
    interaction_reply_targets, validate_conversation_turn_claim_for_gateway,
)
from app.services.task_center.engagement_target_scope import ensure_task_target_scope_claims
from app.services.task_center import dispatcher
from test_engagement_conversation import session, _bound_reply_action, _message, NOW

pytestmark = pytest.mark.no_postgres


def test_closed_claim_gets_fresh_successor_and_replay_is_idempotent(session):
    session.autoflush = False
    task, group = session.get(Task, "group-task"), session.get(TgGroup, 10)
    ensure_task_target_scope_claims(session, task)
    first = _message(session, 101, 501, NOW, "请问活动几点开始？")
    before = interaction_reply_targets(session, task, group, context_rows=[first], now_value=NOW + timedelta(seconds=3))[0]
    late = _message(session, 102, 502, NOW + timedelta(seconds=4), "补充一下，我问的是明天的活动。")
    after = interaction_reply_targets(session, task, group, context_rows=[late], now_value=NOW + timedelta(seconds=8))
    assert len(after) == 1
    assert after[0]["message_id"] == 502
    assert after[0]["conversation_turn_claim_id"] != before["conversation_turn_claim_id"]
    claim = session.get(ConversationTurnClaim, after[0]["conversation_turn_claim_id"])
    assert session.get(ContextTurn, claim.context_turn_id).event_count == 2
    assert session.get(ConversationTurnClaim, before["conversation_turn_claim_id"]).state == "stale"
    assert interaction_reply_targets(session, task, group, context_rows=[first, late], now_value=NOW + timedelta(seconds=8)) == after
    assert len(list(session.scalars(select(ConversationTurnClaim)))) == 2


@pytest.mark.parametrize("issued", [False, True])
def test_bound_pre_gateway_supersedes_but_issued_keeps_old_identity(session, issued):
    session.autoflush = False
    action, target = _bound_reply_action(session, local_id=101, remote_id=501, action_id="reply", content="几点开始？")
    if issued:
        session.add(ExecutionAttempt(tenant_id=1, action_id=action.id, account_id=11,
            gateway_call_started_at=NOW, status="result_unknown"))
        session.flush()
    late = _message(session, 102, 502, NOW - timedelta(seconds=3), "我问的是明天。")
    result = interaction_reply_targets(session, session.get(Task, "group-task"), session.get(TgGroup, 10),
        context_rows=[late], now_value=NOW + timedelta(seconds=1))
    assert len(result) == 1
    old_claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    new_claim = session.get(ConversationTurnClaim, result[0]["conversation_turn_claim_id"])
    assert old_claim.state == ("bound" if issued else "stale")
    assert session.get(ContextTurn, new_claim.context_turn_id).event_count == (1 if issued else 2)
    if not issued:
        assert validate_conversation_turn_claim_for_gateway(session, action, now_value=NOW)[0] is False


def test_provider_unknown_does_not_replay_old_aggregated_events(session):
    action, target = _bound_reply_action(session, local_id=101, remote_id=501, action_id="reply", content="几点开始？")
    job = GenerationJob(id="unknown", tenant_id=1, task_id=action.task_id,
        obligation_type="reply", obligation_id="turn", generation_sequence=1,
        context_snapshot_version=1, state="unknown")
    session.add(job)
    action.payload = {**action.payload, "generation_job_id": job.id}
    session.flush()
    late = _message(session, 102, 502, NOW - timedelta(seconds=3), "我问的是明天。")
    targets = interaction_reply_targets(session, session.get(Task, "group-task"), session.get(TgGroup, 10),
        context_rows=[late], now_value=NOW + timedelta(seconds=1))
    old_claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
    new_claim = session.get(ConversationTurnClaim, targets[0]["conversation_turn_claim_id"])
    assert old_claim.state == "bound" and job.state == "unknown"
    assert session.get(ContextTurn, new_claim.context_turn_id).event_count == 1


def test_reply_invalidated_during_remote_probe_is_rejected_before_call_issued(session, monkeypatch):
    action, target = _bound_reply_action(session, local_id=101, remote_id=501, action_id="reply", content="几点开始？")
    session.commit()
    def fetched(*args, **kwargs):
        claim = session.get(ConversationTurnClaim, target["conversation_turn_claim_id"])
        claim.state = "stale"
        session.flush()
        return [NS(remote_message_id="501", content="几点开始？")]
    errors = []
    monkeypatch.setattr(dispatcher.gateway, "fetch_group_messages", fetched)
    monkeypatch.setattr(dispatcher, "_fail_group_ai_send_before_gateway",
        lambda session, action, payload, reason, *args, **kwargs: errors.append(reason))
    context = NS(account=NS(id=11, session_ciphertext="test"), credentials=None,
        group=session.get(TgGroup, 10), payload=NS(conversation_turn_claim_id=target["conversation_turn_claim_id"]))
    attempt = NS(result_snapshot={"telegram_connect_timeout_seconds": 5})
    assert dispatcher._conversation_remote_context_current(session, action, context, attempt) is False
    assert errors == ["conversation_turn_claim_not_owned"]
