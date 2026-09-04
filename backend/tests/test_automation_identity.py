from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai_gateway import AiGenerationResult, AiUsage
from app.database import Base
from app.models import ContentKeywordRule, Tenant, TgGroup
from app.services.content_filters import filter_outbound_content, looks_like_ai_meta_content
from app.services.task_center import ai_provider_candidate_runtime as drafts
from app.services.task_center import ai_structured_provider_runtime as structured
from app.services.task_center.ai_generation_contract import (
    CHANNEL_COMMENT_PURPOSE, CHANNEL_COMMENT_REPLY_PURPOSE,
    GROUP_CHAT_PURPOSE, GROUP_CHAT_REPLY_PURPOSE,
    TWO_STAGE_BRIEF_PURPOSE, TWO_STAGE_REALIZE_PURPOSE, TWO_STAGE_REVIEW_PURPOSE,
)
from app.services.task_center.ai_generator import _prompt_profile
from app.services.task_center.ai_group_prompt import build_group_prompt
from app.services.automation_identity import (
    AUTOMATION_IDENTITY_SYSTEM_POLICY, with_automation_identity,
)

pytestmark = pytest.mark.no_postgres
DRAFT_PURPOSES = (
    GROUP_CHAT_PURPOSE, GROUP_CHAT_REPLY_PURPOSE,
    CHANNEL_COMMENT_PURPOSE, CHANNEL_COMMENT_REPLY_PURPOSE,
)
FORBIDDEN_INSTRUCTIONS = (
    "不要暴露 AI", "不提 AI、任务", "你是一个 Telegram 频道评论区的真实订阅读者",
    "上周去过态度还行", "上周刚踩雷", "月底发工资我先去探", "不得生成女性或中性身份",
)


def _provider():
    return SimpleNamespace(id=1, provider_name="QA-only", model_name="qa-model")


def _patch_runtime(monkeypatch, module):
    monkeypatch.setattr(module, "begin_provider_call", lambda *_: None)
    monkeypatch.setattr(module, "settle_provider_success", lambda *_: None)


