"""Consumers serialize deliveries without locking the shared Collector inputs."""
from datetime import timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session, sessionmaker

from app.models import Task, TgGroup
from app.models.group_clone import CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery as Delivery,
    TelegramAuthorizationUpdateState as State,
    TelegramAuthorizationUpdateSubscription as Subscription,
)
from app.services._common import _now
from app.services.task_center import (
    channel_comment_update_stream as comment,
    group_ai_update_stream as ai,
    group_clone_source_stream as clone,
)
from app.services.task_center.telegram_update_collector import _claim_state
from app.services.task_center.engagement_update_subscriptions import ensure_task_peer_update_subscription
from app.services.task_center.telegram_update_ingress import (
    NormalizedUpdateIngress, ingest_normalized_update,
)
import test_listener_subscription_lock_postgres as fixtures

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
database = fixtures.database
CONSUMERS = ("ai", "comment", "clone")


def _ingest(session, state, message_id):
    return ingest_normalized_update(session, state.id, NormalizedUpdateIngress(
        update_identity_key=f"edit:{message_id}", constructor_name="UpdateEditChannelMessage",
        pts_evidence=message_id, pts_count_evidence=1, routing_peer_type="channel",
        routing_peer_id="-1007", cursor_scope="event_only",
        normalized_items=({"source_message_id": message_id, "event_type": "message_edit",
                           "media_type": "text", "content": "test edit"},),
    ), owner_id=state.owner_id, owner_fencing_epoch=state.owner_fencing_epoch)


def _seed(database, kind):
    with Session(database) as session:
        task, group, state = fixtures._seed(session)
        task.type = {"ai": "group_ai_chat", "comment": "channel_comment", "clone": "group_clone"}[kind]
        state.owner_id = "seed"
        state.lease_expires_at = _now() + timedelta(minutes=1)
        _, deliveries = _ingest(session, state, 101)
        if kind == "clone":
            session.add(CloneSourceStreamState(
                tenant_id=1, task_id=task.id, task_lifecycle_epoch=1,
                source_peer_type="channel", source_peer_id="-1007", listener_account_id=11,
                authorization_id=21, authorization_update_state_id=state.id,
                start_message_id=100, start_pts=100, channel_pts=100,
                difference_cursor={"start_message_id": 100}, state="catching_up",
            ))
        ids = task.id, group.id, state.id, deliveries[0].id
        state.owner_id = ""
        state.lease_expires_at = None
        session.commit()
        return ids


def _probe_collector(factory, ids):
    _, _, state_id, delivery_id = ids
    with factory() as competing_consumer:
        with pytest.raises(DBAPIError) as failure:
            competing_consumer.scalar(select(Delivery).where(
                Delivery.id == delivery_id,
            ).with_for_update(nowait=True))
        assert failure.value.orig.sqlstate == "55P03"
    with factory() as probe:
        probe.scalar(select(State).where(State.id == state_id).with_for_update(nowait=True))
    assert _claim_state(factory, state_id) is not None
    with factory() as collector:
        state = collector.get(State, state_id)
        _, deliveries = _ingest(collector, state, 102)
        assert len(deliveries) == 1
        collector.commit()


def _observe_processing(monkeypatch, kind, probe):
    module = {"ai": ai, "comment": comment, "clone": clone}[kind]
    name = "_apply_delivery" if kind == "comment" else "_consume_delivery"
    original = getattr(module, name)

    def process(*args, **kwargs):
        probe()
        return original(*args, **kwargs)

    monkeypatch.setattr(module, name, process)


def _consume(session, ids, kind):
    task_id, group_id, _, _ = ids
    task = session.get(Task, task_id)
    if kind == "ai":
        return ai.consume_group_ai_update_deliveries(session, task, session.get(TgGroup, group_id), limit=1)
    if kind == "comment":
        binding = SimpleNamespace(is_current=True, binding_status="active", discussion_peer_id="-1007")
        return comment.consume_channel_comment_update_deliveries(session, task, binding, limit=1)
    return clone.consume_clone_deliveries(session, task, limit=1)


@pytest.mark.parametrize("kind", CONSUMERS)
def test_consumer_allows_collector_claim_and_fanout_but_serializes_delivery(database, monkeypatch, kind):
    ids = _seed(database, kind)
    factory = sessionmaker(bind=database, autoflush=False)
    _observe_processing(monkeypatch, kind, lambda: _probe_collector(factory, ids))
    with factory() as consumer:
        _consume(consumer, ids, kind)
        consumer.commit()
    with factory() as readback:
        assert readback.get(Delivery, ids[3]).delivery_state in {"consumed", "skipped"}
        assert readback.scalar(select(func.count()).select_from(Delivery)) == 2


def _rebind(session, task_id):
    return ensure_task_peer_update_subscription(
        session, session.get(Task, task_id), listener_account_id=11, source_peer_id="-1008",
    )


def test_rebind_serializes_with_inflight_delivery_and_skips_only_pending(database, monkeypatch):
    ids = _seed(database, "comment")
    factory = sessionmaker(bind=database, autoflush=False)

    def probe():
        _probe_collector(factory, ids)
        with factory() as rebind:
            rebind.execute(text("SET LOCAL lock_timeout = '100ms'"))
            with pytest.raises(DBAPIError) as failure:
                _rebind(rebind, ids[0])
            assert failure.value.orig.sqlstate == "55P03"
            assert "UPDATE telegram_authorization_update_deliveries" in failure.value.statement

    _observe_processing(monkeypatch, "comment", probe)
    with factory() as consumer:
        _consume(consumer, ids, "comment")
        consumer.commit()
    with factory() as rebind:
        assert _rebind(rebind, ids[0]).route_changed
        rebind.commit()
    with factory() as readback:
        assert readback.get(Delivery, ids[3]).delivery_state == "consumed"
        remaining = readback.scalar(select(Delivery).where(Delivery.id != ids[3]))
        assert remaining.delivery_state == "skipped"
        assert readback.scalar(select(Subscription)).source_peer_id == "-1008"
