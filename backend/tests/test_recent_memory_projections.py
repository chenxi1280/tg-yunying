from datetime import timedelta
import pytest
from sqlalchemy import event
from app.models import AiGroupMessageMemory, Task, TgGroup
from app.services._common import _now
from app.services.task_center.executors.group_ai_chat import (
    _recent_conversation_target_usage, _recent_group_memory_messages,
)
from tests.test_engagement_unowned_activity import _session, _seed

pytestmark = pytest.mark.no_postgres


def test_recent_memory_columns_keep_order_statuses_and_fallback_text():
    with _session() as session:
        _seed(session)
        now = _now()
        for i, status in enumerate(['success', 'reserved', 'unknown_after_send', 'failed']):
            session.add(AiGroupMessageMemory(id=f'history-{i}', tenant_id=1, group_id=7,
                task_id='group-task', status=status, raw_text=f'第{i}条讨论消息',
                normalized_text='' if i == 1 else f'第{i}条归一消息', planned_at=now+timedelta(seconds=i),
                topic_direction='主题', teacher_target='讨论老师', result={'large': 'x'*100000}))
        session.commit()
        session.expunge_all()
        task, group = session.get(Task, 'group-task'), session.get(TgGroup, 7)
        captured = []
        def capture(_conn, _cursor, _sql, _params, context, _many):
            captured.append(tuple(context.compiled.statement.selected_columns.keys()))
        event.listen(session.bind, 'before_cursor_execute', capture)
        try:
            messages = _recent_group_memory_messages(session, task, group, limit=2)
            usage = _recent_conversation_target_usage(session, task, group)
        finally:
            event.remove(session.bind, 'before_cursor_execute', capture)
        assert messages == ['第1条讨论消息', '第2条归一消息']
        assert usage == {'topics': {'主题': 3}, 'teachers': {'讨论老师': 3}}
        assert captured == [('normalized_text', 'raw_text'), ('topic_direction', 'teacher_target')]
        assert not any(isinstance(obj, AiGroupMessageMemory) for obj in session.identity_map.values())
