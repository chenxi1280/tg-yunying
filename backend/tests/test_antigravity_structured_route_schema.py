from types import SimpleNamespace

import pytest

from app.ai_gateway import AiProviderCredentials
from app.services.task_center import ai_structured_provider_runtime
from app.services.task_center.ai_structured_provider_runtime import StructuredProviderRequest


pytestmark = pytest.mark.no_postgres


def test_structured_provider_schema_uses_effective_route_purpose(monkeypatch):
    captured = {}
    config = {
        "_ai_provider_route_purpose": "group_context_route",
        "_ai_provider_planner_slots": [{
            "slot_id": "slot-1",
            "reply_to_message_id": "",
            "content_mode": "general",
            "route_evidence_ids": ["fact-1"],
        }],
    }
    request = StructuredProviderRequest(
        system_prompt="system",
        user_prompt="user",
        config=config,
        temperature=0.7,
        max_tokens=128,
        count=1,
        purpose="两阶段意图规划",
        model_name="gemini-3.5-flash-medium",
        stage="routing",
        required_model_family="",
    )

    def fake_generate(*_args, **kwargs):
        captured.update(kwargs)
        return {"briefs": []}, SimpleNamespace(total_tokens=0)

    monkeypatch.setattr(
        ai_structured_provider_runtime.ai_gateway, "generate_structured", fake_generate,
    )
    monkeypatch.setattr(
        ai_structured_provider_runtime, "settle_provider_success", lambda _lease: None,
    )

    credentials = AiProviderCredentials(
        provider_name="slot-01",
        provider_type="antigravity_cli",
        base_url="http://host.docker.internal:18101",
        model_name="gemini-3.5-flash-medium",
        api_key="bridge-token",
    )
    ai_structured_provider_runtime.call_structured_provider(
        object(), credentials, request, provider_request_id="request-1",
    )

    schema = captured["json_schema"]
    briefs = schema["properties"]["briefs"]
    assert schema["required"] == ["briefs"]
    assert briefs["minItems"] == briefs["maxItems"] == 1
    variants = briefs["items"]["oneOf"]
    assert all(item["properties"]["slot_id"]["enum"] == ["slot-1"] for item in variants)
