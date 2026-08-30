from collections import Counter
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    OperationTarget,
    ReactionFulfillmentObligation,
    Task,
    Tenant,
    TgAccount,
    TgGroupAccount,
)
from app.services._common import _now
from app.schemas.task_center import ChannelLikeConfig
from app.services.task_center.channel_membership import linked_channel_group
from app.services.task_center.executors.channel_like import (
    LikePlanItem,
    _like_source_slot,
    _reaction_plan,
    build_plan as build_channel_like_plan,
)
from app.services.task_center.executors.channel_like_expiration import (
    active_like_messages,
    close_expired_like_obligations,
)
from app.services.task_center.executors.channel_view import build_plan as build_channel_view_plan
from app.services.task_center.fulfillment_takeover import takeover_task

pytestmark = pytest.mark.no_postgres


def test_channel_like_all_available_random_uses_channel_capability_only():
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
            reaction_capability_mode="all",
            available_reactions=["👍", "❤️", "🔥", "👏", "🎉"],
        )
        session.add_all([*accounts, channel])
        session.flush()
        group = linked_channel_group(session, channel, create=True)
        session.flush()
        for account in accounts:
            session.add(
                TgGroupAccount(
                    tenant_id=1,
                    group_id=group.id,
                    account_id=account.id,
                    permission_label="已关注",
                    can_send=True,
                )
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
                "reaction_scope": "all_available",
                "allowed_reactions": ["👍", "❤️", "🔥"],
                "max_likes_per_account_per_hour": 999,
            },
            stats={},
        )
        session.add_all([message, task])
        session.commit()

        assert build_channel_like_plan(session, task) == 10
        actions = session.scalars(
            select(Action).where(Action.task_id == task.id, Action.action_type == "like_message")
        ).all()

    reactions = Counter(action.payload["reaction_emoji"] for action in actions)
    assert sum(reactions.values()) == 10
    assert set(reactions) <= {"👍", "❤️", "🔥", "👏", "🎉"}
    assert len(reactions) >= 2


def test_reaction_plan_preserves_requested_quantity_when_extra_pool_is_smaller():
    reactions = _reaction_plan(
        ["👍", "❤️", "🔥"],
        100,
        available_reactions=["👍", "❤️", "🔥"],
        reaction_capability_mode="some",
    )

    assert len(reactions) == 100


def test_expired_like_obligation_is_closed_without_touching_active_owner():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    now_value = datetime(2026, 8, 30, 12, 0)
    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        task = Task(
            id="expired-like-task",
            tenant_id=1,
            name="过期点赞",
            type="channel_like",
            status="running",
            fulfillment_contract_version="fact_first_v3",
        )
        channel = OperationTarget(
            id=25,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10025",
            title="过期频道",
        )
        message = ChannelMessage(
            id=35,
            tenant_id=1,
            channel_target_id=channel.id,
            message_id=6105,
            created_at=now_value - timedelta(days=2),
        )
        session.add_all([task, channel, message])
        session.flush()
        open_owner = ReactionFulfillmentObligation(
            id="expired-open-owner",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=message.id,
            account_id=101,
            reaction_contract_version=1,
            status="open",
        )
        held_owner = ReactionFulfillmentObligation(
            id="expired-held-owner",
            tenant_id=1,
            task_id=task.id,
            channel_message_id=message.id,
            account_id=102,
            reaction_contract_version=1,
            status="pending",
        )
        session.add_all([open_owner, held_owner])
        session.flush()

        closed = close_expired_like_obligations(session, task, now_value=now_value)
        active = active_like_messages(task, [message], now_value=now_value)

    assert closed == 1
    assert open_owner.status == "closed_expired"
    assert held_owner.status == "pending"
    assert active == []
    assert task.stats["window_expired_settled_count"] == 1


