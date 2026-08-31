from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from app.models import Action
from app.services.task_center import (
    ai_generation_dispatch,
    ai_generation_pipeline,
    ai_generation_runtime_config,
)
from app.services.task_center.ai_generator import AiGenerationUnavailable, GeneratedContent
from app.services.task_center.ai_generation_pipeline import generate_quality_results
from app.services.task_center.executors import group_ai_chat
from app.services.task_center.payloads import SendMessagePayload
from ai_generation_quality_test_support import (
    _coverage_slot,
    _dependencies,
    _forbidden_generator,
    _quantity_slot,
    _request,
    _stage_generator,
    _unavailable_generator,
)


pytestmark = pytest.mark.no_postgres


def test_quality_pipeline_runs_primary_and_backup_without_open_transactions() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []
    with Session(engine) as session:
        request = _request("😂😂", account_profile="少表情，避免连续 emoji", cached=False)

        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, observed)),
        )

    assert observed == ["primary_default"] * 3 + ["fallback_m25"] * 3
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


def test_voice_profile_never_rewrites_generated_content() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request("今天先聊聊", account_profile="男性老哥夜场表达")
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(),
        )

    assert results[0].rejection_code == ""
    assert results[0].content == "今天先聊聊"
    assert results[0].voice_profile_anchor_rewritten is False


def _adult_quality_config() -> dict:
    return {
        "adult_prompt_enabled": True,
        "content_route": "adult_service",
        "generation_slots": [{"slot_id": "slot-1", "account_id": 11}],
    }


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("照片看着挺靠谱", "adult_content_length_out_of_range"),
        ("照片看着确实靠谱", ""),
        ("照片看着确实靠谱😂", ""),
        ("真" * 20, ""),
        ("真" * 21, "adult_content_length_out_of_range"),
    ],
)
def test_adult_content_enforces_chinese_character_boundaries(content, expected_code) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(content, config=_adult_quality_config(), history="真人用户: 照片看着确实靠谱")

    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == expected_code


def test_general_route_is_not_subject_to_adult_length_contract() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request("今天先聊聊")

    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == ""


@pytest.mark.parametrize(
    ("content", "expected_code"),
    [
        ("照片看着确实靠谱", "adult_generic_warmup_requires_question"),
        ("最近哪位老师值得推荐？", "adult_generic_warmup_scope_violation"),
        ("最近照骗是不是又多了？", ""),
    ],
)
def test_adult_generic_warmup_only_allows_context_free_questions(content, expected_code) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(content, config=_adult_quality_config(), history="", context_message_ids=[])

    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == expected_code


@pytest.mark.parametrize(
    ("content", "history", "expected_code"),
    [
        ("昨晚去试了感觉挺稳", "真人用户: 最近照片修得很夸张", "adult_content_fact_unanchored"),
        ("昨晚去试了感觉挺稳", "真人用户: 昨晚去试了感觉一般", ""),
        ("万达公寓楼下停车方便", "真人用户: 最近照片修得很夸张", "adult_content_fact_unanchored"),
        ("万达公寓楼下停车方便", "真人用户: 万达公寓楼下停车不方便", ""),
        ("这个老师态度确实耐心", "真人用户: 最近照片修得很夸张", "adult_content_fact_unanchored"),
        ("这个老师态度确实耐心", "真人用户: 这个老师态度很耐心", ""),
    ],
)
def test_adult_fact_claims_require_same_kind_of_context_anchor(content, history, expected_code) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(content, config=_adult_quality_config(), history=history)

    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == expected_code


def test_mask_profile_does_not_force_transaction_topic_guidance() -> None:
    slot = group_ai_chat._generation_slot(
        "cycle-1",
        0,
        SimpleNamespace(id=11),
        None,
        {"11": "本地男性短句寻欢客重点问位置时间和避坑"},
    )

    assert "content_guidance" not in slot


def test_coverage_slot_without_primary_quantity_never_uses_static_fallback() -> None:
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

    assert observed == ["primary_default"] * 3 + ["fallback_m25"] * 3
    assert all(result.quality_fallback == "" for result in results)
    assert all(result.rejection_code == "voice_profile_mismatch" for result in results)


def test_static_fallback_setting_disables_primary_quantity_check_in() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={
            "_ai_group_static_fallback_enabled": False,
            "generation_slots": [_quantity_slot("slot-1", 11)],
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


def test_negative_lexicon_rejection_never_turns_into_check_in_fallback() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)

    def banned_generator(_session, _tenant_id, config, **_kwargs):
        return [GeneratedContent(
            "大家来签到",
            slot_id=config["generation_slots"][0]["slot_id"],
            sequence_index=1,
        )], 5

    request = _request(
        "",
        cached=False,
        config={
            "_ai_group_static_fallback_enabled": True,
            "generation_slots": [_quantity_slot("slot-1", 11)],
        },
    )
    with Session(engine) as session:
        results, tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=banned_generator),
        )

    assert tokens == 30
    assert results[0].rejection_code == "negative_lexicon_match"
    assert results[0].quality_fallback == ""
    assert str(results[0].content) == "大家来签到"


