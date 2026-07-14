from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from app.models import Action, ChannelMessageComment, Task
from app.services.task_center import payloads as task_payloads
from app.services.task_center.executors import channel_comment
from app.services.task_center.payloads import PostCommentPayload
from channel_comment_planner_test_support import (
    fixed_profile,
    forbid_planner_external_boundaries,
    planner_session,
    seed_comment_task,
)


pytestmark = pytest.mark.no_postgres


@pytest.mark.parametrize(
    ("mode", "reply_min", "requested_ids", "expected_reply_ids"),
    [
        ("comment", 0, [], []),
        ("mixed", 1, [], [8101]),
        ("reply", 0, [8101, 8102], [8101, 8102]),
    ],
)
def test_planner_creates_stable_pending_comment_blueprints_without_ai(
    monkeypatch,
    mode,
    reply_min,
    requested_ids,
    expected_reply_ids,
):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(
            session,
            mode=mode,
            reply_min=reply_min,
            requested_reply_ids=requested_ids,
        )

        created = channel_comment.build_plan(session, task)
        actions = sorted(
            session.scalars(select(Action).where(Action.task_id == task.id)).all(),
            key=lambda action: action.payload["slot_id"],
        )

    assert created == 2
    assert len(actions) == 2
    assert all(action.status == "pending" for action in actions)
    assert all(action.account_id in {101, 102, 103} for action in actions)
    assert all(isinstance(action.scheduled_at, datetime) for action in actions)
    assert all(action.payload["channel_message_id"] == 41 for action in actions)
    assert all(action.payload["message_id"] == 9001 for action in actions)
    assert all(action.payload["comment_text"] == "" for action in actions)
    assert all(action.payload["ai_generation_status"] == "pending" for action in actions)
    assert all(action.payload["ai_generation_attempt_id"] == "" for action in actions)
    assert all(action.payload["ai_generation_request_id"] == "" for action in actions)
    assert all(action.payload["ai_generation_claim_owner"] == "" for action in actions)
    assert all(action.payload["ai_generation_claim_token"] == "" for action in actions)
    assert all(action.payload["ai_generation_attempt_history"] == [] for action in actions)
    assert all(action.payload["profile_scene"] == "channel_comment" for action in actions)
    assert all(action.payload["profile_version"] == 7 for action in actions)
    assert all(action.payload["profile_hit_summary"] == "读者偏好追问具体细节" for action in actions)
    assert all(action.payload["rule_set_version_id"] == task.type_config["rule_set_version_id"] for action in actions)
    assert all(action.payload["resolved_rule_set_version_id"] == task.type_config["rule_set_version_id"] for action in actions)
    assert [action.payload["reply_to_message_id"] for action in actions if action.payload["reply_to_message_id"]] == expected_reply_ids
    assert [action.payload["comment_mode"] for action in actions].count("reply") == len(expected_reply_ids)


@pytest.mark.parametrize("status", ["pending", "generating", "ai_result_persist_unknown"])
def test_post_comment_payload_allows_empty_text_only_before_generation_is_ready(status):
    payload = PostCommentPayload(
        channel_id="-10031",
        channel_message_id=41,
        message_id=9001,
        comment_text="",
        ai_generation_status=status,
    )

    assert payload.comment_text == ""


def test_post_comment_payload_rejects_empty_ready_text():
    with pytest.raises(ValidationError, match="comment_text"):
        PostCommentPayload(
            channel_id="-10031",
            channel_message_id=41,
            message_id=9001,
            comment_text="",
            ai_generation_status="ready",
        )


def test_post_comment_dedupe_ignores_generated_text_and_generation_audit():
    task = Task(id="comment-dedupe", tenant_id=1, stats={})
    payload = {
        "channel_target_id": 31,
        "channel_message_id": 41,
        "message_id": 9001,
        "comment_mode": "reply",
        "reply_to_message_id": 8101,
        "slot_id": "channel-comment:41:0",
        "comment_text": "",
        "ai_generation_status": "pending",
        "ai_generation_attempt_id": "",
        "ai_generation_request_id": "",
        "ai_generation_claim_owner": "",
        "ai_generation_claim_token": "",
        "ai_generation_attempt_history": [],
    }
    generated = {
        **payload,
        "comment_text": "生成后的评论",
        "ai_generation_status": "ready",
        "ai_generation_attempt_id": "attempt-1",
        "ai_generation_request_id": "request-1",
        "ai_generation_claim_owner": "dispatcher-1",
        "ai_generation_claim_token": "claim-1",
        "ai_generation_attempt_history": [{"outcome": "success"}],
    }

    first_key = task_payloads._action_dedupe_key(task, "batch-a", "post_comment", 101, payload)

    assert task_payloads._action_dedupe_key(task, "batch-b", "post_comment", 101, generated) == first_key
    assert task_payloads._action_dedupe_key(
        task,
        "batch-b",
        "post_comment",
        101,
        {**generated, "reply_to_message_id": 8102},
    ) != first_key
    assert task_payloads._action_dedupe_key(
        task,
        "batch-b",
        "post_comment",
        101,
        {**generated, "slot_id": "channel-comment:41:1"},
    ) != first_key


def test_two_planner_runs_do_not_duplicate_pending_comment_blueprints(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment")

        first_created = channel_comment.build_plan(session, task)
        session.commit()
        second_created = channel_comment.build_plan(session, task)
        count = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert first_created == 2
    assert second_created == 0
    assert count == 2


def test_reply_shortfall_does_not_degrade_to_direct_comments(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="mixed", reply_min=3, target_count=3)
        session.delete(
            session.scalar(
                select(ChannelMessageComment).where(ChannelMessageComment.comment_message_id == 8102)
            )
        )
        session.flush()

        created = channel_comment.build_plan(session, task)
        actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    assert created == 0
    assert actions == []
    assert "可引用评论不足" in task.last_error


def test_lifetime_cap_completion_is_not_revived_by_planner(monkeypatch):
    forbid_planner_external_boundaries(monkeypatch)
    fixed_profile(monkeypatch)
    with planner_session() as session:
        task = seed_comment_task(session, mode="comment", target_count=1)
        task.type_config = {
            **task.type_config,
            "max_total_comments": 1,
            "max_total_comments_jitter": 0,
        }
        task.status = "completed"
        task.stats = {
            "max_total_comments_resolved": 1,
            "completion_reason": "lifetime_cap_reached",
            "completed_at": "2026-07-14T10:00:00",
        }
        session.add(
            Action(
                id="completed-comment",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                account_id=101,
                status="success",
                payload={"channel_message_id": 41, "message_id": 9001, "comment_text": "已完成"},
            )
        )
        session.commit()

        created = channel_comment.build_plan(session, task)

    assert created == 0
    assert task.status == "completed"
    assert task.stats["completion_reason"] == "lifetime_cap_reached"
    assert task.stats["completed_at"] == "2026-07-14T10:00:00"
