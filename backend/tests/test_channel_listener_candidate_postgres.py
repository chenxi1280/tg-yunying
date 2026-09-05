from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.database import SessionLocal
from app.models import AccountPool, ListenerSourceState, Task, Tenant, TgAccount
from app.services.task_center import channel_listener_accounts, channel_listener_runtime
from app.timezone import as_beijing


pytestmark = pytest.mark.allow_missing_rule_binding
TENANT_ID = 950_701
POOL_ID = 950_702
TARGET_ID = 950_703
FIRST_ACCOUNT_ID = 950_710
READY_ACCOUNT_ID = FIRST_ACCOUNT_ID + 44
NOW_UTC = datetime(2026, 9, 5, 7, tzinfo=timezone.utc)


def test_postgres_listener_orders_complete_scope_with_utc_session(monkeypatch):
    monkeypatch.setattr(channel_listener_accounts, "_now", lambda: as_beijing(NOW_UTC))
    monkeypatch.setattr(channel_listener_runtime, "_now", lambda: as_beijing(NOW_UTC))
    with SessionLocal() as session:
        assert session.get_bind().dialect.name == "postgresql"
        session.execute(text("SET LOCAL TIME ZONE 'UTC'"))
        session.add(Tenant(id=TENANT_ID, name="频道观察排序测试"))
        session.flush()
        session.add(AccountPool(id=POOL_ID, tenant_id=TENANT_ID, name="观察范围"))
        session.flush()
        account_ids = list(range(FIRST_ACCOUNT_ID, READY_ACCOUNT_ID + 1))
        session.add_all([TgAccount(id=account_id, tenant_id=TENANT_ID, pool_id=POOL_ID,
            display_name=f"test-{account_id}", phone_masked=f"***{account_id}",
            status="在线", session_ciphertext="isolated-test") for account_id in account_ids])
        session.flush()
        task = Task(tenant_id=TENANT_ID, type="channel_view", name="观察排序",
                    account_config={"selection_mode": "group", "account_group_id": POOL_ID})
        session.add(task)
        session.add_all([ListenerSourceState(tenant_id=TENANT_ID, source_type="channel",
            source_peer_id=str(TARGET_ID), account_id=account_id,
            snapshot_status="ready" if account_id == READY_ACCOUNT_ID else "unavailable",
            next_probe_at=NOW_UTC + timedelta(minutes=1), updated_at=NOW_UTC)
            for account_id in account_ids])
        session.flush()
        candidates = channel_listener_accounts.select_channel_listener_accounts(
            session, task, channel_target_id=TARGET_ID, fallback_limit=10)
        assert candidates[0].id == READY_ACCOUNT_ID
        selected = channel_listener_runtime._preferred_listener_account(
            session, channel_target_id=TARGET_ID, accounts=candidates)
        assert selected.id == READY_ACCOUNT_ID
        session.rollback()
