from datetime import datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    ChannelMessageComment,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
)
from app.services.task_center import payloads as task_payloads
from app.services.task_center.executors import channel_comment
from app.services.task_center.payloads import PostCommentPayload


pytestmark = pytest.mark.no_postgres


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed_comment_task(
    session: Session,
    *,
    mode: str,
    reply_min: int = 0,
    requested_reply_ids: list[int] | None = None,
    target_count: int = 2,
) -> Task:
    session.add(Tenant(id=1, name="测试租户"))
    session.add(
        OperationTarget(
            id=31,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10031",
            title="测试频道",
            can_send=True,
            auth_status="已授权运营",
        )
    )
    session.add(
        ChannelMessage(
            id=41,
            tenant_id=1,
            channel_target_id=31,
            message_id=9001,
            content_preview="频道消息正文",
            comment_available=True,
        )
    )
    for index, reply_id in enumerate((8101, 8102), 1):
        session.add(
            ChannelMessageComment(
                tenant_id=1,
                channel_target_id=31,
                channel_message_id=41,
                comment_message_id=reply_id,
                author_name=f"读者 {index}",
                content_preview=f"读者评论 {index}",
            )
        )
    for account_id in (101, 102, 103):
        session.add(
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"账号 {account_id}",
                username=f"comment_{account_id}",
                tg_first_name=f"评论号{account_id}",
                avatar_object_key=f"avatars/{account_id}.jpg",
                profile_sync_status="已同步",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100,
                session_ciphertext=f"session-{account_id}",
            )
        )
    task = Task(
        id=f"comment-phase-a-{mode}",
        tenant_id=1,
        name=f"{mode} 评论",
        type="channel_comment",
        status="running",
        account_config={
            "selection_mode": "all",
            "max_concurrent": 3,
            "cooldown_per_account_minutes": 0,
        },
        pacing_config={
            "mode": "fixed",
            "max_actions_per_hour": 10,
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        },
        type_config={
            "target_channel_id": 31,
            "message_scope": "specific",
            "message_ids": [41],
            "target_comments_per_message": target_count,
            "comment_count_jitter": 0,
            "max_total_comments": 20,
            "max_total_comments_jitter": 0,
            "max_comments_per_account_per_hour": 500,
            "comment_mode": mode,
            "reply_min_per_message": reply_min,
            "reply_to_message_ids": requested_reply_ids or [],
        },
        stats={},
    )
    session.add(task)
    session.commit()
    return task


def _forbid_planner_generation(monkeypatch) -> None:
    def fail(*_args, **_kwargs):
        pytest.fail("channel comment Planner must not call AI generation")

    monkeypatch.setattr(channel_comment, "generate_channel_comments", fail, raising=False)
    monkeypatch.setattr(channel_comment, "generate_channel_reply_comments", fail, raising=False)


def _fixed_profile(monkeypatch) -> None:
    monkeypatch.setattr(
        channel_comment,
        "tenant_learning_profile_preview",
        lambda *_args: {
            "profile_scene": "channel_comment",
            "profile_version": 7,
            "profile_hit_summary": "读者偏好追问具体细节",
            "profile_unavailable_reason": "",
        },
    )
    monkeypatch.setattr(channel_comment, "audit_learning_profile_use", lambda *_args: None)


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
    _forbid_planner_generation(monkeypatch)
    _fixed_profile(monkeypatch)
    with _session() as session:
        task = _seed_comment_task(
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
    _forbid_planner_generation(monkeypatch)
    _fixed_profile(monkeypatch)
    with _session() as session:
        task = _seed_comment_task(session, mode="comment")

        first_created = channel_comment.build_plan(session, task)
        session.commit()
        second_created = channel_comment.build_plan(session, task)
        count = session.scalar(select(func.count(Action.id)).where(Action.task_id == task.id))

    assert first_created == 2
    assert second_created == 0
    assert count == 2


def test_reply_shortfall_does_not_degrade_to_direct_comments(monkeypatch):
    _forbid_planner_generation(monkeypatch)
    _fixed_profile(monkeypatch)
    with _session() as session:
        task = _seed_comment_task(session, mode="mixed", reply_min=3, target_count=3)
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
    _forbid_planner_generation(monkeypatch)
    _fixed_profile(monkeypatch)
    with _session() as session:
        task = _seed_comment_task(session, mode="comment", target_count=1)
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
