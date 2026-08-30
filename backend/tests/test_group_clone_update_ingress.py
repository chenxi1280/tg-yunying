from __future__ import annotations

from datetime import timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

pytestmark = pytest.mark.no_postgres

from app.database import Base
from app.models import AccountStatus, Task, Tenant, TgAccount, TgAccountAuthorization
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import TelegramAuthorizationUpdateState
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from app.services._common import _now
from app.services.task_center.telegram_update_ingress import (
    NormalizedUpdateIngress,
    ingest_normalized_update,
    subscribe_task_to_updates,
)


@pytest.fixture
def clone_ingress_session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    session.add(Tenant(id=1, name="clone-ingress"))
    session.add(TgAccount(id=1, tenant_id=1, display_name="listener", phone_masked="+1", status=AccountStatus.ACTIVE.value))
    session.flush()
    session.add(TgAccountAuthorization(
        id=2,
        tenant_id=1,
        account_id=1,
        is_current=True,
        status="active",
        telegram_user_id_digest="listener-digest",
    ))
    session.add(Task(
        id="clone-ingress-task",
        tenant_id=1,
        name="clone ingress",
        type="group_clone",
        status="pending",
        task_lifecycle_epoch=1,
        stats={"clone_start_state": "starting"},
    ))
    session.flush()
    state = TelegramAuthorizationUpdateState(
        tenant_id=1,
        account_id=1,
        authorization_id=2,
        session_generation=1,
        state="live",
        owner_id="collector-1",
        lease_expires_at=_now() + timedelta(minutes=10),
    )
    session.add(state)
    session.flush()
    subscribe_task_to_updates(
        session,
        state.id,
        "clone-ingress-task",
        task_epoch=1,
        source_peer_type="channel",
        source_peer_id="-10011",
    ).state = "active"
    session.add(CloneSourceStreamState(
        tenant_id=1,
        task_id="clone-ingress-task",
        task_lifecycle_epoch=1,
        source_peer_type="channel",
        source_peer_id="-10011",
        listener_account_id=1,
        authorization_id=2,
        authorization_update_state_id=state.id,
        start_message_id=10,
        start_pts=100,
        channel_pts=100,
        difference_cursor={"start_message_id": 10, "start_channel_pts": 100},
        state="catching_up",
    ))
    session.commit()
    yield session, state
    session.close()


def test_ingress_retry_is_idempotent(clone_ingress_session):
    session, state = clone_ingress_session
    ingress = _ingress("update:101", message_id=11, pts=101)
    with pytest.raises(ValueError, match="collector_fenced"):
        ingest_normalized_update(
            session,
            state.id,
            ingress,
            owner_id="stale-owner",
            owner_fencing_epoch=state.owner_fencing_epoch,
        )
    event, deliveries = _write_ingress(session, state, ingress)
    retry_event, retry_deliveries = _write_ingress(session, state, ingress)
    assert retry_event.id == event.id
    assert [item.id for item in retry_deliveries] == [item.id for item in deliveries]
    assert state.last_ingress_order_no == 1
    with pytest.raises(ValueError, match="telegram_update_identity_payload_conflict"):
        _write_ingress(session, state, _ingress("update:101", message_id=12, pts=101))


def test_delivery_consumption_promotes_live_and_detects_gap(clone_ingress_session):
    session, state = clone_ingress_session
    task = session.get(Task, "clone-ingress-task")
    _write_ingress(session, state, _ingress("update:101", message_id=11, pts=101))
    assert consume_clone_deliveries(session, task) == 1
    event = session.scalar(select(CloneSourceEvent).where(CloneSourceEvent.task_id == task.id))
    stream = session.scalar(select(CloneSourceStreamState).where(CloneSourceStreamState.task_id == task.id))
    assert event is not None and event.stream_order_no == 1
    assert stream.state == "live"
    assert task.status == "running"

    _write_ingress(session, state, _ingress("difference:101", message_id=11, pts=101))
    assert consume_clone_deliveries(session, task) == 1
    assert len(session.scalars(select(CloneSourceEvent).where(CloneSourceEvent.task_id == task.id)).all()) == 1

    _write_ingress(session, state, _ingress("update:105", message_id=12, pts=105))
    assert consume_clone_deliveries(session, task) == 0
    assert stream.state == "gap"
    assert task.status == "failed"
    assert task.stats["clone_start_state"] == "runtime_blocked"


def _ingress(identity: str, *, message_id: int, pts: int) -> NormalizedUpdateIngress:
    return NormalizedUpdateIngress(
        update_identity_key=identity,
        constructor_name="UpdateNewChannelMessage",
        pts_evidence=pts,
        pts_count_evidence=1,
        routing_peer_type="channel",
        routing_peer_id="-10011",
        normalized_items=({
            "source_message_id": message_id,
            "event_type": "message_new",
            "sender_peer_type": "user",
            "sender_peer_id": "sender-1",
            "media_type": "text",
            "content": f"message-{message_id}",
            "entities": [],
        },),
    )


def _write_ingress(session, state, ingress):
    return ingest_normalized_update(
        session,
        state.id,
        ingress,
        owner_id=state.owner_id,
        owner_fencing_epoch=state.owner_fencing_epoch,
    )
