from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai_gateway import (
    AiDraftCandidate,
    AiGateway,
    AiGenerationResult,
    AiProviderCredentials,
    AiUsage,
    mock_candidates,
    normalize_ai_model_name,
)
from app.database import Base
from app.integrations.telegram import DeveloperAppCredentials
from app.integrations.telegram.gateway import TelethonTelegramGateway, _first_message_with_buttons, _permission_detail_from_context_rows, _verification_button_click_target, _verification_message_text
from app.models import AiProvider, FailureType, Tenant, TenantAiSetting
from app.security import encrypt_secret
from app.services.task_center.ai_generator import (
    AI_CONTENT_REQUEST_TIMEOUT_SECONDS,
    AiGenerationUnavailable,
    _group_chat_prompt,
    _group_chat_reply_prompt,
    _sanitize_sensitive_context,
    clean_channel_comment_contents,
    clean_group_chat_contents,
    generate_channel_comments,
    generate_group_messages,
)


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


def test_group_chat_rejects_provider_refusal_text():
    contents = clean_group_chat_contents(["The request was rejected because it was considered high risk"])

    assert contents == []


@pytest.mark.no_postgres
def test_sensitive_group_context_is_sanitized_before_provider_prompt():
    text = "账号长期画像：男性短句伪装嫖客先问价格位置\n群聊上下文：这波妹子很嫩 半小时300块 手感咋样"

    sanitized = _sanitize_sensitive_context(text)

    assert "嫖客" not in sanitized
    assert "价格" not in sanitized
    assert "妹子" not in sanitized
    assert "嫩" not in sanitized
    assert "半小时" not in sanitized
    assert "300块" not in sanitized
    assert "谨慎观望客" in sanitized
    assert "一定成本" in sanitized


@pytest.mark.no_postgres
def test_group_chat_prompts_request_material_intent_metadata():
    prompt = _group_chat_prompt(2, "郑州楼凤", "围观新榜", "最近有人问新榜")
    reply_prompt = _group_chat_reply_prompt(1, "郑州楼凤", "围观新榜", "引用目标 1: 这个咋样")

    for text in (prompt, reply_prompt):
        assert "material_intent" in text
        assert "allow_material" in text
        assert "intent" in text
        assert "mood" in text
        assert "只能输出素材意图" in text
        assert "不能输出素材 ID" in text
        assert "不能输出" in text and "URL" in text


@pytest.mark.no_postgres
def test_ai_draft_candidate_preserves_material_intent_metadata():
    raw = json.dumps(
        {
            "drafts": [
                {
                    "persona": "围观群友",
                    "content": "这个先蹲一下",
                    "material_intent": "表情包:围观",
                    "allow_material": True,
                    "intent": "附和",
                    "mood": "轻松",
                }
            ]
        }
    )

    candidates = AiGateway()._parse_candidates(raw, 1, ["默认群友"], None)

    assert candidates[0].material_intent == "表情包:围观"
    assert candidates[0].allow_material is True
    assert candidates[0].intent == "附和"
    assert candidates[0].mood == "轻松"


