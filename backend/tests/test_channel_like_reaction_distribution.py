from collections import Counter

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountStatus, Action, ChannelMessage, OperationTarget, Task, Tenant, TgAccount
from app.services.task_center.executors.channel_like import _reaction_plan, build_plan as build_channel_like_plan


def test_channel_like_random_reactions_prefer_primary_and_mix_extra_emojis():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        accounts = [
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name=f"点赞号{account_id}",
                phone_masked=str(account_id),
                status=AccountStatus.ACTIVE.value,
                health_score=100,
                session_ciphertext=f"session-{account_id}",
            )
            for account_id in range(101, 111)
        ]
        channel = OperationTarget(
            id=21,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10021",
            title="点赞频道",
            username="like_channel",
            can_send=True,
            auth_status="已授权运营",
        )
        message = ChannelMessage(
            id=31,
            tenant_id=1,
            channel_target_id=21,
            message_id=6101,
            message_url="https://t.me/like_channel/6101",
            content_preview="点赞分配测试",
        )
        task = Task(
            id="channel-like-reaction-distribution",
            tenant_id=1,
            name="随机点赞表情",
            type="channel_like",
            status="running",
            account_config={
                "selection_mode": "manual",
                "account_ids": [account.id for account in accounts],
                "max_concurrent": 10,
                "cooldown_per_account_minutes": 0,
            },
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0, "jitter_percent": 0},
            type_config={
                "target_channel_id": channel.id,
                "message_scope": "specific",
                "message_ids": [message.id],
                "target_likes_per_message": 10,
                "like_count_jitter": 0,
                "reaction_type": "random",
                "allowed_reactions": ["👍", "❤️", "🔥"],
                "max_likes_per_account_per_hour": 999,
            },
            stats={},
        )
        session.add_all([*accounts, channel, message, task])
        session.commit()

        assert build_channel_like_plan(session, task) == 10
        actions = session.scalars(select(Action).where(Action.task_id == task.id)).all()

    reactions = Counter(action.payload["reaction_emoji"] for action in actions)
    configured_reactions = {"👍", "❤️", "🔥"}

    assert reactions["👍"] == 7
    assert reactions["❤️"] >= 1
    assert reactions["🔥"] >= 1
    assert sum(reactions.values()) == 10
    assert any(reaction not in configured_reactions for reaction in reactions)


def test_reaction_plan_preserves_requested_quantity_when_extra_pool_is_smaller():
    reactions = _reaction_plan(["👍", "❤️", "🔥"], 100)

    assert len(reactions) == 100
