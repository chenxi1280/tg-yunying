from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, OperationTarget, Task, Tenant
from app.schemas.task_center import ChannelCommentTaskConfigUpdate
from app.services._common import _now
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
