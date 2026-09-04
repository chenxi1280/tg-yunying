from datetime import timedelta
from importlib import import_module
from types import SimpleNamespace as NS

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session

from app.models import ChannelSourcePageCursor, ListenerSourceState
from app.services.task_center.channel_source_pagination import advance_source_page, source_page_offset
from engine_source_test_support import NOW, seed_source_session


pytestmark = pytest.mark.no_postgres


def _page(ids, *, album="", metadata=None, at=NOW):
    return [NS(message_id=i, grouped_id=album, published_at=at, content_preview="post",
        source_metadata={"observed": True, **(metadata or {})}) for i in ids]


def _source(task_ids=()):
    return NS(task_ids=list(task_ids), tenant_id=1, channel_target_id=1, fetch_limit=3)


def _state(session, *, previous="100"):
    state = ListenerSourceState(id="source", tenant_id=1, source_type="channel", source_peer_id="1",
        last_remote_message_id=previous, observed_at=NOW-timedelta(minutes=1), last_event_at=NOW-timedelta(days=1))
    session.add(state)
    session.flush()
    return state


def test_gap_is_paged_durably_and_complete_time_is_original_head_not_catchup_time():
    session, _, _, _ = seed_source_session()
    with session:
        state = _state(session)
        first = advance_source_page(session, _source(), state=state, snapshots=_page([106, 105, 104]), observed_at=NOW)
        assert not first.complete
        assert state.last_remote_message_id == "100"
        session.commit()
        session.expire_all()
        assert source_page_offset(session, state.id) == 104
        second = advance_source_page(session, _source(), state=state, snapshots=_page([103, 102, 101]), observed_at=NOW+timedelta(minutes=1))
        assert not second.complete
        third = advance_source_page(session, _source(), state=state, snapshots=_page([100, 99, 98]), observed_at=NOW+timedelta(minutes=2))
        assert third.complete
        assert third.observed_at == NOW
        assert state.last_remote_message_id == "106"
        assert state.backfill_until is None
        assert source_page_offset(session, state.id) == 0


def test_initial_filter_and_album_boundary_cannot_freeze_partial_history():
    session, task, _, _ = seed_source_session()
    with session:
        task.type_config = {**task.type_config, "initial_historical_post_limit": 1}
        state = _state(session, previous="")
        source = _source([task.id])
        first = advance_source_page(session, source, state=state,
            snapshots=_page([9], metadata={"poll": True})+_page([8, 7], album="album"), observed_at=NOW)
        assert not first.complete
        second = advance_source_page(session, source, state=state,
            snapshots=_page([6], album="album")+_page([5, 4]), observed_at=NOW+timedelta(seconds=30))
        assert second.complete
        assert state.last_remote_message_id == "9"
        # Planner waits for a fresh head page; that refresh must not restart the same history scan.
        refresh = advance_source_page(session, source, state=state,
            snapshots=_page([9], metadata={"poll": True})+_page([8, 7], album="album"), observed_at=NOW+timedelta(seconds=60))
        assert refresh.complete
        assert source_page_offset(session, state.id) == 0


def test_nonadvancing_page_is_error_not_fake_progress():
    session, _, _, _ = seed_source_session()
    with session:
        state = _state(session)
        advance_source_page(session, _source(), state=state, snapshots=_page([106, 105, 104]), observed_at=NOW)
        with pytest.raises(ValueError, match="cursor_not_advanced"):
            advance_source_page(session, _source(), state=state, snapshots=_page([106, 105, 104]), observed_at=NOW)


def test_cursor_migration_matches_model_and_preserves_listener(monkeypatch):
    migration = import_module("migrations.versions.0222_channel_source_cursor")
    with create_engine("sqlite:///:memory:").begin() as connection:
        connection.exec_driver_sql("CREATE TABLE listener_source_state (id VARCHAR(36) PRIMARY KEY)")
        connection.exec_driver_sql("INSERT INTO listener_source_state VALUES ('retained')")
        monkeypatch.setattr(migration, "op", Operations(MigrationContext.configure(connection)))
        migration.upgrade()
        columns = {c["name"]: c["nullable"] for c in inspect(connection).get_columns(ChannelSourcePageCursor.__tablename__)}
        assert columns == {c.name: c.nullable for c in ChannelSourcePageCursor.__table__.columns}
        migration.downgrade()
        assert connection.exec_driver_sql("SELECT id FROM listener_source_state").scalar_one() == "retained"


def test_real_listener_pages_then_intake_keeps_all_new_posts(monkeypatch):
    from app.integrations.telegram import ChannelMessageSnapshot
    from app.services.task_center import channel_listener_runtime as runtime
    from app.services.task_center import channel_source_intake as intake
    from app.services.task_center.channel_source_observation import source_interval_complete
    from app.models import TaskSourceSubscription
    session, task, _, _ = seed_source_session(task_type="channel_view")
    engine = session.get_bind()
    state = _state(session)
    state.account_id = 1
    task.type_config = {**task.type_config, "initial_historical_post_limit": 0}
    session.add(TaskSourceSubscription(task_id=task.id, tenant_id=1, lifecycle_epoch=1,
        source_type="channel", source_peer_hash="hash", listener_source_state_id=state.id))
    session.commit()
    source = runtime.ChannelListenerSource(1, 1, "hash", 1, 30, 3, task_ids=[task.id])
    clock, calls = [NOW], []
    pages = {0: [106, 105, 104], 104: [103, 102, 101], 101: [100, 99, 98]}

    def fetch(*args, **kwargs):
        offset = kwargs.get("offset_id", 0)
        calls.append(offset)
        return [ChannelMessageSnapshot(message_id=i, content_preview="post", message_url="",
            published_at=NOW+timedelta(seconds=1) if i > 100 else NOW-timedelta(minutes=1),
            source_metadata={"observed": True}) for i in pages[offset]]

    monkeypatch.setattr(runtime, "_now", lambda: clock[0])
    monkeypatch.setattr(intake, "_now", lambda: clock[0])
    monkeypatch.setattr(runtime, "_fetch_context", lambda *a: ("@channel", "session", object()))
    monkeypatch.setattr(runtime, "_probe_channel_discussion", lambda *a, **kw: (None, ""))
    monkeypatch.setattr(runtime.gateway, "fetch_channel_messages", fetch)
    for index in range(4):
        clock[0] = NOW+timedelta(seconds=30*index)
        assert runtime._drain_channel_source(lambda: Session(engine), source) == "processed"
    session.expire_all()
    accepted = intake.unified_source_intake(session, task, [], config=task.type_config, observation_complete=True)
    assert calls == [0, 104, 101, 0]
    assert {row.message_id for row in accepted} == set(range(101, 107))
    assert source_interval_complete(session, task, since=NOW, until=clock[0])
    session.close()


def test_old_listener_cannot_persist_after_claim_owner_changed():
    from app.services.task_center.channel_listener_claim import ChannelSourceClaimLost, locked_source_state
    session, _, _, _ = seed_source_session()
    with session:
        state = _state(session)
        state.lease_owner = "new-owner"
        session.flush()
        source = NS(claim_owner="old-owner", claimed_revision=0)
        with pytest.raises(ChannelSourceClaimLost):
            locked_source_state(session, source, state.id)
