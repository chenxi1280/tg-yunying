from __future__ import annotations

import pytest
from app.ai_gateway import AiDraftCandidate, AiGenerationResult, AiUsage
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AiProvider, Tenant, TenantAiSetting
from app.schemas.ai_config import TenantAiSettingUpdate
from app.services.ai_config import update_tenant_ai_setting
from app.services.task_center.ai_generator import AiGenerationUnavailable
from app.services.task_center import ai_generator
from app.services.task_center.payloads import SendMessagePayload
from app.services.task_center.ai_group_prompt import GroupPromptBundle
from app.services.task_center.executors import group_ai_chat


pytestmark = pytest.mark.no_postgres


def test_tenant_ai_group_fallback_switches_default_enabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        setting = TenantAiSetting(tenant_id=1)
        session.add(setting)
        session.commit()
        session.refresh(setting)

        assert setting.ai_group_model_fallback_enabled is True
        assert setting.ai_group_grok_fallback_enabled is True
        assert setting.ai_group_static_fallback_enabled is True


def test_tenant_ai_group_fallback_switches_can_be_disabled():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
        session.commit()

        updated = update_tenant_ai_setting(
            session,
            1,
            TenantAiSettingUpdate(
                ai_group_model_fallback_enabled=False,
                ai_group_grok_fallback_enabled=False,
                ai_group_static_fallback_enabled=False,
            ),
            "pytest",
        )

        assert updated.ai_group_model_fallback_enabled is False
        assert updated.ai_group_grok_fallback_enabled is False
        assert updated.ai_group_static_fallback_enabled is False


@pytest.mark.parametrize(
    ("config", "expected"),
    [
        ({}, ("primary_m3", "fallback_m25", "fallback_grok")),
        ({"_ai_group_model_fallback_enabled": False}, ("primary_m3", "fallback_grok")),
        ({"_ai_group_grok_fallback_enabled": False}, ("primary_m3", "fallback_m25")),
    ],
)
def test_ai_group_fallback_stages_follow_explicit_switches(config, expected):
    assert group_ai_chat._fallback_stages(config) == expected


def test_explicit_mimo_requirement_does_not_enter_default_provider_chain():
    assert group_ai_chat._fallback_stages({"require_mimo_draft": True}) == ("direct_mimo",)
    assert group_ai_chat._fallback_stages({"ai_model": "DeepSeek V4 Flash"}) == ("direct_configured_model",)


def test_ai_group_fallback_continues_after_stage_error(monkeypatch):
    task = type("TaskStub", (), {"tenant_id": 1, "stats": {}})()
    visited: list[str] = []

    def fake_generate(_session, _tenant_id, config, *, count, target_label, history):
        stage = config["_ai_fallback_stage"]
        visited.append(stage)
        if stage != "fallback_grok":
            raise AiGenerationUnavailable(f"{stage} unavailable")
        return ["老师今天高跟鞋挺好看"], 7

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate)

    items, tokens, stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=1,
        target_label="测试群",
        history="真人A: 今天这身搭配挺好看",
        turn_count=1,
        duplicate_baseline_messages=[],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=False,
    )

    assert visited == ["primary_m3", "fallback_m25", "fallback_grok"]
    assert [item["content"] for item in items] == ["老师今天高跟鞋挺好看"]
    assert tokens == 7
    assert stats["ai_generation_stage_failures"] == [
        {"stage": "primary_m3", "error_code": "ai_generation_unavailable"},
        {"stage": "fallback_m25", "error_code": "ai_generation_unavailable"},
    ]


def test_ai_group_quality_rejection_is_visible_to_next_stage(monkeypatch):
    task = type("TaskStub", (), {"tenant_id": 1, "stats": {}})()
    attempts_seen: list[list[dict]] = []

    def fake_generate(_session, _tenant_id, config, *, count, target_label, history):
        attempts_seen.append(list(config.get("_ai_generation_attempts") or []))
        if config["_ai_fallback_stage"] == "primary_m3":
            return ["重复内容"], 1
        return ["老师今天高跟鞋挺好看"], 1

    monkeypatch.setattr(group_ai_chat, "generate_group_messages", fake_generate)
    items, _tokens, stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[],
        normal_count=1,
        target_label="测试群",
        history="真人A: 今天这身搭配挺好看",
        turn_count=1,
        duplicate_baseline_messages=["重复内容"],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[1],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=False,
    )

    assert items[0]["content"] == "老师今天高跟鞋挺好看"
    assert attempts_seen[1][0]["outcome"] == "rejected"
    assert stats["ai_generation_stage_failures"][0]["error_code"] == "quality_rejected"


