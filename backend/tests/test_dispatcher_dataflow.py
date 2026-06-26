from types import SimpleNamespace

import pytest

from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import dispatcher
from app.services.task_center.payloads import SendMessagePayload


def _pending_send_payload() -> SendMessagePayload:
    return SendMessagePayload(
        group_id=7,
        target_display="测试群",
        ai_generation_id="batch-1",
        ai_generation_status="pending",
    )


def test_dispatcher_rejects_partial_deferred_ai_generation_batch(monkeypatch):
    payload = _pending_send_payload()
    sibling_payload = _pending_send_payload()
    action = SimpleNamespace(task_id=11, tenant_id=1, payload=payload.model_dump(mode="json"))
    sibling = SimpleNamespace(task_id=11, tenant_id=1, payload=sibling_payload.model_dump(mode="json"))
    task = SimpleNamespace(tenant_id=1, type_config={}, stats={})
    session = SimpleNamespace(get=lambda _model, _id: task)

    monkeypatch.setattr(dispatcher, "_pending_ai_generation_batch", lambda *_args: [(action, payload), (sibling, sibling_payload)])
    monkeypatch.setattr(dispatcher, "generate_group_messages", lambda *_args, **_kwargs: (["只返回一条"], 3))

    with pytest.raises(AiGenerationUnavailable, match="AI 普通发言候选不足"):
        dispatcher._ensure_send_message_content(session, action, SimpleNamespace(), payload)

    assert action.payload["ai_generation_status"] == "pending"
    assert sibling.payload["ai_generation_status"] == "pending"
    assert task.stats["normal_candidate_shortfall_count"] == 1
