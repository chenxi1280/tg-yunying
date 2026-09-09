from __future__ import annotations

from datetime import timedelta

from app.services._common import _now

import pytest

from app.models import AiProvider, GenerationJob, GroupContextMessage, Task, TenantAiSetting, TgAccount, TgGroup
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_dispatch import ensure_send_message_content
from app.services.task_center.ai_generation_topic_context import prepare_topic_payload
from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_group_prompt import build_group_prompt
from app.services.task_center.group_ai_prompt_scope import _scoped_history
from group_ai_content_scope_test_support import _action, _payload, _seed_scope, _session


pytestmark = pytest.mark.no_postgres
TOPIC = {"title": "周末运动", "description": "分享运动安排"}


def _fixture(*, listener_error: bool = False):
    session = _session()
    _seed_scope(session)
    task = session.get(Task, "task-b")
    task.type_config = {"target_group_id": 8, "topic_directions": [TOPIC],
                        "engagement_contract_version": "unified_engagement_v1", "ai_model": "test"}
    session.add_all([
        AiProvider(id=1, provider_name="test", base_url="https://example.invalid", model_name="test",
                   api_key_ciphertext="test-unused"),
        TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True),
        GenerationJob(id="topic-job", tenant_id=1, task_id=task.id, obligation_type="quantity",
                      obligation_id="topic-obligation", generation_sequence=1, context_snapshot_version=1,
                      latest_safe_send_at=_now() + timedelta(hours=1)),
    ])
    group = session.get(TgGroup, 8)
    group.auth_status = "已授权运营"
    group.listener_enabled = True
    group.listener_cursor_status = "contiguous"
    group.listener_last_polled_at = _now() + timedelta(seconds=1)
    group.listener_last_error = "poll failed" if listener_error else ""
    if not listener_error:
        session.delete(session.get(GroupContextMessage, 801))
    payload = _payload(chat_mode="idle_warmup", context_message_ids=[801],
                       anchor_message_ids=[801], context_snapshot_message_id=801,
                       ai_generation_history="过期真人: 曾经聊过", slot_id="topic-slot",
                       generation_job_id="topic-job")
    action = _action(payload)
    action.obligation_type = "quantity"
    action.obligation_id = "topic-obligation"
    session.add(action)
    session.commit()
    return session, task, action, payload


@pytest.mark.parametrize("listener_error", [False, True])
def test_normal_entry_reaches_provider_with_configured_topic_without_human_context(listener_error):
    session, _task, action, payload = _fixture(listener_error=listener_error)
    seen = []

    def generate(_session, _tenant, config, **kwargs):
        seen.append((config, kwargs))
        raise AiGenerationUnavailable("provider_test_stop")

    dependencies = GenerationDependencies(normal_generator=generate, reply_generator=generate,
                                          reply_target_probe=generate, reply_message_fetcher=generate)
    with pytest.raises(AiGenerationUnavailable, match="provider_test_stop"):
        ensure_send_message_content(session, action, session.get(TgAccount, 11), payload=payload,
                                    credentials=object(), dependencies=dependencies)
    assert len(seen) == 1
    config, kwargs = seen[0]
    assert kwargs["history"] == ""
    assert action.payload["ai_generation_context_mode"] == "topic_only"
    assert action.payload["context_message_ids"] == []
    assert action.payload["anchor_message_ids"] == []
    assert action.payload["context_snapshot_message_id"] is None
    assert session.get(TgGroup, 8).listener_last_error == ("poll failed" if listener_error else "")
    bundle = build_group_prompt(config, target_label="B群", history=kwargs["history"], count=1)
    assert bundle.input_payload["generation_slots"][0]["topic_direction"]["title"] == TOPIC["title"]
    assert bundle.input_payload["context_source"] == "generic_warmup"


