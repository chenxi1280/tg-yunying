from __future__ import annotations

import pytest
from datetime import date, datetime, timezone

from app.services.task_center.ai_group_topic_allocation import (
    check_remote_topic_capacity,
    decide_topic_mode,
    is_ordinal_topic_eligible,
    normalize_topic_participation_rate,
    validate_no_consecutive_three_questions,
    validate_question_ratio_in_window,
)
from app.services.task_center.ai_group_topic_policy import (
    effective_topic_rate,
    promote_due_topic_rate,
    stage_topic_rate_update,
)

pytestmark = pytest.mark.no_postgres


def test_normalize_topic_participation_rate_valid_and_invalid():
    assert normalize_topic_participation_rate(0.0) == 0
    assert normalize_topic_participation_rate(0.10) == 1000
    assert normalize_topic_participation_rate(0.30) == 3000

    with pytest.raises(ValueError, match="topic_participation_rate_required"):
        normalize_topic_participation_rate(None)

    with pytest.raises(
        ValueError, match="topic_participation_rate_out_of_range_0_to_0_30"
    ):
        normalize_topic_participation_rate(-0.01)

    with pytest.raises(
        ValueError, match="topic_participation_rate_out_of_range_0_to_0_30"
    ):
        normalize_topic_participation_rate(0.31)

    with pytest.raises(ValueError, match="topic_participation_rate_must_be_numeric"):
        normalize_topic_participation_rate("0.30")

    with pytest.raises(
        ValueError, match="topic_participation_rate_max_two_decimal_places"
    ):
        normalize_topic_participation_rate(0.001)


def test_prefix_hard_cap_formula_30_percent():
    b = 3000  # 30%
    # ordinal 1 to 10
    # floor(1*0.3)=0, floor(0*0.3)=0 -> F
    # floor(2*0.3)=0, floor(1*0.3)=0 -> F
    # floor(3*0.3)=0, floor(2*0.3)=0 -> F
    # floor(4*0.3)=1, floor(3*0.3)=0 -> T (ordinal 4 is first eligible)
    # floor(5*0.3)=1, floor(4*0.3)=1 -> F
    # floor(6*0.3)=1, floor(5*0.3)=1 -> F
    # floor(7*0.3)=2, floor(6*0.3)=1 -> T (ordinal 7 is second eligible)
    # floor(8*0.3)=2, floor(7*0.3)=2 -> F
    # floor(9*0.3)=2, floor(8*0.3)=2 -> F
    # floor(10*0.3)=3, floor(9*0.3)=2 -> T (ordinal 10 is third eligible)
    results = [is_ordinal_topic_eligible(n, b) for n in range(1, 11)]
    assert results == [
        False,
        False,
        False,
        True,
        False,
        False,
        True,
        False,
        False,
        True,
    ]


def test_prefix_hard_cap_formula_zero_percent():
    b = 0
    results = [is_ordinal_topic_eligible(n, b) for n in range(1, 100)]
    assert not any(results)


def test_running_rate_update_is_staged_for_next_task_day():
    today = date(2026, 8, 31)
    staged = stage_topic_rate_update(
        {"topic_participation_rate": 0.10},
        0.30,
        task_status="running",
        today=today,
    )

    assert effective_topic_rate(staged, today) == 0.10
    assert effective_topic_rate(staged, date(2026, 9, 1)) == 0.30
    assert staged["topic_participation_rate_effective_date"] == "2026-09-01"


def test_non_running_rate_update_is_immediate():
    staged = stage_topic_rate_update(
        {"topic_participation_rate": 0.10},
        0.20,
        task_status="pending",
        today=date(2026, 8, 31),
    )

    assert staged == {"topic_participation_rate": 0.20}


def test_paused_task_with_current_day_plan_stages_rate_for_next_task_day():
    today = date(2026, 8, 31)
    staged = stage_topic_rate_update(
        {"topic_participation_rate": 0.10},
        0.20,
        task_status="paused",
        today=today,
        has_current_task_day_plan=True,
    )

    assert staged["topic_participation_rate"] == 0.10
    assert staged["topic_participation_rate_next"] == 0.20
    assert staged["topic_participation_rate_effective_date"] == "2026-09-01"


def test_due_pending_rate_promotes_and_clears_pending_fields():
    promoted = promote_due_topic_rate(
        {
            "topic_participation_rate": 0.10,
            "topic_participation_rate_next": 0.30,
            "topic_participation_rate_effective_date": "2026-09-01",
            "_ai_group_content_policy_meta": {
                "topic_participation_rate": {
                    "current_revision": 7,
                    "current_effective_at": "2026-08-31",
                    "next_revision": 8,
                    "next_effective_at": "2026-09-01",
                }
            },
        },
        date(2026, 9, 1),
    )

    assert promoted == {
        "topic_participation_rate": 0.30,
        "_ai_group_content_policy_meta": {
            "topic_participation_rate": {
                "current_revision": 8,
                "current_effective_at": "2026-09-01",
            }
        },
    }


def test_check_remote_topic_capacity_worst_case():
    # b = 3000 (30%)
    # C=10 (10 confirmed normal), T=2 (2 confirmed topic)
    # num = 2 + 1 = 3; den = 10 + 1 = 11. 3*10000=30000 <= 11*3000=33000 -> True
    assert (
        check_remote_topic_capacity(
            confirmed_normal_count=10,
            confirmed_topic_count=2,
            topic_rate_bps=3000,
        )
        is True
    )

    # C=10, T=3
    # num = 3 + 1 = 4; den = 10 + 1 = 11. 4*10000=40000 > 11*3000=33000 -> False
    assert (
        check_remote_topic_capacity(
            confirmed_normal_count=10,
            confirmed_topic_count=3,
            topic_rate_bps=3000,
        )
        is False
    )