@pytest.mark.parametrize("purpose", DRAFT_PURPOSES)
def test_draft_gateway_receives_identity_contract(monkeypatch, purpose):
    provider = _provider()
    credentials = SimpleNamespace(model_name="qa-model", provider_type="openai_compatible")
    request = drafts.ProviderDraftRequest(
        prompt="请回复群消息", count=1, topic="摄影", tone="简洁", persona_set=("旧面具",),
        temperature=0.7, max_tokens=100, system_prompt="自定义风格：简短", timeout=10,
    )
    captured = []

    def gateway(*args, **kwargs):
        captured.append(kwargs["system_prompt"])
        return AiGenerationResult([], AiUsage())

    _patch_runtime(monkeypatch, drafts)
    monkeypatch.setattr(drafts, "draft_provider_calls", lambda *_: ([provider], [(provider, credentials)]))
    monkeypatch.setattr(drafts, "_record_draft_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(drafts.ai_gateway, "generate_drafts", gateway)
    policy = drafts.ProviderCandidatePolicy(
        model_name="qa-model", required_model_family="", allow_quota_rotation=False,
        purpose=purpose, close_transaction_before_external=False,
    )
    drafts.generate_with_provider_candidates(None, provider, request, policy=policy)
    assert captured == [with_automation_identity(request.system_prompt)]
    assert request.system_prompt == "自定义风格：简短"


@pytest.mark.parametrize("purpose", (
    TWO_STAGE_BRIEF_PURPOSE, TWO_STAGE_REALIZE_PURPOSE, TWO_STAGE_REVIEW_PURPOSE,
))
def test_structured_gateway_receives_identity_contract(monkeypatch, purpose):
    provider = _provider()
    credentials = SimpleNamespace(model_name="qa-model")
    request = structured.StructuredProviderRequest(
        system_prompt="只输出约定 JSON", user_prompt="QA 上下文", config={}, temperature=0.7,
        max_tokens=100, count=1, purpose=purpose, model_name="qa-model", stage="primary",
        required_model_family="",
    )
    captured = []

    def gateway(*args, **kwargs):
        captured.append(kwargs["system_prompt"])
        return {"qa_only": True}, AiUsage()

    _patch_runtime(monkeypatch, structured)
    monkeypatch.setattr(structured, "structured_provider_calls", lambda *_: ([provider], [(provider, credentials)]))
    monkeypatch.setattr(structured, "record_attempt", lambda *args, **kwargs: None)
    monkeypatch.setattr(structured.ai_gateway, "generate_structured", gateway)
    structured.generate_structured_with_candidates(None, provider, request)
    assert captured == [with_automation_identity(request.system_prompt)]
    assert request.system_prompt == "只输出约定 JSON"


@pytest.mark.parametrize("purpose", DRAFT_PURPOSES)
def test_builtin_draft_prompts_do_not_instruct_concealment(purpose):
    prompt, personas, _ = _prompt_profile(
        count=1, purpose=purpose, target_label="QA 摄影", topic="构图", requirements="引用目标 1：光线偏暗",
    )
    assert not any(instruction in prompt for instruction in FORBIDDEN_INSTRUCTIONS)
    assert len(personas) >= 4
    assert all(isinstance(persona, str) and persona for persona in personas)
    assert "JSON" in prompt


@pytest.mark.parametrize("adult", (False, True))
def test_direct_group_bundle_preserves_identity_contract_and_slot(adult):
    config = {
        "adult_prompt_enabled": adult, "system_prompt_override": "自然直接",
        "account_personas": {"1": "历史面具"},
        "generation_slots": [{"sequence_index": 1, "slot_id": "qa-slot", "account_id": 1}],
    }
    bundle = build_group_prompt(config, target_label="QA 群", history="今天聊摄影构图", count=1)
    assert bundle.system_prompt.endswith(AUTOMATION_IDENTITY_SYSTEM_POLICY)
    assert not any(instruction in bundle.system_prompt for instruction in FORBIDDEN_INSTRUCTIONS)
    assert "qa-slot" in bundle.user_prompt


def test_identity_contract_is_idempotent_and_keeps_override_as_input():
    original = "自然风格配置"
    prompt = with_automation_identity(original)
    assert prompt == f"{original}\n\n{AUTOMATION_IDENTITY_SYSTEM_POLICY}"
    assert with_automation_identity(prompt) == prompt
    assert with_automation_identity(None) == AUTOMATION_IDENTITY_SYSTEM_POLICY


def test_announcement_disclosure_contract_is_nonempty_and_truthful():
    assert AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "不要求每条消息重复自报 AI" in AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "身份被询问时如实说明自动化身份" in AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "不编造亲历" in AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "不要假定或声称公告已经发布" in AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert with_automation_identity("旧面具要求冒充真人").endswith(AUTOMATION_IDENTITY_SYSTEM_POLICY)


@pytest.mark.parametrize("text", (
    "我是 AI 自动化助手，不是独立真人。", "这是运营管理的自动化账号。",
    "As an AI assistant, I am operated by this community.",
))
def test_identity_disclosure_is_not_process_content(text):
    assert not looks_like_ai_meta_content(text)


@pytest.mark.parametrize("text", (
    "As an AI, let me analyze this request: <think>internal reasoning</think>",
    "As an AI assistant, let me analyze this request first",
    "我是 AI。让我仔细分析这个请求", "I need to analyze this request first",
))
def test_identity_disclosure_does_not_bypass_process_filter(text):
    assert looks_like_ai_meta_content(text)


def test_outbound_disclosure_still_obeys_tenant_keyword_rules():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="QA only"))
        session.add(ContentKeywordRule(tenant_id=1, keyword="QA禁词", match_type="contains", is_active=True))
        session.flush()
        group = TgGroup(tenant_id=1, title="QA only")
        permitted = filter_outbound_content(
            session, tenant_id=1, group=group, content="As an AI assistant, I can explain this.",
        )
        rejected = filter_outbound_content(
            session, tenant_id=1, group=group, content="我是 AI，QA禁词",
        )
        assert permitted.ok
        assert not rejected.ok
        assert "租户关键词" in rejected.reason
    engine.dispose()