@pytest.mark.no_postgres
def test_group_chat_generation_preserves_ai_material_metadata(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fake_generate_drafts(_credentials, _prompt, **_kwargs):
        return AiGenerationResult(
            candidates=[
                AiDraftCandidate(
                    persona="围观号",
                    content="这个先蹲一下",
                    material_intent="表情包:围观",
                    allow_material=True,
                    intent="附和",
                    mood="轻松",
                )
            ],
            usage=AiUsage(total_tokens=11),
        )

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-chat",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(session, 1, {}, count=1, target_label="活跃群", history="已有上下文")

    assert contents == ["这个先蹲一下"]
    assert contents[0].material_intent == "表情包:围观"
    assert contents[0].allow_material is True
    assert contents[0].intent == "附和"
    assert contents[0].mood == "轻松"


def test_channel_comment_rejects_provider_refusal_text():
    contents = clean_channel_comment_contents(["The request was rejected because it was considered high risk"], limit=1)

    assert contents == []


def test_send_message_private_channel_error_maps_to_group_permission_denied():
    result = TelethonTelegramGateway._map_send_error(RuntimeError("The channel specified is private and you lack permission to access it (caused by SendMessageRequest)"))

    assert result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert result.detail == "群无权限或账号不可发言"


def test_permission_detail_from_context_rows_exposes_actionable_gate_prompt():
    detail = _permission_detail_from_context_rows(
        [
            {"text": "普通聊天内容"},
            {"text": "入群验证：请先关注 @alpha、https://t.me/beta_channel 后输入 3 + 5"},
        ],
    )

    assert detail == "群无权限或账号不可发言：入群验证：请先关注 @alpha、https://t.me/beta_channel 后输入 3 + 5"


def test_verification_message_text_preserves_button_urls_for_auto_follow():
    button = SimpleNamespace(text="天津音乐学院报告频道", url="https://t.me/tj_report")
    message = SimpleNamespace(message="您需要关注我们的频道才能发言", media=None, buttons=[[button]])

    text = _verification_message_text(message)

    assert "天津音乐学院报告频道" in text
    assert "https://t.me/tj_report" in text


def test_first_message_with_buttons_scans_recent_context():
    without_buttons = SimpleNamespace(id=9, buttons=None)
    with_buttons = SimpleNamespace(id=8, buttons=[[SimpleNamespace(text="开始验证")]])

    assert _first_message_with_buttons([without_buttons, with_buttons]) is with_buttons


@pytest.mark.no_postgres
def test_verification_button_click_target_prefers_confirmation_over_channel_urls():
    message = SimpleNamespace(
        buttons=[
            [
                SimpleNamespace(text="郑州楼凤阁车库", url="https://t.me/zz_lfg_garage"),
                SimpleNamespace(text="郑州楼凤报告收录", url="https://t.me/zz_lfg_report"),
                SimpleNamespace(text="✅ 我已加入"),
            ]
        ]
    )

    assert _verification_button_click_target(message) == (0, 2, "✅ 我已加入")


def test_probe_permission_denied_uses_recent_context_detail(monkeypatch):
    class FakeMessage:
        id = 7
        date = None
        media = None
        buttons = None
        message = "入群验证：请先关注 @alpha 和 @beta 后输入 3 + 5"

        async def get_sender(self):
            return SimpleNamespace(first_name="验证机器人")

    class FakeClient:
        async def is_user_authorized(self):
            return True

        async def get_permissions(self, _target, _user):
            return SimpleNamespace(send_messages=False, post_messages=False, participant=None)

        async def get_messages(self, _target, *, limit):
            assert limit > 1
            return [FakeMessage()]

    async def fake_client(*_args, **_kwargs):
        return FakeClient()

    async def fake_target(*_args, **_kwargs):
        return SimpleNamespace(default_banned_rights=None)

    gateway = TelethonTelegramGateway()
    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_target)

    result = gateway._run(
        gateway._probe_target_capabilities_async(
            "raw-session",
            "-1007",
            "group",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is False
    assert result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert result.detail == "群无权限或账号不可发言：入群验证：请先关注 @alpha 和 @beta 后输入 3 + 5"


def test_probe_permission_exception_uses_recent_context_detail(monkeypatch):
    class FakeMessage:
        id = 8
        date = None
        media = None
        buttons = None
        message = "发言验证：请点击下方按钮或回复验证码 1234"

        async def get_sender(self):
            return SimpleNamespace(first_name="验证机器人")

    class FakeClient:
        async def is_user_authorized(self):
            return True

        async def get_permissions(self, _target, _user):
            raise RuntimeError("You can't write in this chat (caused by GetParticipantRequest)")

        async def get_messages(self, _target, *, limit):
            assert limit > 1
            return [FakeMessage()]

    async def fake_client(*_args, **_kwargs):
        return FakeClient()

    async def fake_target(*_args, **_kwargs):
        return SimpleNamespace(default_banned_rights=None)

    gateway = TelethonTelegramGateway()
    monkeypatch.setattr(gateway, "_get_or_create_client", fake_client)
    monkeypatch.setattr("app.integrations.telegram.gateway.resolve_telethon_target", fake_target)

    result = gateway._run(
        gateway._probe_target_capabilities_async(
            "raw-session",
            "-1008",
            "group",
            DeveloperAppCredentials(app_id=1, api_id=123, api_hash="hash", credentials_version=1),
        )
    )

    assert result.ok is False
    assert result.failure_type == FailureType.GROUP_PERMISSION_DENIED.value
    assert result.detail == "群无权限或账号不可发言：发言验证：请点击下方按钮或回复验证码 1234"


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


def test_malformed_json_drafts_error_exposes_payload_fingerprint(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"drafts": [{"content": "断掉的内容"'},
                        "finish_reason": "length",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    with pytest.raises(RuntimeError) as exc_info:
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

    detail = str(exc_info.value)
    assert "AI provider returned malformed JSON drafts" in detail
    assert "len=" in detail
    assert "sha256=" in detail
    assert 'preview={"drafts": [{"content": "断掉的内容"' in detail


def test_mimo_malformed_json_drafts_retry_with_larger_token_budget(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {"choices": [{"message": {"content": '{"drafts": [{"content": "断掉的内容"'}, "finish_reason": "length"}]},
        {"choices": [{"message": {"content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"重试后完整","risk_level":"低"}]}'}, "finish_reason": "stop"}]},
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.8,
        max_tokens=512,
    )

    assert [item["max_tokens"] for item in requests] == [512, 4096]
    assert [candidate.content for candidate in result.candidates] == ["重试后完整"]


def test_generate_drafts_extracts_prefixed_json_object(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '好的，按你的要求生成：\n{"drafts":[{"sequence_index":1,"persona":"A","content":"这句可以继续接","risk_level":"低"}]}\n'
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.8,
        max_tokens=512,
    )

    assert result.candidates[0].content == "这句可以继续接"


def test_generate_drafts_extracts_fenced_json_object(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"drafts":[{"sequence_index":1,"persona":"A","content":"我也想问下这个","risk_level":"低"}]}\n```'
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.8,
        max_tokens=512,
    )

    assert result.candidates[0].content == "我也想问下这个"


def test_generate_drafts_accepts_jsonish_single_quoted_drafts(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "{'drafts':[{'sequence_index':1,'persona':'A','content':'这个点可以继续聊','risk_level':'低'}]}"
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.8,
        max_tokens=512,
    )

    assert result.candidates[0].content == "这个点可以继续聊"


def test_generate_drafts_accepts_data_wrapped_single_draft(monkeypatch):
    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"data":{"sequence_index":1,"persona":"A","content":"这个包装也能解析","risk_level":"低"}}'
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.8,
        max_tokens=512,
    )

    assert result.candidates[0].content == "这个包装也能解析"


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


@pytest.mark.no_postgres
def test_minimax_m3_generation_disables_thinking_without_json_response_format(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        captured["url"] = request.full_url
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"Minimax 接话正常。","risk_level":"低"}]}'
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    minimax_credentials = AiProviderCredentials(
        provider_name="MiniMax",
        provider_type="openai_compatible",
        base_url="https://api.minimax.io/v1",
        model_name="MiniMax-M3",
        api_key="test-key",
    )
    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        minimax_credentials,
        "请输出 json drafts",
        count=1,
        topic="产品讨论",
        tone="自然",
        persona_set=["A"],
        temperature=0.1,
        max_tokens=512,
    )

    assert captured["url"] == "https://api.minimax.io/v1/chat/completions"
    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert "response_format" not in captured["payload"]
    assert result.candidates[0].content == "Minimax 接话正常。"


def test_mimo_image_verification_uses_openai_compatible_image_payload(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"answer":"A7K2","confidence":0.93}'},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 4, "total_tokens": 14},
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().solve_image_verification(
        credentials(),
        b"\x89PNG\r\n",
        "image/png",
        prompt="只识别验证码",
    )

    user_content = captured["payload"]["messages"][1]["content"]
    assert captured["payload"]["model"] == "mimo-v2.5"
    assert user_content[0] == {"type": "text", "text": "只识别验证码"}
    assert user_content[1]["type"] == "image_url"
    assert user_content[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert result.answer == "A7K2"
    assert result.confidence == 0.93
    assert result.usage.total_tokens == 14


@pytest.mark.no_postgres
def test_mimo_generation_disables_thinking(monkeypatch):
    captured: dict[str, Any] = {}

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": (
                                '{"drafts":[{"sequence_index":1,"persona":"A",'
                                '"content":"接一句就行","risk_level":"低"}]}'
                            )
                        },
                        "finish_reason": "stop",
                    }
                ]
            }
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().generate_drafts(
        credentials(),
        "请输出 json drafts",
        count=1,
        topic="群聊",
        tone="自然",
        persona_set=["A"],
        temperature=0.1,
        max_tokens=512,
    )

    assert captured["payload"]["thinking"] == {"type": "disabled"}
    assert result.candidates[0].content == "接一句就行"


def test_mimo_image_verification_retries_reasoning_only_empty_content(monkeypatch):
    requests: list[dict[str, Any]] = []
    responses = [
        {
            "choices": [
                {
                    "message": {"content": "", "reasoning_content": "先分析图片"},
                    "finish_reason": "length",
                }
            ],
            "usage": {"prompt_tokens": 450, "completion_tokens": 512, "total_tokens": 962},
        },
        {
            "choices": [
                {
                    "message": {"content": '{"answer":"7391","confidence":0.91}'},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 450, "completion_tokens": 8, "total_tokens": 458},
        },
    ]

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        requests.append(json.loads(request.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)

    result = AiGateway().solve_image_verification(credentials(), b"\x89PNG\r\n", "image/png")

    assert [request["max_tokens"] for request in requests] == [512, 4096]
    assert result.answer == "7391"
    assert result.confidence == 0.91


def test_deepseek_image_verification_is_rejected_before_network(monkeypatch):
    def should_not_call(*_args, **_kwargs):
        raise AssertionError("DeepSeek must not receive image verification requests")

    monkeypatch.setattr("urllib.request.urlopen", should_not_call)
    deepseek_credentials = AiProviderCredentials(
        provider_name="DeepSeek",
        provider_type="openai_compatible",
        base_url="https://api.deepseek.com",
        model_name="deepseek-v4-flash",
        api_key="test-key",
    )

    with pytest.raises(RuntimeError, match="MiMo"):
        AiGateway().solve_image_verification(deepseek_credentials, b"img", "image/png")


def test_generate_drafts_uses_custom_timeout(monkeypatch):
    timeouts: list[int] = []

    def fake_urlopen(request, timeout):  # noqa: ANN001 - mirrors urllib signature.
        timeouts.append(timeout)
        return FakeResponse(
            {
                "choices": [
                    {
                        "message": {"content": '{"drafts":[{"sequence_index":1,"persona":"A","content":"继续聊。","risk_level":"低"}]}'},
                        "finish_reason": "stop",
                    }
                ]
            }
        )

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
        timeout=120,
    )

    assert timeouts == [120]
    assert result.candidates[0].content == "继续聊。"


def test_channel_comment_generation_uses_long_ai_timeout(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, int] = {}

    def fake_generate_drafts(_credentials, _prompt, **kwargs):
        captured["timeout"] = kwargs["timeout"]
        return mock_generation_result("收纳盒尺寸有人实测过吗")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_channel_comments(
            session,
            1,
            {"comment_style": "mixed"},
            count=1,
            message_content="这款收纳盒宽 12cm，适合桌面小物。",
            target_label="太郎日记",
        )

    assert contents == ["收纳盒尺寸有人实测过吗"]
    assert captured["timeout"] == AI_CONTENT_REQUEST_TIMEOUT_SECONDS


def test_channel_comment_generation_scales_token_budget_for_many_candidates(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, int] = {}

    def fake_generate_drafts(_credentials, _prompt, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return mock_generation_result("河东区这个位置方便吗")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        generate_channel_comments(
            session,
            1,
            {"comment_style": "mixed"},
            count=12,
            message_content="【天津音乐学院】所在位置：河东区；服务：陪洗，无套口，制服",
            target_label="天津音乐",
        )

    assert captured["max_tokens"] >= 12 * 512


def test_group_chat_generation_uses_short_message_token_budget(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, int] = {}

    def fake_generate_drafts(_credentials, _prompt, **kwargs):
        captured["max_tokens"] = kwargs["max_tokens"]
        return mock_generation_result("这句能接上")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        generate_group_messages(session, 1, {}, count=300, target_label="活跃群", history="已有上下文")

    assert captured["max_tokens"] == 300 * 96


@pytest.mark.no_postgres
def test_group_chat_generation_uses_tenant_default_provider_without_model(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        captured["provider_name"] = credentials.provider_name
        captured["model_name"] = credentials.model_name
        return mock_generation_result("这句用默认模型")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(
            AiProvider(
                id=2,
                provider_name="Xiaomi Mino",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mino-v2.5",
                api_key_ciphertext=encrypt_secret("mino-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(session, 1, {}, count=1, target_label="活跃群", history="已有上下文")

    assert contents == ["这句用默认模型"]
    assert captured == {"provider_name": "DeepSeek", "model_name": "deepseek-v4-flash"}


@pytest.mark.no_postgres
def test_group_chat_generation_honors_task_provider_without_model(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        captured["provider_name"] = credentials.provider_name
        captured["model_name"] = credentials.model_name
        return mock_generation_result("任务供应商接话")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(
            AiProvider(
                id=2,
                provider_name="Xiaomi MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("mimo-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(session, 1, {"ai_provider_id": 1}, count=1, target_label="活跃群", history="已有上下文")

    assert contents == ["任务供应商接话"]
    assert captured == {"provider_name": "DeepSeek", "model_name": "deepseek-v4-flash"}


@pytest.mark.no_postgres
def test_group_chat_generation_uses_default_provider_when_mimo_missing(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        captured["provider_name"] = credentials.provider_name
        captured["model_name"] = credentials.model_name
        return mock_generation_result("默认供应商继续生成")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(session, 1, {}, count=1, target_label="活跃群", history="已有上下文")

    assert contents == ["默认供应商继续生成"]
    assert captured == {"provider_name": "DeepSeek", "model_name": "deepseek-v4-flash"}


@pytest.mark.no_postgres
def test_hard_hourly_group_chat_generation_requires_mimo_provider(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        captured["provider_name"] = credentials.provider_name
        captured["model_name"] = credentials.model_name
        return mock_generation_result("硬目标使用小米生成")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(
            AiProvider(
                id=2,
                provider_name="Xiaomi Mino",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mino-v2.5",
                api_key_ciphertext=encrypt_secret("mino-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(
            session,
            1,
            {"require_mimo_draft": True},
            count=1,
            target_label="活跃群",
            history="已有上下文",
        )

    assert contents == ["硬目标使用小米生成"]
    assert captured == {"provider_name": "Xiaomi Mino", "model_name": "mino-v2.5"}


@pytest.mark.no_postgres
def test_hard_hourly_group_chat_generation_fails_without_mimo_provider():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        with pytest.raises(AiGenerationUnavailable, match="小米 MiMo/mino"):
            generate_group_messages(
                session,
                1,
                {"require_mimo_draft": True},
                count=1,
                target_label="活跃群",
                history="已有上下文",
            )


@pytest.mark.no_postgres
def test_group_chat_generation_requires_mimo_when_model_configured():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="DeepSeek",
                provider_type="openai_compatible",
                base_url="https://api.deepseek.com/v1",
                model_name="deepseek-v4-flash",
                api_key_ciphertext=encrypt_secret("deepseek-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        with pytest.raises(AiGenerationUnavailable, match="小米 MiMo/mino"):
            generate_group_messages(session, 1, {"ai_model": "mimo-v2.5"}, count=1, target_label="活跃群", history="已有上下文")


def test_channel_comment_allows_adult_service_context_in_ai_prompt(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(_credentials, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = kwargs["system_prompt"]
        return mock_generation_result("河东区这个位置方便吗")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_channel_comments(
            session,
            1,
            {"comment_style": "mixed"},
            count=1,
            message_content="【天津音乐学院】所在位置：河东区；服务：陪洗，无套口，制服；特别提示：态度超好，刚入行能理解，大蟒蛇",
            target_label="天津音乐",
        )

    assert contents == ["河东区这个位置方便吗"]
    assert "河东区" in captured["prompt"]
    assert "陪洗" in captured["prompt"]
    assert "无套口" in captured["prompt"]
    assert "制服" in captured["prompt"]
    assert "态度超好" in captured["prompt"]
    assert "刚入行能理解" in captured["prompt"]
    assert "大蟒蛇" in captured["prompt"]
    assert "成人服务描述已按安全口径概括" not in captured["prompt"]
    assert "敏感场景描述只能作为既有上下文理解" in captured["system_prompt"]
    assert "成人交易/性服务" not in captured["system_prompt"]
    assert "不要新增联系方式、价格、邀约或交易撮合信息" in captured["system_prompt"]


def test_channel_comment_keeps_adult_service_context_outputs(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fake_generate_drafts(_credentials, _prompt, **_kwargs):
        return AiGenerationResult(
            candidates=[
                AiDraftCandidate(persona="读者", content="价格有变吗"),
                AiDraftCandidate(persona="读者", content="河东区这个位置方便吗"),
                AiDraftCandidate(persona="读者", content="今天有新服务吗"),
                AiDraftCandidate(persona="读者", content="胸围这个点挺特别"),
            ],
            usage=AiUsage(total_tokens=22),
        )

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_channel_comments(
            session,
            1,
            {"comment_style": "mixed"},
            count=3,
            message_content="【天津音乐学院】所在位置：河东区；服务项目：陪洗，无套口，制服",
            target_label="天津音乐",
        )

    assert contents == ["价格有变吗", "河东区这个位置方便吗", "今天有新服务吗"]


def test_group_chat_allows_adult_service_context_in_ai_prompt(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    captured: dict[str, str] = {}

    def fake_generate_drafts(_credentials, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = kwargs["system_prompt"]
        return mock_generation_result("河东这个位置有人去过吗")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(
            session,
            1,
            {"topic_hint": "天津音乐学院", "max_message_length": 80},
            count=1,
            target_label="天津音乐群",
            history="频道原文：所在位置：河东区；服务：陪洗，无套口，制服；特别提示：态度超好，刚入行能理解，大蟒蛇",
        )

    assert contents == ["河东这个位置有人去过吗"]
    assert "河东区" in captured["prompt"]
    assert "陪洗" in captured["prompt"]
    assert "无套口" in captured["prompt"]
    assert "制服" in captured["prompt"]
    assert "态度超好" in captured["prompt"]
    assert "刚入行能理解" in captured["prompt"]
    assert "大蟒蛇" in captured["prompt"]
    assert "成人服务描述已按安全口径概括" not in captured["prompt"]
    assert "敏感场景描述只能作为既有上下文理解" in captured["system_prompt"]
    assert "成人交易/性服务" not in captured["system_prompt"]
    assert "不要新增联系方式、价格、邀约或交易撮合信息" in captured["system_prompt"]


def test_group_chat_keeps_adult_service_context_outputs(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    def fake_generate_drafts(_credentials, _prompt, **_kwargs):
        return AiGenerationResult(
            candidates=[
                AiDraftCandidate(persona="群友", content="怎么联系"),
                AiDraftCandidate(persona="群友", content="河东这个位置有人去过吗"),
                AiDraftCandidate(persona="群友", content="能安排一下吗"),
                AiDraftCandidate(persona="群友", content="大蟒蛇具体是啥"),
            ],
            usage=AiUsage(total_tokens=22),
        )

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(
            AiProvider(
                id=1,
                provider_name="MiMo",
                provider_type="openai_compatible",
                base_url="https://api.xiaomimimo.com/v1",
                model_name="mimo-v2.5",
                api_key_ciphertext=encrypt_secret("test-key"),
                health_status="健康",
            )
        )
        session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
        session.commit()

        contents, _tokens = generate_group_messages(
            session,
            1,
            {"topic_hint": "天津音乐学院", "max_message_length": 80},
            count=3,
            target_label="天津音乐群",
            history="频道原文：所在位置：河东区；服务项目：陪洗，无套口，制服",
        )

    assert contents == ["怎么联系", "河东这个位置有人去过吗", "能安排一下吗", "大蟒蛇具体是啥"]


def mock_generation_result(content: str) -> AiGenerationResult:
    return AiGenerationResult(
        candidates=[AiDraftCandidate(persona="读者", content=content)],
        usage=AiUsage(total_tokens=11),
    )


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


@pytest.mark.no_postgres
def test_known_ai_model_names_are_normalized():
    assert normalize_ai_model_name("DeepSeek V4 Flash") == "deepseek-v4-flash"
    assert normalize_ai_model_name("DeepSeek-V4-Pro") == "deepseek-v4-pro"
    assert normalize_ai_model_name("MiMo-V2.5") == "mimo-v2.5"
    assert normalize_ai_model_name("mino-v2.5") == "mimo-v2.5"
    assert normalize_ai_model_name("Xiaomi Mino V2.5") == "mimo-v2.5"
    assert normalize_ai_model_name("minimax m3") == "MiniMax-M3"
    assert normalize_ai_model_name("MiniMax M2.7") == "MiniMax-M2.7"
    assert normalize_ai_model_name("MiniMax M2.7 Highspeed") == "MiniMax-M2.7-highspeed"
    assert normalize_ai_model_name("custom-model") == "custom-model"
