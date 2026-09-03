from __future__ import annotations

import pytest
from app.models import Task
from scripts.resume_and_align_channel_comment_tasks import (
    build_desired_account_config,
    build_desired_config,
    build_desired_pacing,
    plan_task_action,
    RETIRED_TASK_IDS,
)

pytestmark = pytest.mark.no_postgres


def test_build_desired_config_detects_differences() -> None:
    task = Task(
        id="task-1",
        type="channel_comment",
        type_config={
            "target_comments_per_message": 20,
            "reply_min_per_message": 5,
            "comment_mode": "comment",
            "message_scope": "all",
            "rolling_window_days": 1,
            "allow_returning_accounts": False,
        },
    )
    changes = build_desired_config(
        task,
        target_comments=100,
        reply_min=20,
        comment_mode="mixed",
        message_scope="dynamic_new",
        rolling_window_days=3,
        allow_returning_accounts=True,
    )
    assert changes == {
        "target_comments_per_message": 100,
        "reply_min_per_message": 20,
        "comment_mode": "mixed",
        "message_scope": "dynamic_new",
        "rolling_window_days": 3,
        "allow_returning_accounts": True,
    }


def test_build_desired_pacing_detects_differences() -> None:
    task = Task(
        id="task-1",
        type="channel_comment",
        pacing_config={},
    )
    changes = build_desired_pacing(
        task,
        rolling_window_days=3,
        multi_day_rampup=True,
    )
    assert changes == {
        "rolling_window_days": 3,
        "multi_day_rampup": True,
    }


def test_build_desired_account_config_switches_to_all() -> None:
    task = Task(
        id="task-1",
        type="channel_comment",
        account_config={"selection_mode": "manual", "account_ids": [101, 102], "max_concurrent": 20},
    )
    changes = build_desired_account_config(task, all_accounts=True)
    assert changes == {"selection_mode": "all"}


def test_plan_task_action_resumes_paused_active_task() -> None:
    task = Task(
        id="64f009db-7212-4402-8665-cd4ea8817572",
        name="郑州楼凤",
        status="paused",
        type="channel_comment",
        type_config={
            "target_comments_per_message": 20,
            "reply_min_per_message": 5,
            "comment_mode": "mixed",
            "message_scope": "dynamic_new",
        },
        pacing_config={},
        account_config={"selection_mode": "manual", "max_concurrent": 20},
    )
    plan = plan_task_action(
        task,
        target_comments=100,
        reply_min=20,
        comment_mode="mixed",
        message_scope="dynamic_new",
        rolling_window_days=3,
        multi_day_rampup=True,
        allow_returning_accounts=True,
        all_accounts=True,
    )
    assert plan["should_resume"] is True
    assert plan["target_status"] == "running"
    assert plan["config_changes"] == {
        "target_comments_per_message": 100,
        "reply_min_per_message": 20,
        "rolling_window_days": 3,
        "allow_returning_accounts": True,
    }
    assert plan["pacing_changes"] == {
        "rolling_window_days": 3,
        "multi_day_rampup": True,
    }
    assert plan["account_changes"] == {
        "selection_mode": "all",
    }


def test_plan_task_action_does_not_resume_retired_task() -> None:
    retired_id = next(iter(RETIRED_TASK_IDS))
    task = Task(
        id=retired_id,
        name="成都阿楠旧版",
        status="paused",
        type="channel_comment",
        type_config={"target_comments_per_message": 10},
    )
    plan = plan_task_action(
        task,
        target_comments=100,
        reply_min=20,
        comment_mode="mixed",
        message_scope="dynamic_new",
        rolling_window_days=3,
        multi_day_rampup=True,
        allow_returning_accounts=True,
        all_accounts=True,
    )
    assert plan["is_retired"] is True
    assert plan["should_resume"] is False
    assert plan["target_status"] == "paused"
