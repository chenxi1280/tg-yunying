from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupMessageMemory,
    ExecutionAttempt,
    GroupContextMessage,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
    TgGroup,
    TgGroupAccount,
)
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_dispatch import (
    _duplicate_baseline_messages,
    _require_normal_context_watermark,
    _record_should_speak_shadow,
    ensure_send_message_content,
)
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import dispatcher
from app.services.task_center.executors.group_ai_chat import (
    _group_reply_target_pool,
    _historical_group_reply_targets,
    _recent_account_memories,
    account_profile_summaries,
)
from app.services.task_center.group_ai_scope import validate_group_ai_content_scope
from app.services.task_center.payloads import SendMessagePayload
from app.services.task_center.details import _conversation_quality_status
from app.services.task_center.details import _ai_account_profiles
from group_ai_content_scope_test_support import (
    _action,
    _forbidden_dependencies,
    _payload,
    _seed_scope,
    _session,
)


pytestmark = pytest.mark.no_postgres


def test_scope_validator_rejects_a_group_context_for_b_group_action():
    session = _session()
    _seed_scope(session)
    payload = _payload(context_message_ids=[701], context_snapshot_message_id=701)
    action = _action(payload)
    session.add(action)
    session.commit()

    violation = validate_group_ai_content_scope(
        session,
        action,
        payload=payload,
        account_id=11,
    )

    assert violation is not None
    assert violation.code == "cross_group_content_scope_mismatch"
    assert violation.field == "context_message_ids"


def test_legacy_action_without_scope_contract_requires_replan():
    session = _session()
    _seed_scope(session)
    payload = _payload(
        content_scope_contract_version="",
        content_scope_tenant_id=None,
        content_scope_group_id=None,
        content_scope_task_id="",
    )
    action = _action(payload)
    session.add(action)
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.code == "scope_contract_missing"
    assert violation.field == "scope_contract"

    calls = {"provider": 0}
    with pytest.raises(AiGenerationUnavailable, match="scope_contract_missing"):
        ensure_send_message_content(
            session,
            action,
            session.get(TgAccount, 11),
            payload=payload,
            credentials=object(),
            dependencies=_forbidden_dependencies(calls),
        )

    assert calls == {"provider": 0}
    assert action.status == "failed"
    assert action.result["error_code"] == "scope_contract_missing"


def test_partial_scope_contract_remains_cross_group_mismatch():
    session = _session()
    _seed_scope(session)
    payload = _payload(content_scope_task_id="")
    action = _action(payload)
    session.add(action)
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.code == "cross_group_content_scope_mismatch"


def test_scope_validator_accepts_operation_target_bound_to_same_group():
    session = _session()
    _seed_scope(session)
    session.add(OperationTarget(
        id=88,
        tenant_id=1,
        target_type="group",
        tg_peer_id="-1008",
        title="B群运营目标",
    ))
    task = session.get(Task, "task-b")
    task.type_config = {"target_operation_target_id": 88}
    payload = _payload(context_message_ids=[801], context_snapshot_message_id=801)
    action = _action(payload)
    session.add(action)
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is None


def test_scope_validator_accepts_authoritative_own_history_without_listener_context():
    session = _session()
    _seed_scope(session)
    prior = _successful_own_history_action(remote_message_id="3721281")
    payload = _payload(reply_to_message_id=3721281)
    action = _action(payload, action_id="current-own-history-reply")
    session.add_all([prior, action])
    session.flush()
    session.add(ExecutionAttempt(
        action_id=prior.id,
        status="success",
        remote_message_id="3721281",
    ))
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is None


def test_scope_validator_rejects_human_context_as_reply_target():
    session = _session()
    _seed_scope(session)
    human = session.get(GroupContextMessage, 801)
    human.remote_message_id = "3721281"
    payload = _payload(reply_to_message_id=3721281)
    action = _action(payload, action_id="current-human-context-reply")
    session.add(action)
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.field == "reply_to_message_id"


