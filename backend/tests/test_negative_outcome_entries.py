from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from sqlalchemy import select

from app.models import (
    Action, ExecutionAttempt, FulfillmentRemoteFact, NegativeOutcomeCircuitState,
    PostSendVisibilityObservation, PostSendVisibilityPolicyRevision, TelegramAuthorizationUpdateDelivery,
)
from app.services._common import _now
from app.services.task_center import negative_outcome_circuit as circuit
from app.services.task_center.engagement_runtime_resources import _assert_negative_outcome_circuit, RuntimeResourceBlocked
from app.services.task_center.post_send_visibility import open_visibility_observation, settle_visibility_observation
from test_group_ai_update_stream import _session, _seed, _ingest, consume_group_ai_update_deliveries
from test_channel_comment_update_stream import _seed as comment_seed, _ingest as comment_ingest, consume_channel_comment_update_deliveries

pytestmark = pytest.mark.no_postgres


def _sent_action(session, task, *, peer, remote_id="500", confirmed=True):
    action = Action(id="sent", tenant_id=1, task_id=task.id, task_type=task.type,
        action_type="send_message" if task.type == "group_ai_chat" else "post_comment",
        account_id=11, payload={"actual_target_peer": peer}, status="success")
    attempt = ExecutionAttempt(id="attempt", tenant_id=1, action_id=action.id, account_id=11,
        remote_message_id=remote_id, status="success", gateway_call_started_at=_now())
    session.add_all([action, attempt])
    if confirmed:
        session.add(FulfillmentRemoteFact(tenant_id=1, task_id=task.id, task_type=task.type,
            action_id=action.id, attempt_id=attempt.id, obligation_type="test", obligation_id="test",
            mutation_kind="send_message", remote_mutation_key_hash="key", gateway_request_hash="req",
            fact_kind="remote_message_observed", fact_identity_hash="fact"))
    session.flush()
    return action, attempt


def _feedback_delivery(session, *, parent="500"):
    delivery = session.scalar(select(TelegramAuthorizationUpdateDelivery).where(
        TelegramAuthorizationUpdateDelivery.delivery_state == "pending"))
    delivery.normalized_payload = {**delivery.normalized_payload,
        "reply_to_message_id": int(parent), "sent_at": _now().isoformat()}
    session.flush()
    return delivery


@pytest.mark.parametrize("route", ["group_ai_chat", "channel_comment"])
def test_real_update_consumption_records_one_attributed_negative_event(route):
    with _session() as session:
        session.autoflush = False
        if route == "group_ai_chat":
            task, target, state = _seed(session)
            peer, ingest, consume = "-1007", _ingest, consume_group_ai_update_deliveries
        else:
            task, target, state, _ = comment_seed(session)
            peer, ingest, consume = "-1008", comment_ingest, consume_channel_comment_update_deliveries
        action, _ = _sent_action(session, task, peer=peer)
        ingest(session, state, remote_id=1001, content="别再刷了，你回复得像机器人")
        delivery = _feedback_delivery(session)
        consume(session, task, target)
        session.flush()
        result = session.scalar(select(NegativeOutcomeCircuitState))
        assert result.level == "proactive_throttled"
        assert result.route == route and result.account_id == 11
        assert result.events[0]["evidence"]["action_id"] == action.id
        version, until = result.version, result.eligible_exit_at
        delivery.delivery_state = "pending"
        session.flush()
        consume(session, task, target)
        assert len(result.events) == 1 and result.version == version and result.eligible_exit_at == until
        context = NS(account=NS(id=11))
        with pytest.raises(RuntimeResourceBlocked, match="negative_outcome_policy_blocked"):
            _assert_negative_outcome_circuit(session, action, context=context)
        # Route-specific feedback must not affect another adapter on the same peer/account.
        action.task_type = "channel_comment" if route == "group_ai_chat" else "group_ai_chat"
        _assert_negative_outcome_circuit(session, action, context=context)
        for passive in ("channel_like", "channel_view"):
            action.task_type = passive
            _assert_negative_outcome_circuit(session, action, context=context)


@pytest.mark.parametrize("content,confirmed,peer,parent", [
    ("感谢这个AI机器人帮忙", True, "-1007", "500"),
    ("你回复得像机器人一样准确", True, "-1007", "500"),
    ("机器人技术今天有新发布", True, "-1007", "500"),
    ("别再像机器人一样刷屏", False, "-1007", "500"),
    ("别再像机器人一样刷屏", True, "-1009", "500"),
    ("别再像机器人一样刷屏", True, "-1007", "999"),
])
def test_nonnegative_or_unattributed_messages_do_not_create_circuit(content, confirmed, peer, parent):
    with _session() as session:
        task, group, state = _seed(session)
        _sent_action(session, task, peer=peer, confirmed=confirmed)
        _ingest(session, state, remote_id=1001, content=content)
        _feedback_delivery(session, parent=parent)
        consume_group_ai_update_deliveries(session, task, group)
        assert list(session.scalars(select(NegativeOutcomeCircuitState))) == []


