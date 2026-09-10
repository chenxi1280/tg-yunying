from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import delete

from app.database import SessionLocal
from app.models import Action, AiGroupMessageMemory, Task, Tenant, TgAccount
from app.services._common import _now
from app.services.task_center.ai_message_duplicate_queries import _find_exact_duplicate
from app.services.task_center.ai_message_memory import _find_duplicate
from app.services.task_center.managed_presence_queries import _managed_actions

TENANT_ID = 926_914
ACCOUNT_ID = 926_914
GROUP_ID = 21
TASK_ID = 'hotpath-pg-task'


@pytest.fixture
def scope():
    with SessionLocal.begin() as s:
        s.add(Tenant(id=TENANT_ID, name='hotpath-test'))
        s.flush()
        s.add(TgAccount(id=ACCOUNT_ID, tenant_id=TENANT_ID,
            display_name='test', phone_masked='test', status='在线'))
        s.add(Task(id=TASK_ID, tenant_id=TENANT_ID, name='test', type='channel_view', status='paused'))
    yield
    with SessionLocal.begin() as s:
        s.execute(delete(AiGroupMessageMemory).where(AiGroupMessageMemory.tenant_id == TENANT_ID))
        s.execute(delete(Action).where(Action.tenant_id == TENANT_ID))
        s.execute(delete(Task).where(Task.id == TASK_ID))
        s.execute(delete(TgAccount).where(TgAccount.id == ACCOUNT_ID))
        s.execute(delete(Tenant).where(Tenant.id == TENANT_ID))


@pytest.mark.parametrize('group_value', [21, '021', None])
def test_postgres_json_projection_keeps_scalar_types_and_empty_identity_map(scope, group_value):
    now = _now()
    with SessionLocal.begin() as s:
        s.add(Action(id=str(uuid4()), tenant_id=TENANT_ID, task_id=TASK_ID,
            task_type='group_ai_chat', action_type='send_message', status='success',
            scheduled_at=now, payload={'group_id': group_value, 'large': 'x' * 100000},
            result={'visibility_status': 'visible_confirmed', 'large': 'y' * 100000}))
    with SessionLocal() as s:
        rows = _managed_actions(s, SimpleNamespace(tenant_id=TENANT_ID),
            SimpleNamespace(period_start_at=now-timedelta(seconds=1), deadline_at=now+timedelta(seconds=1)),
            group=SimpleNamespace(id=GROUP_ID))
        assert len(rows) == int(int(group_value or 0) == GROUP_ID)
        assert not s.identity_map
        if rows:
            assert rows[0].visibility_status == 'visible_confirmed'


def test_postgres_separate_commit_is_seen_by_next_check(scope):
    now = _now()
    args = dict(tenant_id=TENANT_ID, account_id=ACCOUNT_ID, group_id=GROUP_ID,
        fingerprint='candidate', normalized='abcdef', template_shell_key='', now=now)
    with SessionLocal() as reader:
        assert _find_duplicate(reader, **args) == (None, '')
        with SessionLocal.begin() as writer:
            writer.add(AiGroupMessageMemory(id='hotpath-history', tenant_id=TENANT_ID,
                account_id=ACCOUNT_ID, group_id=GROUP_ID, task_id=TASK_ID,
                status='success', raw_text='abcdef', normalized_text='abcdef',
                text_fingerprint='reference', planned_at=now, result={'large': 'x'*100000}))
        match, window = _find_duplicate(reader, **args)
        assert match.id == 'hotpath-history' and window == '10d_similar'
        found = _find_exact_duplicate(reader, tenant_id=TENANT_ID, account_id=ACCOUNT_ID,
            fingerprint='reference', now=now)
        assert tuple(found._mapping) == ('id',)
        assert not any(isinstance(obj, AiGroupMessageMemory) for obj in reader.identity_map.values())
