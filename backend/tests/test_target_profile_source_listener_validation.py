from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import func, select

from app.models import OperationTarget, Tenant, TenantLearningSource, TgAccount, TgGroup, TgGroupAccount
from app.models.enums import AccountStatus
from app.services.tenant_target_profile import update_sources

from test_tenant_target_profile import _session


pytestmark = pytest.mark.no_postgres


def _account(account_id: int, tenant_id: int, status: str = AccountStatus.ACTIVE.value, *, deleted: bool = False) -> TgAccount:
    return TgAccount(
        id=account_id,
        tenant_id=tenant_id,
        phone_masked=f"+100000{account_id}",
        display_name=f"账号{account_id}",
        status=status,
        deleted_at=datetime(2026, 1, 1) if deleted else None,
    )


def test_channel_learning_source_rejects_listener_accounts_outside_candidate_coverage() -> None:
    with _session() as session:
        session.add_all([Tenant(id=1, name="默认运营空间"), Tenant(id=2, name="其他租户")])
        session.add(OperationTarget(id=32, tenant_id=1, target_type="channel", tg_peer_id="-10032", title="频道"))
        session.add_all([
            _account(51, 1),
            _account(52, 2),
            _account(53, 1, AccountStatus.NEED_RELOGIN.value),
            _account(54, 1, deleted=True),
        ])
        session.commit()

        with pytest.raises(ValueError, match="监听账号不属于该学习来源"):
            update_sources(
                session,
                1,
                {"sources": [{"target_id": 32, "listener_account_ids": [51, 52, 53, 54]}]},
                actor="tester",
                reason="校验监听账号",
            )
        source_count = session.scalar(
            select(func.count()).select_from(TenantLearningSource).where(TenantLearningSource.target_id == 32)
        )

    assert source_count == 0


def test_group_learning_source_rejects_listener_accounts_without_group_listener_link() -> None:
    with _session() as session:
        session.add(Tenant(id=1, name="默认运营空间"))
        session.add_all([_account(51, 1), _account(52, 1)])
        session.add(TgGroup(id=41, tenant_id=1, tg_peer_id="-10041", title="活群", listener_enabled=True))
        session.add_all([
            TgGroupAccount(tenant_id=1, group_id=41, account_id=51, is_listener=True),
            TgGroupAccount(tenant_id=1, group_id=41, account_id=52, is_listener=False),
        ])
        session.commit()

        with pytest.raises(ValueError, match="监听账号不属于该学习来源"):
            update_sources(
                session,
                1,
                {"sources": [{"group_id": 41, "listener_account_ids": [51, 52]}]},
                actor="tester",
                reason="校验群监听账号",
            )
        session.rollback()

        result = update_sources(
            session,
            1,
            {"sources": [{"group_id": 41, "listener_account_ids": [51]}]},
            actor="tester",
            reason="保存群监听账号",
        )

    assert result["items"][0]["listener_account_ids"] == [51]
