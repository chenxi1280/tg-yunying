from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.task_center.ai_generator import GeneratedContent
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_pipeline import generate_quality_results


pytestmark = pytest.mark.no_postgres


def test_quality_pipeline_runs_m3_m25_grok_without_open_transactions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []
    with Session(engine) as session:
        request = _request("😂😂", account_profile="少表情，避免连续 emoji", cached=False)

        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, observed)),
        )

    assert observed == ["primary_m3", "fallback_m25", "fallback_grok"]
    assert results[0].rejection_code == "voice_profile_mismatch"


@pytest.mark.parametrize(
    ("content", "request_updates", "expected_code"),
    [
        ("确实不错", {}, "template_shell_limited"),
        ("照片没p", {"duplicate_baseline_messages": ["照片准"]}, "duplicate_risk"),
        ("我上次准点到", {"chat_mode": "idle_warmup"}, "hallucination_risk"),
        (
            "之前位置发过",
            {"chat_mode": "bootstrap", "fact_anchor_required": False},
            "hallucination_risk",
        ),
        ("绝对可以", {"stance_summary": "谨慎观望，再看看"}, "stance_conflict"),
    ],
)
def test_cached_result_reenters_pure_quality_gates(content, request_updates, expected_code) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(content, **request_updates)
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(),
        )

    assert results[0].rejection_code == expected_code


def test_voice_profile_anchor_is_rewritten_before_match() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request("今天先聊聊", account_profile="男性老哥夜场表达")
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(),
        )

    assert results[0].rejection_code == ""
    assert "价格咋说" in results[0].content
    assert results[0].voice_profile_anchor_rewritten is True


def _request(
    content: str,
    *,
    account_profile: str = "",
    stance_summary: str = "",
    cached: bool = True,
    **updates,
):
    values = {
        "batch_ids": ["action-1"],
        "cached_contents": [GeneratedContent(content, sequence_index=1)] if cached else [],
        "cached_tokens": 0,
        "duplicate_baseline_messages": [],
        "quality_snapshots": [{"account_profile": account_profile, "stance_summary": stance_summary}],
        "config": {"generation_slots": [{"slot_id": "slot-1", "account_id": 11}]},
        "chat_mode": "reply",
        "context_message_ids": [1],
        "fact_anchor_required": True,
        "low_confidence_silence_enabled": True,
        "is_reply": False,
        "tenant_id": 1,
        "reply_targets": [],
        "target_label": "运营群",
        "history": "真人用户: 今天聊聊",
    }
    if "stance_summary" in updates:
        values["quality_snapshots"][0]["stance_summary"] = updates.pop("stance_summary")
    values.update(updates)
    return SimpleNamespace(**values)


def _stage_generator(session: Session, observed: list[str]):
    def generate(_session, _tenant_id, config, **_kwargs):
        assert session.in_transaction() is False
        observed.append(str(config.get("_ai_fallback_stage") or "direct"))
        return [GeneratedContent("😂😂", sequence_index=1)], 1

    return generate


def _forbidden_generator(*_args, **_kwargs):
    raise AssertionError("cached quality validation must not call a provider")


def _dependencies(*, normal_generator=_forbidden_generator) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=_forbidden_generator,
        reply_target_probe=_forbidden_generator,
        reply_messages_fetcher=_forbidden_generator,
    )
