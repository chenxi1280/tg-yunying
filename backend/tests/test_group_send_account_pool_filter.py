import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import AccountPool, Tenant, TgAccount, TgGroup, TgGroupAccount
from app.services.task_center.account_pool import select_task_accounts

pytestmark = pytest.mark.no_postgres


def test_target_group_filter_keeps_sendable_accounts_and_excludes_permission_denied():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        session.add_all([
            Tenant(id=1, name="测试"), TgGroup(id=10, tenant_id=1, title="测试", tg_peer_id="-10010"),
            AccountPool(id=1, tenant_id=1, name="普通", pool_purpose="normal"),
        ])
        for account_id, label, can_send in ((1, None, True), (2, "", True),
                                           (3, "群无权限", True), (4, "可发言", True), (5, "可发言", False)):
            session.add(TgAccount(id=account_id, tenant_id=1, pool_id=1, display_name="测试",
                phone_masked=str(account_id), status="在线", account_identity="normal", health_score=100))
            session.add(TgGroupAccount(tenant_id=1, group_id=10, account_id=account_id,
                                      can_send=can_send, permission_label=label))
        session.flush()
        accounts = select_task_accounts(session, 1, {"selection_mode": "all"},
            target_group_id=10, enforce_capacity=False)
        assert {account.id for account in accounts} == {1, 2, 4}