@pytest.mark.parametrize("task_type", ("CHANNEL_REPLY", "GROUP_SEND"))
def test_legacy_operation_gateway_receives_identity_contract(monkeypatch, task_type):
    from app.services import operations

    captured = []

    def gateway(*args, **kwargs):
        captured.append((args[1], kwargs["system_prompt"]))
        return SimpleNamespace(candidates=[SimpleNamespace(content="QA 内容")])

    monkeypatch.setattr(operations, "_pick_ai_provider", lambda *_: _provider())
    monkeypatch.setattr(operations, "ai_provider_credentials", lambda value: value)
    monkeypatch.setattr(operations, "get_tenant_ai_setting", lambda *_: SimpleNamespace(temperature=0.7, max_tokens=100))
    monkeypatch.setattr(operations.ai_gateway, "generate_drafts", gateway)
    task = SimpleNamespace(tenant_id=1, task_type=task_type, title="QA", content="QA", account_ids="1")
    operations._generate_operation_contents(None, task, count=1, target_label="QA")
    assert captured[0][1] == AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "不要暴露 AI" not in captured[0][0]


def test_mirror_rewrite_preserves_attribution_and_identity_contract(monkeypatch):
    from app.services import campaign_runs

    captured = []

    def gateway(*args, **kwargs):
        captured.append((args[1], kwargs["system_prompt"]))
        return SimpleNamespace(candidates=[SimpleNamespace(content="原作者表示光线偏暗")])

    monkeypatch.setattr(campaign_runs, "pick_ai_provider", lambda *_: SimpleNamespace(provider_type="qa-only"))
    monkeypatch.setattr(campaign_runs, "ai_provider_credentials", lambda value: value)
    monkeypatch.setattr(campaign_runs, "get_tenant_ai_setting", lambda *_: SimpleNamespace(ai_enabled=True, temperature=0.7, max_tokens=100))
    monkeypatch.setattr(campaign_runs.ai_gateway, "generate_drafts", gateway)
    campaign_runs._rewrite_mirror_content(
        None, campaign=SimpleNamespace(tenant_id=1, topic="摄影"),
        target_group=SimpleNamespace(title="QA", topic_direction="摄影"),
        message=SimpleNamespace(sender_name="原作者", content="我拍的照片光线偏暗"),
    )
    assert captured[0][1] == AUTOMATION_IDENTITY_SYSTEM_POLICY
    assert "保留来源归属" in captured[0][0]
    assert "不要暴露转发、监听、AI" not in captured[0][0]


def test_legacy_campaign_render_keeps_account_order_without_concealment():
    from app.models import Campaign, PromptTemplate, TgAccount
    from app.schemas import GenerateDraftsRequest
    from app.services.campaigns import render_prompt

    prompt = render_prompt(
        PromptTemplate(name="QA", template_type="多账号对话脚本", content="{{topic}} {{conversation_context}}"),
        campaign=Campaign(tenant_id=1, group_id=1, title="QA", topic="摄影"),
        group=TgGroup(tenant_id=1, title="QA", topic_direction="摄影"),
        payload=GenerateDraftsRequest(count=1), materials=[],
        selected_accounts=[TgAccount(id=1, tenant_id=1, display_name="QA 自动化", username="qa", health_score=90)],
        listener_account=None,
    )
    assert "A账号: #1 QA 自动化 @qa" in prompt
    assert "如实说明 AI/自动化身份" not in prompt
    assert "不要暴露运营、脚本、AI" not in prompt