def test_like_source_slot_keeps_extended_frozen_plan_total():
    task = Task(
        id="like-overrun",
        tenant_id=1,
        name="点赞补位",
        type="channel_like",
        created_at=datetime(2026, 8, 30, 8, 0),
    )
    message = ChannelMessage(
        id=41,
        tenant_id=1,
        channel_target_id=21,
        message_id=6141,
        created_at=datetime(2026, 8, 30, 8, 0),
    )
    owner = ReactionFulfillmentObligation(
        id="like-overrun-owner",
        tenant_id=1,
        task_id=task.id,
        channel_message_id=message.id,
        account_id=101,
        reaction_contract_version=1,
        pacing_plan_total=51,
        pacing_slot_ordinal=50,
        pacing_due_at=datetime(2026, 8, 30, 9, 0),
    )
    item = LikePlanItem(message, owner.account_id, "👍", 0, 50)

    slot = _like_source_slot(task, item, owner=owner, source_hash="a" * 64)

    assert slot.slot_ordinal == 50
    assert slot.plan_total == 51


def test_channel_like_clears_account_error_when_targets_are_already_reached():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=101,
            tenant_id=1,
            display_name="点赞号",
            phone_masked="101",
            status=AccountStatus.ACTIVE.value,
            health_score=100,
            session_ciphertext="session-101",
        )
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
        message = ChannelMessage(id=31, tenant_id=1, channel_target_id=21, message_id=6101)
        task = Task(
            id="channel-like-target-reached",
            tenant_id=1,
            name="点赞已达标",
            type="channel_like",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [101], "max_concurrent": 1},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "target_channel_id": channel.id,
                "message_scope": "specific",
                "message_ids": [message.id],
                "target_likes_per_message": 1,
                "like_count_jitter": 0,
                "allowed_reactions": ["👍"],
            },
            last_error="没有可新增的有效点赞账号",
        )
        session.add_all([account, channel])
        session.flush()
        group = linked_channel_group(session, channel, create=True)
        session.flush()
        session.add(
            TgGroupAccount(
                tenant_id=1,
                group_id=group.id,
                account_id=account.id,
                permission_label="已关注",
                can_send=True,
            )
        )
        session.add_all([message, task])
        session.flush()
        session.add(
            Action(
                id="like-existing",
                tenant_id=1,
                task_id=task.id,
                task_type="channel_like",
                action_type="like_message",
                account_id=account.id,
                status="success",
                payload={
                    "channel_id": channel.tg_peer_id,
                    "channel_target_id": channel.id,
                    "channel_message_id": message.id,
                    "message_id": message.message_id,
                    "reaction_emoji": "👍",
                },
            )
        )
        session.commit()
        takeover_task(session, task, now=_now(), write_audit=False)
        session.commit()

        assert build_channel_like_plan(session, task) == 0

    assert task.last_error == ""


def test_channel_view_clears_account_error_when_messages_are_expired():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)

    with Session(engine) as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        account = TgAccount(
            id=201,
            tenant_id=1,
            display_name="浏览号",
            phone_masked="201",
            status=AccountStatus.ACTIVE.value,
            health_score=100,
            session_ciphertext="session-201",
        )
        channel = OperationTarget(
            id=22,
            tenant_id=1,
            target_type="channel",
            tg_peer_id="-10022",
            title="浏览频道",
            username="view_channel",
            can_send=True,
            auth_status="已授权运营",
        )
        session.add_all([account, channel])
        session.flush()
        group = linked_channel_group(session, channel, create=True)
        session.flush()
        session.add(
            TgGroupAccount(
                tenant_id=1,
                group_id=group.id,
                account_id=account.id,
                permission_label="已关注",
                can_send=True,
            )
        )
        message = ChannelMessage(
            id=41,
            tenant_id=1,
            channel_target_id=22,
            message_id=6201,
            published_at=_now() - timedelta(days=2),
        )
        task = Task(
            id="channel-view-expired",
            tenant_id=1,
            name="浏览消息过期",
            type="channel_view",
            status="running",
            account_config={"selection_mode": "manual", "account_ids": [201], "max_concurrent": 1},
            pacing_config={"mode": "fixed", "interval_seconds_min": 0, "interval_seconds_max": 0},
            type_config={
                "target_channel_id": channel.id,
                "message_scope": "specific",
                "message_ids": [message.id],
                "target_views_per_message": 1,
                "message_active_days": 1,
            },
            last_error="没有可新增的有效浏览账号",
        )
        session.add_all([message, task])
        session.commit()

        assert build_channel_view_plan(session, task) == 0

    assert task.last_error == ""


