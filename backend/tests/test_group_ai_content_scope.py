from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    Action,
    AiGroupMessageMemory,
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
    _recent_account_memories,
    account_profile_summaries,
)
from app.services.task_center.group_ai_scope import validate_group_ai_content_scope
from app.services.task_center.payloads import SendMessagePayload
from app.services.task_center.details import _conversation_quality_status
from app.services.task_center.details import _ai_account_profiles


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_scope(session: Session) -> None:
    now_value = datetime.now(UTC)
    session.add(Tenant(id=1, name="租户"))
    session.add_all([
        TgGroup(id=7, tenant_id=1, tg_peer_id="-1007", title="A群"),
        TgGroup(id=8, tenant_id=1, tg_peer_id="-1008", title="B群"),
        TgAccount(
            id=11,
            tenant_id=1,
            display_name="账号",
            phone_masked="***11",
            status="在线",
            session_ciphertext="session",
        ),
        TgGroupAccount(tenant_id=1, group_id=7, account_id=11, can_send=True),
        TgGroupAccount(tenant_id=1, group_id=8, account_id=11, can_send=True),
        Task(
            id="task-b",
            tenant_id=1,
            name="B群任务",
            type="group_ai_chat",
            status="running",
            type_config={"target_group_id": 8},
        ),
        GroupContextMessage(
            id=701,
            tenant_id=1,
            group_id=7,
            listener_account_id=11,
            content="A群内容",
            remote_message_id="a-1",
            sent_at=now_value,
        ),
        GroupContextMessage(
            id=801,
            tenant_id=1,
            group_id=8,
            listener_account_id=11,
            content="B群内容",
            remote_message_id="b-1",
            sent_at=now_value,
        ),
    ])
    session.commit()


def _payload(**updates) -> SendMessagePayload:
    data = {
        "chat_id": "-1008",
        "group_id": 8,
        "message_text": "",
        "ai_generation_status": "pending",
        "chat_mode": "reply",
        "content_scope_contract_version": "group_content_scope_v1",
        "content_scope_tenant_id": 1,
        "content_scope_group_id": 8,
        "content_scope_task_id": "task-b",
    }
    data.update(updates)
    return SendMessagePayload.model_validate(data)


def _action(payload: SendMessagePayload, *, action_id: str = "action-b") -> Action:
    return Action(
        id=action_id,
        tenant_id=1,
        task_id="task-b",
        task_type="group_ai_chat",
        action_type="send_message",
        account_id=11,
        status="executing",
        payload=payload.model_dump(mode="json"),
    )


def _forbidden_dependencies(calls: dict[str, int]) -> GenerationDependencies:
    def forbidden(*_args, **_kwargs):
        calls["provider"] += 1
        raise AssertionError("scope mismatch must stop before provider")

    return GenerationDependencies(
        normal_generator=forbidden,
        reply_generator=forbidden,
        reply_target_probe=forbidden,
        reply_messages_fetcher=forbidden,
    )


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


def test_pending_duplicate_baseline_never_reads_another_group():
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
    session.add_all([current, other_group, same_group])
    session.commit()

    baseline = _duplicate_baseline_messages(
        session,
        [(current, payload)],
        payload=payload,
    )

    assert "B群待发送正文" in baseline
    assert "A群待发送正文" not in baseline


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


def test_scope_validator_rejects_cross_group_message_memory():
    session = _session()
    _seed_scope(session)
    payload = _payload(message_text="B群待发", ai_generation_status="ready", ai_message_memory_id="memory-a")
    action = _action(payload)
    session.add(action)
    session.add(AiGroupMessageMemory(
        id="memory-a",
        tenant_id=1,
        group_id=7,
        task_id=action.task_id,
        action_id=action.id,
        account_id=11,
        raw_text="A群旧正文",
    ))
    session.commit()

    violation = validate_group_ai_content_scope(session, action, payload=payload, account_id=11)

    assert violation is not None
    assert violation.field == "ai_message_memory_id"


def test_account_prompt_history_reads_only_current_group():
    session = _session()
    _seed_scope(session)
    session.add_all([
        Action(
            id="sent-a",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 7, "message_text": "A群秘密", "account_role": "群友"},
        ),
        Action(
            id="sent-b",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 8, "message_text": "B群本地表达", "account_role": "群友"},
        ),
    ])
    session.commit()
    task = session.get(Task, "task-b")

    memories = _recent_account_memories(session, task, [11], group_id=8, depth=5)
    profiles = account_profile_summaries(session, task, [11], group_id=8)

    assert "B群本地表达" in memories["11"]
    assert "A群秘密" not in memories["11"]
    assert "B群本地表达" in profiles["11"]
    assert "A群秘密" not in profiles["11"]


