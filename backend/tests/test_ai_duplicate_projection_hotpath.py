from datetime import timedelta

import pytest
from sqlalchemy import event

from app.models import AiGroupMessageMemory
from app.services._common import _now
from app.services.task_center.ai_message_memory import _find_duplicate
from app.services.task_center.ai_message_duplicate_queries import _find_exact_duplicate, _find_template_shell_duplicate
from app.services.task_center.ai_message_window_dedupe import find_group_window_exact_duplicate
from tests.test_ai_group_message_memory import _session

pytestmark = pytest.mark.no_postgres


def _seed(session, *, identity='history', text='历史内容', **overrides):
    row = AiGroupMessageMemory(**({'id': identity, 'tenant_id': 1, 'account_id': 101,
        'group_id': 21, 'task_id': 'task', 'status': 'success',
        'raw_text': text, 'normalized_text': text, 'text_fingerprint': 'exact',
        'template_shell_key': 'template', 'planned_at': _now(),
        'result': {'large': 'unneeded' * 1000}} | overrides))
    session.add(row)
    session.commit()
    session.expunge_all()


@pytest.mark.parametrize('kind', ['exact', 'template', 'group'])
def test_identity_lookups_only_return_id_and_do_not_hydrate_memory(kind):
    with _session() as session:
        _seed(session)
        args = {'tenant_id': 1, 'now': _now()}
        if kind == 'group':
            row = find_group_window_exact_duplicate(session, **args, group_id=21,
                fingerprint='exact', statuses={'success'})
        elif kind == 'exact':
            row = _find_exact_duplicate(session, **args, account_id=101, fingerprint='exact')
        else:
            row = _find_template_shell_duplicate(session, **args, account_id=101, template_shell_key='template')
        assert tuple(row._mapping) == ('id',)
        assert row.id == 'history'
        assert not any(isinstance(obj, AiGroupMessageMemory) for obj in session.identity_map.values())


def _check(session, *, text='候选文本', exclude=''):
    return _find_duplicate(session, tenant_id=1, account_id=101, group_id=21,
        fingerprint='not-an-exact-match', normalized=text, template_shell_key='',
        now=_now(), exclude_id=exclude)


def test_no_match_reads_similarity_window_once_per_check():
    with _session() as session:
        _seed(session, text='abcdef')
        statements = []
        def capture(_conn, _cursor, statement, *_args):
            if statement.startswith('SELECT ai_group_message_memory.id, ai_group_message_memory.normalized_text'):
                statements.append(statement)
        event.listen(session.bind, 'before_cursor_execute', capture)
        try:
            assert _check(session, text='uvwxyz') == (None, '')
            assert _check(session, text='uvwxyz') == (None, '')
        finally:
            event.remove(session.bind, 'before_cursor_execute', capture)
        assert len(statements) == 2


def test_next_call_observes_newly_committed_memory_and_excludes_self():
    with _session() as session:
        assert _check(session, text='abcdef') == (None, '')
        _seed(session, text='abcdef')
        row, window = _check(session, text='abcdef')
        assert row.id == 'history' and window == '10d_similar'
        assert _check(session, text='abcdef', exclude='history') == (None, '')


def test_lookup_window_and_tenant_account_filters_remain_authoritative():
    with _session() as session:
        _seed(session, identity='other-tenant', tenant_id=2)
        _seed(session, identity='other-account', account_id=102)
        _seed(session, identity='expired', planned_at=_now() - timedelta(days=11))
        _seed(session, identity='inactive', status='cancelled')
        assert _find_exact_duplicate(session, tenant_id=1, account_id=101,
            fingerprint='exact', now=_now()) is None
