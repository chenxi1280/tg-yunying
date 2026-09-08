"""Foreign-key references must not form upgrade cycles with runtime row locks."""
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import TgGroup, TgGroupAccount
from app.services.task_center.account_assignment_locks import lock_execution_account
from app.services.task_center.ai_group_content_allocation import _lock_group_surface
from app.services.task_center.engagement_runtime_error import RuntimeResourceBlocked
from tests.test_engagement_assignment_postgres import _seed
from tests.test_runtime_retention_protection_postgres import database

pytestmark = [pytest.mark.isolated_postgres, pytest.mark.allow_missing_rule_binding]
GROUP_ID = 101
WAIT_SECONDS = 6


def _seed_group(database):
    with Session(database) as session:
        _seed(session)
        session.add(TgGroup(id=GROUP_ID, tenant_id=1, tg_peer_id="-100101", title="FK lock QA"))
        session.commit()


def test_content_surface_serializes_two_writers_already_holding_group_foreign_keys(database):
    _seed_group(database)
    with Session(database) as first, Session(database) as second:
        for session, account_id in ((first, 11), (second, 12)):
            session.add(TgGroupAccount(tenant_id=1, group_id=GROUP_ID, account_id=account_id))
            session.flush()
        _lock_group_surface(first, GROUP_ID)
        first.get(TgGroup, GROUP_ID).member_count += 1
        with pytest.raises(RuntimeResourceBlocked, match="ai_group_surface_busy"):
            _lock_group_surface(second, GROUP_ID)
        first.commit()
        _lock_group_surface(second, GROUP_ID)
        second.get(TgGroup, GROUP_ID).member_count += 1
        second.commit()
    with Session(database) as session:
        assert session.get(TgGroup, GROUP_ID).member_count == 2
        assert session.scalar(select(func.count(TgGroupAccount.id))) == 2


def _add_account_reference(database):
    with Session(database) as session:
        session.add(TgGroupAccount(tenant_id=1, group_id=GROUP_ID, account_id=11))
        session.commit()
        return "reference_committed"


def test_execution_account_lock_allows_other_transaction_to_persist_foreign_key(database):
    _seed_group(database)
    with Session(database) as runtime, ThreadPoolExecutor(max_workers=1) as executor:
        lock_execution_account(runtime, 1, 11)
        future = executor.submit(_add_account_reference, database)
        try:
            assert future.result(timeout=WAIT_SECONDS) == "reference_committed"
        finally:
            runtime.rollback()
