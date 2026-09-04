from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.models import ListenerChannelSnapshotItem, ListenerSourceState, OperationTarget
from app.services.task_center.channel_listener_runtime import ensure_channel_subscription
from app.services.task_center.channel_source_pagination import advance_source_page
from app.services.task_center.executors.channel_comment import _persisted_channel_scope
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def _ready_source(session, task, rows):
    source = ListenerSourceState(id="source", tenant_id=task.tenant_id, source_type="channel",
        source_peer_id="1", snapshot_status="ready", snapshot_revision=1,
        last_event_at=NOW-timedelta(days=1), observed_at=NOW, fresh_until_at=NOW+timedelta(days=1))
    session.add(source)
    session.flush()
    subscription = ensure_channel_subscription(session, task, session.get(OperationTarget, 1))
    subscription.listener_source_state_id = source.id
    advance_source_page(session, SimpleNamespace(task_ids=[task.id], tenant_id=task.tenant_id,
        channel_target_id=1, fetch_limit=len(rows)+1), state=source,
        snapshots=sorted(rows, key=lambda row: row.message_id, reverse=True), observed_at=NOW)
    for row in rows:
        session.add(ListenerChannelSnapshotItem(listener_source_state_id=source.id,
            snapshot_revision=1, channel_message_id=row.id))
    session.commit()


def test_comment_actual_scope_uses_frozen_initial_and_all_dynamic_sources():
    session, task, _, _ = seed_source_session(task_type="channel_comment")
    with session:
        rows = [message(session, i, at=NOW-timedelta(minutes=20-i)) for i in range(1, 11)]
        for row in rows:
            row.comment_available = True
        task.type_config = {**task.type_config, "message_scope": "latest_n", "message_count": 1}
        _ready_source(session, task, rows)
        _, first = _persisted_channel_scope(session, task, task.type_config)
        assert {row.id for row in first} == set(range(6, 11))
        for i in range(11, 23):
            row = message(session, i, at=NOW+timedelta(minutes=i), metadata={"poll": i == 22})
            row.comment_available = True
        session.commit()
        _, second = _persisted_channel_scope(session, task, task.type_config)
        assert {row.id for row in second} == set(range(6, 22))
        assert task.stats["source_intake"]["counts"]["source_filtered_non_content"] == 1


def test_comment_actual_scope_does_not_generate_from_missing_source_observation():
    session, task, _, _ = seed_source_session(task_type="channel_comment")
    with session:
        row = message(session, 1)
        row.comment_available = True
        session.commit()
        assert _persisted_channel_scope(session, task, task.type_config) == (None, [])
        assert task.stats["source_intake"]["state"] == "source_ingestion_unproven"


def test_closed_comments_are_capability_blocked_not_missing_posts():
    session, task, _, _ = seed_source_session(task_type="channel_comment")
    with session:
        row = message(session, 1)
        row.comment_available = False
        _ready_source(session, task, [row])
        assert _persisted_channel_scope(session, task, task.type_config) == (None, [])
        assert task.stats["source_intake"]["state"] == "source_capability_blocked"
        assert task.stats["source_intake"]["capability_blocked_count"] == 1
