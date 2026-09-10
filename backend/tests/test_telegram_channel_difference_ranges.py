from dataclasses import replace
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.integrations.telegram.update_contracts import TelegramDifferenceBatch
from app.models import Task
from app.models.group_clone import CloneSourceEvent, CloneSourceStreamState
from app.models.telegram_updates import (
    TelegramAuthorizationUpdateDelivery as Delivery,
    TelegramAuthorizationUpdateEvent as UpdateEvent,
    TelegramAuthorizationUpdateSubscription as Subscription,
)
from app.services.task_center.group_clone_source_stream import consume_clone_deliveries
from app.services.task_center.telegram_channel_difference_ranges import (
    CHANNEL_DIFFERENCE_RANGE, record_channel_difference_range,
)
from app.services.task_center.telegram_update_channels import channel_cursors
from app.services.task_center.telegram_update_collector import _persist_batch
import test_group_clone_update_ingress as ingress_fixtures
from test_group_clone_update_ingress import _ingress, _write_ingress

pytestmark = pytest.mark.no_postgres
clone_ingress_session = ingress_fixtures.clone_ingress_session


def _claim(state):
    return SimpleNamespace(
        state_id=state.id, owner_id=state.owner_id, fencing_epoch=state.owner_fencing_epoch,
    )


def _range(session, state, *, start=100, end=103, **kwargs):
    batch = TelegramDifferenceBatch(scope="channel", status="slice", cursor={"pts": end}, final=False)
    record_channel_difference_range(
        session, state, batch, claim=_claim(state), peer_id="-10011", requested_pts=start, **kwargs,
    )


def test_later_channel_range_unblocks_fifo_without_skipping_or_duplicate(clone_ingress_session):
    session, state = clone_ingress_session
    task = session.get(Task, "clone-ingress-task")
    _write_ingress(session, state, _ingress("early-head", message_id=11, pts=102))
    _write_ingress(session, state, _ingress("later-copy", message_id=11, pts=102))
    _range(session, state)
    assert consume_clone_deliveries(session, task, limit=1) == 1
    stream = session.scalar(select(CloneSourceStreamState))
    assert stream.channel_pts == 102  # Advance to the consumed event, not the proof's end.
    assert stream.last_consumed_ingress_order_no == 1
    assert consume_clone_deliveries(session, task) == 1
    assert len(session.scalars(select(CloneSourceEvent)).all()) == 1
    assert len(session.scalars(select(Delivery)).all()) == 2


@pytest.mark.parametrize("invalid", [
    "other_state", "other_peer", "old_epoch", "old_subscription", "uncovered",
    "generic_difference", "cursor_only",
])
def test_head_requires_scoped_channel_range(clone_ingress_session, invalid):
    session, state = clone_ingress_session
    task = session.get(Task, "clone-ingress-task")
    _write_ingress(session, state, _ingress("head", message_id=11, pts=102))
    _range(session, state, start=101 if invalid == "uncovered" else 100)
    proof = session.scalar(select(UpdateEvent).where(UpdateEvent.constructor_name == CHANNEL_DIFFERENCE_RANGE))
    subscription = session.scalar(select(Subscription))
    if invalid == "other_state":
        proof.authorization_update_state_id = "other-state"
    elif invalid == "other_peer":
        proof.routing_peer_id = "-10022"
    elif invalid == "old_epoch":
        subscription.task_epoch = 2
    elif invalid == "old_subscription":
        subscription.start_ingress_order = proof.ingress_order_no
    elif invalid == "generic_difference":
        proof.constructor_name = "DifferenceMessages"
    elif invalid == "cursor_only":
        session.delete(proof)
        state.difference_cursor = {"channels": {"-10011": {"pts": 103, "status": "live", "final": True}}}
    session.flush()
    if invalid == "old_epoch":
        from app.services.task_center.group_clone_source_stream import _pts_continuous
        head = session.scalar(select(UpdateEvent).where(UpdateEvent.ingress_order_no == 1))
        assert not _pts_continuous(session, session.scalar(select(CloneSourceStreamState)), head)
    else:
        assert consume_clone_deliveries(session, task) == 0
    assert session.scalar(select(CloneSourceEvent)) is None


@pytest.mark.parametrize("scope,status", [("common", "live"), ("channel", "too_long")])
def test_common_and_lost_history_never_create_range(clone_ingress_session, scope, status):
    session, state = clone_ingress_session
    batch = TelegramDifferenceBatch(scope=scope, status=status, cursor={"pts": 103})
    record_channel_difference_range(session, state, batch, claim=_claim(state), peer_id="-10011", requested_pts=100)
    assert session.scalar(select(UpdateEvent)) is None


def test_empty_range_is_idempotent_and_does_not_deliver_messages(clone_ingress_session):
    session, state = clone_ingress_session
    _range(session, state, end=100)
    _range(session, state, end=100)
    assert len(session.scalars(select(UpdateEvent)).all()) == 1
    assert session.scalar(select(Delivery)) is None
    assert session.scalar(select(CloneSourceEvent)) is None


def test_collector_persists_actual_request_range_and_rejects_missing_start(clone_ingress_session):
    session, state = clone_ingress_session
    claim = _claim(state)
    factory = sessionmaker(bind=session.get_bind())
    batch = TelegramDifferenceBatch(scope="channel", status="live", cursor={"pts": 103})
    with pytest.raises(ValueError, match="range_invalid"):
        _persist_batch(factory, claim, batch, peer_id="-10011")
    _persist_batch(factory, claim, batch, peer_id="-10011", requested_pts=101)
    session.expire_all()
    proof = session.scalar(select(UpdateEvent))
    assert (proof.pts_evidence, proof.pts_count_evidence) == (103, 2)


def test_gap_recovery_requests_frontier_until_first_page_covers_head(clone_ingress_session):
    session, state = clone_ingress_session
    task = session.get(Task, "clone-ingress-task")
    state.difference_cursor = {"channels": {"-10011": {"pts": 110, "status": "live", "final": True}}}
    _write_ingress(session, state, replace(_ingress("head", message_id=11, pts=102), cursor_scope="event_only"))
    assert consume_clone_deliveries(session, task) == 0
    session.commit()
    factory = sessionmaker(bind=session.get_bind())
    assert channel_cursors(factory, state.id) == [("-10011", 100)]
    # Collector may have already projected a final response back to catching_up.
    session.scalar(select(CloneSourceStreamState)).state = "catching_up"
    task.status = "running"
    session.commit()
    assert channel_cursors(factory, state.id) == [("-10011", 100)]
    task.status = "paused"
    session.commit()
    assert channel_cursors(factory, state.id) == [("-10011", 110)]
    task.status = "running"
    _range(session, state)
    session.commit()
    assert channel_cursors(factory, state.id) == [("-10011", 110)]
    assert session.get(type(state), state.id).difference_cursor["channels"]["-10011"]["pts"] == 110