def test_legacy_runtime_binds_generation_job_for_attempt_ledger() -> None:
    config = {}
    payload = SimpleNamespace(generation_job_id="job-legacy-1")

    ai_generation_runtime_config._bind_legacy_attempt_job(
        config,
        [(SimpleNamespace(), payload)],
    )

    assert config["_generation_job_id"] == "job-legacy-1"


def test_coverage_slot_provider_unavailability_remains_visible() -> None:
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
        with pytest.raises(AiGenerationUnavailable, match="provider unavailable"):
            generate_quality_results(
                session,
                request,
                _dependencies(normal_generator=_unavailable_generator),
            )


def test_extra_volume_quantity_slot_uses_check_in_when_all_providers_unavailable() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "",
        cached=False,
        config={
            "generation_slots": [{
                "slot_id": "slot-extra-1",
                "account_id": 11,
                "group_id": 2,
                "primary_quantity_slot_id": "quantity-extra-1",
                "coverage_ledger_id": "",
                "content_obligation_fallback_ready": True,
            }],
        },
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_unavailable_generator),
        )

    assert results[0].rejection_code == ""
    assert results[0].quality_fallback == "check_in_fallback"
    assert results[0].fallback_reason == "all_model_stages_rejected"
    assert str(results[0].content) == "签到"


def test_due_catch_up_bypasses_provider_for_eligible_quantity_slot() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "",
        cached=False,
        config={
            "_ai_group_due_catch_up_required": True,
            "generation_slots": [_quantity_slot("slot-1", 11)],
        },
    )

    with Session(engine) as session:
        results, tokens = generate_quality_results(
            session,
            request,
            _dependencies(),
        )

    assert tokens == 0
    assert str(results[0].content) == "签到"
    assert results[0].quality_fallback == "check_in_fallback"
    assert results[0].fallback_reason == "due_catch_up_provider_budget_exhausted"


def test_due_catch_up_respects_explicit_model_contract() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []
    request = _request(
        "",
        cached=False,
        config={
            "ai_model": "explicit-model",
            "_ai_group_due_catch_up_required": True,
            "generation_slots": [_quantity_slot("slot-1", 11)],
        },
    )

    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, observed)),
        )

    assert observed == ["direct"]
    assert results[0].quality_fallback == ""


def test_due_catch_up_never_bypasses_pending_content_obligation() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "",
        cached=False,
        config={
            "_ai_group_due_catch_up_required": True,
            "generation_slots": [{
                **_quantity_slot("slot-1", 11),
                "content_obligation_fallback_ready": False,
            }],
        },
    )

    with Session(engine) as session:
        with pytest.raises(AiGenerationUnavailable, match="provider unavailable"):
            generate_quality_results(
                session,
                request,
                _dependencies(normal_generator=_unavailable_generator),
            )


def test_due_catch_up_requires_debt_and_full_provider_timeout(monkeypatch) -> None:
    now_value = datetime(2026, 8, 3, 15, 0, tzinfo=UTC)
    action = SimpleNamespace(scheduled_at=now_value - timedelta(seconds=120))
    payload = SimpleNamespace(daily_group_target_id="target-1")
    target = SimpleNamespace(due_message_count=100, confirmed_message_count=20)
    session = SimpleNamespace(get=lambda _model, _target_id: target)
    monkeypatch.setattr(ai_generation_runtime_config, "_now", lambda: now_value)

    assert ai_generation_runtime_config._due_catch_up_required(
        session, action, payload,
    ) is True
    action.scheduled_at = now_value - timedelta(seconds=119)
    assert ai_generation_runtime_config._due_catch_up_required(
        session, action, payload,
    ) is False
    target.due_message_count = target.confirmed_message_count
    action.scheduled_at = now_value - timedelta(seconds=120)
    assert ai_generation_runtime_config._due_catch_up_required(
        session, action, payload,
    ) is False


def test_pending_content_obligation_never_degrades_to_check_in() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    request = _request(
        "",
        cached=False,
        config={
            "generation_slots": [{
                **_quantity_slot("slot-extra-1", 11),
                "content_obligation_fallback_ready": False,
            }],
        },
    )
    with Session(engine) as session:
        with pytest.raises(AiGenerationUnavailable, match="provider unavailable"):
            generate_quality_results(
                session,
                request,
                _dependencies(normal_generator=_unavailable_generator),
            )


