from __future__ import annotations

from datetime import timedelta

from sqlalchemy import create_engine
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
    TgGroup,
    TgGroupAccount,
)
from app.services._common import _now


def comment_dispatch_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def seed_dispatch_scope(session: Session, *, reply: bool = False) -> Action:
    session.add(Tenant(id=1, name="评论 Dispatcher 测试"))
    _seed_channel_and_group(session)
    _seed_account(session)
    task = _comment_task()
    session.add(task)
    session.flush()
    action = _comment_action(task, reply=reply)
    session.add(action)
    session.commit()
    return action


def _seed_channel_and_group(session: Session) -> None:
    session.add(OperationTarget(
        id=31,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-10031",
        title="测试频道",
        can_send=True,
        auth_status="已授权运营",
    ))
    session.add(TgGroup(
        id=71,
        tenant_id=1,
        tg_peer_id="-10031",
        title="测试频道讨论组",
        auth_status="已授权运营",
    ))
    session.add(ChannelMessage(
        id=41,
        tenant_id=1,
        channel_target_id=31,
        message_id=9001,
        content_preview="频道消息正文",
        comment_available=True,
    ))
    session.add(ChannelMessageComment(
        id=51,
        tenant_id=1,
        channel_target_id=31,
        channel_message_id=41,
        comment_message_id=8101,
        author_name="读者 A",
        content_preview="这个尺寸是多少",
    ))


def _seed_account(session: Session) -> None:
    session.add(TgAccount(
        id=101,
        tenant_id=1,
        display_name="评论账号",
        username="comment_101",
        tg_first_name="评论号",
        avatar_object_key="avatars/101.jpg",
        profile_sync_status="已同步",
        phone_masked="101",
        status=AccountStatus.ACTIVE.value,
        health_score=100,
        session_ciphertext="session-101",
    ))
    session.add(TgGroupAccount(
        tenant_id=1,
        group_id=71,
        account_id=101,
        can_send=True,
    ))


def _comment_task() -> Task:
    return Task(
        id="comment-dispatch-task",
        tenant_id=1,
        name="评论 Dispatcher",
        type="channel_comment",
        status="running",
        account_config={"selection_mode": "all", "max_concurrent": 1},
        pacing_config={"mode": "fixed", "max_actions_per_hour": 10},
        type_config={
            "target_channel_id": 31,
            "target_comments_per_message": 2,
            "max_total_comments": 10,
            "max_total_comments_jitter": 0,
            "context_bound_schedule_window_seconds": 300,
        },
        stats={},
    )


def _comment_action(task: Task, *, reply: bool) -> Action:
    reply_payload = {
        "comment_mode": "reply",
        "reply_to_message_id": 8101,
        "reply_target_author": "读者 A",
        "reply_target_preview": "这个尺寸是多少",
        "reply_target_source": "persisted",
    } if reply else {"comment_mode": "comment"}
    return Action(
        id="comment-dispatch-action",
        tenant_id=1,
        task_id=task.id,
        task_type="channel_comment",
        action_type="post_comment",
        account_id=101,
        status="executing",
        scheduled_at=_now(),
        lease_owner="dispatcher-1",
        lease_expires_at=_now() + timedelta(minutes=30),
        payload={
            "channel_id": "-10031",
            "channel_target_id": 31,
            "channel_message_id": 41,
            "message_id": 9001,
            "target_display": "测试频道",
            "message_content": "频道消息正文",
            "comment_text": "",
            "slot_id": "channel-comment:41:0",
            "ai_generation_id": f"{task.id}:channel-comment:41:0",
            "ai_generation_status": "pending",
            "ai_generation_attempt_id": "",
            "ai_generation_request_id": "",
            "ai_generation_claim_owner": "dispatcher-1",
            "ai_generation_claim_token": "claim-1",
            "ai_generation_attempt_history": [],
            **reply_payload,
        },
    )


def expire_comment_action(action: Action) -> None:
    action.created_at = _now() - timedelta(minutes=10)


__all__ = ["comment_dispatch_session", "expire_comment_action", "seed_dispatch_scope"]
