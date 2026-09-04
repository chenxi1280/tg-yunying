from datetime import timedelta

import pytest
from sqlalchemy import select

from app.models import ChannelTaskIntake, ListenerSourceState, OperationTarget, Task
from app.services.task_center import channel_listener_runtime as runtime
from app.services.task_center.channel_source_pagination import advance_source_page
from app.services.task_center.executors.common import channel_scope
from engine_source_test_support import NOW, message, seed_source_session


pytestmark = pytest.mark.no_postgres


def _ready(session, task):
    state = ListenerSourceState(tenant_id=1, source_type="channel", source_peer_id="1",
        account_id=1, snapshot_status="ready", snapshot_revision=1, observed_at=NOW,
        fresh_until_at=NOW+timedelta(minutes=2), next_probe_at=NOW+timedelta(seconds=30),
        last_event_at=NOW-timedelta(minutes=2), last_remote_message_id="100")
    session.add(state)
    session.flush()
    source = runtime.ChannelListenerSource(1, 1, "hash", 1, 30, 3, task_ids=[task.id])
    runtime._bind_subscription(session, task, source)
    return state, source


def _status(session, task):
    return runtime.channel_snapshot_binding(session, task, session.get(OperationTarget, 1), now_value=NOW)[0]


def test_shared_ready_snapshot_cannot_freeze_new_tasks_partial_history(monkeypatch):
    monkeypatch.setattr(runtime, "_now", lambda: NOW)
    session, task, _, _ = seed_source_session()
    with session:
        state, source = _ready(session, task)
        first = message(session, 100)
        assert _status(session, task) == "pending"
        assert channel_scope(session, task, task.type_config) == (None, [])
        assert session.scalar(select(ChannelTaskIntake)) is None
        rows = [first]+[message(session, i) for i in range(99, 94, -1)]
        assert not advance_source_page(session, source, state=state, snapshots=rows[:3], observed_at=NOW).complete
        assert advance_source_page(session, source, state=state, snapshots=rows[3:], observed_at=NOW).complete
        session.commit()
        session.expire_all()
        assert _status(session, task) == "ready"
        _, accepted = channel_scope(session, task, task.type_config)
        assert {row.message_id for row in accepted} == set(range(96, 101))
        # Once initialized, a later requirement change does not redraw history.
        task.type_config = {**task.type_config, "initial_historical_post_limit": 10}
        assert _status(session, task) == "ready"
        assert len(session.scalar(select(ChannelTaskIntake)).initial_source_keys) == 5


def test_task_added_mid_page_waits_for_its_own_history_proof():
    session, task, _, _ = seed_source_session()
    with session:
        state, source = _ready(session, task)
        rows = [message(session, i) for i in range(100, 94, -1)]
        advance_source_page(session, source, state=state, snapshots=rows[:3], observed_at=NOW)
        newcomer = Task(id="new", tenant_id=1, name="new", type=task.type, status="running",
            created_at=NOW, stats=task.stats, type_config=task.type_config, task_lifecycle_epoch=1)
        session.add(newcomer)
        session.flush()
        source.task_ids.append(newcomer.id)
        runtime._bind_subscription(session, newcomer, source)
        advance_source_page(session, source, state=state, snapshots=rows[3:], observed_at=NOW)
        assert _status(session, newcomer) == "pending"
        advance_source_page(session, source, state=state, snapshots=rows[:3], observed_at=NOW)
        advance_source_page(session, source, state=state, snapshots=rows[3:], observed_at=NOW)
        assert _status(session, newcomer) == "ready"


def test_exhausted_history_can_prove_fewer_than_n():
    session, task, _, _ = seed_source_session()
    with session:
        state, source = _ready(session, task)
        assert advance_source_page(session, source, state=state,
            snapshots=[message(session, 100)], observed_at=NOW).complete
        assert _status(session, task) == "ready"
        task.type_config = {**task.type_config, "initial_historical_post_limit": 10}
        assert _status(session, task) == "pending"


@pytest.mark.parametrize("change", ["epoch", "anchor", "type"])
def test_history_proof_must_match_current_task_identity(change):
    session, task, _, _ = seed_source_session()
    with session:
        state, source = _ready(session, task)
        advance_source_page(session, source, state=state, snapshots=[message(session, 100)], observed_at=NOW)
        if change == "epoch":
            task.task_lifecycle_epoch += 1
            runtime._bind_subscription(session, task, source)
        elif change == "anchor":
            task.stats = {"started_at": (NOW+timedelta(seconds=1)).isoformat()}
        else:
            task.type = "channel_comment"
        assert _status(session, task) == "pending"


@pytest.mark.parametrize("config", [dict(engagement_contract_version="legacy_v0"),
    dict(message_scope="specific"), dict(initial_message_scope="specific")])
def test_legacy_and_explicit_id_sources_do_not_need_history_scan(config):
    session, task, _, _ = seed_source_session()
    with session:
        task.type_config = {**task.type_config, **config}
        _ready(session, task)
        assert _status(session, task) == "ready"
