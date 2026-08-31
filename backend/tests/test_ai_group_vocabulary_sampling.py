from __future__ import annotations

from datetime import date
import pytest

from app.services.task_center.ai_group_vocabulary_sampling import (
    extract_vocabulary_usage,
    protected_frequency_phrases,
    sample_vocabulary_for_slot,
    surface_phrase_fingerprints,
)
from app.services.task_center.ai_group_vocabulary_catalog import (
    get_vocabulary_catalog,
)
from app.services.task_center.ai_group_content_intent_support import (
    vocabulary_suppression_reason,
)

pytestmark = pytest.mark.no_postgres


def test_sample_vocabulary_for_slot_basic_properties():
    res = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:adult",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_001",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=1,
        route="adult_service",
        act_type="short_react",
        stance="positive",
    )
    assert res.effective_state == "active"
    assert len(res.sample_units) <= 2
    assert len(res.sample_ids) <= 2
    assert res.candidate_count > 0


def test_sample_vocabulary_cooldown_excludes_recent_ids():
    # 第一次采样
    res1 = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:adult",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_001",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=1,
        act_type="short_react",
        stance="positive",
    )
    assert len(res1.sample_ids) > 0
    sampled_id = res1.sample_ids[0]

    # 将其加入 recent_used_ids，再次采样不能包含该 ID
    res2 = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:adult",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_001",
        plan_unit_ordinal=2,
        daily_vocabulary_theme_id=1,
        act_type="short_react",
        stance="positive",
        recent_used_ids_by_message=[(sampled_id,)],
    )
    assert sampled_id not in res2.sample_ids


def test_extract_vocabulary_usage_matches_exact_and_normalized():
    text = "真人比照片显瘦，而且态度挺温和不机车，老哥稳少走弯路"
    used_ids, used_terms = extract_vocabulary_usage(text, route_family="adult")
    assert "adult_app_02" in used_ids
    assert "adult_att_01" in used_ids
    assert "真人显瘦" in used_terms
    assert "态度温和" in used_terms


def test_sparse_exact_compatibility_cell_does_not_publish_samples():
    result = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:adult",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_sparse",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=0,
        route="adult_service",
        act_type="short_react",
        stance="reserved",
    )

    assert result.effective_state == "pool_exhausted"
    assert result.candidate_count == 0
    assert result.sample_ids == ()


def test_missing_stance_cannot_bypass_compatibility_manifest():
    result = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:general",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_missing_stance",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=1,
        route="general_chat",
        route_family="general",
        act_type="question",
        stance=None,
    )

    assert result.effective_state == "effective_pool_low"
    assert result.sample_ids == ()


@pytest.mark.parametrize("route_family,route", [("general", "general_chat"), ("adult", "adult_service")])
def test_generic_warmup_uses_expression_only_pool(route_family: str, route: str):
    result = sample_vocabulary_for_slot(
        surface_scope_key=f"tenant_1:group_100:{route_family}",
        task_day=date(2026, 8, 31),
        allocation_plan_id=f"plan_{route_family}",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=2,
        route=route,
        route_family=route_family,
        act_type="question",
        stance="neutral",
        is_generic_warmup=True,
    )

    assert result.effective_state == "active"
    assert result.candidate_count >= 12
    assert all(unit.fact_class == "expression_only" for unit in result.sample_units)


def test_context_bound_units_require_matching_evidence_anchor():
    result = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:general",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_no_evidence",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=1,
        route="general_chat",
        route_family="general",
        act_type="short_react",
        stance="positive",
        topic_mode="human_context",
        evidence_text="完全无关的真人消息",
    )

    assert result.effective_state == "active"
    assert all(unit.fact_class == "expression_only" for unit in result.sample_units)


def test_rare_cooldown_counts_messages_instead_of_flattened_sample_ids():
    rare = next(
        unit
        for unit in get_vocabulary_catalog("adult")
        if unit.fact_class == "expression_only"
        and unit.rarity == "rare"
        and unit.allowed_act_types == ("short_react",)
    )
    recent_messages = [(f"other-{index}-a", f"other-{index}-b") for index in range(14)]
    recent_messages.append(("other-14", rare.vocabulary_id))

    result = sample_vocabulary_for_slot(
        surface_scope_key="tenant_1:group_100:adult",
        task_day=date(2026, 8, 31),
        allocation_plan_id="plan_message_window",
        plan_unit_ordinal=1,
        daily_vocabulary_theme_id=rare.theme_tags[0],
        route="adult_service",
        route_family="adult",
        act_type="short_react",
        stance="positive",
        recent_used_ids_by_message=recent_messages,
    )

    assert rare.vocabulary_id not in result.sample_ids


def test_override_tone_and_persona_suppression_reasons_are_explicit():
    assert (
        vocabulary_suppression_reason({"system_prompt_override": "自定义"})
        == "suppressed_by_override"
    )
    assert (
        vocabulary_suppression_reason({"tone": "professional"}) == "suppressed_by_tone"
    )
    assert (
        vocabulary_suppression_reason({"tone": "auto"}, persona="正式行业观察员")
        == "suppressed_by_persona"
    )
    assert (
        vocabulary_suppression_reason({"tone": "auto"}, persona="轻松邻家老哥")
        == "suppressed_by_persona"
    )


def test_required_topic_and_teacher_phrases_are_excluded_from_frequency_fingerprint():
    excluded = protected_frequency_phrases(
        {"title": "每日主题词"},
        {"name": "王老师"},
    )

    assert (
        surface_phrase_fingerprints("每日主题词王老师", excluded_phrases=excluded) == ()
    )
    assert surface_phrase_fingerprints(
        "每日主题词后续看看", excluded_phrases=excluded
    ) == surface_phrase_fingerprints("后续看看")
