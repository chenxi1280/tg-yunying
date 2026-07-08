from types import SimpleNamespace

import pytest

from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import dispatcher
from app.services.task_center.payloads import SendMessagePayload


pytestmark = pytest.mark.no_postgres


def _pending_send_payload() -> SendMessagePayload:
    return SendMessagePayload(
        group_id=7,
        target_display="测试群",
        ai_generation_id="batch-1",
        ai_generation_status="pending",
    )


def test_dispatcher_keeps_partial_deferred_ai_generation_batch(monkeypatch):
    payload = _pending_send_payload()
    sibling_payload = _pending_send_payload()
    action = SimpleNamespace(task_id=11, task_type="group_ai_chat", tenant_id=1, payload=payload.model_dump(mode="json"))
    sibling = SimpleNamespace(task_id=11, task_type="group_ai_chat", tenant_id=1, payload=sibling_payload.model_dump(mode="json"))
    task = SimpleNamespace(tenant_id=1, type_config={}, stats={})
    session = SimpleNamespace(get=lambda _model, _id: task)

    monkeypatch.setattr(dispatcher, "_pending_ai_generation_batch", lambda *_args: [(action, payload), (sibling, sibling_payload)])
    monkeypatch.setattr(dispatcher, "generate_group_messages", lambda *_args, **_kwargs: (["只返回一条"], 3))
    monkeypatch.setattr(dispatcher, "_attach_generated_message_memory", lambda *_args, **_kwargs: None)

    refreshed = dispatcher._ensure_send_message_content(session, action, SimpleNamespace(), payload)

    assert refreshed.message_text == "只返回一条"
    assert action.payload["ai_generation_status"] == "success"
    assert sibling.payload["ai_generation_status"] == "pending"
    assert task.stats["normal_candidate_shortfall_count"] == 1


def test_dispatcher_rejects_empty_deferred_ai_generation_batch(monkeypatch):
    payload = _pending_send_payload()
    action = SimpleNamespace(task_id=11, tenant_id=1, payload=payload.model_dump(mode="json"))
    task = SimpleNamespace(tenant_id=1, type_config={}, stats={})
    session = SimpleNamespace(get=lambda _model, _id: task)

    monkeypatch.setattr(dispatcher, "_pending_ai_generation_batch", lambda *_args: [(action, payload)])
    monkeypatch.setattr(dispatcher, "generate_group_messages", lambda *_args, **_kwargs: ([], 0))

    with pytest.raises(AiGenerationUnavailable, match="AI 普通发言候选不足"):
        dispatcher._ensure_send_message_content(session, action, SimpleNamespace(), payload)

    assert action.payload["ai_generation_status"] == "pending"
    assert task.stats["normal_candidate_shortfall_count"] == 1


def test_dispatcher_runtime_config_preserves_deferred_generation_slots():
    first = _pending_send_payload()
    first.slot_id = "task-1:cycle:1:turn:1"
    first.turn_index = 1
    first.act_type = "short_react"
    first.account_profile = "青年短句，接话快"
    first.topic_direction = {"title": "郑州楼凤妹子怎么样"}
    first.teacher_target = {"name": "花花老师"}
    second = _pending_send_payload()
    second.slot_id = "task-1:cycle:1:turn:2"
    second.turn_index = 2
    second.act_type = "question"
    second.account_profile = "中年谨慎，常追问"
    second.topic_direction = {"title": "主任最近约新妹子了"}
    second.teacher_target = {"name": "新人榜单妹子"}
    batch = [
        (SimpleNamespace(account_id=11), first),
        (SimpleNamespace(account_id=12), second),
    ]

    config = dispatcher._runtime_group_ai_config(SimpleNamespace(type_config={}), batch)

    assert [slot["topic_direction"]["title"] for slot in config["generation_slots"]] == [
        "郑州楼凤妹子怎么样",
        "主任最近约新妹子了",
    ]
    assert [slot["teacher_target"]["name"] for slot in config["generation_slots"]] == [
        "花花老师",
        "新人榜单妹子",
    ]
    assert [slot["account_profile"] for slot in config["generation_slots"]] == [
        "青年短句，接话快",
        "中年谨慎，常追问",
    ]


def test_dispatcher_runtime_config_does_not_force_mimo_for_hard_hourly_without_model():
    payload = _pending_send_payload()
    payload.hard_hourly_target = True
    batch = [(SimpleNamespace(account_id=11), payload)]

    config = dispatcher._runtime_group_ai_config(SimpleNamespace(type_config={}), batch)

    assert config.get("require_mimo_draft") is None
