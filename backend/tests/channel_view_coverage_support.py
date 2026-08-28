from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    Action,
    ChannelMessage,
    OperationTarget,
    Task,
    Tenant,
    TgAccount,
    TgGroupAccount,
    ViewRemoteFact,
)
from app.services.task_center.channel_fulfillment import (
    confirm_view_action,
    ensure_view_action_contract,
)
from app.services.task_center.channel_membership import linked_channel_group
from app.services.task_center.payloads import ViewMessagePayload


@dataclass(frozen=True)
class ChannelScenario:
    session: Session
    channel: OperationTarget
    accounts: list[TgAccount]


def new_session() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name="默认运营空间"))
    session.flush()
    return session


def seed_channel_scenario(
    session: Session,
    *,
    channel_id: int,
    account_count: int,
    linked: bool = True,
) -> ChannelScenario:
    channel = OperationTarget(
        id=channel_id,
        tenant_id=1,
        target_type="channel",
        tg_peer_id=f"-100{channel_id:010d}",
        title=f"测试频道{channel_id}",
        username=f"channel_{channel_id}",
        can_send=True,
        auth_status="已授权运营",
        reaction_capability_mode="all",
        available_reactions=["👍", "❤️", "🔥"],
    )
    accounts = [_account(account_id) for account_id in range(1, account_count + 1)]
    session.add_all([channel, *accounts])
    session.flush()
    if linked:
        link_accounts(session, channel=channel, accounts=accounts)
    session.commit()
    return ChannelScenario(session, channel, accounts)


def _account(account_id: int) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=1,
        display_name=f"运营号{account_id}",
        phone_masked=str(account_id),
        status=AccountStatus.ACTIVE.value,
        health_score=100,
        session_ciphertext=f"session-{account_id}",
    )


def link_accounts(
    session: Session,
    *,
    channel: OperationTarget,
    accounts: list[TgAccount],
) -> None:
    group = linked_channel_group(session, channel, create=True)
    session.flush()
    session.add_all(
        [
            TgGroupAccount(
                tenant_id=1,
                group_id=group.id,
                account_id=account.id,
                permission_label="已关注",
                can_send=True,
            )
            for account in accounts
        ]
    )
    session.commit()


def add_message(
    session: Session,
    *,
    channel: OperationTarget,
    message_id: int,
    published_at: datetime,
) -> ChannelMessage:
    message = ChannelMessage(
        id=message_id,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=message_id * 100,
        message_url=f"https://t.me/{channel.username}/{message_id * 100}",
        content_preview=f"帖子{message_id}",
        published_at=published_at,
        created_at=published_at,
    )
    session.add(message)
    session.commit()
    return message


def add_view_task(
    session: Session,
    *,
    channel: OperationTarget,
    messages: list[ChannelMessage],
    task_id: str,
    daily_target: int,
    total_target: int,
    created_at: datetime | None = None,
) -> Task:
    task = Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type="channel_view",
        status="running",
        timezone="Asia/Shanghai",
        account_config={"selection_mode": "all"},
        pacing_config=_fixed_pacing(),
        type_config={
            "target_channel_id": channel.id,
            "message_scope": "specific",
            "message_ids": [message.id for message in messages],
            "per_message_daily_view_target": daily_target,
            "per_message_total_view_target": total_target,
            "message_active_days": 7,
            "account_coverage_mode": "all_accounts_daily",
        },
    )
    if created_at is not None:
        task.created_at = created_at
    session.add(task)
    session.commit()
    return task


def add_like_task(
    session: Session,
    *,
    channel: OperationTarget,
    message: ChannelMessage,
    target: int,
) -> Task:
    task = Task(
        id="task-like-membership",
        tenant_id=1,
        name="频道点赞任务",
        type="channel_like",
        status="running",
        account_config={"selection_mode": "all"},
        pacing_config=_fixed_pacing(),
        type_config={
            "target_channel_id": channel.id,
            "message_scope": "specific",
            "message_ids": [message.id],
            "target_likes_per_message": target,
            "allowed_reactions": ["👍"],
        },
    )
    session.add(task)
    session.commit()
    return task


def _fixed_pacing() -> dict:
    return {
        "mode": "fixed",
        "interval_seconds_min": 0,
        "interval_seconds_max": 0,
        "jitter_percent": 0,
    }


def view_actions(session: Session, task: Task) -> list[Action]:
    return list(
        session.scalars(
            select(Action)
            .where(
                Action.task_id == task.id,
                Action.action_type == "view_message",
            )
            .order_by(Action.pacing_slot_ordinal.asc(), Action.id.asc())
        )
    )


def confirm_actions(
    scenario: ChannelScenario,
    *,
    actions: list[Action],
    confirmed_at: datetime,
) -> None:
    for action in actions:
        payload = ViewMessagePayload(**(action.payload or {}))
        obligation = ensure_view_action_contract(
            scenario.session,
            action,
            payload,
            now=confirmed_at,
        )
        confirm_view_action(
            scenario.session,
            obligation.id,
            action.id,
            target_peer_id=scenario.channel.tg_peer_id,
            confirmed_at=confirmed_at,
        )
        action.status = "success"
    scenario.session.commit()


def add_lifetime_fact(
    scenario: ChannelScenario,
    *,
    message: ChannelMessage,
    account: TgAccount,
    confirmed_at: datetime,
) -> None:
    scenario.session.add(
        ViewRemoteFact(
            tenant_id=1,
            obligation_id=f"legacy-{message.id}-{account.id}",
            obligation_local_date=confirmed_at.date(),
            target_peer_id=scenario.channel.tg_peer_id,
            channel_message_id=message.id,
            account_id=account.id,
            remote_confirmed_at=confirmed_at,
        )
    )
    scenario.session.commit()