def test_decide_topic_mode_human_context_priority():
    decision = decide_topic_mode(
        normal_text_ordinal=4,
        topic_rate_bps=3000,
        has_configured_topics=True,
        has_human_context=True,
        confirmed_normal_count=10,
        confirmed_topic_count=1,
    )
    assert decision.topic_mode == "human_context"
    assert decision.topic_direction is None


def test_decide_topic_mode_configured_topic_success():
    topic = {"title": "身材颜值打听"}
    decision = decide_topic_mode(
        normal_text_ordinal=4,
        topic_rate_bps=3000,
        has_configured_topics=True,
        has_human_context=False,
        confirmed_normal_count=10,
        confirmed_topic_count=1,
        chosen_topic_direction=topic,
    )
    assert decision.topic_mode == "configured_topic"
    assert decision.topic_direction == topic


def test_decide_topic_mode_fallback_when_no_remote_capacity():
    decision = decide_topic_mode(
        normal_text_ordinal=4,
        topic_rate_bps=3000,
        has_configured_topics=True,
        has_human_context=False,
        confirmed_normal_count=10,
        confirmed_topic_count=3,  # capacity full
    )
    assert decision.topic_mode == "group_free_chat"
    assert decision.topic_direction is None


def test_validate_question_streak_and_ratio():
    assert (
        validate_no_consecutive_three_questions(["short_react", "question", "question"])
        is True
    )
    assert (
        validate_no_consecutive_three_questions(["question", "question", "question"])
        is False
    )

    assert (
        validate_question_ratio_in_window(["question"] * 4 + ["short_react"] * 6)
        is True
    )
    assert (
        validate_question_ratio_in_window(["question"] * 5 + ["short_react"] * 5)
        is False
    )


def test_schema_topic_participation_rate_validation():
    from app.schemas.task_center import (
        GroupAIChatConfig,
        GroupAIChatTaskCreate,
        TaskSettingsUpdate,
    )
    from pydantic import ValidationError

    cfg = GroupAIChatConfig(target_group_id=123, topic_participation_rate=0.25)
    assert cfg.topic_participation_rate == 0.25

    up = TaskSettingsUpdate(topic_participation_rate=0.30)
    assert up.topic_participation_rate == 0.30

    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=123, topic_participation_rate=0.35)

    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=123, topic_participation_rate=-0.01)

    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=123, topic_participation_rate="0.30")

    with pytest.raises(ValidationError):
        GroupAIChatConfig(target_group_id=123, topic_participation_rate=0.301)

    with pytest.raises(ValidationError):
        GroupAIChatTaskCreate(name="缺少显式确认", target_group_id=123)

    created = GroupAIChatTaskCreate(
        name="显式确认",
        target_group_id=123,
        topic_participation_rate=0.30,
    )
    assert created.topic_participation_rate == 0.30


def test_task_out_projects_per_field_effective_scope():
    from app.schemas.task_center import TaskOut

    now = datetime(2026, 8, 31, tzinfo=timezone.utc)
    projected = TaskOut(
        id="task-1",
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        status="running",
        priority=3,
        timezone="Asia/Shanghai",
        scheduled_start=None,
        scheduled_end=None,
        max_duration_hours=None,
        next_run_at=None,
        last_error="",
        account_config={},
        pacing_config={},
        failure_policy={},
        type_config={
            "topic_participation_rate": 0.10,
            "topic_participation_rate_next": 0.30,
            "topic_participation_rate_effective_date": "2026-09-01",
            "daily_message_target": 10,
            "_ai_group_content_policy_revision": 12,
            "_ai_group_content_policy_meta": {
                "topic_participation_rate": {
                    "current_revision": 8,
                    "current_effective_at": "2026-08-30T08:00:00+00:00",
                    "next_revision": 12,
                    "next_effective_at": "2026-09-01",
                },
                "topic_directions": {
                    "revision": 11,
                    "effective_at": "2026-08-31T09:00:00+00:00",
                },
                "teacher_targets": {
                    "revision": 10,
                    "effective_at": "2026-08-31T07:00:00+00:00",
                },
            },
        },
        config_revision=7,
        stats={},
        created_at=now,
        updated_at=now,
    )

    assert projected.content_policy_effective_scopes["topic_participation_rate"] == {
        "effective_scope": "next_task_day",
        "effective_revision": 12,
        "effective_at": "2026-09-01",
        "current_value": 0.10,
        "next_value": 0.30,
    }
    assert (
        projected.content_policy_effective_scopes["topic_directions"]["effective_scope"]
        == "new_content_intent"
    )
    assert (
        projected.content_policy_effective_scopes["topic_directions"][
            "effective_revision"
        ]
        == 11
    )
    assert projected.topic_policy_inventory["projected_topic_max_counts"] == {
        "0.00": 0,
        "0.10": 1,
        "0.20": 2,
        "0.30": 3,
    }


@pytest.mark.no_postgres
def test_content_policy_change_uses_independent_revision_without_changing_task_revision():
    from app.models import Task
    from app.services.task_center.continuity_config import (
        increment_revision_for_content_policy_change,
    )

    task = Task(
        id="task-1",
        tenant_id=1,
        name="AI 活群",
        type="group_ai_chat",
        type_config={"topic_directions": [{"title": "新话题", "weight": 1}]},
        config_revision=4,
        task_lifecycle_epoch=9,
    )

    changed = increment_revision_for_content_policy_change(
        task,
        previous_config={"topic_directions": []},
        previous_revision=4,
    )

    assert changed is True
    assert task.config_revision == 4
    assert task.type_config["_ai_group_content_policy_revision"] == 5
    assert task.task_lifecycle_epoch == 9
