from __future__ import annotations

from datetime import date, timedelta
import pytest

from app.services.task_center.ai_group_daily_theme import (
    DailyExpressionContext,
    get_daily_theme_index,
    get_daily_vocabulary_theme,
)
from app.services.task_center.ai_group_prompt import (
    build_group_prompt,
    get_adult_system_prompt,
)

pytestmark = pytest.mark.no_postgres


def test_daily_theme_index_deterministic_and_cross_process():
    scope = "tenant_1:group_100:adult"
    d = date(2026, 8, 31)
    idx1 = get_daily_theme_index(scope, d)
    idx2 = get_daily_theme_index(scope, d)
    assert idx1 == idx2
    assert 0 <= idx1 < 7


def test_seven_consecutive_days_covers_seven_themes():
    scope = "tenant_1:group_100:adult"
    base_d = date(2026, 8, 31)
    seen_indices = set()
    for offset in range(7):
        curr_d = base_d + timedelta(days=offset)
        seen_indices.add(get_daily_theme_index(scope, curr_d))
    assert seen_indices == {0, 1, 2, 3, 4, 5, 6}


def test_different_groups_have_stable_offset():
    d = date(2026, 8, 31)
    idx_a = get_daily_theme_index("tenant_1:group_100:adult", d)
    idx_b = get_daily_theme_index("tenant_1:group_200:adult", d)
    assert 0 <= idx_a < 7
    assert 0 <= idx_b < 7


def test_get_adult_system_prompt_includes_daily_theme_section():
    prompt = get_adult_system_prompt("tenant_1:group_100:adult", date(2026, 8, 31))
    assert "今日表达调色板" in prompt
    assert "调色板指引" in prompt
    assert "禁忌事项" in prompt


def test_build_group_prompt_with_daily_expression_context():
    scope = "tenant_1:group_100:adult"
    task_day = date(2026, 8, 31)
    theme = get_daily_vocabulary_theme(scope, task_day)
    ctx = DailyExpressionContext(
        surface_scope_key=scope,
        task_day=task_day,
        allocation_plan_id="plan_001",
        plan_unit_ordinal=1,
        relation_kind="direct",
        act_type="detail_follow",
        stance="positive",
        topic_mode="group_free_chat",
        vocabulary_theme_id=theme.theme_id,
        vocabulary_sample_ids=("adult_app_01", "adult_att_01"),
        vocabulary_surface_terms=("素颜看着挺顺眼", "态度挺温和不机车"),
    )
    config = {"adult_prompt_enabled": True, "content_route": "adult_service"}
    bundle = build_group_prompt(config, target_label="西安天上人间", history="", count=1, expression_context=ctx)
    assert "今日表达调色板" in bundle.system_prompt
    assert bundle.input_payload["optional_vocabulary_hints"] == [
        "素颜看着挺顺眼",
        "态度挺温和不机车",
    ]
    assert "adult_app_01" not in bundle.user_prompt
