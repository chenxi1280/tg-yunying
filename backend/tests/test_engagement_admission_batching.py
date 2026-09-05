"""Batch admission must preserve per-account membership and dependency evidence."""
import pytest
from sqlalchemy import event

from app.models import AccountProxy, OperationTarget, TgGroup, TgGroupAccount
from app.services.task_center import engagement_planning_admission as admission
from tests.test_engagement_participation import _account, _seed, _session


pytestmark = pytest.mark.no_postgres
MAX_ADMISSION_QUERIES = 7
ACCOUNT_COUNT = 20


def _seed_members(session):
    task = _seed(session)
    target = session.get(OperationTarget, 101)
    session.add(TgGroup(id=101, tenant_id=1, tg_peer_id=target.tg_peer_id, title="group"))
    for account_id in range(20, 20 + ACCOUNT_COUNT):
        account = _account(account_id)
        account.proxy = AccountProxy(tenant_id=1, name=f"proxy-{account_id}",
                                     port=1080, status="healthy", alert_status="normal")
        session.add(account)
        session.add(TgGroupAccount(tenant_id=1, group_id=101, account_id=account_id,
                                  can_send=account_id % 2 == 0))
    session.commit()
    return task, target


def test_admission_batches_queries_and_preserves_per_account_results():
    with _session() as session:
        task, target = _seed_members(session)
        ids = list(range(20, 20 + ACCOUNT_COUNT)) + [999]
        expected = [admission._planning_paths(session, task, [account_id],
                    target=target, require_send=True)[0] for account_id in ids]
        session.expire_all()
        statements = []
        connection = session.connection()
        def record(conn, cursor, statement, parameters, context, executemany):
            statements.append(statement)
        event.listen(connection, "before_cursor_execute", record)
        try:
            actual = admission._planning_paths(session, task, ids, target=target, require_send=True)
        finally:
            event.remove(connection, "before_cursor_execute", record)
        assert actual == expected
        assert [row["account_id"] for row in actual] == ids
        assert len(statements) <= MAX_ADMISSION_QUERIES + 2  # expired Task and target
        ready = [row["account_id"] for row in actual if row["admissible"]]
        assert ready == [account_id for account_id in ids if account_id < 999 and account_id % 2 == 0]
