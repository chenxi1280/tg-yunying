from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant
from app.schemas.task_center import ChannelCommentTaskConfigUpdate
from app.services._common import _now
from app.services.task_center import dispatcher
from app.services.task_center.payloads import PostCommentPayload
from app.services.task_center.service import update_channel_comment_config


def test_channel_comment_config_update_clears_pending_comment_plan():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        task = Task(
            id="task-comment-update",
            tenant_id=1,
            name="频道评论",
            type="channel_comment",
            status="running",
            type_config={
                "target_channel_id": 6,
                "target_comments_per_message": 80,
                "message_scope": "dynamic_new",
                "message_count": 10,
            },
        )
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=6, tenant_id=1, target_type="channel", tg_peer_id="-1006", title="频道"))
        session.add(task)
        session.add(
            Action(
                id="pending-comment",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_comment",
                action_type="post_comment",
                account_id=11,
                status="pending",
                scheduled_at=now_value,
                payload={"channel_target_id": 6, "channel_message_id": 66, "message_id": 336, "comment_text": "旧计划"},
            )
        )
        session.commit()

        update_channel_comment_config(
            session,
            1,
            task.id,
            ChannelCommentTaskConfigUpdate(
                target_channel_id=6,
                target_comments_per_message=30,
                message_scope="dynamic_new",
                message_count=10,
            ),
            "tester",
        )

        remaining = session.scalars(select(Action).where(Action.task_id == task.id, Action.status == "pending")).all()

    assert remaining == []


def test_channel_comment_config_update_allows_ai_model_switch():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        task = Task(
            id="task-comment-model",
            tenant_id=1,
            name="频道评论",
            type="channel_comment",
            status="running",
            type_config={
                "target_channel_id": 6,
                "target_comments_per_message": 80,
                "message_scope": "dynamic_new",
                "message_count": 10,
            },
        )
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(OperationTarget(id=6, tenant_id=1, target_type="channel", tg_peer_id="-1006", title="频道"))
        session.add(task)
        session.commit()

        updated = update_channel_comment_config(
            session,
            1,
            task.id,
            ChannelCommentTaskConfigUpdate(target_channel_id=6, ai_model="deepseek-v4-flash"),
            "tester",
        )

    assert updated.type_config["ai_model"] == "deepseek-v4-flash"
    assert updated.type_config["target_comments_per_message"] == 80


def test_channel_comment_dispatch_detects_existing_success_limit():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        task = Task(
            id="task-comment-dispatch-cap",
            tenant_id=1,
            name="频道评论",
            type="channel_comment",
            status="running",
            type_config={
                "target_channel_id": 6,
                "target_comments_per_message": 30,
                "comment_count_jitter": 0.3,
                "message_scope": "dynamic_new",
                "message_count": 10,
            },
        )
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(task)
        for index in range(39):
            session.add(
                Action(
                    id=f"success-comment-{index}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="channel_comment",
                    action_type="post_comment",
                    status="success",
                    scheduled_at=now_value,
                    payload={"channel_target_id": 6, "channel_message_id": 66, "message_id": 336},
                )
            )
        current = Action(
            id="pending-comment-over-cap",
            tenant_id=1,
            task_id=task.id,
            task_type="channel_comment",
            action_type="post_comment",
            status="pending",
            scheduled_at=now_value,
            payload={"channel_target_id": 6, "channel_message_id": 66, "message_id": 336},
        )
        session.add(current)
        session.commit()

        payload = PostCommentPayload(
            channel_id="-1006",
            channel_target_id=6,
            channel_message_id=66,
            message_id=336,
            comment_text="不应继续发送",
        )

        assert dispatcher._comment_success_limit_reached(session, current, payload)


def test_channel_comment_dispatch_detects_task_total_limit():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = _now()

    with Session(engine) as session:
        task = Task(
            id="task-comment-total-cap",
            tenant_id=1,
            name="频道评论",
            type="channel_comment",
            status="running",
            type_config={
                "target_channel_id": 6,
                "target_comments_per_message": 30,
                "max_total_comments": 80,
                "max_total_comments_jitter": 0,
                "message_scope": "dynamic_new",
                "message_count": 10,
            },
            stats={},
        )
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add(task)
        for index in range(80):
            session.add(
                Action(
                    id=f"counted-comment-{index}",
                    tenant_id=1,
                    task_id=task.id,
                    task_type="channel_comment",
                    action_type="post_comment",
                    status="success",
                    scheduled_at=now_value,
                    payload={"channel_target_id": 6, "channel_message_id": 66, "message_id": 336},
                )
            )
        current = Action(
            id="pending-comment-total-over-cap",
            tenant_id=1,
            task_id=task.id,
            task_type="channel_comment",
            action_type="post_comment",
            status="pending",
            scheduled_at=now_value,
            payload={"channel_target_id": 6, "channel_message_id": 66, "message_id": 336},
        )
        session.add(current)
        session.commit()

        assert dispatcher._comment_total_limit_reached(session, current)
