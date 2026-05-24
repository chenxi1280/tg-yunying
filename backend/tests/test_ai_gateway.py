from __future__ import annotations

import json
from typing import Any

import pytest

from app.ai_gateway import AiGateway, AiProviderCredentials, mock_candidates, normalize_ai_model_name
from app.services.task_center.ai_generator import clean_channel_comment_contents


def credentials() -> AiProviderCredentials:
    return AiProviderCredentials(
        provider_name="MiMo",
        provider_type="openai_compatible",
        base_url="https://api.xiaomimimo.com/v1",
        model_name="mimo-v2.5",
        api_key="test-key",
    )


def test_mock_channel_comment_candidates_survive_quality_filter():
    candidates = mock_candidates(
        3,
        "评论可以多条",
        "像真实 Telegram 频道评论区，短句、贴原文、不重复",
        ["随手评论的读者", "追问细节的读者", "补充经验的读者"],
    )

    contents = clean_channel_comment_contents([candidate.content for candidate in candidates], limit=3)

    assert len(contents) == 3


class FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_check_accepts_ok_content_and_uses_larger_probe(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {
                        "content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"可以接着聊这个点。","risk_level":"低","suggested_account_id":101}]}'
                    },
                    "finish_reason": "stop",
                }
            ]
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail = AiGateway().check(credentials())

    assert ok is True
    assert detail == "provider ready; chat capability ready"
    assert requests[0]["max_tokens"] == 256
    assert requests[0]["messages"][1]["content"] == "请直接回复 OK，不要解释，不要推理过程。"
    assert requests[1]["max_tokens"] == 512
    assert requests[1]["messages"][1]["content"] == '只输出这个 JSON，不要解释：{"drafts":[{"content":"OK"}]}'


def test_check_retries_reasoning_only_chat_probe(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "thinking before final answer"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 512, "total_tokens": 524},
        },
        {
            "choices": [
                {
                    "message": {"content": '{"drafts":[{"content":"OK"}]}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail = AiGateway().check(credentials())

    assert ok is True
    assert detail == "provider ready; chat capability ready"
    assert [request["max_tokens"] for request in requests] == [256, 512, 2048]


def test_check_warns_when_chat_probe_stays_reasoning_only(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "thinking before final answer"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 256, "total_tokens": 268},
        },
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "still thinking"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 12, "completion_tokens": 2048, "total_tokens": 2060},
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail = AiGateway().check(credentials())

    assert ok is True
    assert detail.startswith("provider ready; chat capability warning:")
    assert "AI provider returned empty final content" in detail
    assert "finish_reason=length" in detail
    assert "usage=prompt:12, completion:2048, total:2060" in detail
    assert "reasoning_content present" in detail
    assert "retry used a higher max_tokens budget" in detail
    assert [request["max_tokens"] for request in requests] == [256, 512, 2048]


def test_openai_compatible_content_list_is_extracted(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": [
                                {"type": "text", "text": "OK"},
                                {"type": "text", "text": " from list"},
                            ]
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    content, usage = AiGateway()._post_openai_compatible(credentials(), "probe", 0.1, 256)

    assert content == "OK from list"
    assert usage.total_tokens == 6


def test_malformed_json_drafts_are_not_used_as_chat_lines(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"drafts": ['},
                        "finish_reason": "length",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="malformed JSON drafts"):
        AiGateway().generate_drafts(
            credentials(),
            "请输出 json drafts",
            count=1,
            topic="群聊",
            tone="自然",
            persona_set=["A"],
            temperature=0.8,
            max_tokens=512,
        )


def test_deepseek_uses_official_chat_completion_path_and_json_mode(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"继续接话。","risk_level":"低"}]}'
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    deepseek_credentials = AiProviderCredentials(
        provider_name="DeepSeek",
        provider_type="openai_compatible",
        base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
        api_key="test-key",
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        deepseek_credentials,
        "请输出 json drafts",
        count=1,
        topic="产品讨论",
        tone="自然",
        persona_set=["A"],
        temperature=0.1,
        max_tokens=512,
    )

    assert captured["url"] == "https://api.deepseek.com/chat/completions"
    assert captured["payload"]["response_format"] == {"type": "json_object"}
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert result.candidates[0].content == "继续接话。"
    assert AiGateway()._chat_completions_url("https://api.deepseek.com/v1") == "https://api.deepseek.com/chat/completions"


def test_deepseek_health_check_also_disables_thinking(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]},
        {
            "choices": [
                {
                    "message": {"content": '{"drafts":[{"content":"OK"}]}'},
                    "finish_reason": "stop",
                }
            ]
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    deepseek_credentials = AiProviderCredentials(
        provider_name="DeepSeek V4",
        provider_type="openai_compatible",
        base_url="https://api.deepseek.com/v1",
        model_name="deepseek-v4-pro",
        api_key="test-key",
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    ok, detail = AiGateway().check(deepseek_credentials)

    assert ok is True
    assert detail == "provider ready; chat capability ready"
    assert [request["thinking"] for request in requests] == [{"type": "disabled"}, {"type": "disabled"}]
    assert "response_format" not in requests[0]
    assert requests[1]["response_format"] == {"type": "json_object"}


def test_generate_drafts_retries_reasoning_only_empty_content(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "thinking"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 512, "total_tokens": 532},
        },
        {
            "choices": [
                {
                    "message": {"content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"继续接话。","risk_level":"低"}]}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30},
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="产品讨论",
        tone="自然",
        persona_set=["A"],
        temperature=0.1,
        max_tokens=512,
    )

    assert [request["max_tokens"] for request in requests] == [512, 4096]
    assert result.candidates[0].content == "继续接话。"
    assert result.usage.total_tokens == 30


def test_known_ai_model_names_are_normalized():
    assert normalize_ai_model_name("DeepSeek V4 Flash") == "deepseek-v4-flash"
    assert normalize_ai_model_name("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert normalize_ai_model_name("MiMo-V2.5") == "mimo-v2.5"
    assert normalize_ai_model_name("custom-model") == "custom-model"
