from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.integrations.telegram.update_contracts import (
    TelegramDifferenceBatch,
    TelegramNormalizedUpdate,
)
from app.models import (
    AccountStatus,
    Action,
    ExecutionAttempt,
    Task,
    TelegramDeveloperApp,
    Tenant,
    TgAccount,
    TgAccountAuthorization,
)
from app.models.group_clone import (
    CloneSourceEvent,
    CloneSourceStreamState,
    TelegramGatewayMutationIdentity,
)
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery,
    TelegramAuthorizationUpdateState,
)
from app.security import encrypt_secret
from app.services._common import _now
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from app.services.task_center.telegram_update_collector import (
    _mapping_runtime,
    drain_telegram_update_collector,
)
from app.services.task_center.telegram_update_ingress import subscribe_task_to_updates


@pytest.fixture
def collector_runtime():
    engine = create_engine("sqlite:///:memory:")
    factory = sessionmaker(bind=engine)
    Base.metadata.create_all(engine)
    with factory() as session:
        _seed_runtime(session)
        session.commit()
    yield factory
    engine.dispose()


def test_collector_takes_over_and_persists_channel_delivery(
    collector_runtime,
    monkeypatch,
) -> None:
    initial = TelegramDifferenceBatch(
        scope="common",
        status="empty",
        cursor={"pts": 100, "qts": 0, "date": 1000, "seq": 1},
    )
    channel = TelegramDifferenceBatch(
        scope="channel",
        status="live",
        cursor={"pts": 501},
        updates=(_channel_update(),),
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_update_state",
        lambda **_kwargs: initial,
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_channel_difference",
        lambda *_args, **_kwargs: channel,
    )

    result = drain_telegram_update_collector(collector_runtime, tenant_id=1)

    assert result.error_count == 0
    assert result.batch_count == 2
    with collector_runtime() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        assert state.state == "live" and state.common_pts == 100
        assert state.owner_fencing_epoch == 2
        assert state.owner_id and state.owner_id != "expired-owner"
        delivery = session.scalar(select(TelegramAuthorizationUpdateDelivery))
        assert delivery is not None and delivery.delivery_state == "pending"
        task = session.get(Task, "clone-collector-task")
        assert consume_clone_deliveries(session, task) == 1
        event = session.scalar(select(CloneSourceEvent))
        stream = session.scalar(select(CloneSourceStreamState))
        assert event is not None and event.source_message_id == 11
        assert stream.channel_pts == 501


def test_active_foreign_owner_is_not_stolen(collector_runtime, monkeypatch) -> None:
    with collector_runtime() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        state.owner_id = "other-live-owner"
        state.lease_expires_at = _now() + timedelta(minutes=5)
        session.commit()
    called = False

    def unexpected(**_kwargs):
        nonlocal called
        called = True
        raise AssertionError("foreign live owner must not be called")

    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_update_state",
        unexpected,
    )
    result = drain_telegram_update_collector(collector_runtime, tenant_id=1)
    assert result.processed_count == 0
    assert called is False


def test_channel_difference_recovers_failed_gap_stream(
    collector_runtime,
    monkeypatch,
) -> None:
    with collector_runtime() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        state.state = "gap"
        state.common_pts = 100
        state.common_date = 1000
        task = session.get(Task, "clone-collector-task")
        task.status = "failed"
        task.last_error = "group_clone_source_pts_gap"
        stream = session.scalar(select(CloneSourceStreamState))
        stream.state = "gap"
        session.commit()
    common = TelegramDifferenceBatch(
        scope="common",
        status="live",
        cursor={"pts": 100, "qts": 0, "date": 1000, "seq": 1},
        final=True,
    )
    channel = TelegramDifferenceBatch(
        scope="channel",
        status="live",
        cursor={"pts": 501},
        updates=(_channel_update(),),
        final=True,
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_difference",
        lambda *_args, **_kwargs: common,
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_channel_difference",
        lambda *_args, **_kwargs: channel,
    )

    result = drain_telegram_update_collector(collector_runtime, tenant_id=1)

    assert result.error_count == 0
    with collector_runtime() as session:
        task = session.get(Task, "clone-collector-task")
        stream = session.scalar(select(CloneSourceStreamState))
        assert task.status == "running"
        assert task.stats["clone_start_state"] == "runtime_recovering"
        assert stream.state == "catching_up"
        assert consume_clone_deliveries(session, task) == 1
        assert stream.state == "live"
        assert task.stats["clone_start_state"] == "running"


def test_channel_too_long_continues_from_persisted_cursor_until_final(
    collector_runtime,
    monkeypatch,
) -> None:
    _mark_collector_gap(collector_runtime)
    common = TelegramDifferenceBatch(
        scope="common",
        status="live",
        cursor={"pts": 100, "qts": 0, "date": 1000, "seq": 1},
        final=True,
    )
    batches = [
        TelegramDifferenceBatch(
            scope="channel", status="too_long", cursor={"pts": 550}, final=False,
        ),
        TelegramDifferenceBatch(
            scope="channel", status="live", cursor={"pts": 551}, final=True,
        ),
    ]
    requested_pts: list[int] = []

    def fetch_channel(_peer_id, pts, **_kwargs):
        requested_pts.append(pts)
        return batches.pop(0)

    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_authorization_difference",
        lambda *_args, **_kwargs: common,
    )
    monkeypatch.setattr(
        "app.services.task_center.telegram_update_collector.gateway.fetch_raw_channel_difference",
        fetch_channel,
    )

    first = drain_telegram_update_collector(collector_runtime, tenant_id=1)
    with collector_runtime() as session:
        task = session.get(Task, "clone-collector-task")
        stream = session.scalar(select(CloneSourceStreamState))
        assert first.error_count == 0
        assert task.status == "failed"
        assert stream.state == "gap"
    second = drain_telegram_update_collector(collector_runtime, tenant_id=1)

    with collector_runtime() as session:
        task = session.get(Task, "clone-collector-task")
        stream = session.scalar(select(CloneSourceStreamState))
        assert second.error_count == 0
        assert requested_pts == [500, 550]
        assert task.status == "running"
        assert stream.state == "catching_up"


