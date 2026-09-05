from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select, text

from app.database import SessionLocal
from app.models import ChannelMessageSourceRevision, OperationTarget, Tenant
from app.services.task_center.channel_source_message_persistence import _upsert_channel_message
from app.timezone import BEIJING_TZ, as_beijing

pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 950_601
CHANNEL_ID = 950_602
PUBLISHED = datetime(2026, 9, 4, 18, 44, 35, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 9, 5, 3, 0, tzinfo=BEIJING_TZ)


def test_postgres_source_correction_is_independent_of_session_timezone():
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == 'postgresql'
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        session.add(Tenant(id=TENANT_ID, name='时间规范化测试'))
        session.flush()
        channel = OperationTarget(id=CHANNEL_ID, tenant_id=TENANT_ID, target_type='channel',
                                  tg_peer_id='-100950602', title='测试频道')
        session.add(channel)
        session.flush()
        source = SimpleNamespace(tenant_id=TENANT_ID)
        snapshot = dict(message_id=5981, edited_at=PUBLISHED, content_text='来源',
                        content_preview='来源', message_url='', comment_available=True)
        scope = dict(observed_at=OBSERVED, binding=None, thread_root_id=0)
        message, _ = _upsert_channel_message(session, source, channel,
            snapshot=SimpleNamespace(**snapshot, published_at=PUBLISHED.replace(tzinfo=None)), **scope)
        original = session.get(ChannelMessageSourceRevision, message.current_source_revision_id)
        original_time = original.source_published_at
        message, created = _upsert_channel_message(session, source, channel,
            snapshot=SimpleNamespace(**snapshot, published_at=PUBLISHED), **scope)
        assert created
        current_id = message.current_source_revision_id
        session.flush()
        session.expire_all()
        current = session.get(ChannelMessageSourceRevision, current_id)
        assert current.source_published_at == PUBLISHED
        assert current.source_operation == 'timestamp_corrected'
        assert as_beijing(message.published_at) == as_beijing(PUBLISHED)
        assert original.source_published_at == original_time
        _, created = _upsert_channel_message(session, source, channel,
            snapshot=SimpleNamespace(**snapshot, published_at=PUBLISHED.astimezone(BEIJING_TZ)), **scope)
        assert not created
        assert session.scalar(select(func.count()).select_from(ChannelMessageSourceRevision).where(
            ChannelMessageSourceRevision.channel_message_id == message.id)) == 2
        session.rollback()