def test_provider_rebuilds_prompt_inputs_from_target_group_only():
    session = _session()
    _seed_scope(session)
    session.add_all([
        Action(
            id="sent-a-prompt",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 7, "message_text": "A群秘密", "account_role": "群友"},
        ),
        Action(
            id="sent-b-prompt",
            tenant_id=1,
            task_id="task-b",
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 8, "message_text": "B群本地表达", "account_role": "群友"},
        ),
    ])
    context = session.get(GroupContextMessage, 801)
    context.remote_message_id = "8001"
    payload = _payload(
        slot_id="scope-reply-slot",
        reply_to_message_id=8001,
        context_message_ids=[801],
        context_snapshot_message_id=801,
        ai_generation_history="A群秘密",
        account_memory="A群秘密",
        account_profile="近期表达：A群秘密",
    )
    action = _action(payload, action_id="scope-prompt-rebuild")
    session.add(action)
    session.commit()
    observed: list[tuple[str, dict, dict]] = []

    def reject_after_observation(_session, _tenant_id, config, *, history, **_kwargs):
        observed.append((history, config["account_memories"], config["account_profiles"]))
        raise AiGenerationUnavailable("forced_provider_stop")

    dependencies = GenerationDependencies(
        normal_generator=reject_after_observation,
        reply_generator=reject_after_observation,
        reply_target_probe=lambda *_args, **_kwargs: type("Probe", (), {"ok": True, "detail": ""})(),
        reply_messages_fetcher=lambda *_args, **_kwargs: [type("Message", (), {"remote_message_id": "8001"})()],
    )

    with pytest.raises(AiGenerationUnavailable, match="forced_provider_stop"):
        ensure_send_message_content(
            session,
            action,
            session.get(TgAccount, 11),
            payload=payload,
            credentials=object(),
            dependencies=dependencies,
        )

    assert observed
    for history, memories, profiles in observed:
        assert "B群内容" in history
        assert "A群秘密" not in history
        assert "A群秘密" not in memories.get("11", "")
        assert "A群秘密" not in profiles.get("11", "")


def test_task_detail_account_profiles_use_task_target_group():
    session = _session()
    _seed_scope(session)
    task = session.get(Task, "task-b")
    actions = [
        Action(
            id="profile-a",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 7, "message_text": "A群详情秘密"},
        ),
        Action(
            id="profile-b",
            tenant_id=1,
            task_id=task.id,
            task_type="group_ai_chat",
            action_type="send_message",
            account_id=11,
            status="success",
            payload={"group_id": 8, "message_text": "B群详情表达"},
        ),
    ]
    session.add_all(actions)
    session.commit()

    profiles = _ai_account_profiles(session, task, actions)

    assert len(profiles) == 1
    assert "B群详情表达" in profiles[0]["profile_summary"]
    assert "A群详情秘密" not in profiles[0]["profile_summary"]


def test_success_only_clears_quality_blocker_for_same_scope():
    session = _session()
    _seed_scope(session)
    task = session.get(Task, "task-b")
    failed = _action(_payload(), action_id="quality-failed")
    failed.content_mix_cycle_slot_id = "scope-slot-failed"
    succeeded = _action(_payload(), action_id="quality-succeeded")
    succeeded.content_mix_cycle_slot_id = "scope-slot-succeeded"
    dispatcher._record_conversation_quality_event(
        session,
        failed,
        "pre_gateway_scope_reject_count",
        blocker="cross_group_content_scope_mismatch",
    )
    dispatcher._clear_conversation_quality_blocker(session, succeeded)

    assert task.stats["conversation_quality_active_blocker"] == "cross_group_content_scope_mismatch"
    dispatcher._clear_conversation_quality_blocker(session, failed)
    assert "conversation_quality_active_blocker" not in task.stats


def test_success_does_not_clear_legacy_unscoped_quality_blocker():
    session = _session()
    _seed_scope(session)
    task = session.get(Task, "task-b")
    task.stats = {"conversation_quality_active_blocker": "legacy_unresolved"}

    dispatcher._clear_conversation_quality_blocker(session, _action(_payload()))

    assert task.stats["conversation_quality_active_blocker"] == "legacy_unresolved"