def _mark_collector_gap(collector_runtime) -> None:
    with collector_runtime() as session:
        state = session.scalar(select(TelegramAuthorizationUpdateState))
        state.state = "gap"
        state.common_pts = 100
        state.common_date = 1000
        task = session.get(Task, "clone-collector-task")
        task.status = "failed"
        stream = session.scalar(select(CloneSourceStreamState))
        stream.state = "gap"
        session.commit()


def test_topic_create_mapping_resolves_mutation_action(collector_runtime) -> None:
    with collector_runtime() as session:
        identity = TelegramGatewayMutationIdentity(
            id="topic-identity",
            tenant_id=1,
            task_id="clone-collector-task",
            epoch=1,
            obligation_id="topic-obligation",
            mutation_kind="createForumTopic",
            execution_role="target_control",
            account_id=1,
            telegram_account_peer_id="listener",
            authorization_id=2,
            session_generation=1,
            target_peer_type="channel",
            target_peer_id="-10022",
            random_id=991,
            request_fingerprint="a" * 64,
        )
        action = Action(
            id="topic-action",
            tenant_id=1,
            task_id="clone-collector-task",
            task_type="group_clone",
            action_type="group_clone_mutation",
            obligation_type="group_clone_delivery",
            obligation_id="topic-obligation",
            task_lifecycle_epoch=1,
            payload={"gateway_mutation_identity_id": identity.id},
        )
        attempt = ExecutionAttempt(
            id="topic-attempt",
            tenant_id=1,
            action_id=action.id,
            account_id=1,
            attempt_no=1,
        )
        session.add_all([identity, action, attempt])
        session.flush()

        resolved_action, resolved_attempt, _journal = _mapping_runtime(session, identity)

        assert resolved_action.id == action.id
        assert resolved_attempt.id == attempt.id


def _seed_runtime(session) -> None:
    session.add(Tenant(id=1, name="collector"))
    session.add(TelegramDeveloperApp(
        id=51,
        app_name="collector-app",
        api_id=12345,
        api_hash_ciphertext=encrypt_secret("collector-api-hash"),
        is_active=True,
        health_status="健康",
    ))
    session.add(TgAccount(
        id=1,
        tenant_id=1,
        display_name="listener",
        phone_masked="+1",
        status=AccountStatus.ACTIVE.value,
        developer_app_id=51,
        developer_app_version=1,
    ))
    session.flush()
    authorization = TgAccountAuthorization(
        id=2,
        tenant_id=1,
        account_id=1,
        is_current=True,
        status="active",
        slot_generation=1,
        developer_app_id=51,
        developer_app_api_id_snapshot=12345,
        session_ciphertext="session",
        telegram_user_id_digest="listener",
    )
    session.add(authorization)
    session.add(Task(
        id="clone-collector-task",
        tenant_id=1,
        name="collector task",
        type="group_clone",
        status="running",
        task_lifecycle_epoch=1,
        stats={"clone_start_state": "running"},
    ))
    session.flush()
    state = TelegramAuthorizationUpdateState(
        tenant_id=1,
        account_id=1,
        authorization_id=2,
        session_generation=1,
        state="initializing",
        owner_id="expired-owner",
        owner_fencing_epoch=1,
        lease_expires_at=_now() - timedelta(seconds=1),
    )
    session.add(state)
    session.flush()
    subscribe_task_to_updates(
        session,
        state.id,
        "clone-collector-task",
        task_epoch=1,
        source_peer_type="channel",
        source_peer_id="-10011",
    ).state = "active"
    session.add(CloneSourceStreamState(
        tenant_id=1,
        task_id="clone-collector-task",
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-10011",
        listener_account_id=1,
        authorization_id=2,
        authorization_update_state_id=state.id,
        start_message_id=10,
        start_pts=500,
        channel_pts=500,
        difference_cursor={"start_message_id": 10, "start_channel_pts": 500},
        state="live",
    ))


def _channel_update() -> TelegramNormalizedUpdate:
    return TelegramNormalizedUpdate(
        identity_key="channel:501:11",
        constructor_name="ChannelDifferenceMessages",
        pts=501,
        pts_count=1,
        routing_peer_type="channel",
        routing_peer_id="-10011",
        normalized_items=({
            "source_message_id": 11,
            "event_type": "message_new",
            "sender_peer_type": "user",
            "sender_peer_id": "9",
            "reply_to_message_id": None,
            "source_top_message_id": None,
            "grouped_id": None,
            "media_type": "text",
            "content": "hello",
            "entities": [],
            "poll_snapshot": {},
            "protected_content": False,
            "message_revision": 1,
        },),
    )
