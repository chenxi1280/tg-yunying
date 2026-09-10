import time
from datetime import timedelta
from sqlalchemy import text
from app.database import SessionLocal
from app.models import Action, AiGroupMessageMemory, ExecutionAttempt
from app.services._common import _now
from tests.test_hotpath_projections_postgres import scope, TENANT_ID, ACCOUNT_ID, GROUP_ID, TASK_ID

ROW_COUNT = 12000
LOOKUP_BUDGET_SECONDS = 1


def _seed_history(session, now):
    session.add(Action(id='lookup-parent', tenant_id=TENANT_ID, task_id=TASK_ID,
        task_type='group_ai_chat', action_type='send_message', status='success',
        scheduled_at=now, payload={'group_id': GROUP_ID}))
    session.flush()
    session.execute(ExecutionAttempt.__table__.insert(), [dict(
        id=f'lookup-attempt-{i}', tenant_id=TENANT_ID, action_id='lookup-parent',
        account_id=ACCOUNT_ID, attempt_no=i+1, status='result_unknown',
        remote_message_id=str(i), result_snapshot={'unused': 'x'*1000}) for i in range(ROW_COUNT)])
    session.execute(AiGroupMessageMemory.__table__.insert(), [dict(
        id=f'lookup-memory-{i}', tenant_id=TENANT_ID, account_id=ACCOUNT_ID,
        group_id=GROUP_ID, task_id=TASK_ID, status='success',
        raw_text='history', normalized_text='history', planned_at=now+timedelta(seconds=i),
        result={'unused': 'y'*1000}) for i in range(ROW_COUNT)])


def _index_names(plan):
    names = {plan['Index Name']} if 'Index Name' in plan else set()
    for child in plan.get('Plans', []):
        names.update(_index_names(child))
    return names


def test_all_status_remote_and_recent_group_queries_use_ordered_indexes(scope):
    with SessionLocal.begin() as session:
        _seed_history(session, _now())
        session.execute(text('ANALYZE execution_attempts'))
        session.execute(text('ANALYZE ai_group_message_memory'))
    queries = [
        ("SELECT action_id FROM execution_attempts WHERE remote_message_id=:remote",
         {'remote': str(ROW_COUNT-1)}, 'ix_execution_attempts_remote_identity', 1),
        ("SELECT normalized_text,raw_text FROM ai_group_message_memory WHERE tenant_id=:tenant "
         "AND group_id=:group AND status IN ('reserved','success','unknown_after_send') "
         "ORDER BY planned_at DESC LIMIT 120", {'tenant': TENANT_ID, 'group': GROUP_ID},
         'ix_ai_group_memory_group_recent', 120),
    ]
    with SessionLocal() as session:
        for sql, params, index, expected_count in queries:
            plan = session.execute(text('EXPLAIN (FORMAT JSON) '+sql), params).scalar()[0]['Plan']
            assert index in _index_names(plan)
            started = time.monotonic()
            assert len(session.execute(text(sql), params).all()) == expected_count
            assert time.monotonic()-started < LOOKUP_BUDGET_SECONDS
