from __future__ import annotations

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.ai_gateway import AiDraftCandidate, AiGenerationResult, AiUsage
from app.database import Base
from app.models import AiProvider, Tenant, TenantAiSetting
from app.security import encrypt_secret
from app.services.task_center.ai_generator import AiGenerationUnavailable, generate_channel_comments, generate_channel_reply_comments


@pytest.mark.no_postgres
def test_channel_comment_provider_prompt_uses_implicit_sensitive_terms(monkeypatch):
    captured = _capture_provider_prompt(monkeypatch)

    with _session() as session:
        generate_channel_comments(
            session,
            1,
            {"comment_style": "relevant", "language": "zh-CN"},
            count=1,
            message_content="裸露的情趣内衣让人血脉喷张，她慢慢蹲下含住小小j，双峰贴上来",
            target_label="阿哥日记",
        )

    provider_text = captured["prompt"] + captured["system_prompt"]
    for raw in ["情趣内衣", "小小j", "双峰", "含住", "裸露", "血脉喷张"]:
        assert raw not in provider_text
    for implicit in ["氛围装扮", "私密称呼", "身形细节", "亲密互动"]:
        assert implicit in provider_text
    assert "隐晦中性" in provider_text


@pytest.mark.no_postgres
def test_channel_reply_provider_prompt_sanitizes_reply_targets(monkeypatch):
    captured = _capture_provider_prompt(monkeypatch)

    with _session() as session:
        generate_channel_reply_comments(
            session,
            1,
            {"comment_style": "relevant"},
            reply_targets=[
                {
                    "message_id": 9001,
                    "author": "读者",
                    "preview": "情趣内衣和小小j这段太直白",
                    "source": "channel_comment",
                }
            ],
            message_content="她穿着情趣内衣，双峰贴上来",
            target_label="阿哥日记",
        )

    provider_text = captured["prompt"] + captured["system_prompt"]
    for raw in ["情趣内衣", "小小j", "双峰"]:
        assert raw not in provider_text
    assert "氛围装扮" in provider_text
    assert "私密称呼" in provider_text


@pytest.mark.no_postgres
def test_minimax_sensitive_rejection_falls_back_to_older_models(monkeypatch):
    requested_models: list[str] = []

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        requested_models.append(credentials.model_name)
        if credentials.model_name in {"MiniMax-M3", "MiniMax-M2.7"}:
            raise RuntimeError(
                'AI provider HTTP 422: {"error":{"message":"input new_sensitive (1026)"}}'
            )
        return AiGenerationResult(
            candidates=[AiDraftCandidate(persona="读者", content="这段氛围感挺明显")],
            usage=AiUsage(total_tokens=11),
        )

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with _session() as session:
        contents, _tokens = generate_channel_comments(
            session,
            1,
            {"comment_style": "relevant"},
            count=1,
            message_content="她穿着情趣内衣，双峰贴上来",
            target_label="阿哥日记",
        )

    assert contents == ["这段氛围感挺明显"]
    assert requested_models == ["MiniMax-M3", "MiniMax-M2.7", "MiniMax-M2.5"]


@pytest.mark.no_postgres
def test_minimax_generic_safety_rejection_does_not_fall_back_to_older_models(monkeypatch):
    requested_models: list[str] = []

    def fake_generate_drafts(credentials, _prompt, **_kwargs):
        requested_models.append(credentials.model_name)
        raise RuntimeError("AI provider rejected request: content policy violation")

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)

    with _session() as session:
        with pytest.raises(AiGenerationUnavailable, match="AI 生成不可用"):
            generate_channel_comments(
                session,
                1,
                {"comment_style": "relevant"},
                count=1,
                message_content="她穿着情趣内衣，双峰贴上来",
                target_label="阿哥日记",
            )

    assert requested_models == ["MiniMax-M3"]


def _capture_provider_prompt(monkeypatch) -> dict[str, str]:
    captured: dict[str, str] = {}

    def fake_generate_drafts(_credentials, prompt, **kwargs):
        captured["prompt"] = prompt
        captured["system_prompt"] = str(kwargs.get("system_prompt") or "")
        return AiGenerationResult(
            candidates=[AiDraftCandidate(persona="读者", content="这段氛围感挺明显")],
            usage=AiUsage(total_tokens=11),
        )

    monkeypatch.setattr("app.services.task_center.ai_generator.ai_gateway.generate_drafts", fake_generate_drafts)
    return captured


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.add(
        AiProvider(
            id=1,
            provider_name="MiniMax",
            provider_type="openai_compatible",
            base_url="https://api.minimax.io/v1",
            model_name="MiniMax-M3",
            api_key_ciphertext=encrypt_secret("test-key"),
            health_status="健康",
        )
    )
    session.add(TenantAiSetting(tenant_id=1, default_provider_id=1, ai_enabled=True, max_tokens=1024))
    session.commit()
    return session