def test_reply_target_pool_ignores_human_context_rows():
    session = _session()
    _seed_scope(session)
    human = session.get(GroupContextMessage, 801)
    human.remote_message_id = "3721281"
    session.commit()

    targets = _group_reply_target_pool(
        session,
        session.get(Task, "task-b"),
        session.get(TgGroup, 8),
        [human],
    )

    assert targets == []


def test_scope_validator_rejects_own_history_without_successful_attempt():
    session = _session()
    _seed_scope(session)
    prior = _successful_own_history_action(remote_message_id="3721281")
    payload = _payload(reply_to_message_id=3721281)
    action = _action(payload, action_id="current-unproven-own-history-reply")
    session.add_all([prior, action])
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.field == "reply_to_message_id"


@pytest.mark.parametrize(("prior_task_id", "prior_group_id"), [
    ("other-task", 8),
    ("task-b", 7),
])
def test_scope_validator_rejects_own_history_outside_current_task_or_group(
    prior_task_id: str,
    prior_group_id: int,
):
    session = _session()
    _seed_scope(session)
    if prior_task_id == "other-task":
        session.add(Task(
            id=prior_task_id,
            tenant_id=1,
            name="其他任务",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 8},
        ))
    prior = _successful_own_history_action(remote_message_id="3721281")
    prior.task_id = prior_task_id
    prior.payload = {**prior.payload, "group_id": prior_group_id}
    payload = _payload(reply_to_message_id=3721281)
    action = _action(payload, action_id=f"current-reply-{prior_task_id}-{prior_group_id}")
    session.add_all([prior, action])
    session.flush()
    session.add(ExecutionAttempt(action_id=prior.id, status="success", remote_message_id="3721281"))
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.field == "reply_to_message_id"


def test_historical_reply_targets_use_success_attempt_for_normal_result_shape():
    session = _session()
    _seed_scope(session)
    prior = _successful_own_history_action(remote_message_id="3721281")
    prior.result = {"telegram_msg_id": "3721281"}
    session.add(prior)
    session.flush()
    session.add(ExecutionAttempt(
        action_id=prior.id,
        status="success",
        remote_message_id="3721281",
    ))
    session.commit()

    targets = _historical_group_reply_targets(
        session,
        session.get(Task, "task-b"),
        session.get(TgGroup, 8),
    )

    assert targets == [{
        "message_id": 3721281,
        "author": "B群",
        "preview": "托管账号此前已发送正文",
        "source": "own_history",
    }]


def test_own_history_limit_is_applied_after_cross_task_used_targets_are_excluded():
    session = _session()
    _seed_scope(session)
    session.add(Task(
        id="other-task",
        tenant_id=1,
        name="其他任务",
        type="group_ai_chat",
        status="running",
        type_config={"target_group_id": 8},
    ))
    now_value = datetime.now(UTC)
    for index in range(25):
        remote_id = str(3800000 + index)
        prior = _successful_own_history_action(remote_message_id=remote_id)
        prior.id = f"prior-own-history-{index}"
        prior.executed_at = now_value - timedelta(minutes=index)
        session.add(prior)
        session.flush()
        session.add(ExecutionAttempt(
            action_id=prior.id,
            status="success",
            remote_message_id=remote_id,
        ))
        if index < 20:
            session.add(Action(
                id=f"used-own-history-{index}",
                tenant_id=1,
                task_id="other-task",
                task_type="group_ai_chat",
                action_type="send_message",
                account_id=11,
                status="pending",
                payload={"group_id": 8, "reply_to_message_id": int(remote_id)},
            ))
    session.commit()

    targets = _group_reply_target_pool(
        session,
        session.get(Task, "task-b"),
        session.get(TgGroup, 8),
        [],
    )

    assert [target["message_id"] for target in targets] == [3800020, 3800021, 3800022, 3800023, 3800024]