def test_provider_attempt_stops_when_deadline_budget_is_insufficient(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    now_value = datetime(2026, 7, 31, 12, 0, tzinfo=UTC)
    observed: list[str] = []
    request = _request(
        "",
        cached=False,
        config={
            "_ai_generation_latest_safe_send_at": (
                now_value + timedelta(seconds=119)
            ).isoformat(),
            "generation_slots": [_quantity_slot("slot-1", 11)],
        },
    )
    monkeypatch.setattr(ai_generation_pipeline, "_now", lambda: now_value)

    with Session(engine) as session:
        with pytest.raises(
            AiGenerationUnavailable,
            match="ai_generation_deadline_budget_exhausted",
        ):
            generate_quality_results(
                session,
                request,
                _dependencies(normal_generator=_stage_generator(session, observed)),
            )

    assert observed == []


@pytest.mark.parametrize(
    "slot_update",
    [
        {"reply_to_message_id": 9001},
        {"material_intent": "表情包:围观"},
    ],
)
def test_reply_or_material_obligation_never_degrades_to_check_in(slot_update) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    slot = {**_quantity_slot("slot-1", 11), **slot_update}
    request = _request(
        "😂😂",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={"generation_slots": [slot]},
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=_stage_generator(session, [])),
        )

    assert results[0].quality_fallback == ""
    assert results[0].rejection_code == "voice_profile_mismatch"


def test_runtime_generation_slot_carries_primary_quantity_slot_id() -> None:
    action = Action(account_id=11, primary_quantity_slot_id="quantity-extra-1")
    payload = SendMessagePayload(
        slot_id="slot-extra-1",
        group_id=2,
        primary_quantity_slot_id="quantity-extra-1",
        ai_generation_status="pending",
    )

    slot = ai_generation_dispatch._generation_slot(action, payload, 1)

    assert slot["primary_quantity_slot_id"] == "quantity-extra-1"


def test_provider_unavailability_closes_read_transaction_before_next_stage() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []

    def unavailable_after_lookup(session, _tenant_id, config, **_kwargs):
        session.execute(text("SELECT 1"))
        observed.append(str(config.get("_ai_fallback_stage") or ""))
        raise AiGenerationUnavailable("configured model unavailable")

    request = _request(
        "",
        cached=False,
        config={"generation_slots": [_quantity_slot("slot-1", 11)]},
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=unavailable_after_lookup),
        )

    assert observed == ["primary_default"] * 3 + ["fallback_m25"] * 3
    assert results[0].quality_fallback == "check_in_fallback"
    assert results[0].content == "签到"


def test_cached_coverage_rejection_does_not_enter_static_fallback() -> None:
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

    assert results[0].rejection_code == "voice_profile_mismatch"
    assert results[0].quality_fallback == ""


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
            "generation_slots": [_quantity_slot("slot-1", 11)],
        },
    )
    request.cached_contents = [GeneratedContent(
        "👋✨🌟🌈",
        generation_source="static_safe_fallback",
        fallback_stage="static_safe_fallback",
        fallback_reason="provider_unavailable",
        quality_fallback="check_in_fallback",
        slot_id="slot-1",
        sequence_index=1,
    )]
    with Session(engine) as session:
        results, _tokens = generate_quality_results(session, request, _dependencies())

    assert results[0].rejection_code == ""
    assert results[0].quality_fallback == "check_in_fallback"
    assert results[0].fallback_reason == "provider_unavailable"

    request.config["_ai_group_static_fallback_enabled"] = False
    with Session(engine) as session:
        disabled_results, _tokens = generate_quality_results(session, request, _dependencies())
    assert disabled_results[0].quality_fallback == ""
    assert disabled_results[0].rejection_code == "static_fallback_disabled"


def test_primary_third_attempt_can_complete_without_backup() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    observed: list[str] = []

    def generator(_session, _tenant_id, config, **_kwargs):
        stage = str(config.get("_ai_fallback_stage") or "")
        observed.append(stage)
        content = "今天先聊聊" if len(observed) == 3 else "😂😂"
        slot = config["generation_slots"][0]
        return [GeneratedContent(content, slot_id=slot["slot_id"], sequence_index=1)], 1

    request = _request(
        "",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        config={"generation_slots": [_coverage_slot("slot-1", 11)]},
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(normal_generator=generator),
        )

    assert observed == ["primary_default"] * 3
    assert results[0].content == "今天先聊聊"


def test_reply_coverage_never_degrades_to_check_in() -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    slot = {
        **_coverage_slot("slot-reply", 11),
        "reply_to_message_id": "991",
    }
    request = _request(
        "",
        account_profile="少表情，避免连续 emoji",
        cached=False,
        is_reply=True,
        reply_targets=[{"message_id": "991"}],
        config={"generation_slots": [slot]},
    )
    with Session(engine) as session:
        results, _tokens = generate_quality_results(
            session,
            request,
            _dependencies(
                normal_generator=_forbidden_generator,
                reply_generator=_stage_generator(session, []),
            ),
        )

    assert results[0].quality_fallback == ""
    assert results[0].rejection_code == "voice_profile_mismatch"
