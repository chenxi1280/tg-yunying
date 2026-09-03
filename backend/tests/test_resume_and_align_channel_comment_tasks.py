from __future__ import annotations

import pytest
from app.models import Task
from scripts.resume_and_align_channel_comment_tasks import (
    build_desired_config,
    plan_task_action,
    RETIRED_TASK_IDS,
)

pytestmark = pytest.mark.no_postgres


def test_build_desired_config_detects_differences() -> None:
    task = Task(
        id="task-1",
        type="channel_comment",
        type_config={
            "target_comments_per_message": 10,
            "reply_min_per_message": 2,
            "comment_mode": "comment",
            "message_scope": "all",
        },
    )
    changes = build_desired_config(
        task,
        target_comments=20,
        reply_min=5,
        comment_mode="mixed",
        message_scope="dynamic_new",
    )
    assert changes == {
        "target_comments_per_message": 20,
        "reply_min_per_message": 5,
        "comment_mode": "mixed",
        "message_scope": "dynamic_new",
    }


def test_build_desired_config_empty_when_already_matching() -> None:
    task = Task(
        id="task-1",
        type="channel_comment",
        type_config={
            "target_comments_per_message": 20,
            "reply_min_per_message": 5,
            "comment_mode": "mixed",
            "message_scope": "dynamic_new",
        },
    )
    changes = build_desired_config(
        task,
        target_comments=20,
        reply_min=5,
        comment_mode="mixed",
        message_scope="dynamic_new",
    )
    assert changes == {}


def test_plan_task_action_resumes_paused_active_task() -> None:
    task = Task(
        id="64f009db-7212-4402-8665-cd4ea8817572",
        name="郑州楼凤",
        status="paused",
        type="channel_comment",
        type_config={
            "target_comments_per_message": 10,
            "reply_min_per_message": 2,
            "comment_mode": "comment",
            "message_scope": "dynamic_new",
        },
    )
    plan = plan_task_action(
        task,
        target_comments=20,
        reply_min=5,
        comment_mode="mixed",
        message_scope="dynamic_new",
    )
    assert plan["should_resume"] is True
    assert plan["target_status"] == "running"
    assert plan["config_changes"] == {
        "target_comments_per_message": 20,
        "reply_min_per_message": 5,
        "comment_mode": "mixed",
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
        target_comments=20,
        reply_min=5,
        comment_mode="mixed",
        message_scope="dynamic_new",
    )
    assert plan["is_retired"] is True
    assert plan["should_resume"] is False
    assert plan["target_status"] == "paused"
