import json
from dataclasses import replace

import pytest

from app.ai_gateway import AiGateway, AiProviderCredentials
from app.services.task_center.semantic_review_contract import _parse_semantic_review


pytestmark = pytest.mark.no_postgres
CREDENTIALS = AiProviderCredentials(provider_name="MiniMax", provider_type="openai_compatible",
    base_url="https://api.minimax.io/v1", model_name="MiniMax-M2.5", api_key="test")


@pytest.mark.parametrize("decision", ["pass", "fail"])
def test_review_uses_final_decision_when_reasoning_contains_unrelated_json(monkeypatch, decision):
    review = {"decision": decision, "confidence": .95,
        "codes": [] if decision == "pass" else ["unsupported_claim"],
        "evidence": [{"criterion": "facts", "observed": "candidate checked"}],
        "prompt_version": "semantic_reviewer_v1"}
    intermediate = json.dumps([{"anchor_id": "f1"}, {"anchor_id": "f2"}])
    requests = []

    def provider(_credentials, payload, **_kwargs):
        requests.append(payload)
        if payload.get("reasoning_split"):
            message = {"content": json.dumps(review),
                "reasoning_details": [{"text": intermediate}]}
        else:
            message = {"content": f"<think>{intermediate}</think>\n{json.dumps(review)}"}
        return {"choices": [{"message": message, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 20, "completion_tokens": 30, "total_tokens": 50}}

    gateway = AiGateway()
    monkeypatch.setattr(gateway, "_read_openai_response", provider)
    payload, usage = gateway.generate_structured(CREDENTIALS, "check candidate",
        system_prompt="independent review", temperature=.8, max_tokens=2048)
    result = _parse_semantic_review(payload, {"ai_semantic_reviewer_model": "MiniMax-M2.5"})
    assert result["decision"] == decision and result["codes"] == review["codes"]
    assert len(requests) == 1 and usage.total_tokens == 50
    assert "anchor_id" not in str(result)


@pytest.mark.parametrize("model,thinking", [("MiniMax-M2.5", None),
    ("MiniMax-M3", {"type": "disabled"})])
def test_minimax_keeps_original_sampling_while_separating_reasoning(model, thinking):
    credentials = replace(CREDENTIALS, model_name=model)
    payload = AiGateway()._chat_payload(credentials, "input", "system", .8, 2048, True)
    assert payload["reasoning_split"] is True
    assert payload.get("thinking") == thinking
    assert payload["temperature"] == .8 and payload["max_tokens"] == 2048
    assert "response_format" not in payload


def test_other_provider_does_not_receive_minimax_extension():
    credentials = replace(CREDENTIALS, provider_name="Other", model_name="other-model",
        base_url="https://example.com/v1")
    payload = AiGateway()._chat_payload(credentials, "input", "system", .8, 2048, True)
    assert "reasoning_split" not in payload
