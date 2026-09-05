from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Action, Task, ChannelMessage, ChannelMessageSourceRevision, OperationTarget, Tenant
from app.services.operations import _normalize_snapshot_datetime
from app.services.task_center.channel_source_message_persistence import (
    _source_observation_hash, _source_operation, _upsert_channel_message,
)
from app.timezone import BEIJING_TZ

pytestmark = pytest.mark.no_postgres
PUBLISHED = datetime(2026, 9, 4, 18, 44, 35, tzinfo=timezone.utc)
LOCAL_PUBLISHED = PUBLISHED.astimezone(BEIJING_TZ).replace(tzinfo=None)
OBSERVED = datetime(2026, 9, 5, 3, 0)


def _snapshot(published=PUBLISHED):
    return SimpleNamespace(message_id=5981, published_at=published, edited_at=None,
                           content_preview='来源内容', content_text='来源内容', message_url='',
                           comment_available=True)


def _save(session, target, snapshot):
    return _upsert_channel_message(session, SimpleNamespace(tenant_id=1), target,
                          snapshot=snapshot, observed_at=OBSERVED, binding=None, thread_root_id=0)


def _session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    session = Session(engine)
    session.add(Tenant(id=1, name='测试空间'))
    session.add(OperationTarget(id=31, tenant_id=1, target_type='channel', tg_peer_id='-10031', title='测试频道'))
    session.flush()
    return session


@pytest.mark.parametrize('value', [PUBLISHED, PUBLISHED.isoformat(), PUBLISHED.isoformat().replace('+00:00', 'Z')])
def test_operation_snapshot_preserves_instant_at_beijing_day_boundary(value):
    assert _normalize_snapshot_datetime(value) == LOCAL_PUBLISHED


def test_equivalent_offsets_do_not_change_source_identity_or_edit_kind():
    fields = dict(channel_target_id=31, remote_message_id=5981, content_hash='body', binding=None, thread_root_id=0)
    utc_hash = _source_observation_hash(1, published_at=PUBLISHED, edited_at=PUBLISHED, **fields)
    local = PUBLISHED.astimezone(BEIJING_TZ)
    assert utc_hash == _source_observation_hash(1, published_at=local, edited_at=local, **fields)
    current = SimpleNamespace(source_content_hash='body', telegram_edit_date=local)
    assert _source_operation(current, 'body', PUBLISHED) == 'observed'


def test_new_source_persists_beijing_time():
    with _session() as session:
        message, created = _save(session, session.get(OperationTarget, 31), _snapshot())
        assert created and message.published_at == LOCAL_PUBLISHED
        revision = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        assert revision.source_published_at.replace(tzinfo=None) == LOCAL_PUBLISHED


def test_authoritative_capture_corrects_only_legacy_projection_and_is_idempotent():
    with _session() as session:
        target = session.get(OperationTarget, 31)
        message, _ = _save(session, target, _snapshot(PUBLISHED.replace(tzinfo=None)))
        previous = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        original_time, original_id = previous.source_published_at, previous.id
        task = Task(id='old-comment-task', tenant_id=1, type='channel_comment', name='旧评论')
        session.add(task)
        session.flush()
        held = Action(tenant_id=1, task_id=task.id, task_type=task.type, action_type='post_comment',
                      status='unknown_after_send', scheduled_at=OBSERVED,
                      payload={'source_revision_id': original_id, 'latest_safe_send_at': OBSERVED.isoformat()})
        session.add(held)
        session.flush()
        frozen = (held.status, held.scheduled_at, dict(held.payload))
        message, created = _save(session, target, _snapshot())
        current = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        assert created and message.published_at == LOCAL_PUBLISHED
        assert current.source_operation == 'timestamp_corrected'
        assert current.id != original_id and previous.source_published_at == original_time
        assert message.source_metadata['source_time_correction']['previous_revision_id'] == original_id
        _, created = _save(session, target, _snapshot(PUBLISHED.astimezone(BEIJING_TZ)))
        assert not created and message.current_source_revision_id == current.id
        assert session.scalar(select(func.count()).select_from(ChannelMessageSourceRevision)) == 2
        session.refresh(held)
        assert (held.status, held.scheduled_at, held.payload) == frozen
        assert message.source_metadata['source_time_correction']['previous_revision_id'] == original_id


def test_other_publication_time_changes_are_not_repaired():
    with _session() as session:
        target = session.get(OperationTarget, 31)
        _save(session, target, _snapshot(PUBLISHED.replace(tzinfo=None) - timedelta(hours=1)))
        with pytest.raises(RuntimeError, match='source_published_at_conflict'):
            _save(session, target, _snapshot())


def test_operation_sync_correction_is_consumed_by_next_listener_observation():
    from app.services.operations_channel_snapshot import _record_channel_time_correction
    with _session() as session:
        target = session.get(OperationTarget, 31)
        message, _ = _save(session, target, _snapshot(PUBLISHED.replace(tzinfo=None)))
        previous_id = message.current_source_revision_id
        _record_channel_time_correction(session, message, _snapshot())
        message.published_at = _normalize_snapshot_datetime(PUBLISHED)
        message, created = _save(session, target, _snapshot())
        assert created
        revision = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        assert revision.source_operation == 'timestamp_corrected'
        assert message.source_metadata['source_time_correction']['previous_revision_id'] == previous_id


def test_conflicting_revision_time_is_not_reinterpreted_as_legacy_timezone():
    with _session() as session:
        target = session.get(OperationTarget, 31)
        message, _ = _save(session, target, _snapshot(PUBLISHED.replace(tzinfo=None)))
        previous = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        previous.source_published_at += timedelta(minutes=1)
        with pytest.raises(RuntimeError, match='source_published_at_conflict'):
            _save(session, target, _snapshot())


def test_time_correction_with_real_edit_keeps_edit_reconciliation(monkeypatch):
    from app.services.task_center import channel_source_message_persistence as persistence
    observed = []
    monkeypatch.setattr(persistence, 'reconcile_channel_comment_source_edit',
                        lambda _session, _message, revision, **_kwargs: observed.append(revision.id))
    with _session() as session:
        target = session.get(OperationTarget, 31)
        message, _ = _save(session, target, _snapshot(PUBLISHED.replace(tzinfo=None)))
        edited = _snapshot()
        edited.content_text = '真实修改后的来源内容'
        edited.edited_at = PUBLISHED + timedelta(minutes=1)
        message, created = _save(session, target, edited)
        revision = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        assert created and revision.source_operation == 'edited'
        assert observed == [revision.id]


def test_missing_publication_time_does_not_fabricate_a_source_revision():
    with _session() as session:
        message, created = _save(session, session.get(OperationTarget, 31), _snapshot(None))
        assert not created and message.published_at is None
        assert message.current_source_revision_id is None


def test_operation_snapshot_writer_preserves_count_scope_and_correction():
    from app.services.operations_channel_snapshot import persist_channel_message_snapshot
    with _session() as session:
        target = session.get(OperationTarget, 31)
        options = {'message_url': lambda _target, message_id: f'test/{message_id}'}
        assert persist_channel_message_snapshot(session, target,
            _snapshot(PUBLISHED.replace(tzinfo=None)), **options) == 1
        session.flush()
        assert persist_channel_message_snapshot(session, target, _snapshot(), **options) == 0
        message = session.scalar(select(ChannelMessage))
        assert message.published_at == LOCAL_PUBLISHED
        assert message.message_url == 'test/5981'
        assert message.source_metadata['source_time_correction']['normalization_version'] == 'beijing_instant_v1'
        assert session.scalar(select(func.count()).select_from(ChannelMessage)) == 1