def test_completed_reply_does_not_permanently_consume_own_history_target():
    session = _session()
    _seed_scope(session)
    for remote_id in ("3900001", "3900002"):
        prior = _successful_own_history_action(remote_message_id=remote_id)
        session.add(prior)
        session.flush()
        session.add(ExecutionAttempt(
            action_id=prior.id,
            status="success",
            remote_message_id=remote_id,
        ))
    session.add_all([
        Action(
            id="completed-reply-use",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 8, "reply_to_message_id": 3900001},
        ),
        Action(
            id="pending-reply-use",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="pending",
            payload={"group_id": 8, "reply_to_message_id": 3900002},
        ),
    ])
    session.commit()

    targets = _group_reply_target_pool(
        session,
        session.get(Task, "task-b"),
        session.get(TgGroup, 8),
        [],
    )

    assert [target["message_id"] for target in targets] == [3900001]


def _successful_own_history_action(*, remote_message_id: str) -> Action:
    return Action(
        id=f"prior-own-history-{remote_message_id}",
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="success",
        payload={"group_id": 8, "message_text": "托管账号此前已发送正文"},
        result={"remote_message_id": remote_message_id},
        executed_at=datetime.now(UTC),
    )


def test_scope_mismatch_stops_before_provider_and_never_falls_back():
    session = _session()
    _seed_scope(session)
    payload = _payload(context_message_ids=[701], context_snapshot_message_id=701)
    action = _action(payload)
    session.add(action)
    session.commit()
    calls = {"provider": 0}

    with pytest.raises(AiGenerationUnavailable, match="cross_group_content_scope_mismatch"):
        ensure_send_message_content(
            session,
            action,
            session.get(TgAccount, 11),
            payload=payload,
            credentials=object(),
            dependencies=_forbidden_dependencies(calls),
        )

    assert calls == {"provider": 0}
    assert action.status == "failed"
    assert action.result["error_code"] == "cross_group_content_scope_mismatch"
    assert action.payload["ai_generation_status"] == "cross_group_content_scope_mismatch"
    assert action.payload.get("message_text") != "签到"
    task = session.get(Task, "task-b")
    assert task.stats["pre_provider_scope_reject_count"] == 1
    assert task.stats["conversation_quality_active_blocker"] == "cross_group_content_scope_mismatch"


def test_pending_duplicate_baseline_reads_only_same_group_and_account():
    session = _session()
    _seed_scope(session)
    payload = _payload(message_text="")
    current = _action(payload, action_id="action-current-b")
    other_group = _action(payload, action_id="action-old-a")
    other_group.status = "pending"
    other_group.payload = {
        **dict(other_group.payload or {}),
        "group_id": 7,
        "message_text": "A群待发送正文",
    }
    same_group = _action(payload, action_id="action-pending-b")
    same_group.status = "pending"
    same_group.payload = {
        **dict(same_group.payload or {}),
        "message_text": "B群待发送正文",
    }
    other_account = _action(payload, action_id="action-other-account-b")
    other_account.account_id = 12
    other_account.status = "pending"
    other_account.payload = {
        **dict(other_account.payload or {}),
        "message_text": "B群其他账号待发送正文",
    }
    session.add_all([current, other_group, same_group, other_account])
    session.commit()

    baseline = _duplicate_baseline_messages(
        session,
        [(current, payload)],
        payload=payload,
    )

    assert "B群待发送正文" in baseline
    assert "A群待发送正文" not in baseline
    assert "B群其他账号待发送正文" not in baseline


def test_normal_generation_waits_when_listener_watermark_is_unproven():
    session = _session()
    _seed_scope(session)
    payload = _payload(
        chat_mode="idle_warmup",
        context_message_ids=[801],
        context_snapshot_message_id=801,
    )
    action = _action(payload)
    session.add(action)
    session.commit()
    calls = {"provider": 0}

    with pytest.raises(AiGenerationUnavailable, match="context_freshness_unproven"):
        ensure_send_message_content(
            session,
            action,
            session.get(TgAccount, 11),
            payload=payload,
            credentials=object(),
            dependencies=_forbidden_dependencies(calls),
        )

    assert calls == {"provider": 0}
    assert action.status == "pending"
    assert action.result["error_code"] == "context_freshness_unproven"
    task = session.get(Task, "task-b")
    assert task.stats["context_freshness_unproven_count"] == 1