def test_ai_group_fallback_retries_the_same_reply_target(monkeypatch):
    task = type("TaskStub", (), {"tenant_id": 1, "stats": {}})()
    visited: list[tuple[str, int]] = []

    def fake_reply(_session, _tenant_id, config, *, reply_targets, target_label, history):
        visited.append((config["_ai_fallback_stage"], reply_targets[0]["message_id"]))
        if config["_ai_fallback_stage"] == "primary_m3":
            raise AiGenerationUnavailable("primary failed")
        return ["这双高跟鞋确实很搭"], 1

    monkeypatch.setattr(group_ai_chat, "generate_group_reply_messages", fake_reply)
    items, _tokens, _stats = group_ai_chat._generate_quality_filled_items(
        None,
        task,
        {},
        reply_targets=[{"message_id": 88, "preview": "今天这身搭配挺好看"}],
        normal_count=0,
        target_label="测试群",
        history="真人A: 今天这身搭配挺好看",
        turn_count=1,
        duplicate_baseline_messages=[],
        chat_mode=group_ai_chat.CHAT_MODE_REPLY,
        context_message_ids=[88],
        fact_anchor_required=False,
        low_confidence_silence_enabled=False,
        fill_reply_shortfall_with_normal=False,
        enable_quality_fallback=False,
    )

    assert visited == [("primary_m3", 88), ("fallback_m25", 88)]
    assert items[0]["reply_target"]["message_id"] == 88


def test_ai_group_stage_provider_requires_exact_model():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            AiProvider(
                provider_name="MiniMax M3",
                base_url="https://api.minimax.io/v1",
                model_name="MiniMax-M3",
                api_key_ciphertext="test",
            ),
            AiProvider(
                provider_name="MiniMax M2.5",
                base_url="https://api.minimax.io/v1",
                model_name="MiniMax-M2.5",
                api_key_ciphertext="test",
            ),
        ])
        session.commit()

        assert ai_generator._provider_for_exact_model(session, "MiniMax-M3").model_name == "MiniMax-M3"
        assert ai_generator._provider_for_exact_model(session, "MiniMax-M2.5").model_name == "MiniMax-M2.5"
        assert ai_generator._provider_for_exact_model(session, "MiniMax-M2.7") is None


def test_provider_generation_metadata_is_accepted_by_send_payload():
    content = ai_generator.GeneratedContent(
        "老师今天高跟鞋挺好看",
        requested_model="MiniMax-M3",
        actual_model="MiniMax-M2.5",
        fallback_stage="fallback_m25",
        fallback_reason="previous_stage_failed_or_rejected",
        provider_duration_ms=1234,
        generation_attempts=[
            {"stage": "primary_m3", "model": "MiniMax-M3", "outcome": "failed"},
            {"stage": "fallback_m25", "model": "MiniMax-M2.5", "outcome": "success"},
        ],
    )
    item = {"content": str(content), **group_ai_chat._generated_content_metadata(content)}
    payload = SendMessagePayload(
        chat_id="-1001",
        message_text=item["content"],
        **group_ai_chat._provider_generation_payload(item),
    )

    assert payload.requested_model == "MiniMax-M3"
    assert payload.actual_model == "MiniMax-M2.5"
    assert payload.fallback_stage == "fallback_m25"
    assert payload.provider_duration_ms == 1234
    assert len(payload.generation_attempts) == 2


def test_grok_stage_uses_cli_bridge_and_preserves_stage_metadata(monkeypatch):
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    class FakeBridge:
        def generate(self, *, system_prompt, user_prompt, count):
            assert session.in_transaction() is False
            assert system_prompt == "system"
            assert user_prompt == "user"
            assert count == 1
            return AiGenerationResult(
                candidates=[AiDraftCandidate(persona="群友", content="老师今天高跟鞋挺好看")],
                usage=AiUsage(),
            )

    monkeypatch.setattr(ai_generator, "GrokCliBridge", FakeBridge)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(TenantAiSetting(tenant_id=1, ai_enabled=True))
        session.commit()
        contents, tokens = ai_generator._generate_group_prompt_contents(
            session,
            1,
            {
                "_ai_fallback_stage": "fallback_grok",
                "_close_db_transaction_before_ai": True,
            },
            GroupPromptBundle(
                system_prompt="system",
                user_prompt="user",
                context_source="neutral_fallback",
                sanitized_context=(),
                input_payload={},
                output_contract={},
            ),
            count=1,
            purpose=ai_generator.GROUP_CHAT_PURPOSE,
        )

    assert tokens == 0
    assert contents[0].actual_model == "grok-4.5"
    assert contents[0].fallback_stage == "fallback_grok"
