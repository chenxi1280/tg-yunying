from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import (
    AccountStatus,
    ChannelMessage,
    ListenerChannelSnapshotItem,
    ListenerSourceState,
    OperationTarget,
    Task,
    TaskPlannerWakeState,
    TaskSourceSubscription,
    Tenant,
    TgAccount,
)
from app.services.task_center import channel_listener_runtime
from app.services.task_center.executors import common as executor_common
from app.services.task_center.channel_listener_runtime import (
    channel_snapshot_state,
    drain_channel_listener_runtime,
)


pytestmark = pytest.mark.no_postgres
NOW = datetime(2026, 8, 17, 12, 0)


def test_listener_owns_channel_fetch_and_publishes_fresh_snapshot(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_listener_task(
        engine,
        task_id="channel-like-task",
        task_type="channel_like",
        channel_id=31,
        account_id=101,
        username="channel_test",
    )

    calls: list[tuple] = []
    _stub_listener_fetch(monkeypatch, calls)

    result = drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    assert result.source_count == 1
    assert result.processed_count == 1
    assert len(calls) == 1
    with Session(engine) as session:
        task = session.get(Task, "channel-like-task")
        channel = session.get(OperationTarget, 31)
        message = session.scalar(select(ChannelMessage))
        state = session.scalar(select(ListenerSourceState))
        subscription = session.scalar(select(TaskSourceSubscription))
        wake = session.scalar(select(TaskPlannerWakeState))
        assert message is not None and message.message_id == 9001
        assert state is not None and state.snapshot_status == "ready"
        assert state.snapshot_revision == 1
        assert state.fresh_until_at == NOW + timedelta(seconds=60)
        assert subscription is not None and subscription.listener_source_state_id == state.id
        assert wake is not None and wake.reason_code == "channel_source_snapshot_ready"
        assert wake.not_before_at == NOW
        assert channel_snapshot_state(session, task, channel, now_value=NOW) == (
            "ready",
            NOW + timedelta(seconds=30),
        )


def test_fresh_empty_snapshot_hides_messages_from_previous_revision(monkeypatch) -> None:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    _seed_dynamic_channel_task(engine)
    current_time = [NOW]
    snapshots = [[_snapshot(9001)], []]
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: current_time[0])
    monkeypatch.setattr(executor_common, "_now", lambda: current_time[0])
    monkeypatch.setattr(
        channel_listener_runtime,
        "credentials_for_task_account",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *_args, **_kwargs: snapshots.pop(0),
    )

    drain_channel_listener_runtime(lambda: Session(engine), limit=10)
    current_time[0] = NOW + timedelta(seconds=30)
    drain_channel_listener_runtime(lambda: Session(engine), limit=10)

    with Session(engine) as session:
        task = session.get(Task, "empty-snapshot-task")
        channel, messages = executor_common.channel_scope(
            session,
            task,
            task.type_config,
        )
        state = session.scalar(select(ListenerSourceState))
        item_count = session.scalar(select(func.count(ListenerChannelSnapshotItem.id)))
        assert state.snapshot_revision == 2
        assert session.scalar(select(func.count(ChannelMessage.id))) == 1
        assert item_count == 0
        assert channel is None
        assert messages == []


def _seed_dynamic_channel_task(engine) -> None:
    _seed_listener_task(
        engine,
        task_id="empty-snapshot-task",
        task_type="channel_view",
        channel_id=41,
        account_id=201,
    )


def _seed_listener_task(
    engine,
    *,
    task_id: str,
    task_type: str,
    channel_id: int,
    account_id: int,
    username: str = "",
) -> None:
    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="tenant"),
            OperationTarget(
                id=channel_id,
                tenant_id=1,
                target_type="channel",
                tg_peer_id=f"-100{channel_id}",
                title="channel",
                username=username,
            ),
            TgAccount(
                id=account_id,
                tenant_id=1,
                display_name="listener",
                phone_masked="***listener",
                status=AccountStatus.ACTIVE.value,
                session_ciphertext="encrypted-session",
            ),
            Task(
                id=task_id,
                tenant_id=1,
                name=task_id,
                type=task_type,
                status="running",
                account_config={"selection_mode": "manual", "account_ids": [account_id]},
                type_config={
                    "target_channel_id": channel_id,
                    "message_scope": "dynamic_new",
                    "listener_interval_seconds": 30,
                },
            ),
        ])
        session.commit()


def _snapshot(message_id: int):
    return SimpleNamespace(
        message_id=message_id,
        content_preview="new message",
        message_url="",
        comment_available=True,
        published_at=NOW,
    )


def _stub_listener_fetch(monkeypatch, calls: list[tuple]) -> None:
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: NOW)
    monkeypatch.setattr(
        channel_listener_runtime,
        "credentials_for_task_account",
        lambda *_args: object(),
    )
    monkeypatch.setattr(
        channel_listener_runtime.gateway,
        "fetch_channel_messages",
        lambda *args, **kwargs: calls.append((args, kwargs)) or [_snapshot(9001)],
    )