def test_normal_generation_accepts_fresh_listener_watermark():
    session = _session()
    _seed_scope(session)
    group = session.get(TgGroup, 8)
    context = session.get(GroupContextMessage, 801)
    group.listener_enabled = True
    group.listener_last_polled_at = context.sent_at + timedelta(seconds=1)
    group.listener_remote_cursor = "8001"
    group.listener_cursor_status = "contiguous"
    payload = _payload(
        chat_mode="idle_warmup",
        context_message_ids=[801],
        context_snapshot_message_id=801,
    )
    action = _action(payload)
    session.add(action)
    session.commit()

    _require_normal_context_watermark(
        session,
        session.get(Task, "task-b"),
        action,
        payload=payload,
    )

    assert action.status == "executing"


def test_scope_mismatch_stops_at_gateway_precondition():
    session = _session()
    _seed_scope(session)
    payload = _payload(
        message_text="准备发送",
        ai_generation_status="ready",
        voice_profile_contract_version="style_only_v2",
        context_message_ids=[701],
        context_snapshot_message_id=701,
    )
    action = _action(payload)
    session.add(action)
    session.commit()
    context = dispatcher.GroupSendGatewayContext(
        account=session.get(TgAccount, 11),
        credentials=object(),
        group=session.get(TgGroup, 8),
        link=session.query(TgGroupAccount).filter_by(group_id=8, account_id=11).one(),
        payload=payload,
        content=payload.message_text,
    )

    allowed = dispatcher._group_send_preconditions_pass(session, action, context)

    assert allowed is False
    assert action.status == "failed"
    assert action.result["error_code"] == "cross_group_content_scope_mismatch"
    assert action.payload.get("message_text") != "签到"


@pytest.mark.parametrize(
    ("stats", "expected"),
    [
        ({}, "evaluating"),
        ({"conversation_quality_active_blocker": "context_superseded_requeue"}, "at_risk"),
        ({"conversation_quality_active_blocker": "cross_group_content_scope_mismatch"}, "blocked"),
        ({"conversation_quality_e4_passed": True}, "met"),
    ],
)
def test_conversation_quality_status_does_not_fake_met(stats, expected):
    assert _conversation_quality_status(stats) == expected


def test_question_floor_shadow_waits_until_new_human_message():
    session = _session()
    _seed_scope(session)
    now_value = datetime.now(UTC)
    prior = Action(
        id="prior-question",
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="success",
        executed_at=now_value,
        payload={"group_id": 8, "message_text": "大家怎么看？"},
    )
    current = _action(_payload(), action_id="current-shadow")
    human = session.get(GroupContextMessage, 801)
    human.sent_at = now_value - timedelta(seconds=10)
    session.add_all([prior, current])
    session.commit()
    task = session.get(Task, "task-b")

    _record_should_speak_shadow(session, task, current, payload=_payload())

    assert current.result["should_speak_shadow_decision"] == "wait"
    assert current.result["should_speak_shadow_reason"] == "awaiting_human_response"
    assert current.result["should_speak_shadow_observed_watermark"]
    assert current.result["should_speak_shadow_next_eligible_at"] is None
    assert task.stats["question_floor_shadow_violation_count"] == 1

    human.sent_at = now_value + timedelta(seconds=10)
    session.commit()
    _record_should_speak_shadow(session, task, current, payload=_payload())

    assert current.result["should_speak_shadow_decision"] == "send"
    assert current.result["awaiting_human_response_shadow"] is False
    assert current.result["should_speak_shadow_next_eligible_at"]