def test_topic_history_cannot_be_restored_by_prompt_rebuild():
    session, task, action, payload = _fixture(listener_error=True)
    updated = prepare_topic_payload(session, task, action, payload=payload)
    assert _scoped_history(session, task, updated.model_copy(update={"context_message_ids": [801]})) == ""
    assert updated.ai_generation_topic_direction == TOPIC
    assert payload.ai_generation_history == "过期真人: 曾经聊过"


@pytest.mark.parametrize("updates", [
    {"reply_to_message_id": 8001},
    {"message_text": "已冻结正文", "ai_generation_status": "ready"},
    {"interaction_opportunity_id": "interaction"}, {"conversation_turn_claim_id": "claim"},
])
def test_topic_preparation_preserves_reply_interaction_and_ready_identity(updates):
    session, task, action, payload = _fixture(listener_error=True)
    original = payload.model_copy(update=updates)
    assert prepare_topic_payload(session, task, action, payload=original) is original


def test_batch_reply_mode_without_reply_identity_uses_configured_topic():
    session, task, action, payload = _fixture(listener_error=True)
    original = payload.model_copy(update={"chat_mode": "reply"})
    updated = prepare_topic_payload(session, task, action, payload=original)
    assert updated.ai_generation_context_mode == "topic_only"
    assert updated.ai_generation_context_reason == "listener_error"
    assert updated.ai_generation_history == ""
    assert updated.reply_to_message_id is None
    assert updated.chat_mode == "reply"


def test_foreign_context_is_not_laundered_into_topic_mode():
    session, task, action, payload = _fixture(listener_error=True)
    original = payload.model_copy(update={"context_message_ids": [701]})
    assert prepare_topic_payload(session, task, action, payload=original) is original


def test_missing_configured_topic_is_explicit():
    session, task, action, payload = _fixture(listener_error=True)
    task.type_config = {"target_group_id": 8, "engagement_contract_version": "unified_engagement_v1"}
    with pytest.raises(AiGenerationUnavailable, match="topic_only_topic_missing"):
        prepare_topic_payload(session, task, action, payload=payload)


def test_low_information_context_uses_topic_only():
    session, task, action, payload = _fixture(listener_error=True)
    session.get(TgGroup, 8).listener_last_error = ""
    session.get(GroupContextMessage, 801).content = "嗯"
    updated = prepare_topic_payload(session, task, action, payload=payload)
    assert updated.ai_generation_context_reason == "no_human_context"
    assert updated.ai_generation_history == ""


def test_legacy_context_contract_is_not_changed():
    session, task, action, payload = _fixture(listener_error=True)
    task.type_config = {"target_group_id": 8, "topic_directions": [TOPIC]}
    assert prepare_topic_payload(session, task, action, payload=payload) is payload


@pytest.mark.parametrize("chat_mode", ["idle_warmup", "reply"])
def test_topic_only_normal_content_can_become_ready_without_human_reference(chat_mode):
    session, _task, action, payload = _fixture(listener_error=True)
    payload = payload.model_copy(update={"chat_mode": chat_mode})
    action.payload = payload.model_dump(mode="json")

    def generate(_session, _tenant, config, **kwargs):
        assert kwargs["history"] == ""
        slot = config["generation_slots"][0]
        return [GeneratedContent("周末运动更喜欢跑步还是骑车？", slot_id=slot["slot_id"], sequence_index=1)], 1

    dependencies = GenerationDependencies(normal_generator=generate, reply_generator=generate,
                                          reply_target_probe=generate, reply_message_fetcher=generate)
    ready = ensure_send_message_content(session, action, session.get(TgAccount, 11), payload=payload,
                                        credentials=object(), dependencies=dependencies)
    assert ready.ai_generation_status == "ready"
    assert ready.ai_generation_context_mode == "topic_only"
    assert ready.message_text == "周末运动更喜欢跑步还是骑车？"
    assert ready.reply_to_message_id is None
    assert ready.context_message_ids == [] and ready.anchor_message_ids == []
    assert ready.context_snapshot_message_id is None
    assert ready.ai_generation_history == ""
