from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.models import Action, AiGroupMessageMemory, ExecutionAttempt, GroupContextMessage, Task, TgAccount
from app.services.task_center import dispatcher
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_dispatch import ensure_send_message_content
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center.details import _ai_account_profiles
from app.services.task_center.executors.group_ai_chat import (
    _recent_account_memories,
    account_profile_summaries,
)
from app.services.task_center.group_ai_scope import validate_group_ai_content_scope
from group_ai_content_scope_test_support import _action, _payload, _seed_scope, _session


pytestmark = pytest.mark.no_postgres


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
            executed_at=datetime.now() - timedelta(minutes=1),
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
    session.flush()
    session.add(ExecutionAttempt(
        tenant_id=1,
        action_id="sent-b-prompt",
        status="success",
        remote_message_id="8001",
    ))
    session.commit()
    observed: list[tuple[str, dict, dict]] = []

    def reject_after_observation(_session, _tenant_id, config, *, history, **_kwargs):
        observed.append((history, config["account_memories"], config["account_profiles"]))
        raise AiGenerationUnavailable("forced_provider_stop")

    dependencies = GenerationDependencies(
        normal_generator=reject_after_observation,
        reply_generator=reject_after_observation,
        reply_target_probe=lambda *_args, **_kwargs: type("Probe", (), {"ok": True, "detail": ""})(),
        reply_message_fetcher=lambda *_args, **_kwargs: type("Message", (), {"remote_message_id": "8001"})(),
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
