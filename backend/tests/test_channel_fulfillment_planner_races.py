from __future__ import annotations

import os
from datetime import timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, ChannelMessage, OperationTarget, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center import channel_fulfillment, channel_view_targets
from app.services.task_center.executors import channel_like, channel_view
from app.services.task_center.pacing import next_local_day_deadline


@pytest.fixture
def session() -> Session:
    engine = create_engine(os.environ["TEST_DATABASE_URL"], future=True)
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with Session(engine) as current:
        current.add(Tenant(id=1, name="单用户"))
        current.commit()
        yield current


def test_view_planner_skips_source_confirmed_after_candidate_snapshot(
    session: Session,
    monkeypatch,
) -> None:
    task, channel, message = _scope(
        session,
        task_id="view-race-task",
        task_type="channel_view",
        type_config={
            "target_channel_id": 901,
            "message_scope": "specific",
            "message_ids": [902],
            "per_message_daily_view_target": 2,
            "per_message_total_view_target": 2,
            "view_count_jitter": 0,
        },
    )
    original = channel_view.ensure_view_obligation

    def confirm_during_bind(current, ledger, current_message, account_id):
        obligation = original(current, ledger, current_message, account_id)
        if account_id == 31:
            channel_fulfillment.confirm_view_obligation(
                current,
                obligation,
                target_peer_id=channel.tg_peer_id,
                confirmed_at=_now(),
            )
        return obligation

    monkeypatch.setattr(
        channel_view,
        "ensure_view_obligation",
        confirm_during_bind,
    )

    assert channel_view.build_plan(session, task) == 1
    actions = _main_actions(session, task.id, "view_message")
    assert [action.account_id for action in actions] == [32]
    assert actions[0].payload["channel_message_id"] == message.id


def test_like_planner_skips_source_confirmed_after_candidate_snapshot(
    session: Session,
    monkeypatch,
) -> None:
    task, channel, message = _scope(
        session,
        task_id="like-race-task",
        task_type="channel_like",
        type_config={
            "target_channel_id": 901,
            "message_scope": "specific",
            "message_ids": [902],
            "target_likes_per_message": 2,
            "like_count_jitter": 0,
            "allowed_reactions": ["👍"],
            "reaction_type": "specific",
        },
    )
    original = channel_like.ensure_reaction_obligation

    def confirm_during_bind(current, current_task, current_message, account_id):
        obligation = original(
            current,
            current_task,
            current_message,
            account_id,
        )
        if account_id == 31:
            channel_fulfillment.confirm_reaction_obligation(
                current,
                obligation,
                target_peer_id=channel.tg_peer_id,
                reaction_emoji="👍",
                confirmed_at=_now(),
            )
        return obligation

    monkeypatch.setattr(
        channel_like,
        "ensure_reaction_obligation",
        confirm_during_bind,
    )

    assert channel_like.build_plan(session, task) == 1
    actions = _main_actions(session, task.id, "like_message")
    assert [action.account_id for action in actions] == [32]
    assert actions[0].payload["channel_message_id"] == message.id


def test_view_planner_does_not_append_after_latest_future_action(
    session: Session,
    monkeypatch,
) -> None:
    task, _channel, _message = _scope(
        session,
        task_id="view-future-tail-task",
        task_type="channel_view",
        type_config={
            "target_channel_id": 901,
            "message_scope": "specific",
            "message_ids": [902],
            "per_message_daily_view_target": 2,
            "per_message_total_view_target": 2,
            "view_count_jitter": 0,
        },
    )
    now_value = _now()
    task.pacing_config = {"mode": "template", "template": "moderate_6h"}
    session.add(Action(
        id="future-tail-action",
        tenant_id=1,
        task_id=task.id,
        task_type=task.type,
        action_type="view_message",
        account_id=31,
        status="pending",
        scheduled_at=next_local_day_deadline(now_value, task.timezone) - timedelta(minutes=1),
        payload={"channel_message_id": 999999},
    ))
    session.commit()
    monkeypatch.setattr(
        channel_view_targets,
        "cumulative_pacing_due",
        lambda target, *_args, **_kwargs: target,
    )

    assert channel_view.build_plan(session, task) == 2
    created = [
        action for action in _main_actions(session, task.id, "view_message")
        if action.id != "future-tail-action"
    ]
    assert len(created) == 2
    deadline = next_local_day_deadline(now_value, task.timezone)
    assert all(
        action.scheduled_at.timestamp() < deadline.timestamp()
        for action in created
    )


def _scope(
    session: Session,
    *,
    task_id: str,
    task_type: str,
    type_config: dict,
) -> tuple[Task, OperationTarget, ChannelMessage]:
    now_value = _now()
    session.add_all([
        TgAccount(
            id=31,
            tenant_id=1,
            display_name="账号31",
            phone_masked="31",
            status="在线",
            session_ciphertext="s31",
        ),
        TgAccount(
            id=32,
            tenant_id=1,
            display_name="账号32",
            phone_masked="32",
            status="在线",
            session_ciphertext="s32",
        ),
    ])
    channel = OperationTarget(
        id=901,
        tenant_id=1,
        target_type="channel",
        tg_peer_id="-100901",
        title="履约竞态频道",
        username="fulfillment_race",
        auth_status="已授权运营",
        can_send=True,
    )
    message = ChannelMessage(
        id=902,
        tenant_id=1,
        channel_target_id=channel.id,
        message_id=1902,
        content_preview="履约竞态消息",
        published_at=now_value,
    )
    task = Task(
        id=task_id,
        tenant_id=1,
        name=task_id,
        type=task_type,
        status="running",
        next_run_at=now_value,
        account_config={
            "selection_mode": "manual",
            "account_ids": [31, 32],
            "max_concurrent": 2,
            "cooldown_per_account_minutes": 0,
        },
        pacing_config={
            "mode": "fixed",
            "interval_seconds_min": 0,
            "interval_seconds_max": 0,
            "jitter_percent": 0,
        },
        failure_policy={"max_retries": 0},
        type_config=type_config,
        stats={},
    )
    session.add(channel)
    session.flush()
    session.add_all([message, task])
    session.commit()
    return task, channel, message


def _main_actions(
    session: Session,
    task_id: str,
    action_type: str,
) -> list[Action]:
    return list(
        session.query(Action)
        .filter(
            Action.task_id == task_id,
            Action.action_type == action_type,
        )
        .order_by(Action.account_id)
    )