def test_reaction_plan_respects_channel_available_reactions_whitelist():
    # Channel only allows 👍 and ❤️, but user asked for 👍, 🔥, 🎉
    available = ["👍", "❤️"]
    configured = ["👍", "🔥", "🎉"]
    plan = _reaction_plan(
        configured,
        50,
        "random",
        seed_id="whitelist-test",
        available_reactions=available,
        reaction_capability_mode="some",
    )
    assert len(plan) == 50
    # Every reaction in the plan must strictly be in available
    assert set(plan) == {"👍"}
    # No illegal reaction should ever be planned
    assert "🔥" not in plan
    assert "🎉" not in plan


def test_reaction_plan_does_not_substitute_when_configured_not_in_available():
    # User configured 🔥, but channel only allows 👍
    available = ["👍"]
    configured = ["🔥"]
    plan = _reaction_plan(
        configured,
        20,
        "random",
        seed_id="fallback-test",
        available_reactions=available,
        reaction_capability_mode="some",
    )
    assert plan == []


def test_specific_reaction_is_not_replaced_when_unavailable():
    plan = _reaction_plan(
        ["🔥"],
        5,
        "specific",
        available_reactions=["👍"],
        reaction_capability_mode="some",
    )

    assert plan == []


def test_all_available_scope_randomizes_over_full_channel_pool():
    plan = _reaction_plan(
        ["👍"],
        100,
        "random",
        seed_id="all-available",
        reaction_scope="all_available",
        available_reactions=["👍", "❤️", "🔥", "👏", "🎉", "🤩"],
        reaction_capability_mode="all",
    )

    assert len(plan) == 100
    assert set(plan) == {"👍", "❤️", "🔥", "👏", "🎉", "🤩"}


def test_all_available_reaction_counts_have_no_fixed_quota():
    pool = ["👍", "❤️", "🔥", "👏", "🎉", "🤩", "😁", "🤔"]
    first = _reaction_plan(
        ["👍"],
        500,
        seed_id="random-counts:first",
        reaction_scope="all_available",
        available_reactions=pool,
        reaction_capability_mode="all",
    )
    second = _reaction_plan(
        ["👍"],
        500,
        seed_id="random-counts:second",
        reaction_scope="all_available",
        available_reactions=pool,
        reaction_capability_mode="all",
    )

    assert set(first) == set(pool)
    assert set(second) == set(pool)
    assert Counter(first) != Counter(second)


@pytest.mark.parametrize("mode", ["unknown", "none"])
def test_reaction_plan_blocks_when_channel_capability_is_not_actionable(mode):
    plan = _reaction_plan(
        ["👍", "❤️"],
        5,
        "random",
        available_reactions=[],
        reaction_capability_mode=mode,
    )

    assert plan == []


def test_channel_like_config_accepts_all_available_scope_without_runtime_capability_fields():
    config = ChannelLikeConfig(
        target_channel_id=1,
        reaction_scope="all_available",
        allowed_reactions=["👍"],
    )

    assert config.reaction_scope == "all_available"
    assert "available_reactions" not in config.model_dump()


def test_channel_like_config_defaults_to_all_available_reactions():
    config = ChannelLikeConfig(target_channel_id=1)

    assert config.reaction_scope == "all_available"