@pytest.mark.parametrize("reason,expected", [("post_send_intercepted", 1), ("not_visible", 0)])
def test_visibility_settlement_produces_only_proven_interception(reason, expected):
    with _session() as session:
        task, _, _ = _seed(session)
        action, attempt = _sent_action(session, task, peer="-1007")
        open_visibility_observation(session, action, attempt=attempt, remote_message_id="500",
            target_peer="-1007", window_seconds=15)
        for _ in range(2):
            settle_visibility_observation(session, action, state="post_send_intercepted", terminal_reason=reason)
        states = list(session.scalars(select(NegativeOutcomeCircuitState)))
        assert len(states) == expected
        if expected:
            assert len(states[0].events) == 1
            assert states[0].level == "proactive_throttled"


def test_read_unknown_and_recovery_are_explicit_and_manual_review_is_sticky(monkeypatch):
    with _session() as session:
        task, _, _ = _seed(session)
        now = _now()
        monkeypatch.setattr(circuit, "_now", lambda: now)
        scope = dict(tenant_id=1, peer_id="-1007", account_id=11, route=task.type)
        circuit.assert_negative_outcome_circuit_clear(session, **scope)
        assert session.scalar(select(NegativeOutcomeCircuitState)) is None
        state = circuit.record_negative_outcome(session, **scope, event_type="unknown", event_id="unknown")
        assert state.level == "normal"
        for number in range(5):
            circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id=str(number))
        assert state.level == "manual_review"
        now += timedelta(hours=1)
        circuit.recover_circuit_from_visibility(session, **scope, observed_at=now)
        circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="new")
        assert state.level == "manual_review"
        # Old re-deliveries cannot re-enter the active counting window.
        version = state.version
        circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="old",
            observed_at=now - timedelta(hours=1))
        assert state.version == version


def test_recovery_needs_hold_window_and_new_visibility_evidence(monkeypatch):
    with _session() as session:
        task, _, _ = _seed(session)
        now = _now()
        monkeypatch.setattr(circuit, "_now", lambda: now)
        scope = dict(tenant_id=1, peer_id="-1007", account_id=11, route=task.type)
        state = circuit.record_negative_outcome(session, **scope, event_type="premature_answer", event_id="one")
        now += timedelta(minutes=10)
        circuit.recover_circuit_from_visibility(session, **scope, observed_at=now)
        assert state.level == "proactive_throttled"
        now += timedelta(minutes=30)
        assert circuit.evaluate_circuit_state(session, **scope).level == "proactive_throttled"
        circuit.recover_circuit_from_visibility(session, **scope, observed_at=now)
        assert state.level == "normal"


def test_new_visibility_policy_does_not_count_the_same_remote_interception_twice():
    with _session() as session:
        task, _, _ = _seed(session)
        action, attempt = _sent_action(session, task, peer="-1007")
        open_visibility_observation(session, action, attempt=attempt, remote_message_id="500",
            target_peer="-1007", window_seconds=15)
        settle_visibility_observation(session, action, state="post_send_intercepted", terminal_reason="post_send_intercepted")
        policy = session.scalar(select(PostSendVisibilityPolicyRevision))
        policy.state = "retired"
        session.add(PostSendVisibilityPolicyRevision(tenant_id=1, revision=2, state="active"))
        session.flush()
        open_visibility_observation(session, action, attempt=attempt, remote_message_id="500",
            target_peer="-1007", window_seconds=15)
        settle_visibility_observation(session, action, state="post_send_intercepted", terminal_reason="post_send_intercepted")
        assert len(list(session.scalars(select(PostSendVisibilityObservation)))) == 2
        result = session.scalar(select(NegativeOutcomeCircuitState))
        assert len(result.events) == 1 and result.level == "proactive_throttled"


@pytest.mark.parametrize("route,payload", [
    ("group_ai_chat", {"conversation_turn_claim_id": "turn"}),
    ("channel_comment", {"comment_mode": "reply", "reply_target_source": "channel_comment"}),
])
def test_proactive_throttle_still_allows_native_responses(route, payload):
    with _session() as session:
        task, _, _ = _seed(session)
        action, _ = _sent_action(session, task, peer="-1007")
        action.task_type = route
        action.payload = {**action.payload, **payload}
        circuit.record_negative_outcome(session, tenant_id=1, peer_id="-1007", account_id=11, route=route,
            event_type="premature_answer", event_id="one")
        _assert_negative_outcome_circuit(session, action, context=NS(account=NS(id=11)))
