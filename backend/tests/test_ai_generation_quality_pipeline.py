from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_generation_dependencies import GenerationDependencies
from app.services.task_center.ai_generation_pipeline import _static_emoji_text, generate_quality_results


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


def test_daily_coverage_uses_distinct_explicit_static_fallback_after_all_models_reject() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [
                _coverage_slot("slot-1", 11),
                _coverage_slot("slot-2", 12),
            ],
        },
        batch_ids=["action-1", "action-2"],
        quality_snapshots=[
            {"account_profile": "少表情，避免连续 emoji", "stance_summary": ""},
            {"account_profile": "少表情，避免连续 emoji", "stance_summary": ""},
        ],
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, observed)),
        )

    assert observed == ["primary_m3", "fallback_m25", "fallback_grok"]
    assert len({str(result.content) for result in results}) == 2
    assert {result.rejection_code for result in results} == {""}
    assert {result.quality_fallback for result in results} == {"emoji_react"}
    assert {getattr(result.content, "fallback_stage") for result in results} == {"static_safe_fallback"}
    assert {getattr(result.content, "generation_source") for result in results} == {"static_safe_fallback"}


def test_static_fallback_switch_off_keeps_quality_rejection_visible() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={
            "_ai_group_static_fallback_enabled": False,
            "generation_slots": [_coverage_slot("slot-1", 11)],
        },
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, [])),
        )

    assert results[0].rejection_code == "voice_profile_mismatch"
    assert results[0].quality_fallback == ""


def test_daily_coverage_static_fallback_handles_provider_unavailability() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "",
        cached=False,
        config={
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [_coverage_slot("slot-1", 11)],
        },
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_unavailable_generator),
        )

    assert results[0].rejection_code == ""
    assert results[0].quality_fallback == "emoji_react"
    assert results[0].fallback_reason == "all_model_stages_rejected"


def test_cached_rejection_reenters_daily_coverage_static_fallback() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        config={
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [_coverage_slot("slot-1", 11)],
        },
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == ""
    assert results[0].quality_fallback == "emoji_react"


def test_explicit_single_model_does_not_enter_default_static_fallback_chain() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={
            "ai_model": "DeepSeek V4 Flash",
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [_coverage_slot("slot-1", 11)],
        },
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, [])),
        )

    assert results[0].rejection_code == "voice_profile_mismatch"
    assert results[0].quality_fallback == ""


def test_cached_static_fallback_keeps_explicit_audit_without_profile_rejection() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "placeholder",
        config={
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [_coverage_slot("slot-1", 11)],
        },
    )
    request.cached_contents = [GeneratedContent(
        "👋✨🌟🌈",
        generation_source="static_safe_fallback",
        fallback_stage="static_safe_fallback",
        fallback_reason="provider_unavailable",
        quality_fallback="emoji_react",
        slot_id="slot-1",
        sequence_index=1,
    )]
    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == ""
    assert results[0].quality_fallback == "emoji_react"
    assert results[0].fallback_reason == "provider_unavailable"

    request.config["_ai_group_static_fallback_enabled"] = False
    with Session(engine) as session:
        disabled_results, _tokens = generate_quality_results(session, request, _dependencies())
    assert disabled_results[0].rejection_code == "static_fallback_disabled"


def test_static_fallback_is_distinct_for_second_daily_target() -> None:
    first = _coverage_slot("slot-1", 11)
    first.update({"group_id": 201, "account_id": 9001})
    second = {**first, "slot_id": "slot-2", "coverage_account_completed_before_action": 1}

    assert _static_emoji_text(first) != _static_emoji_text(second)


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
        "cached_contents": [
            GeneratedContent(content, slot_id="slot-1", sequence_index=1)
        ] if cached else [],
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
        return [
            GeneratedContent(
                "😂😂",
                slot_id=slot["slot_id"],
                sequence_index=index,
            )
            for index, slot in enumerate(config["generation_slots"], 1)
        ], 1

    return generate


def _coverage_slot(slot_id: str, account_id: int) -> dict:
    return {
        "slot_id": slot_id,
        "account_id": account_id,
        "group_id": 2,
        "coverage_ledger_id": f"coverage-{account_id}",
        "coverage_window_date": "2026-07-16",
    }


def _forbidden_generator(*_args, **_kwargs):
    raise AssertionError("cached quality validation must not call a provider")


def _unavailable_generator(*_args, **_kwargs):
    raise AiGenerationUnavailable("provider unavailable")


def _dependencies(*, normal_generator=_forbidden_generator) -> GenerationDependencies:
    return GenerationDependencies(
        normal_generator=normal_generator,
        reply_generator=_forbidden_generator,
        reply_target_probe=_forbidden_generator,
        reply_messages_fetcher=_forbidden_generator,
    )
